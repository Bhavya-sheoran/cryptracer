"""Address clustering heuristics, as Cypher + GDS jobs.

Two UTXO heuristics, both of which materialise `:SAME_OWNER` edges, and one
connected-components pass over those edges via GDS Weakly Connected Components.
Separating "assert co-ownership" from "resolve co-ownership into clusters" means
each heuristic stays independently testable, and adding a third heuristic later
needs no change to the resolver.

  1. common-input-ownership (UTXO only)
     Addresses that co-spend into the same transaction are presumed to share an
     owner - to sign that transaction, one party had to hold every input key.

  2. change-address (UTXO only)
     Conservative. An output is treated as change only when a transaction has
     exactly two outputs, exactly one of which goes to an address never seen
     before that transaction and which is not itself an input. A false merge
     corrupts every attribution downstream, so this deliberately declines to
     guess in ambiguous cases.

  3. account_single (ETH / TRON)
     Account-model chains have no co-spend structure, so an address is its own
     cluster unless a curated tag says otherwise.

Mixers are never unwound here. Interaction with a tagged mixer is recorded as a
risk signal elsewhere; this module makes no attempt to defeat mixing.
"""

from __future__ import annotations

import logging

from app.db.neo4j import get_driver
from app.services.connectors.base import UTXO_CHAINS

logger = logging.getLogger(__name__)

GDS_GRAPH_NAME = "sameOwnerGraph"

HEURISTIC_COMMON_INPUT = "common_input_ownership"
HEURISTIC_CHANGE_ADDRESS = "change_address"
HEURISTIC_ACCOUNT_SINGLE = "account_single"


# ---------------------------------------------------------------------------
# Heuristic 1: common input ownership
# ---------------------------------------------------------------------------
_COMMON_INPUT = """
MATCH (a:Address)-[:SENT]->(t:Transaction)<-[:SENT]-(b:Address)
WHERE t.chain = $chain
  AND a.address_norm < b.address_norm
MERGE (a)-[r:SAME_OWNER {heuristic: $heuristic}]->(b)
  ON CREATE SET r.txid = t.txid, r.created_at = datetime()
RETURN count(r) AS edges
"""


def apply_common_input_ownership(chain: str = "BTC") -> int:
    """Link addresses that co-spend into the same transaction."""
    if chain not in UTXO_CHAINS:
        return 0
    with get_driver().session() as session:
        record = session.run(
            _COMMON_INPUT, chain=chain, heuristic=HEURISTIC_COMMON_INPUT
        ).single()
    edges = record["edges"] if record else 0
    logger.info("clustering: common-input-ownership produced %d SAME_OWNER edges", edges)
    return edges


# ---------------------------------------------------------------------------
# Heuristic 2: change address
# ---------------------------------------------------------------------------
# `first_seen >= t.timestamp` is the "never seen before this transaction" test:
# the graph writer keeps first_seen as the earliest timestamp an address appears
# at, so equality means this transaction is its debut.
_CHANGE_ADDRESS = """
MATCH (t:Transaction)
WHERE t.chain = $chain AND t.output_count = 2
MATCH (t)-[r:RECEIVED_BY]->(o:Address)

// Is this output address also on the input side of the same transaction?
OPTIONAL MATCH (o)-[spent:SENT]->(t)
WITH t, r, o, spent IS NOT NULL AS is_input

// "Fresh" = making its debut in this transaction and not also an input.
WITH t, collect({
       rel: r,
       addr: o,
       fresh: (o.first_seen >= t.timestamp AND NOT is_input)
     }) AS outs
WHERE size(outs) = 2

WITH t, [x IN outs WHERE x.fresh] AS fresh
WHERE size(fresh) = 1

WITH t, fresh[0].rel AS change_rel, fresh[0].addr AS change_addr
SET change_rel.is_change = true

WITH t, change_addr
MATCH (inp:Address)-[:SENT]->(t)
WHERE inp.address_norm <> change_addr.address_norm
MERGE (inp)-[so:SAME_OWNER {heuristic: $heuristic}]->(change_addr)
  ON CREATE SET so.txid = t.txid, so.created_at = datetime()
RETURN count(DISTINCT t) AS transactions, count(so) AS edges
"""


def apply_change_address(chain: str = "BTC") -> dict:
    """Flag change outputs and link them to the spending address."""
    if chain not in UTXO_CHAINS:
        return {"transactions": 0, "edges": 0}
    with get_driver().session() as session:
        record = session.run(
            _CHANGE_ADDRESS, chain=chain, heuristic=HEURISTIC_CHANGE_ADDRESS
        ).single()
    result = dict(record) if record else {"transactions": 0, "edges": 0}
    logger.info(
        "clustering: change-address flagged %s transactions, %s SAME_OWNER edges",
        result.get("transactions"),
        result.get("edges"),
    )
    return result


# ---------------------------------------------------------------------------
# Resolver: GDS weakly connected components over SAME_OWNER
# ---------------------------------------------------------------------------
_DROP_GRAPH = """
CALL gds.graph.exists($name) YIELD exists
WITH exists WHERE exists
CALL gds.graph.drop($name) YIELD graphName
RETURN graphName
"""

_PROJECT = """
MATCH (source:Address)
OPTIONAL MATCH (source)-[r:SAME_OWNER]-(target:Address)
RETURN gds.graph.project($name, source, target,
  {}, {undirectedRelationshipTypes: ['*']}) AS g
"""

_WCC_STREAM = """
CALL gds.wcc.stream($name)
YIELD nodeId, componentId
WITH gds.util.asNode(nodeId) AS a, componentId
WHERE a.chain = $chain
  AND (a.tx_count > 0
       OR EXISTS { MATCH (a)-[:TRANSFERRED]-() }
       OR EXISTS { MATCH (a)-[:SENT|RECEIVED_BY]-() })
RETURN componentId, collect(a.address_norm) AS members
"""

# Cluster keys are deterministic - derived from the lexicographically smallest
# member - so re-running clustering does not churn cluster identity.
_WRITE_CLUSTERS = """
UNWIND $clusters AS c
MERGE (cl:Cluster {cluster_key: c.cluster_key})
  ON CREATE SET cl.chain = $chain, cl.heuristic = c.heuristic
  SET cl.size = c.size, cl.updated_at = datetime()
WITH cl, c
UNWIND c.members AS member
MATCH (a:Address {chain: $chain, address_norm: member})

// Drop stale memberships first. A component that grows to include a
// lexicographically smaller address gets a NEW cluster_key, and without this
// the address would end up MEMBER_OF both the old and the new cluster.
CALL (a, cl) {
  MATCH (a)-[old:MEMBER_OF]->(other:Cluster)
  WHERE other.cluster_key <> cl.cluster_key
  DELETE old
}

MERGE (a)-[m:MEMBER_OF]->(cl)
  ON CREATE SET m.assigned_at = datetime()
SET m.heuristic = c.heuristic
RETURN count(DISTINCT cl) AS clusters, count(m) AS memberships
"""

# Clusters can be emptied by the re-key above; drop the husks so cluster counts
# stay truthful.
_PRUNE_EMPTY_CLUSTERS = """
MATCH (cl:Cluster {chain: $chain})
WHERE NOT EXISTS { MATCH (:Address)-[:MEMBER_OF]->(cl) }
DELETE cl
RETURN count(cl) AS pruned
"""


def _drop_projection(session) -> None:
    session.run(_DROP_GRAPH, name=GDS_GRAPH_NAME).consume()


def resolve_clusters(chain: str) -> dict:
    """Run WCC over SAME_OWNER edges and materialise :Cluster nodes.

    Account-model chains skip the graph algorithm entirely - with no co-spend
    structure every component would be a singleton anyway.
    """
    driver = get_driver()

    if chain not in UTXO_CHAINS:
        with driver.session() as session:
            record = session.run(
                """
                MATCH (a:Address {chain: $chain})
                // Only addresses actually seen in the graph. The tagged-address
                // seed holds thousands of exchange addresses we have never
                // traced; minting a cluster for each would be noise and would
                // make every clustering pass proportional to the tag DB.
                WHERE a.tx_count > 0
                   OR EXISTS { MATCH (a)-[:TRANSFERRED]-() }
                   OR EXISTS { MATCH (a)-[:SENT|RECEIVED_BY]-() }
                MERGE (cl:Cluster {cluster_key: $prefix + a.address_norm})
                  ON CREATE SET cl.chain = $chain, cl.heuristic = $heuristic, cl.size = 1
                MERGE (a)-[m:MEMBER_OF]->(cl)
                  ON CREATE SET m.assigned_at = datetime(), m.heuristic = $heuristic
                RETURN count(DISTINCT cl) AS clusters, count(m) AS memberships
                """,
                chain=chain,
                prefix=f"{chain.lower()}:single:",
                heuristic=HEURISTIC_ACCOUNT_SINGLE,
            ).single()
        result = dict(record) if record else {"clusters": 0, "memberships": 0}
        logger.info("clustering: %s account_single -> %s clusters", chain, result.get("clusters"))
        return result

    with driver.session() as session:
        _drop_projection(session)
        session.run(_PROJECT, name=GDS_GRAPH_NAME).consume()
        try:
            components = [
                {"componentId": r["componentId"], "members": sorted(r["members"])}
                for r in session.run(_WCC_STREAM, name=GDS_GRAPH_NAME, chain=chain)
            ]
        finally:
            _drop_projection(session)

    clusters = []
    for comp in components:
        members = comp["members"]
        if not members:
            continue
        heuristic = (
            HEURISTIC_COMMON_INPUT if len(members) > 1 else HEURISTIC_ACCOUNT_SINGLE
        )
        clusters.append(
            {
                "cluster_key": f"{chain.lower()}:{members[0]}",
                "members": members,
                "size": len(members),
                "heuristic": heuristic,
            }
        )

    if not clusters:
        return {"clusters": 0, "memberships": 0, "multi_address_clusters": 0}

    with driver.session() as session:
        record = session.run(_WRITE_CLUSTERS, clusters=clusters, chain=chain).single()
        session.run(_PRUNE_EMPTY_CLUSTERS, chain=chain).consume()

    result = dict(record) if record else {"clusters": 0, "memberships": 0}
    result["multi_address_clusters"] = sum(1 for c in clusters if c["size"] > 1)
    result["largest_cluster"] = max((c["size"] for c in clusters), default=0)
    logger.info(
        "clustering: %s resolved %s clusters (%s multi-address, largest %s)",
        chain,
        result.get("clusters"),
        result["multi_address_clusters"],
        result["largest_cluster"],
    )
    return result


def run_clustering(chain: str) -> dict:
    """Full clustering pass for one chain."""
    stats = {
        "chain": chain,
        "common_input_edges": apply_common_input_ownership(chain),
        "change_address": apply_change_address(chain),
    }
    stats.update(resolve_clusters(chain))
    return stats


def get_cluster_for_address(chain: str, address_norm: str) -> dict | None:
    """Return the cluster an address belongs to, with its members."""
    query = """
    MATCH (a:Address {chain: $chain, address_norm: $address_norm})-[:MEMBER_OF]->(cl:Cluster)
    OPTIONAL MATCH (m:Address)-[:MEMBER_OF]->(cl)
    RETURN cl.cluster_key AS cluster_key, cl.heuristic AS heuristic,
           cl.chain AS chain, count(m) AS size, collect(m.address_norm) AS members
    """
    with get_driver().session() as session:
        record = session.run(query, chain=chain, address_norm=address_norm).single()
    return dict(record) if record else None


def gds_available() -> bool:
    """True when the GDS plugin is loaded."""
    try:
        with get_driver().session() as session:
            return session.run("RETURN gds.version() AS v").single() is not None
    except Exception:
        return False
