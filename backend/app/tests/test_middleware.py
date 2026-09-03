"""Security headers and rate limiting.

The rest of the suite runs with the limiter switched off (see conftest), so
these tests build their own app with it switched on. Without this file the
limiter would be entirely untested in CI and only exercised by the live probe.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.middleware import RateLimitMiddleware, SecurityHeadersMiddleware


def build_app(**limits) -> FastAPI:
    app = FastAPI()

    @app.get("/api/v1/thing")
    def thing():
        return {"ok": True}

    @app.get("/api/v1/health/ready")
    def ready():
        return {"status": "ready"}

    @app.post("/api/v1/auth/login")
    def login(payload: dict):
        # Stand-in for the real endpoint: any password but "right" fails.
        if payload.get("password") != "right":
            from fastapi import HTTPException

            raise HTTPException(status_code=401, detail="bad credentials")
        return {"access_token": "t"}

    if limits:
        app.add_middleware(RateLimitMiddleware, **limits)
    app.add_middleware(SecurityHeadersMiddleware)
    return app


@pytest.fixture
def unique_client():
    """A fresh source IP per test.

    The limiter keys on the client address and, when Redis is up, that state
    outlives the process. Reusing one address would make these tests order- and
    history-dependent - the classic way a rate-limit test passes alone and
    fails in a suite.
    """
    n = uuid.uuid4().int
    return f"198.51.100.{n % 200 + 1}" if n % 2 else f"203.0.113.{n % 200 + 1}"


# --- headers -------------------------------------------------------------


def test_security_headers_present_on_api_responses():
    client = TestClient(build_app())
    r = client.get("/api/v1/thing")

    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"


def test_api_csp_executes_nothing():
    """An API response rendered in a browser must be inert."""
    client = TestClient(build_app())
    csp = client.get("/api/v1/thing").headers["content-security-policy"]
    assert csp.startswith("default-src 'none'")


def test_hsts_absent_over_plain_http():
    """Pinning localhost to https would make the demo stack unreachable."""
    client = TestClient(build_app())
    assert "strict-transport-security" not in client.get("/api/v1/thing").headers


# --- rate limiting -------------------------------------------------------


def test_general_limit_blocks_after_ceiling(unique_client):
    app = build_app(general_per_minute=5, auth_failures=99, auth_window=60)
    client = TestClient(app)
    headers = {"X-Forwarded-For": unique_client}

    codes = [client.get("/api/v1/thing", headers=headers).status_code for _ in range(8)]

    assert codes[:5] == [200] * 5
    assert codes[5:] == [429] * 3


def test_health_is_never_throttled(unique_client):
    """The container healthcheck polls this; throttling it reports a false outage."""
    app = build_app(general_per_minute=2, auth_failures=99, auth_window=60)
    client = TestClient(app)
    headers = {"X-Forwarded-For": unique_client}

    codes = [client.get("/api/v1/health/ready", headers=headers).status_code for _ in range(10)]
    assert set(codes) == {200}


def test_failed_logins_lock_out_but_successes_do_not(unique_client):
    app = build_app(general_per_minute=500, auth_failures=3, auth_window=60)
    client = TestClient(app)
    headers = {"X-Forwarded-For": unique_client}

    # Successful sign-ins are free: an officer signing in all day is not an
    # attacker, and a limit that punishes them gets switched off.
    for _ in range(10):
        assert client.post(
            "/api/v1/auth/login", json={"password": "right"}, headers=headers
        ).status_code == 200

    codes = [
        client.post("/api/v1/auth/login", json={"password": "wrong"}, headers=headers).status_code
        for _ in range(5)
    ]
    assert codes[:3] == [401, 401, 401]
    assert codes[3:] == [429, 429]

    # And the lockout holds even once the attacker guesses correctly.
    assert client.post(
        "/api/v1/auth/login", json={"password": "right"}, headers=headers
    ).status_code == 429


def test_lockout_is_per_client(unique_client):
    """One locked-out address must not lock out everyone else."""
    app = build_app(general_per_minute=500, auth_failures=2, auth_window=60)
    client = TestClient(app)
    attacker = {"X-Forwarded-For": unique_client}
    officer = {"X-Forwarded-For": "192.0.2.77"}

    for _ in range(4):
        client.post("/api/v1/auth/login", json={"password": "wrong"}, headers=attacker)

    assert client.post(
        "/api/v1/auth/login", json={"password": "wrong"}, headers=attacker
    ).status_code == 429
    assert client.post(
        "/api/v1/auth/login", json={"password": "right"}, headers=officer
    ).status_code == 200


def test_forged_forwarded_for_is_not_trusted_as_an_ip(unique_client):
    """A non-IP X-Forwarded-For falls back to the real peer, not a free bucket."""
    app = build_app(general_per_minute=3, auth_failures=99, auth_window=60)
    client = TestClient(app)

    codes = [
        client.get("/api/v1/thing", headers={"X-Forwarded-For": f"not-an-ip-{i}"}).status_code
        for i in range(6)
    ]
    assert 429 in codes, "rotating a junk header should not mint new rate-limit buckets"


def test_throttled_response_is_still_hardened(unique_client):
    app = build_app(general_per_minute=1, auth_failures=99, auth_window=60)
    client = TestClient(app)
    headers = {"X-Forwarded-For": unique_client}

    client.get("/api/v1/thing", headers=headers)
    blocked = client.get("/api/v1/thing", headers=headers)

    assert blocked.status_code == 429
    assert blocked.headers["retry-after"] == "60"
    assert blocked.headers["x-content-type-options"] == "nosniff"
