"""The /api/v1/exposure endpoint.

Covers the contract an investigator and the dashboard both depend on, plus the
persistence that makes a finding reproducible after the graph has moved on.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


@pytest.fixture(scope="module")
def stack_up() -> bool:
    resp = client.get("/api/v1/health/ready")
    return resp.status_code == 200 and resp.json()["status"] == "ready"


@pytest.fixture(scope="module")
def officer(stack_up):
    if not stack_up:
        pytest.skip("compose stack not fully up")
    client.post("/api/v1/auth/seed-demo-users")
    resp = client.post(
        "/api/v1/auth/login", data={"username": "investigator", "password": "investigator123"}
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture
def traced_address(officer):
    """A synthetic complaint address with its flow already in the graph."""
    from app.services.connectors.synthetic import get_complaints

    complaints = get_complaints()
    if not complaints:
        pytest.skip("synthetic dataset not generated")
    address = next(c["address"] for c in complaints if c["chain"] == "TRON")
    client.post("/api/v1/wallets", json={"address": address, "source": "synthetic"})
    return address


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------
def test_exposure_requires_authentication(stack_up):
    """Results name services, transaction hashes and rupee amounts."""
    if not stack_up:
        pytest.skip("stack not up")
    assert client.get("/api/v1/exposure", params={"address": "x"}).status_code == 401
    assert client.get("/api/v1/exposure/history", params={"address": "x"}).status_code == 401


def test_invalid_address_is_rejected(officer):
    resp = client.get("/api/v1/exposure", params={"address": "not-a-wallet"}, headers=officer)
    assert resp.status_code == 422


def test_bad_checksum_is_rejected_before_any_traversal(officer):
    """A mistyped address must not be silently traced as if it were valid."""
    resp = client.get(
        "/api/v1/exposure",
        params={"address": "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAeD"},
        headers=officer,
    )
    assert resp.status_code == 422
    assert "EIP-55" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------
def test_response_carries_the_full_contract(officer, traced_address):
    resp = client.get(
        "/api/v1/exposure",
        params={"address": traced_address, "max_hops": 8},
        headers=officer,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    for key in ("kind", "candidates", "scoring_version", "explanation", "data_provenance"):
        assert key in body, f"missing contract field: {key}"

    assert body["kind"] in ("direct", "indirect", "none")
    assert body["data_provenance"] in ("synthetic", "live_indexer_apis")
    assert "approval" in body["notice"].lower()


def test_indirect_result_is_ranked_and_explained(officer, traced_address):
    body = client.get(
        "/api/v1/exposure",
        params={"address": traced_address, "max_hops": 8},
        headers=officer,
    ).json()
    if body["kind"] != "indirect":
        pytest.skip("this fixture produced a direct exposure")

    top = body["top"]
    assert top["rank"] == 1
    features = top["features"]
    assert features["service"]
    assert features["hop"] >= 1

    # Every factor is reported, and they must account for the whole score.
    assert len(top["explanation"]) == len(body["weights"])
    total = sum(e["contribution"] for e in top["explanation"])
    assert total == pytest.approx(top["score"], abs=0.001)


def test_volume_is_reported_in_rupees(officer, traced_address):
    """Absolute valuation is what makes the number mean something."""
    body = client.get(
        "/api/v1/exposure",
        params={"address": traced_address, "max_hops": 8},
        headers=officer,
    ).json()
    if body["kind"] != "indirect":
        pytest.skip("direct exposure carries evidence rather than aggregates")

    features = body["top"]["features"]
    assert features["priced"] is True
    assert features["total_volume_inr"] > 0
    assert features["volume_basis"] == "INR"
    assert features["price_sources"], "a valuation must name its price source"


def test_unreachable_address_reports_none_not_an_error(officer):
    """A valid but untraced address is a legitimate 'nothing found'."""
    resp = client.get(
        "/api/v1/exposure",
        params={"address": "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"},
        headers=officer,
    )
    assert resp.status_code == 200
    assert resp.json()["kind"] == "none"


def test_max_hops_is_bounded(officer, traced_address):
    """Depth must be capped so a request cannot ask for an unbounded traversal."""
    assert (
        client.get(
            "/api/v1/exposure",
            params={"address": traced_address, "max_hops": 99},
            headers=officer,
        ).status_code
        == 422
    )


# ---------------------------------------------------------------------------
# Persistence and reproducibility
# ---------------------------------------------------------------------------
def test_finding_is_persisted_with_its_scoring_version(officer, traced_address):
    """A finding an officer may act on has to survive the graph changing."""
    body = client.get(
        "/api/v1/exposure",
        params={"address": traced_address, "max_hops": 8},
        headers=officer,
    ).json()
    assert body["exposure_id"], "the finding should have been stored"

    history = client.get(
        "/api/v1/exposure/history", params={"address": traced_address}, headers=officer
    ).json()
    assert history["count"] >= 1

    stored = history["findings"][0]
    assert stored["scoring_version"] == body["scoring_version"]
    assert stored["kind"] == body["kind"]
    assert stored["computed_at"]


def test_persistence_can_be_declined(officer, traced_address):
    """An exploratory query should not litter the record."""
    body = client.get(
        "/api/v1/exposure",
        params={"address": traced_address, "max_hops": 8, "persist": False},
        headers=officer,
    ).json()
    assert body["exposure_id"] is None


def test_stored_candidates_keep_their_arithmetic(officer, traced_address):
    """The explanation is stored, not recomputed.

    Recomputing later against different weights would silently rewrite what the
    officer actually saw.
    """
    from sqlalchemy import select

    from app.db.postgres import SessionLocal
    from app.models import ExposurePathRecord

    body = client.get(
        "/api/v1/exposure",
        params={"address": traced_address, "max_hops": 8},
        headers=officer,
    ).json()
    if not body["exposure_id"] or body["kind"] == "none":
        pytest.skip("nothing to store for this fixture")

    with SessionLocal() as db:
        rows = (
            db.execute(
                select(ExposurePathRecord).where(
                    ExposurePathRecord.exposure_id == body["exposure_id"]
                )
            )
            .scalars()
            .all()
        )

    assert rows, "candidates should have been stored alongside the finding"
    top = min(rows, key=lambda r: r.rank)
    assert top.explanation, "the per-feature arithmetic must be stored"
    assert top.features, "the raw measurements must be stored"
    assert top.service
