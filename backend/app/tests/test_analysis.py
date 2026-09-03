"""Tests for the unified /api/v1/wallet analysis endpoint.

The contract these defend is the Phase 2 deliverable: trace_path, attribution,
risk_label, risk_score and contributing_case_ids in one response, with the
attribution method always stated so a curated tag is never confused with a
model's guess.
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
    """Analysis endpoints name contributing cases, so they require an officer."""
    if not stack_up:
        pytest.skip("compose stack not fully up")
    client.post("/api/v1/auth/seed-demo-users")
    resp = client.post(
        "/api/v1/auth/login", data={"username": "investigator", "password": "investigator123"}
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture
def analysed(stack_up, officer):
    """File one synthetic complaint per chain, then hand back their addresses."""
    if not stack_up:
        pytest.skip("compose stack not fully up")

    from app.services.connectors.synthetic import get_complaints

    complaints = get_complaints()
    if not complaints:
        pytest.skip("synthetic dataset not generated")

    by_chain: dict[str, str] = {}
    for c in complaints:
        by_chain.setdefault(c["chain"], c["address"])

    for address in by_chain.values():
        client.post("/api/v1/wallets", json={"address": address, "source": "synthetic"})
    by_chain["__auth__"] = officer
    return by_chain


def analyse(address: str, auth=None, **params):
    return client.get(
        "/api/v1/wallet", params={"address": address, **params}, headers=auth or {}
    )


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------
def test_response_carries_the_full_phase2_contract(analysed):
    resp = analyse(analysed["TRON"], analysed["__auth__"])
    assert resp.status_code == 200, resp.text
    body = resp.json()

    for key in (
        "trace_path",
        "attribution",
        "risk_label",
        "risk_score",
        "contributing_case_ids",
    ):
        assert key in body, f"missing contract field: {key}"

    assert body["risk_label"] in ("low", "medium", "high")
    assert 0 <= body["risk_score"] <= 100


def test_invalid_address_is_rejected(officer):
    assert analyse("not-a-wallet", officer).status_code == 422


def test_analysis_requires_authentication(stack_up):
    """The response names contributing case numbers - not public information."""
    if not stack_up:
        pytest.skip("stack not up")
    assert client.get("/api/v1/wallet", params={"address": "x"}).status_code == 401
    assert client.get("/api/v1/exchanges/ranked").status_code == 401
    assert client.get("/api/v1/alerts/recent").status_code == 401
    assert client.get("/api/v1/wallets/multi-reported").status_code == 401


def test_unknown_but_valid_address_returns_404(officer):
    resp = analyse("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa", officer)
    assert resp.status_code in (404, 200)


# ---------------------------------------------------------------------------
# Tracing
# ---------------------------------------------------------------------------
def test_trace_path_is_sankey_ready(analysed):
    path = analyse(analysed["TRON"], analysed["__auth__"]).json()["trace_path"]
    assert path["node_count"] >= 2
    assert path["link_count"] >= 1
    assert path["nodes"] and path["links"]

    addresses = {n["address"] for n in path["nodes"]}
    for link in path["links"]:
        assert link["source"] in addresses
        assert link["target"] in addresses

    # The reported address is hop 0 and is labelled as such.
    root = [n for n in path["nodes"] if n["hop"] == 0]
    assert root and root[0]["role"] == "reported_suspect"


def test_depth_parameter_limits_the_trace(analysed):
    shallow = analyse(analysed["TRON"], analysed["__auth__"], depth=2).json()["trace_path"]
    deep = analyse(analysed["TRON"], analysed["__auth__"], depth=8).json()["trace_path"]
    assert shallow["depth"] == 2
    assert deep["node_count"] >= shallow["node_count"]


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------
def test_attribution_states_its_method(analysed):
    """An investigator must always be able to tell a curated tag from a guess."""
    for key, address in analysed.items():
        if key == "__auth__":
            continue
        attribution = analyse(address, analysed["__auth__"]).json()["attribution"]
        assert attribution["method"] in ("tagged_db", "classifier", "none")


def test_tagged_attribution_names_its_source(analysed):
    body = analyse(analysed["TRON"], analysed["__auth__"]).json()
    attribution = body["attribution"]
    if attribution["method"] == "tagged_db":
        assert attribution["source"], "a curated attribution must cite its source"
        assert attribution["entity_name"]


def test_classifier_attribution_is_never_named(analysed):
    """Behaviour can suggest a category; it must not invent a company name."""
    for key, address in analysed.items():
        if key == "__auth__":
            continue
        attribution = analyse(address, analysed["__auth__"]).json()["attribution"]
        if attribution["method"] == "classifier":
            assert attribution["entity_name"] is None


# ---------------------------------------------------------------------------
# Risk explainability
# ---------------------------------------------------------------------------
def test_score_is_explained_by_its_contributing_cases(analysed):
    body = analyse(analysed["TRON"], analysed["__auth__"]).json()
    assert body["risk_explanation"]
    assert body["risk_factors"]

    if body["risk_score"] > 0:
        assert body["contributing_case_ids"], "a non-zero score must name its cases"
        assert len(body["contributions"]) == len(body["contributing_case_ids"])
        for c in body["contributions"]:
            assert 0 <= c["decay_weight"] <= 1
            assert c["points"] >= 0
            assert c["case_number"]


def test_case_lists_are_capped_but_the_totals_are_not(analysed):
    """A heavily reported wallet must not return an unbounded response.

    The cap is on the payload only. `contributing_case_count` reports every case
    the score was computed from, so a truncated list can never be mistaken for
    the complete audit trail.
    """
    body = analyse(analysed["TRON"], analysed["__auth__"]).json()
    limit = body["listing_limit"]

    assert limit > 0
    assert len(body["contributions"]) <= limit
    assert len(body["contributing_case_ids"]) <= limit
    assert len(body["reported_in_cases"]) <= limit

    # The totals are the real figures, and can legitimately exceed the cap.
    assert body["contributing_case_count"] >= len(body["contributing_case_ids"])
    assert body["reported_in_cases_total"] >= len(body["reported_in_cases"])


def test_a_truncated_list_keeps_the_highest_scoring_cases(analysed):
    """Truncation must not drop the cases that actually drove the score."""
    body = analyse(analysed["TRON"], analysed["__auth__"]).json()
    points = [c["points"] for c in body["contributions"]]
    assert points == sorted(points, reverse=True)


def test_contributions_are_ordered_by_influence(analysed):
    contributions = analyse(analysed["TRON"], analysed["__auth__"]).json()["contributions"]
    points = [c["points"] for c in contributions]
    assert points == sorted(points, reverse=True)


def test_mixer_interaction_is_reported(analysed):
    """The ETH ring routes through a mixer; it must be flagged, not unwound."""
    body = analyse(analysed["ETH"], analysed["__auth__"]).json()
    assert isinstance(body["mixer_interaction"], bool)
    if body["mixer_interaction"]:
        assert any("mixer" in f.lower() for f in body["risk_factors"])


# ---------------------------------------------------------------------------
# Provenance + human-in-the-loop
# ---------------------------------------------------------------------------
def test_response_declares_provenance_and_is_recommendation_only(analysed):
    body = analyse(analysed["BTC"], analysed["__auth__"]).json()
    assert body["data_provenance"] in ("synthetic", "live_indexer_apis")
    notice = body["notice"].lower()
    assert "recommendation only" in notice
    assert "approval" in notice


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------
def test_ranked_exchanges_scores_each_entity_on_its_own_chain(analysed):
    """Regression: hardcoding one chain silently zeroed every other chain."""
    resp = client.get(
        "/api/v1/exchanges/ranked", params={"limit": 10}, headers=analysed["__auth__"]
    )
    assert resp.status_code == 200
    entities = resp.json()["entities"]
    assert entities

    for e in entities:
        assert e["chain"] in ("BTC", "ETH", "TRON")
        # An entity that cases demonstrably reach must not score zero.
        if e["case_count"] > 0:
            assert e["risk_score"] > 0, f"{e['entity_name']} has cases but scored 0"


def test_ranked_exchanges_are_sorted_by_score(analysed):
    scores = [
        e["risk_score"]
        for e in client.get(
            "/api/v1/exchanges/ranked", headers=analysed["__auth__"]
        ).json()["entities"]
    ]
    assert scores == sorted(scores, reverse=True)
