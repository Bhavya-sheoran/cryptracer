"""Startup secret validation.

The guarantee under test: the system cannot claim to be handling live data
while signing tokens with a key that is published in this repository.
"""

from __future__ import annotations

import pytest

from app.config import (
    DEFAULT_JWT_SECRET,
    MIN_JWT_SECRET_BYTES,
    InsecureConfiguration,
    Settings,
    check_secrets,
)

STRONG = "x" * 64


def settings(**overrides) -> Settings:
    base = {
        "jwt_secret": STRONG,
        "demo_mode": True,
        "neo4j_password": "a-real-password",
    }
    return Settings(**{**base, **overrides})


def test_shipped_default_is_long_enough_for_hs256():
    """PyJWT warns below 32 bytes; the placeholder must not trip that on its own."""
    assert len(DEFAULT_JWT_SECRET.encode()) >= MIN_JWT_SECRET_BYTES


def test_demo_mode_tolerates_the_default_secret(caplog):
    """`docker compose up` has to work with no secret management at all."""
    problems = check_secrets(settings(jwt_secret=DEFAULT_JWT_SECRET))

    assert len(problems) == 1
    assert "shipped default" in problems[0]
    assert any("INSECURE CONFIG" in r.message for r in caplog.records)


def test_live_mode_refuses_the_default_secret():
    with pytest.raises(InsecureConfiguration) as exc:
        check_secrets(settings(jwt_secret=DEFAULT_JWT_SECRET, demo_mode=False))

    assert "Refusing to start" in str(exc.value)
    assert "secrets.token_urlsafe" in str(exc.value), "must say how to fix it"


def test_live_mode_refuses_a_short_secret():
    with pytest.raises(InsecureConfiguration) as exc:
        check_secrets(settings(jwt_secret="short-but-not-the-default", demo_mode=False))

    assert "25 bytes" in str(exc.value)


def test_live_mode_refuses_the_development_graph_password():
    with pytest.raises(InsecureConfiguration) as exc:
        check_secrets(settings(neo4j_password="sihdevpass", demo_mode=False))

    assert "NEO4J_PASSWORD" in str(exc.value)


def test_properly_configured_live_mode_starts():
    assert check_secrets(settings(demo_mode=False)) == []


def test_the_old_default_would_now_be_rejected():
    """Regression guard for the 30-byte secret PyJWT flagged."""
    with pytest.raises(InsecureConfiguration):
        check_secrets(settings(jwt_secret="change-me-in-production-please", demo_mode=False))
