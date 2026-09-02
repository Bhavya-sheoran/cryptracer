"""Clustering heuristic tests against a real Neo4j graph.

Addresses are named A_/B_/... with a valid-looking suffix; clustering never
validates address format (that happened at intake), so readable names make the
assertions legible.
"""

from __future__ import annotations

import pytest

from app.services import clustering
from app.tests.conftest import tx

A = "addrA"
B = "addrB"
C = "addrC"
D = "addrD"
E = "addrE"
P = "addrP"
Q = "addrQ"
R = "addrR"
X = "addrX"
Y = "addrY"


def members_of(address: str) -> set[str]:
    cluster = clustering.get_cluster_for_address("BTC", address)
    return set(cluster["members"]) if cluster else set()


# ---------------------------------------------------------------------------
# Common input ownership
# ---------------------------------------------------------------------------
def test_co_spending_addresses_are_clustered(graph):
    """Two addresses funding the same transaction must land in one cluster."""
    graph.write_transactions(
        [
            tx("t1", [(X, 1.0)], [(A, 0.5), (B, 0.5)], minutes=0),
            tx("t2", [(A, 0.5), (B, 0.5)], [(D, 0.99)], minutes=10),  # co-spend
            tx("t3", [(Y, 2.0)], [(C, 2.0)], minutes=5),
        ]
    )

    edges = clustering.apply_common_input_ownership("BTC")
    assert edges == 1, "expected exactly one SAME_OWNER edge from the co-spend"

    clustering.resolve_clusters("BTC")

    assert members_of(A) == {A, B}
    assert members_of(B) == {A, B}
    # An address that never co-spent must not be dragged in.
    assert members_of(C) == {C}


def test_co_spend_clustering_is_transitive(graph):
    """A+B co-spend and B+C co-spend implies one cluster of three."""
    graph.write_transactions(
        [
            tx("f1", [(X, 3.0)], [(A, 1.0), (B, 1.0), (C, 1.0)], minutes=0),
            tx("t1", [(A, 1.0), (B, 1.0)], [(D, 1.9)], minutes=10),
            tx("t2", [(B, 0.5), (C, 1.0)], [(E, 1.4)], minutes=20),
        ]
    )

    clustering.apply_common_input_ownership("BTC")
    result = clustering.resolve_clusters("BTC")

    assert members_of(A) == {A, B, C}
    assert result["largest_cluster"] == 3


def test_single_input_transactions_produce_no_edges(graph):
    """Nothing to infer when every transaction has one input."""
    graph.write_transactions(
        [
            tx("t1", [(A, 1.0)], [(B, 0.99)], minutes=0),
            tx("t2", [(B, 0.9)], [(C, 0.89)], minutes=10),
        ]
    )
    assert clustering.apply_common_input_ownership("BTC") == 0


def test_clustering_is_idempotent(graph):
    """Re-running must not duplicate edges or churn cluster identity."""
    graph.write_transactions(
        [
            tx("f1", [(X, 2.0)], [(A, 1.0), (B, 1.0)], minutes=0),
            tx("t1", [(A, 1.0), (B, 1.0)], [(D, 1.9)], minutes=10),
        ]
    )

    first_edges = clustering.apply_common_input_ownership("BTC")
    clustering.resolve_clusters("BTC")
    key_first = clustering.get_cluster_for_address("BTC", A)["cluster_key"]

    second_edges = clustering.apply_common_input_ownership("BTC")
    clustering.resolve_clusters("BTC")
    key_second = clustering.get_cluster_for_address("BTC", A)["cluster_key"]

    assert first_edges == 1
    assert second_edges == 1, "MERGE should not create a duplicate SAME_OWNER edge"
    assert key_first == key_second, "cluster identity must be stable across runs"
    assert members_of(A) == {A, B}


# ---------------------------------------------------------------------------
# Change address
# ---------------------------------------------------------------------------
def test_change_address_is_flagged_and_clustered(graph):
    """A 2-output spend whose only fresh output is not an input is change."""
    graph.write_transactions(
        [
            tx("seed_q", [(Y, 5.0)], [(Q, 5.0)], minutes=0),    # Q exists beforehand
            tx("seed_p", [(X, 4.0)], [(P, 4.0)], minutes=5),    # P exists beforehand
            tx("spend", [(P, 4.0)], [(Q, 1.0), (R, 3.0)], minutes=30),  # R is fresh
        ]
    )

    result = clustering.apply_change_address("BTC")
    assert result["transactions"] == 1
    assert result["edges"] == 1

    clustering.resolve_clusters("BTC")
    assert members_of(P) == {P, R}, "spender and its change address share an owner"
    assert Q not in members_of(P), "the payee must not be absorbed into the cluster"


def test_change_heuristic_declines_when_both_outputs_are_fresh(graph):
    """Ambiguous: two brand-new outputs give no basis to pick the change one.

    A false merge corrupts attribution downstream, so the heuristic abstains.
    """
    graph.write_transactions(
        [
            tx("seed_p", [(X, 4.0)], [(P, 4.0)], minutes=0),
            tx("spend", [(P, 4.0)], [(Q, 1.0), (R, 3.0)], minutes=30),  # both fresh
        ]
    )

    result = clustering.apply_change_address("BTC")
    assert result["transactions"] == 0
    assert result["edges"] == 0


def test_change_heuristic_ignores_non_two_output_transactions(graph):
    """Only exactly-two-output transactions are considered."""
    graph.write_transactions(
        [
            tx("seed_p", [(X, 6.0)], [(P, 6.0)], minutes=0),
            tx("three_out", [(P, 6.0)], [(Q, 2.0), (R, 2.0), (D, 1.9)], minutes=30),
        ]
    )
    assert clustering.apply_change_address("BTC")["transactions"] == 0


def test_change_output_relationship_is_marked(graph):
    """The RECEIVED_BY edge to the change output carries is_change = true."""
    graph.write_transactions(
        [
            tx("seed_q", [(Y, 5.0)], [(Q, 5.0)], minutes=0),
            tx("seed_p", [(X, 4.0)], [(P, 4.0)], minutes=5),
            tx("spend", [(P, 4.0)], [(Q, 1.0), (R, 3.0)], minutes=30),
        ]
    )
    clustering.apply_change_address("BTC")

    from app.db.neo4j import get_driver

    with get_driver().session() as session:
        record = session.run(
            """
            MATCH (:Transaction {txid: 'spend'})-[r:RECEIVED_BY]->(a:Address)
            RETURN a.address_norm AS addr, coalesce(r.is_change, false) AS is_change
            """
        ).data()

    flags = {row["addr"]: row["is_change"] for row in record}
    assert flags[R] is True
    assert flags[Q] is False


# ---------------------------------------------------------------------------
# Account-model chains
# ---------------------------------------------------------------------------
def test_account_chain_addresses_are_their_own_cluster(graph):
    """ETH/TRON have no co-spend structure to infer shared ownership from."""
    graph.write_transactions(
        [
            tx("e1", [("0xaaa", 1.0)], [("0xbbb", 1.0)], minutes=0, chain="ETH"),
            tx("e2", [("0xbbb", 1.0)], [("0xccc", 1.0)], minutes=10, chain="ETH"),
        ]
    )

    assert clustering.apply_common_input_ownership("ETH") == 0
    result = clustering.resolve_clusters("ETH")

    # Assert on these three addresses rather than a global count: the seeded
    # tagged-address database also contains ETH addresses.
    assert result["clusters"] >= 3
    for addr in ("0xaaa", "0xbbb", "0xccc"):
        assert clustering.get_cluster_for_address("ETH", addr)["size"] == 1
    cluster = clustering.get_cluster_for_address("ETH", "0xbbb")
    assert cluster["heuristic"] == clustering.HEURISTIC_ACCOUNT_SINGLE
    assert cluster["size"] == 1


# ---------------------------------------------------------------------------
# Full pass
# ---------------------------------------------------------------------------
def test_run_clustering_combines_both_heuristics(graph):
    """Co-spend and change-address edges resolve into a single owner cluster."""
    graph.write_transactions(
        [
            tx("seed", [(X, 10.0)], [(A, 5.0), (B, 5.0)], minutes=0),
            tx("seed_q", [(Y, 5.0)], [(Q, 5.0)], minutes=1),
            tx("cospend", [(A, 5.0), (B, 5.0)], [(P, 9.9)], minutes=10),
            tx("spend", [(P, 9.9)], [(Q, 2.0), (R, 7.8)], minutes=20),  # R fresh -> change
        ]
    )

    stats = clustering.run_clustering("BTC")

    assert stats["common_input_edges"] == 1
    assert stats["change_address"]["edges"] == 1
    # A+B joined by co-spend; P+R joined by change. P is not co-spent with A/B
    # (it was an output, not an input), so these stay two distinct clusters.
    assert members_of(A) == {A, B}
    assert members_of(P) == {P, R}


def test_gds_plugin_is_available(neo4j_available):
    if not neo4j_available:
        pytest.skip("Neo4j not reachable")
    assert clustering.gds_available(), "GDS plugin must be loaded for clustering"


def test_growing_component_does_not_leave_stale_membership(graph):
    """A component that merges under a new cluster key must not leave the
    address attached to its previous cluster.

    Cluster keys derive from the lexicographically smallest member, so pulling
    in a smaller address re-keys the whole cluster.
    """
    # First pass: B and C co-spend -> cluster keyed on the smaller of the two.
    graph.write_transactions(
        [
            tx("f1", [(X, 2.0)], [(B, 1.0), (C, 1.0)], minutes=0),
            tx("t1", [(B, 1.0), (C, 1.0)], [(D, 1.9)], minutes=10),
        ]
    )
    clustering.apply_common_input_ownership("BTC")
    clustering.resolve_clusters("BTC")
    first_key = clustering.get_cluster_for_address("BTC", B)["cluster_key"]

    # Second pass: A (lexicographically smaller) joins the same component.
    graph.write_transactions(
        [
            tx("f2", [(Y, 1.0)], [(A, 1.0)], minutes=15),
            tx("t2", [(A, 1.0), (B, 0.5)], [(E, 1.4)], minutes=20),
        ]
    )
    clustering.apply_common_input_ownership("BTC")
    clustering.resolve_clusters("BTC")

    second_key = clustering.get_cluster_for_address("BTC", B)["cluster_key"]
    assert second_key != first_key, "cluster should be re-keyed on the new smallest member"
    assert members_of(B) == {A, B, C}

    from app.db.neo4j import get_driver

    with get_driver().session() as session:
        multi = session.run(
            """
            MATCH (a:Address)-[:MEMBER_OF]->(c:Cluster)
            WITH a, count(c) AS n WHERE n > 1
            RETURN count(a) AS bad
            """
        ).single()["bad"]
        orphans = session.run(
            """
            MATCH (c:Cluster) WHERE NOT EXISTS { MATCH (:Address)-[:MEMBER_OF]->(c) }
            RETURN count(c) AS orphans
            """
        ).single()["orphans"]

    assert multi == 0, "no address may belong to two clusters"
    assert orphans == 0, "emptied clusters must be pruned"
