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
from decimal import Decimal

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
  SET         t.asset_key = tx.asset_key,
              t.token_contract = tx.token_contract,
              t.transfer_type = tx.transfer_type,
              t.status = tx.status

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
// Pairs are computed in Python (see _transfer_pairs) rather than by an
// UNWIND x UNWIND cartesian product here. That earlier form set the FULL
// output value on every input->output edge, so summing edge values across a
// UTXO path overstated volume by a factor equal to the input count.
WITH tx
UNWIND tx.transfers AS t
MATCH (a:Address {chain: tx.chain, address_norm: t.from_norm})
MATCH (b:Address {chain: tx.chain, address_norm: t.to_norm})
MERGE (a)-[tr:TRANSFERRED {txid: tx.txid}]->(b)
  SET tr.value            = t.value,
      tr.value_attributed = t.value_attributed,
      tr.timestamp        = datetime(tx.timestamp),
      tr.asset            = tx.asset,
      tr.asset_key        = tx.asset_key,
      tr.token_contract   = tx.token_contract,
      tr.transfer_type    = tx.transfer_type
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


def _transfer_pairs(tx: ChainTransaction) -> list[dict]:
    """Flatten a transaction into address-to-address transfers with a value that
    is safe to sum across a path.

    A UTXO transaction has no per-pair value on chain: N inputs fund M outputs
    collectively, and which rupee went where is not recorded. Attributing the
    full output value to every input->output pair (the previous behaviour)
    inflates any path total by the input count - a 2-input transaction moving
    0.369 BTC summed to 0.739.

    We instead split each output across the inputs in proportion to what each
    input contributed:

        attributed(i, o) = o.value * (i.value / total_input_value)

    Summed over every pair this telescopes back to the true total output value,
    so `sum(tr.value_attributed)` along a path is a defensible volume figure.
    `tr.value` keeps the raw output amount, because that is what appears on the
    block explorer and belongs in evidence.

    Account-model chains have exactly one input and one output, so attributed
    and raw are identical and this is a no-op for them.

    A failed transaction yields no pairs at all. It consumed gas but moved
    nothing, so it must never appear as value flow - while the :Transaction node
    and its SENT/RECEIVED_BY edges still record that the attempt was made, which
    is itself evidence.
    """
    if not tx.moved_value:
        return []

    # Collapse repeated addresses first. One address commonly appears as several
    # inputs (spending several UTXOs it owns) or several outputs; pairing before
    # collapsing would count its value once per occurrence.
    inputs_by_addr: dict[str, Decimal] = {}
    for inp in tx.inputs:
        norm = _normalise_address(tx.chain, inp.address)
        inputs_by_addr[norm] = inputs_by_addr.get(norm, Decimal(0)) + inp.value

    outputs_by_addr: dict[str, Decimal] = {}
    for out in tx.outputs:
        norm = _normalise_address(tx.chain, out.address)
        outputs_by_addr[norm] = outputs_by_addr.get(norm, Decimal(0)) + out.value

    total_in = sum(inputs_by_addr.values(), Decimal(0))
    n_inputs = max(len(inputs_by_addr), 1)

    pairs: list[dict] = []
    for to_norm, out_value in outputs_by_addr.items():
        for from_norm, in_value in inputs_by_addr.items():
            if from_norm == to_norm:
                continue  # change returning to the spender is not a transfer

            if total_in > 0:
                share = out_value * (in_value / total_in)
            else:
                # Coinbase, or an input set carrying no recorded value: split
                # evenly rather than crediting each input with the whole output.
                share = out_value / Decimal(n_inputs)

            pairs.append(
                {
                    "from_norm": from_norm,
                    "to_norm": to_norm,
                    # Raw amount this address received in this transaction -
                    # what a block explorer shows, so it belongs in evidence.
                    "value": float(out_value),
                    # Safe to sum along a path.
                    "value_attributed": float(share),
                }
            )

    return pairs


def _tx_to_params(tx: ChainTransaction) -> dict:
    return {
        "chain": tx.chain,
        "txid": tx.txid,
        "timestamp": tx.timestamp.isoformat(),
        "block_height": tx.block_height,
        "fee": float(tx.fee),
        # `asset` stays a display symbol so the Sankey tooltip and every
        # existing consumer keep working; identity travels alongside it.
        "asset": tx.asset.symbol,
        "asset_key": tx.asset.key,
        "token_contract": tx.asset.contract,
        "transfer_type": tx.transfer_type,
        "status": tx.status,
        "transfers": _transfer_pairs(tx),
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
