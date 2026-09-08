"""Wallet intake orchestration.

One complaint arrives, and this walks it end to end:

    validate address -> resolve/create wallet -> dedupe against prior cases
    -> create case -> BFS expand the money flow via a connector
    -> write the graph -> run clustering -> report what happened

Kept out of the API layer so Phase 4's NCRP intake endpoint and the synthetic
demo loader can reuse exactly the same path a manual submission takes.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Case, CaseWallet, TraceRun, Wallet
from app.services import clustering, graph_writer, illicit_model
from app.services.chain_detect import AddressInfo, detect, normalize_address
from app.services.connectors import get_connector
from app.services.connectors.base import BlockchainConnector, ChainTransaction, ConnectorError

logger = logging.getLogger(__name__)
settings = get_settings()

# Known mixer addresses are tagged in Phase 2; until then the graph flag set by
# the tagged-address seed is the signal. Interaction is recorded, never unwound.
MIXER_FLAG_PROPERTY = "is_mixer"


class IntakeError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Wallet + case persistence
# ---------------------------------------------------------------------------
def resolve_or_create_wallet(db: Session, info: AddressInfo) -> tuple[Wallet, bool]:
    """Fetch the wallet row for this address, creating it if new."""
    existing = db.execute(
        select(Wallet).where(
            Wallet.chain == info.chain, Wallet.address_norm == info.address_norm
        )
    ).scalar_one_or_none()

    if existing is not None:
        return existing, False

    wallet = Wallet(
        address=info.address.strip(),
        address_norm=info.address_norm,
        chain=info.chain,
        address_kind=info.address_kind,
    )
    db.add(wallet)
    db.flush()
    return wallet, True


def find_prior_cases(db: Session, wallet: Wallet) -> list[Case]:
    """Cases that already implicate this wallet - the multi-victim signal."""
    return list(
        db.execute(
            select(Case)
            .join(CaseWallet, CaseWallet.case_id == Case.id)
            .where(CaseWallet.wallet_id == wallet.id)
            .order_by(Case.reported_at.desc())
        )
        .scalars()
        .all()
    )


def next_case_number(db: Session) -> str:
    """Sequential, human-quotable case reference."""
    year = datetime.now(UTC).year
    prefix = f"SIH183-{year}-"
    count = db.execute(
        select(func.count()).select_from(Case).where(Case.case_number.like(f"{prefix}%"))
    ).scalar_one()
    return f"{prefix}{count + 1:06d}"


# ---------------------------------------------------------------------------
# Graph expansion
# ---------------------------------------------------------------------------
def expand_money_flow(
    connector: BlockchainConnector,
    chain: str,
    root_address: str,
    max_depth: int,
    max_breadth: int,
) -> dict:
    """Breadth-first walk of the outbound money flow from `root_address`.

    Follows the direction the funds moved: from an address we take the
    transactions it *spent* into, and the outputs of those become the next hop.
    Transactions where the address only received are still written to the graph
    for context (they show where the victim's money came in) but are not
    expanded, or every trace would balloon backwards into unrelated history.
    """
    seen: set[str] = set()
    frontier = [root_address]
    collected: dict[str, ChainTransaction] = {}
    hops = 0

    # One upstream call per address expanded, and nothing previously bounded
    # how many addresses reached the next level: `max_breadth` caps
    # transactions per address, not the fan-out those transactions produce. A
    # wallet that spent into 25 transactions with 100 outputs each yields 2,500
    # addresses at depth 1, and squares from there. On real data the walk stays
    # small, but "small in the cases we tried" is not a bound - and the person
    # who exhausts a 100,000-call daily quota does it with one click, having
    # been given no indication that the click was expensive.
    budget = max(1, settings.connector_call_budget)
    calls_made = 0
    budget_exhausted = False
    frontier_truncated = False

    for depth in range(max_depth):
        if not frontier or budget_exhausted:
            break
        next_frontier: list[str] = []

        for address in frontier:
            if address in seen:
                continue

            if calls_made >= budget:
                budget_exhausted = True
                logger.warning(
                    "trace of %s stopped at depth %d: upstream call budget of %d spent",
                    root_address,
                    depth,
                    budget,
                )
                break

            seen.add(address)

            try:
                calls_made += 1
                txs = connector.get_transactions(address, limit=max_breadth)
            except ConnectorError as exc:
                logger.warning("connector failed for %s at depth %d: %s", address, depth, exc)
                continue

            for tx in txs:
                collected.setdefault(f"{tx.chain}:{tx.txid}", tx)
                # Compare in normalised space. Connectors return addresses in
                # their native form (EIP-55 checksummed for ETH), while the
                # frontier holds normalised ones - comparing the two directly
                # silently never matches on ETH.
                inputs_norm = {normalize_address(chain, a) for a in tx.input_addresses}
                if address in inputs_norm:
                    for out in tx.output_addresses:
                        out_norm = normalize_address(chain, out)
                        if out_norm not in seen:
                            next_frontier.append(out_norm)

        if next_frontier:
            hops = depth + 1

        # Cap each level as well as the total. Without this a single wide hop
        # could consume the whole budget at depth 1 and report a one-hop trace,
        # which is the least useful shape a forensic answer can take - the
        # money is followed further by going deeper, not by enumerating every
        # sibling of the first hop.
        level_cap = max_breadth * 2
        if len(next_frontier) > level_cap:
            logger.info(
                "trace of %s: depth %d frontier %d addresses, capped to %d",
                root_address,
                depth + 1,
                len(next_frontier),
                level_cap,
            )
            next_frontier = next_frontier[:level_cap]
            frontier_truncated = True

        frontier = next_frontier

    return {
        "transactions": list(collected.values()),
        "addresses_touched": len(seen),
        "hops_discovered": hops,
        # Surfaced, never silent. A trace that stopped early is a materially
        # different finding from one that ran to completion and found nothing,
        # and an investigator has to be able to tell the two apart.
        "upstream_calls": calls_made,
        "budget_exhausted": budget_exhausted,
        "frontier_truncated": frontier_truncated,
        "complete": not (budget_exhausted or frontier_truncated),
    }


def detect_mixer_interaction(chain: str, addresses: set[str]) -> bool:
    """True when the traced subgraph touches an address tagged as a mixer.

    Flagged as a risk signal only. Nothing here attempts to de-mix.
    """
    if not addresses:
        return False
    query = """
    MATCH (a:Address)
    WHERE a.chain = $chain AND a.address_norm IN $addresses
      AND (a.is_mixer = true OR EXISTS {
            MATCH (a)-[:TAGGED_AS]->(e:Entity) WHERE e.entity_type = 'mixer'
          })
    RETURN count(a) AS hits
    """
    from app.db.neo4j import get_driver

    with get_driver().session() as session:
        record = session.run(query, chain=chain, addresses=sorted(addresses)).single()
    return bool(record and record["hits"] > 0)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def intake_wallet(
    db: Session,
    address: str,
    victim_ref: str | None = None,
    amount_inr=None,
    narrative: str | None = None,
    ncrp_ref: str | None = None,
    source: str = "manual",
    trace_depth: int | None = None,
    run_clustering: bool = True,
) -> dict:
    """Full intake for one reported wallet. Returns a summary dict."""
    info = detect(address)
    if not info.valid:
        raise IntakeError(info.reason or "invalid address")

    depth = trace_depth or settings.trace_max_depth
    wallet, _created = resolve_or_create_wallet(db, info)

    prior_cases = find_prior_cases(db, wallet)
    duplicate = {
        "is_duplicate": bool(prior_cases),
        "prior_case_count": len(prior_cases),
        "prior_case_numbers": [c.case_number for c in prior_cases],
        "note": (
            f"This address was already reported in {len(prior_cases)} earlier case(s). "
            "Repeat reports of one address across victims are themselves a fraud signal."
            if prior_cases
            else None
        ),
    }

    case = Case(
        case_number=next_case_number(db),
        ncrp_ref=ncrp_ref,
        source=source,
        status="tracing",
        victim_ref=victim_ref,
        amount_inr=amount_inr,
        narrative=narrative,
    )
    db.add(case)
    db.flush()
    db.add(CaseWallet(case_id=case.id, wallet_id=wallet.id, role="reported_suspect"))
    wallet.report_count = len(prior_cases) + 1

    chain = info.chain
    connector = get_connector(chain)
    trace = TraceRun(
        case_id=case.id,
        root_wallet_id=wallet.id,
        max_depth=depth,
        status="running",
        data_source=connector.source_name,
    )
    db.add(trace)
    db.flush()

    try:
        expansion = expand_money_flow(
            connector, info.chain, info.address_norm, depth, settings.trace_max_breadth
        )
        # Score before writing, while full input/output lists are still in
        # hand. The graph keeps only counts and a fee, so scoring later would
        # mean re-fetching from the indexer and spending an API call to
        # re-derive something already known. Returns {} for chains with no
        # trained model, which leaves the property null rather than zero.
        scores = illicit_model.score_transactions(expansion["transactions"])
        write_stats = graph_writer.write_transactions(
            expansion["transactions"],
            data_source=connector.source_name,
            scores=scores,
            model_version=illicit_model.model_info()["version"],
        )
        graph_writer.link_case_to_address(
            case_id=str(case.id),
            case_number=case.case_number,
            reported_at=case.reported_at.isoformat()
            if case.reported_at
            else datetime.now(UTC).isoformat(),
            chain=info.chain,
            address_norm=info.address_norm,
        )

        touched = {info.address_norm}
        for tx in expansion["transactions"]:
            touched.update(normalize_address(chain, a) for a in tx.input_addresses)
            touched.update(normalize_address(chain, a) for a in tx.output_addresses)

        trace.mixer_interaction = detect_mixer_interaction(info.chain, touched)
        trace.hops_discovered = expansion["hops_discovered"]
        trace.addresses_touched = expansion["addresses_touched"]
        # "complete" here means the run finished without error, which is what
        # the trace_status_t enum models. Whether it explored the whole graph
        # is a separate axis, carried in the response as `coverage` - a trace
        # cut short by the call budget must not be presented as exhaustive.
        trace.status = "complete"
        trace.finished_at = datetime.now(UTC)
        if not expansion["complete"]:
            logger.warning(
                "trace of %s was truncated: %d upstream calls, budget_exhausted=%s, "
                "frontier_truncated=%s",
                info.address_norm,
                expansion["upstream_calls"],
                expansion["budget_exhausted"],
                expansion["frontier_truncated"],
            )

        if run_clustering and expansion["transactions"]:
            clustering.run_clustering(info.chain)

    except Exception as exc:  # keep the case; record the failure honestly
        logger.exception("trace failed for %s", info.address_norm)
        trace.status = "failed"
        trace.error = str(exc)[:500]
        trace.finished_at = datetime.now(UTC)
        write_stats = {"transactions": 0}
    finally:
        connector.close()

    wallet.last_traced_at = datetime.now(UTC)
    case.status = "analysed" if trace.status == "complete" else "open"
    db.commit()

    cluster = clustering.get_cluster_for_address(info.chain, info.address_norm) or {}

    return {
        "case_id": case.id,
        "case_number": case.case_number,
        "wallet_id": wallet.id,
        "address": wallet.address,
        "chain": wallet.chain,
        "address_kind": wallet.address_kind,
        "reported_at": case.reported_at,
        "duplicate": duplicate,
        "trace": {
            "trace_run_id": trace.id,
            "status": trace.status,
            "data_source": trace.data_source,
            "max_depth": trace.max_depth,
            "hops_discovered": trace.hops_discovered,
            "addresses_touched": trace.addresses_touched,
            "transactions_ingested": write_stats.get("transactions", 0),
            "mixer_interaction": trace.mixer_interaction,
            "upstream_calls": expansion["upstream_calls"],
            "complete": expansion["complete"],
            "budget_exhausted": expansion["budget_exhausted"],
            "frontier_truncated": expansion["frontier_truncated"],
            "coverage_note": (
                None
                if expansion["complete"]
                else "Trace stopped early to bound upstream API usage. Findings are "
                "valid but not exhaustive; absence of a result is not evidence of "
                "absence."
            ),
        },
        "cluster": {
            "cluster_key": cluster.get("cluster_key"),
            "heuristic": cluster.get("heuristic"),
            "size": cluster.get("size", 0),
            "members": cluster.get("members", []),
        },
        "warnings": info.warnings,
        "data_provenance": trace.data_source,
    }
