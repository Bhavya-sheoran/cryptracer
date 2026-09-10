"""Token revocation.

The property under test is that a session can be ended before its token
expires. Before this existed, a JWT was valid for its full eight hours no
matter what: a stolen token, a laptop left open, an officer dismissed at 10am
could still approve a freeze at 5pm.

The most important test here is `test_check_fails_closed_when_redis_is_down`.
Everything else describes what revocation does; that one describes what happens
when the mechanism itself is broken, which is when a wrong answer costs most.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import sessions
from app.services.auth import decode_token

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_revocations():
    """Each test starts with nothing revoked, and leaves nothing behind.

    Revocation state is shared with the running stack, so a leaked cutoff would
    lock the demo accounts out of every later test - and out of the demo.
    """
    yield
    try:
        redis = sessions._client()  # noqa: SLF001 - fixture cleanup
        for prefix in (sessions.REVOKED_TOKEN_PREFIX, sessions.USER_CUTOFF_PREFIX):
            keys = list(redis.scan_iter(f"{prefix}*"))
            if keys:
                redis.delete(*keys)
    except Exception:  # noqa: BLE001 - cleanup must never fail a test
        pass


def sign_in(username: str = "investigator", password: str = "investigator123") -> str:
    client.post("/api/v1/auth/seed-demo-users")
    resp = client.post(
        "/api/v1/auth/login", data={"username": username, "password": password}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --- token shape ---------------------------------------------------------


def test_issued_tokens_carry_a_unique_id():
    """Without a jti there is no way to revoke one session and not another."""
    first = decode_token(sign_in())
    second = decode_token(sign_in())

    assert first["jti"] and second["jti"]
    assert first["jti"] != second["jti"]


def test_a_token_without_a_jti_is_refused():
    """Tokens predating revocation must not be honoured.

    They cannot be revoked, so accepting them would leave a class of session
    that silently ignores every termination.
    """
    import jwt as pyjwt

    from app.config import get_settings

    settings = get_settings()
    legacy = pyjwt.encode(
        {"sub": "abc", "iat": int(time.time()), "exp": int(time.time()) + 3600},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    assert client.get("/api/v1/auth/me", headers=auth(legacy)).status_code == 401


# --- single-session revocation -------------------------------------------


def test_sign_out_ends_that_session(clean_revocations):
    token = sign_in()
    assert client.get("/api/v1/auth/me", headers=auth(token)).status_code == 200

    assert client.post("/api/v1/auth/logout", headers=auth(token)).status_code == 204
    assert client.get("/api/v1/auth/me", headers=auth(token)).status_code == 401


def test_sign_out_leaves_other_sessions_alone(clean_revocations):
    """Signing out on a shared machine must not sign you out on your phone."""
    laptop = sign_in()
    phone = sign_in()

    client.post("/api/v1/auth/logout", headers=auth(laptop))

    assert client.get("/api/v1/auth/me", headers=auth(laptop)).status_code == 401
    assert client.get("/api/v1/auth/me", headers=auth(phone)).status_code == 200


def test_signing_out_twice_is_not_an_error(clean_revocations):
    token = sign_in()
    client.post("/api/v1/auth/logout", headers=auth(token))
    # The second call is rejected because the token is already dead, which is
    # the correct outcome - not a 500.
    assert client.post("/api/v1/auth/logout", headers=auth(token)).status_code == 401


# --- blanket revocation --------------------------------------------------


def test_logout_everywhere_ends_every_session(clean_revocations):
    laptop = sign_in()
    phone = sign_in()

    assert client.post("/api/v1/auth/logout-everywhere", headers=auth(laptop)).status_code == 204

    assert client.get("/api/v1/auth/me", headers=auth(laptop)).status_code == 401
    assert client.get("/api/v1/auth/me", headers=auth(phone)).status_code == 401


def test_a_new_session_works_after_logout_everywhere(clean_revocations):
    """The cutoff must not lock the account out permanently."""
    client.post("/api/v1/auth/logout-everywhere", headers=auth(sign_in()))
    time.sleep(1.1)  # the cutoff has one-second resolution

    assert client.get("/api/v1/auth/me", headers=auth(sign_in())).status_code == 200


def test_revoking_one_user_does_not_affect_another(clean_revocations):
    investigator = sign_in("investigator", "investigator123")
    supervisor = sign_in("supervisor", "supervisor123")

    client.post("/api/v1/auth/logout-everywhere", headers=auth(investigator))

    assert client.get("/api/v1/auth/me", headers=auth(investigator)).status_code == 401
    assert client.get("/api/v1/auth/me", headers=auth(supervisor)).status_code == 200


# --- failure behaviour ---------------------------------------------------


def test_check_fails_closed_when_redis_is_down(monkeypatch, clean_revocations):
    """The test that matters most.

    If revocation state cannot be read, the request is refused. Failing open
    would mean a Redis blip silently re-admits every session anyone has
    terminated - and nobody would notice, because everything would appear to
    work.
    """
    token = sign_in()

    def unavailable():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(sessions, "_client", unavailable)

    response = client.get("/api/v1/auth/me", headers=auth(token))
    assert response.status_code == 503, "a request must not proceed on unknown revocation state"
    assert response.status_code != 200


def test_revocation_records_expire_with_the_token(clean_revocations):
    """The store must not grow forever.

    A revocation stops being interesting once the token would have expired on
    its own, so the record is given exactly that much life.
    """
    token = sign_in()
    payload = decode_token(token)
    sessions.revoke_token(payload["jti"], payload["exp"])

    ttl = sessions._client().ttl(  # noqa: SLF001 - asserting the TTL is the point
        f"{sessions.REVOKED_TOKEN_PREFIX}{payload['jti']}"
    )
    assert 0 < ttl <= sessions.MAX_TTL_SECONDS


def test_an_already_expired_token_still_gets_a_positive_ttl(clean_revocations):
    """Guards an off-by-one that would set a zero or negative TTL.

    Redis rejects those, so the revocation would silently not be recorded.
    """
    sessions.revoke_token("stale-jti", int(time.time()) - 9999)
    ttl = sessions._client().ttl(f"{sessions.REVOKED_TOKEN_PREFIX}stale-jti")  # noqa: SLF001
    assert ttl > 0
