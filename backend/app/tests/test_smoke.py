"""Phase 0 smoke tests: the app boots and the liveness contract holds.

These intentionally avoid Postgres/Neo4j/Redis so they can run without the
compose stack up. Dependency-backed tests arrive in Phase 1.
"""

from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app

client = TestClient(app)


def test_root_returns_provenance_notice():
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.json()
    assert "Synthetic" in body["notice"]


def test_liveness_probe():
    resp = client.get("/api/v1/health/live")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_openapi_schema_generates():
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    assert "/api/v1/health/ready" in resp.json()["paths"]


def test_settings_defaults_to_demo_mode():
    """Fail-safe: absent explicit configuration the system must assume synthetic
    data, never claim live-chain provenance."""
    assert get_settings().demo_mode is True
