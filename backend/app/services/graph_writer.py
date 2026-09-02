"""Writes normalised transactions into the Neo4j money graph.

Writes both shapes described in docs/schema.md, on purpose:

  (:Address)-[:SENT]->(:Transaction)-[:RECEIVED_BY]->(:Address)
      full UTXO structure - required by common-input-ownership, which needs the
      complete input set of a transaction as a first-class thing.

  (:Address)-[:TRANSFERRED]->(:Address)
      denormalised edge - what multi-hop tracing traverses, so a variable-length
      match does not have to bounce through a :Transaction node at every step.

Everything is MERGE-based and therefore idempotent: re-ingesting the same
transaction updates properties instead of duplicating the graph.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from app.db.neo4j import get_driver
from app.services.connectors.base import ChainTransaction

logger = logging.getLogger(__name__)


_MERGE_TX = """
UNWIND $txs AS tx
MERGE (t:Transaction {chain: tx.chain, txid: tx.txid})
  ON CREATE SET t.timestamp = datetime(tx.timestamp),
                t.block_height = tx.block_height,
                t.fee = tx.fee,
                t.asset = tx.asset,
                t.input_count = size(tx.inputs),
                t.output_count = size(tx.outputs)
  ON MATCH  SET t.input_count = size(tx.inputs),
                t.output_count = size(tx.outputs)

// --- inputs -------------------------------------------------------------
WITH t, tx
CALL (t, tx) {
  UNWIND tx.inputs AS inp
  MERGE (a:Address {chain: tx.chain, address_norm: inp.address_norm})
    ON CREATE SET a.address = inp.address,
                  a.first_seen = datetime(tx.timestamp),
                  a.last_seen  = datetime(tx.timestamp),
                  a.tx_count = 0
  SET a.first_seen = CASE WHEN a.first_seen > datetime(tx.timestamp)
                          THEN datetime(tx.timestamp) ELSE a.first_seen END,
      a.last_seen  = CASE WHEN a.last_seen  < datetime(tx.timestamp)
                          THEN datetime(tx.timestamp) ELSE a.last_seen END
  MERGE (a)-[s:SENT {vin_index: inp.index}]->(t)
    ON CREATE SET s.value = inp.value
  RETURN count(*) AS _in
}

// --- outputs ------------------------------------------------------------
WITH t, tx
CALL (t, tx) {
  UNWIND tx.outputs AS outp
  MERGE (b:Address {chain: tx.chain, address_norm: outp.address_norm})
    ON CREATE SET b.address = outp.address,
                  b.first_seen = datetime(tx.timestamp),
                  b.last_seen  = datetime(tx.timestamp),
                  b.tx_count = 0
  SET b.first_seen = CASE WHEN b.first_seen > datetime(tx.timestamp)
                          THEN datetime(tx.timestamp) ELSE b.first_seen END,
      b.last_seen  = CASE WHEN b.last_seen  < datetime(tx.timestamp)
                          THEN datetime(tx.timestamp) ELSE b.last_seen END
  MERGE (t)-[r:RECEIVED_BY {vout_index: outp.index}]->(b)
    ON CREATE SET r.value = outp.value
  SET r.is_change = outp.is_change
  RETURN count(*) AS _out
}

// --- denormalised transfer edges (tracing path) -------------------------
WITH tx
UNWIND tx.inputs AS inp
UNWIND tx.outputs AS outp
MATCH (a:Address {chain: tx.chain, address_norm: inp.address_norm})
MATCH (b:Address {chain: tx.chain, address_norm: outp.address_norm})
WHERE a.address_norm <> b.address_norm
MERGE (a)-[tr:TRANSFERRED {txid: tx.txid}]->(b)
  ON CREATE SET tr.value = outp.value,
                tr.timestamp = datetime(tx.timestamp),
                tr.asset = tx.asset
RETURN count(*) AS edges
"""

_REFRESH_TX_COUNT = """
MATCH (a:Address)
WHERE a.chain = $chain
OPTIONAL MATCH (a)-[:SENT]->(:Transaction)
WITH a, count(*) AS sent
OPTIONAL MATCH (:Transaction)-[:RECEIVED_BY]->(a)
WITH a, sent, count(*) AS received
SET a.tx_count = sent + received
RETURN count(a) AS updated
"""

_LINK_CASE = """
MERGE (k:Case {case_id: $case_id})
  ON CREATE SET k.case_number = $case_number,
                k.reported_at = datetime($reported_at)
WITH k
MATCH (a:Address {chain: $chain, address_norm: $address_norm})
MERGE (k)-[r:REPORTED]->(a)
  ON CREATE SET r.reported_at = datetime($reported_at)
RETURN count(r) AS linked
"""


from app.services.chain_detect import normalize_address as _normalise_address  # noqa: E402


def _tx_to_params(tx: ChainTransaction) -> dict:
    return {
        "chain": tx.chain,
        "txid": tx.txid,
        "timestamp": tx.timestamp.isoformat(),
        "block_height": tx.block_height,
        "fee": float(tx.fee),
        "asset": tx.asset,
        "inputs": [
            {
                "address": i.address,
                "address_norm": _normalise_address(tx.chain, i.address),
                "value": float(i.value),
                "index": i.index,
            }
            for i in tx.inputs
        ],
        "outputs": [
            {
                "address": o.address,
                "address_norm": _normalise_address(tx.chain, o.address),
                "value": float(o.value),
                "index": o.index,
                "is_change": o.is_change,
            }
            for o in tx.outputs
        ],
    }


def write_transactions(transactions: Iterable[ChainTransaction], batch_size: int = 100) -> dict:
    """Write transactions into the graph. Idempotent."""
    txs = list(transactions)
    if not txs:
        return {"transactions": 0, "edges": 0}

    total_edges = 0
    driver = get_driver()
    with driver.session() as session:
        for start in range(0, len(txs), batch_size):
            batch = [_tx_to_params(t) for t in txs[start : start + batch_size]]
            record = session.run(_MERGE_TX, txs=batch).single()
            total_edges += record["edges"] if record else 0

    chains = {t.chain for t in txs}
    with driver.session() as session:
        for chain in chains:
            session.run(_REFRESH_TX_COUNT, chain=chain)

    logger.info("graph: wrote %d transactions, %d transfer edges", len(txs), total_edges)
    return {"transactions": len(txs), "edges": total_edges}


def link_case_to_address(
    case_id: str, case_number: str, reported_at: str, chain: str, address_norm: str
) -> int:
    """Attach a :Case node to the reported address so cross-case queries work."""
    with get_driver().session() as session:
        record = session.run(
            _LINK_CASE,
            case_id=str(case_id),
            case_number=case_number,
            reported_at=reported_at,
            chain=chain,
            address_norm=address_norm,
        ).single()
    return record["linked"] if record else 0


def graph_stats(chain: str | None = None) -> dict:
    """Node/edge counts, for health output and tests."""
    where = "WHERE a.chain = $chain" if chain else ""
    query = f"""
    MATCH (a:Address) {where}
    WITH count(a) AS addresses
    MATCH (t:Transaction) {'WHERE t.chain = $chain' if chain else ''}
    WITH addresses, count(t) AS transactions
    MATCH ()-[r:TRANSFERRED]->()
    RETURN addresses, transactions, count(r) AS transfers
    """
    with get_driver().session() as session:
        record = session.run(query, chain=chain).single()
    if record is None:
        return {"addresses": 0, "transactions": 0, "transfers": 0}
    return dict(record)


def clear_graph() -> None:
    """Delete EVERY node and relationship, seeded tags included.

    Destructive and rarely what you want - `clear_test_data()` is the one tests
    should use. Kept for a deliberate full reset.
    """
    with get_driver().session() as session:
        session.run("MATCH (n) DETACH DELETE n")


def clear_test_data() -> None:
    """Remove transaction/graph data while preserving the seeded tag database.

    The tagged-address seed (OFAC, Etherscan labels, WalletExplorer, TagPacks)
    is expensive to rebuild and is reference data, not test data. A blanket
    `MATCH (n) DETACH DELETE n` in a test fixture silently destroys it and
    leaves attribution returning `none` for everything afterwards.

    Seeded addresses are exactly those carrying a :TAGGED_AS edge, so sweeping
    only untagged addresses keeps the reference data intact. Test-created
    entities use an `entity_id` prefixed "test-" and are removed first, so the
    addresses they tagged get swept too.
    """
    statements = [
        "MATCH (e:Entity) WHERE e.entity_id STARTS WITH 'test-' DETACH DELETE e",
        "MATCH (t:Transaction) DETACH DELETE t",
        "MATCH (c:Cluster) DETACH DELETE c",
        "MATCH (k:Case) DETACH DELETE k",
        "MATCH (a:Address) WHERE NOT (a)-[:TAGGED_AS]->() DETACH DELETE a",
        # Seeded addresses survive above; drop any graph edges left on them.
        "MATCH (:Address)-[r:TRANSFERRED|SAME_OWNER|MEMBER_OF]-() DELETE r",
    ]
    with get_driver().session() as session:
        for stmt in statements:
            session.run(stmt).consume()
