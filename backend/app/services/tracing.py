"""Trace path assembly for the investigator view.

The graph already holds the money flow; this turns it into the ordered hop
sequence and the node/link sets the Sankey visualisation consumes in Phase 3.
"""

from __future__ import annotations

import logging

from app.db.neo4j import get_driver

logger = logging.getLogger(__name__)

# Shortest path to each reachable address, so the Sankey shows the primary route
# rather than every permutation of a dense subgraph.
_TRACE_PATH = """
MATCH (root:Address {chain: $chain, address_norm: $address_norm})
CALL (root) {
  MATCH p = shortestPath((root)-[:TRANSFERRED*1..%(depth)d]->(dest:Address))
  WHERE dest <> root
  RETURN p, dest
  LIMIT $max_nodes
}
WITH root, p, dest
OPTIONAL MATCH (dest)-[:TAGGED_AS]->(e:Entity)
OPTIONAL MATCH (dest)-[:MEMBER_OF]->(cl:Cluster)
RETURN dest.address_norm AS address,
       length(p)         AS hop,
       [n IN nodes(p) | n.address_norm] AS path,
       [r IN relationships(p) | {
          txid: r.txid, value: r.value,
          timestamp: toString(r.timestamp), asset: r.asset
       }] AS transfers,
       e.name        AS entity_name,
       e.entity_type AS entity_type,
       cl.cluster_key AS cluster_key
ORDER BY hop, address
"""

_TERMINALS = """
MATCH (root:Address {chain: $chain, address_norm: $address_norm})
MATCH p = shortestPath((root)-[:TRANSFERRED*1..%(depth)d]->(dest:Address))
MATCH (dest)-[:TAGGED_AS]->(e:Entity)
WHERE e.entity_type IN ['exchange', 'sanctioned', 'payment_processor', 'gambling']
OPTIONAL MATCH (dest)-[:MEMBER_OF]->(cl:Cluster)
RETURN dest.address_norm AS address, length(p) AS hop,
       e.name AS entity_name, e.entity_type AS entity_type,
       cl.cluster_key AS cluster_key
ORDER BY hop ASC
"""

# Endpoints of the flow: nothing leaves them within the traced subgraph.
_SINKS = """
MATCH (root:Address {chain: $chain, address_norm: $address_norm})
MATCH p = shortestPath((root)-[:TRANSFERRED*1..%(depth)d]->(dest:Address))
WHERE NOT EXISTS { MATCH (dest)-[:TRANSFERRED]->(:Address) }
OPTIONAL MATCH (dest)-[:MEMBER_OF]->(cl:Cluster)
RETURN dest.address_norm AS address, length(p) AS hop,
       cl.cluster_key AS cluster_key
ORDER BY hop DESC
LIMIT 25
"""


def trace_path(chain: str, address_norm: str, depth: int = 6, max_nodes: int = 300) -> dict:
    """Ordered hops reachable from `address_norm`, plus Sankey-ready node/link sets."""
    query = _TRACE_PATH % {"depth": max(1, min(depth, 8))}
    with get_driver().session() as session:
        rows = [
            dict(r)
            for r in session.run(
                query, chain=chain, address_norm=address_norm, max_nodes=max_nodes
            )
        ]

    nodes: dict[str, dict] = {
        address_norm: {"address": address_norm, "hop": 0, "role": "reported_suspect"}
    }
    links: dict[tuple[str, str, str], dict] = {}

    for row in rows:
        addr = row["address"]
        nodes.setdefault(
            addr,
            {
                "address": addr,
                "hop": row["hop"],
                "role": "terminal" if row["entity_name"] else "intermediate",
                "entity_name": row["entity_name"],
                "entity_type": row["entity_type"],
                "cluster_key": row["cluster_key"],
            },
        )
        path, transfers = row["path"], row["transfers"]
        for i, transfer in enumerate(transfers):
            src, dst = path[i], path[i + 1]
            nodes.setdefault(src, {"address": src, "hop": i, "role": "intermediate"})
            nodes.setdefault(dst, {"address": dst, "hop": i + 1, "role": "intermediate"})
            key = (src, dst, transfer.get("txid") or "")
            if key not in links:
                links[key] = {
                    "source": src,
                    "target": dst,
                    "txid": transfer.get("txid"),
                    "value": transfer.get("value"),
                    "timestamp": transfer.get("timestamp"),
                    "asset": transfer.get("asset"),
                }

    return {
        "root": address_norm,
        "chain": chain,
        "depth": depth,
        "hops": [
            {
                "address": r["address"],
                "hop": r["hop"],
                "entity_name": r["entity_name"],
                "entity_type": r["entity_type"],
                "cluster_key": r["cluster_key"],
            }
            for r in rows
        ],
        "nodes": sorted(nodes.values(), key=lambda n: (n["hop"], n["address"])),
        "links": list(links.values()),
        "node_count": len(nodes),
        "link_count": len(links),
        "truncated": len(rows) >= max_nodes,
    }


def terminal_attributions(chain: str, address_norm: str, depth: int = 6) -> list[dict]:
    """Tagged services reachable from the reported address, nearest hop first."""
    query = _TERMINALS % {"depth": max(1, min(depth, 8))}
    with get_driver().session() as session:
        rows = [dict(r) for r in session.run(query, chain=chain, address_norm=address_norm)]

    seen: set[str] = set()
    unique = []
    for row in rows:
        if row["address"] in seen:
            continue
        seen.add(row["address"])
        unique.append(row)
    return unique


def sink_addresses(chain: str, address_norm: str, depth: int = 6) -> list[dict]:
    """Where the traced flow stops within the subgraph we hold."""
    query = _SINKS % {"depth": max(1, min(depth, 8))}
    with get_driver().session() as session:
        return [dict(r) for r in session.run(query, chain=chain, address_norm=address_norm)]
