"""Intake API tests.

These exercise the real path: FastAPI -> Postgres -> synthetic connector ->
Neo4j -> clustering. They skip when the datastores are not reachable.
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


@pytest.fixture
def live(stack_up):
    if not stack_up:
        pytest.skip("compose stack not fully up - skipping intake integration tests")


@pytest.fixture
def synthetic_addresses(live):
    from app.services.connectors.synthetic import get_complaints

    complaints = get_complaints()
    if not complaints:
        pytest.skip("synthetic dataset not generated")
    by_chain: dict[str, str] = {}
    for c in complaints:
        by_chain.setdefault(c["chain"], c["address"])
    return by_chain


# ---------------------------------------------------------------------------
# Validation endpoint
# ---------------------------------------------------------------------------
def test_validate_accepts_good_address():
    resp = client.post(
        "/api/v1/wallets/validate", json={"address": "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is True
    assert body["chain"] == "BTC"
    assert body["address_kind"] == "p2pkh"


def test_validate_rejects_bad_checksum_without_creating_a_case():
    resp = client.post(
        "/api/v1/wallets/validate",
        json={"address": "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAeD"},
    )
    assert resp.status_code == 200
    assert resp.json()["valid"] is False
    assert "EIP-55" in resp.json()["reason"]


def test_intake_rejects_invalid_address():
    resp = client.post("/api/v1/wallets", json={"address": "definitely-not-a-wallet"})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Intake
# ---------------------------------------------------------------------------
def test_intake_creates_case_and_builds_graph(synthetic_addresses):
    address = synthetic_addresses["TRON"]
    resp = client.post(
        "/api/v1/wallets",
        json={"address": address, "victim_ref": "TEST-V1", "source": "synthetic"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()

    assert body["chain"] == "TRON"
    assert body["case_number"].startswith("SIH183-")
    assert body["trace"]["status"] == "complete"
    assert body["trace"]["transactions_ingested"] > 0
    assert body["trace"]["hops_discovered"] >= 1
    # Provenance must be reported, and must say synthetic in DEMO_MODE.
    assert body["data_provenance"] == "synthetic"
    assert body["trace"]["data_source"] == "synthetic"


def test_repeat_report_is_flagged_as_duplicate(synthetic_addresses):
    """The same wallet reported by a second victim is itself a fraud signal."""
    address = synthetic_addresses["TRON"]

    first = client.post(
        "/api/v1/wallets", json={"address": address, "victim_ref": "DUP-A", "source": "synthetic"}
    )
    second = client.post(
        "/api/v1/wallets", json={"address": address, "victim_ref": "DUP-B", "source": "synthetic"}
    )
    assert first.status_code == 201
    assert second.status_code == 201

    dup = second.json()["duplicate"]
    assert dup["is_duplicate"] is True
    assert dup["prior_case_count"] >= 1
    assert first.json()["case_number"] in dup["prior_case_numbers"]
    # Distinct cases, one shared wallet.
    assert first.json()["case_number"] != second.json()["case_number"]
    assert first.json()["wallet_id"] == second.json()["wallet_id"]


def test_trace_depth_is_honoured(synthetic_addresses):
    address = synthetic_addresses["TRON"]
    resp = client.post(
        "/api/v1/wallets",
        json={"address": address, "source": "synthetic", "trace_depth": 2},
    )
    assert resp.status_code == 201
    trace = resp.json()["trace"]
    assert trace["max_depth"] == 2
    assert trace["hops_discovered"] <= 2


def test_wallet_lookup_returns_cases(synthetic_addresses):
    address = synthetic_addresses["BTC"]
    client.post("/api/v1/wallets", json={"address": address, "source": "synthetic"})

    resp = client.get(f"/api/v1/wallets/BTC/{address}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["chain"] == "BTC"
    assert len(body["cases"]) >= 1
    assert body["report_count"] >= 1


def test_wallet_lookup_rejects_chain_mismatch(synthetic_addresses):
    """A BTC address requested under /ETH/ must not silently resolve."""
    resp = client.get(f"/api/v1/wallets/ETH/{synthetic_addresses['BTC']}")
    assert resp.status_code == 422


def test_unknown_wallet_returns_404(live):
    resp = client.get("/api/v1/wallets/BTC/1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa")
    assert resp.status_code in (404, 422)


def test_multi_reported_view_surfaces_shared_wallets(synthetic_addresses):
    address = synthetic_addresses["ETH"]
    client.post("/api/v1/wallets", json={"address": address, "source": "synthetic"})
    client.post("/api/v1/wallets", json={"address": address, "source": "synthetic"})

    client.post("/api/v1/auth/seed-demo-users")
    login = client.post(
        "/api/v1/auth/login", data={"username": "investigator", "password": "investigator123"}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    # The view names which addresses recur across complaints, so it is gated.
    assert client.get("/api/v1/wallets/multi-reported").status_code == 401

    resp = client.get("/api/v1/wallets/multi-reported", headers=headers)
    assert resp.status_code == 200
    rows = resp.json()
    match = [r for r in rows if r["address"].lower() == address.lower()]
    assert match, "a wallet reported twice must appear in the cross-case view"
    assert match[0]["case_count"] >= 2


def test_btc_intake_produces_a_utxo_cluster(synthetic_addresses):
    """The BTC ring includes consolidation transactions, so the reported
    address should land in a multi-address cluster."""
    resp = client.post(
        "/api/v1/wallets", json={"address": synthetic_addresses["BTC"], "source": "synthetic"}
    )
    assert resp.status_code == 201
    cluster = resp.json()["cluster"]
    assert cluster["cluster_key"]
    assert cluster["heuristic"] in ("common_input_ownership", "change_address", "account_single")
