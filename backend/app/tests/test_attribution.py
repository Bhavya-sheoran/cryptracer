"""VASP attribution tests.

The behavioural fallback must never name a company - it can only suggest a
category. That constraint is asserted here because violating it would mean the
UI shows a fabricated exchange name to an investigator.
"""

from __future__ import annotations

import pytest

from app.services import attribution, clustering
from app.tests.conftest import tx

EXCHANGE_HOT = "exchHot1"
DEPOSITOR = "depositor"


@pytest.fixture
def tagged_graph(graph):
    """A graph with one tagged exchange address, tagged from a named source."""
    from app.db.neo4j import get_driver

    graph.write_transactions(
        [tx("seed", [("payerA", 5.0)], [(EXCHANGE_HOT, 5.0)], minutes=0)]
    )
    with get_driver().session() as session:
        session.run(
            """
            MERGE (e:Entity {entity_id: 'test-entity-1'})
              SET e.name = 'Test Exchange', e.entity_type = 'exchange'
            WITH e
            MATCH (a:Address {chain: 'BTC', address_norm: $addr})
            MERGE (a)-[t:TAGGED_AS {source: 'walletexplorer'}]->(e)
              SET t.confidence = 0.9
            """,
            addr=EXCHANGE_HOT,
        )
    return graph


# ---------------------------------------------------------------------------
# Tier 1: curated tags
# ---------------------------------------------------------------------------
def test_tagged_address_attributes_via_tagged_db(tagged_graph):
    result = attribution.attribute("BTC", EXCHANGE_HOT)
    assert result.method == attribution.METHOD_TAGGED
    assert result.entity_name == "Test Exchange"
    assert result.entity_type == "exchange"
    assert result.source == "walletexplorer"
    assert result.confidence == pytest.approx(0.9)


def test_attribution_always_cites_its_source(tagged_graph):
    """An attribution with no provenance is not usable as evidence."""
    result = attribution.attribute("BTC", EXCHANGE_HOT)
    assert result.source
    assert result.evidence
    assert all(e["source"] for e in result.evidence)


def test_tag_on_one_cluster_member_attributes_the_whole_cluster(tagged_graph):
    """Clustering already asserted shared ownership, so the tag propagates."""
    sibling = "exchSibling"
    tagged_graph.write_transactions(
        [
            tx("fund", [("payerB", 4.0)], [(sibling, 4.0)], minutes=5),
            # co-spend puts EXCHANGE_HOT and sibling in one cluster
            tx("cospend", [(EXCHANGE_HOT, 2.0), (sibling, 2.0)], [("out1", 3.9)], minutes=10),
        ]
    )
    clustering.run_clustering("BTC")

    result = attribution.attribute("BTC", sibling)
    assert result.method == attribution.METHOD_TAGGED
    assert result.entity_name == "Test Exchange"
    assert result.cluster_size >= 2
    assert result.matched_address == EXCHANGE_HOT


# ---------------------------------------------------------------------------
# Tier 2: behavioural fallback
# ---------------------------------------------------------------------------
def test_untagged_cluster_falls_back_to_classifier(graph):
    """Many distinct senders into one address is the exchange deposit shape."""
    txs = [
        tx(f"dep{i}", [(f"sender{i}", 1.0)], [(DEPOSITOR, 1.0)], minutes=i)
        for i in range(12)
    ]
    graph.write_transactions(txs)
    clustering.run_clustering("BTC")

    result = attribution.attribute("BTC", DEPOSITOR)
    assert result.method == attribution.METHOD_CLASSIFIER
    assert result.entity_type == "exchange"
    assert 0 < result.confidence < 1


def test_classifier_never_invents_an_entity_name(graph):
    """Behaviour can suggest a category. It cannot name a company."""
    txs = [
        tx(f"dep{i}", [(f"s{i}", 1.0)], [(DEPOSITOR, 1.0)], minutes=i) for i in range(15)
    ]
    graph.write_transactions(txs)
    clustering.run_clustering("BTC")

    result = attribution.attribute("BTC", DEPOSITOR)
    assert result.entity_name is None, "a behavioural guess must remain unnamed"
    assert "not named" in (result.note or "")


def test_classifier_confidence_stays_below_curated_tags(graph):
    """A guess must never outrank a curated fact in the UI."""
    txs = [tx(f"d{i}", [(f"s{i}", 1.0)], [(DEPOSITOR, 1.0)], minutes=i) for i in range(20)]
    graph.write_transactions(txs)
    clustering.run_clustering("BTC")
    result = attribution.attribute("BTC", DEPOSITOR)
    assert result.confidence <= 0.75


def test_sparse_address_yields_low_confidence_unknown(graph):
    graph.write_transactions([tx("t1", [("lonelyA", 1.0)], [("lonelyB", 1.0)], minutes=0)])
    clustering.run_clustering("BTC")
    result = attribution.attribute("BTC", "lonelyB")
    assert result.entity_type in ("unknown", "payment_processor")
    assert (result.confidence or 0) < 0.4


def test_unknown_address_returns_none_method(graph):
    """No data at all is reported as `none`, not guessed at."""
    result = attribution.attribute("BTC", "neverSeenAddress")
    assert result.method == attribution.METHOD_NONE
    assert result.entity_name is None


# ---------------------------------------------------------------------------
# Behavioural feature extraction
# ---------------------------------------------------------------------------
def test_behavioural_features_count_distinct_counterparties(graph):
    txs = [tx(f"in{i}", [(f"from{i}", 1.0)], [(DEPOSITOR, 1.0)], minutes=i) for i in range(5)]
    txs.append(tx("out", [(DEPOSITOR, 4.0)], [("sink", 4.0)], minutes=30))
    graph.write_transactions(txs)

    features = attribution.behavioural_features("BTC", DEPOSITOR)
    assert features["distinct_senders"] == 5
    assert features["distinct_receivers"] == 1
    assert features["counterparty_diversity"] == 6


def test_classify_category_handles_empty_features():
    entity_type, confidence, reason = attribution.classify_category({})
    assert entity_type == "unknown"
    assert confidence == 0.0
    assert reason
