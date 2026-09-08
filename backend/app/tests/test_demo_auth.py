"""The demonstration-account gate.

The rest of the suite runs with ALLOW_DEMO_AUTH=true (see conftest), because
most API tests sign in through those accounts. These tests assert the gate
itself, so they drive the setting directly rather than through the environment.

What is being protected: `/auth/seed-demo-users` creates a supervisor whose
password is published in the repository. Reachable in a real deployment, it is
a complete authentication bypass - anyone who can send it a POST can then
approve a freeze.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings


def settings(**kwargs) -> Settings:
    base = {"jwt_secret": "x" * 40, "demo_mode": True, "allow_demo_auth": True}
    return Settings(**{**base, **kwargs})


# --- the switch itself ---------------------------------------------------


def test_enabled_only_when_both_switches_are_on():
    assert settings().demo_auth_enabled is True


def test_off_by_default():
    """A deployment that sets nothing must not get demo accounts.

    Asserted against the declared field default rather than a constructed
    Settings: this suite runs with ALLOW_DEMO_AUTH=true in the environment, and
    an instance would pick that up. What matters here is what the code ships as
    the default for a deployment that sets nothing at all.
    """
    assert Settings.model_fields["allow_demo_auth"].default is False
    assert settings(allow_demo_auth=False).demo_auth_enabled is False


def test_explicitly_disabled():
    assert settings(allow_demo_auth=False).demo_auth_enabled is False


def test_live_data_forces_it_off_even_when_requested():
    """The property that matters most.

    Turning off DEMO_MODE is the act of pointing this system at real chain
    data. That must close the demo-account door even if someone forgot to unset
    ALLOW_DEMO_AUTH - the two mistakes are highly correlated, so they must not
    both be required.
    """
    assert settings(allow_demo_auth=True, demo_mode=False).demo_auth_enabled is False


# --- the endpoint --------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    """Build a client whose auth router sees a patched settings object."""

    def build(**overrides):
        from app.api.v1 import auth as auth_router

        monkeypatch.setattr(auth_router, "settings", settings(**overrides))
        from app.main import app

        return TestClient(app)

    return build


def test_endpoint_serves_accounts_when_enabled(client):
    r = client(allow_demo_auth=True).post("/api/v1/auth/seed-demo-users")
    assert r.status_code == 200
    assert "accounts" in r.json()


def test_endpoint_is_404_when_disabled(client):
    r = client(allow_demo_auth=False).post("/api/v1/auth/seed-demo-users")
    assert r.status_code == 404


def test_endpoint_is_404_with_live_data(client):
    r = client(allow_demo_auth=True, demo_mode=False).post("/api/v1/auth/seed-demo-users")
    assert r.status_code == 404


def test_disabled_response_does_not_advertise_the_endpoint(client):
    """404 rather than 403.

    A 403 confirms the route exists and is worth attacking. A 404 is
    indistinguishable from a build that never shipped it, and the response body
    must not leak the difference either.
    """
    r = client(allow_demo_auth=False).post("/api/v1/auth/seed-demo-users")
    body = r.text.lower()
    for leak in ("demo", "investigator", "supervisor", "password", "allow_demo_auth"):
        assert leak not in body, f"disabled response mentions {leak!r}"
