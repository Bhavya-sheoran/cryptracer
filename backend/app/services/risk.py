"""Fraud-linkage risk scoring for an exchange / VASP cluster.

The question this answers is *not* "is this transaction illicit". It is: **is
this exchange a repeat destination for funds from reported fraud?** That is a
property of the destination accumulated over many complaints, so it is computed
by aggregation over cases, not by a per-transaction model.

Score construction, deliberately arithmetic rather than learned:

    weight_i = 0.5 ^ (age_days_i / half_life_days)     # time decay
    points_i = BASE_POINTS_PER_CASE * weight_i * severity_i
    raw      = sum(points_i) + mixer_bonus
    score    = 100 * (1 - exp(-raw / SATURATION))      # squashed into 0..100

Why this shape:
  * **Time decay** - an exchange that was a fraud destination two years ago and
    has since tightened KYC should not be scored like one receiving fraud
    proceeds this week. Half-life is configurable.
  * **Saturating curve** - the difference between 1 and 5 linked cases matters a
    lot; between 80 and 85 it does not. A linear sum would let one prolific
    reporter dominate the ranking.
  * **Per-case contributions are stored** in `risk_contributions`, so the number
    is reconstructable by hand. The dashboard renders the contributing cases
    beneath the score. This is the explainability requirement: an investigator
    must be able to see *which complaints* drove a High rating.

The XGBoost model trained on the Elliptic dataset is a *separate* signal
(`ml/src/train_fraud_clf.py`). It contributes here as a capped advisory
modifier, never as the score itself:

  * It scores **Bitcoin transactions only**. Elliptic is a Bitcoin dataset,
    and no comparable public labelled set exists for other chains, so
    `tx_features` refuses them outright rather than returning a confident
    number from constants and mismatched units.
  * Elliptic labels transactions, not exchange culpability. A flagged
    transaction reaching an exchange says something about the transaction, not
    about the exchange's conduct.
  * At its 0.95 operating point it is roughly 0.84 precision - about one flag
    in six is wrong - so it is capped at 6 points against a saturation
    constant of 60. It can reorder two near-equal candidates and cannot, by
    construction, move a cluster from low to high on its own.
  * Its contribution appears in `factors` with the model version and its
    measured precision, so a score that includes it can still be recomputed by
    hand from the case table plus that one line.

Where a model is unavailable or the chain unsupported, the contribution is
zero and no factor is recorded - the score is exactly what it was before.
"""

from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Case, Cluster, Entity, RiskContribution, RiskScore

logger = logging.getLogger(__name__)
settings = get_settings()

MODEL_VERSION = "risk-v1-timedecay"

BASE_POINTS_PER_CASE = 10.0
SATURATION = 60.0            # raw points at which the curve is ~81% of the way up
MIXER_BONUS = 8.0            # flat uplift when traced flow touched a tagged mixer
SANCTIONED_BONUS = 40.0      # an OFAC-listed destination is categorically severe


@dataclass
class Contribution:
    case_id: str
    case_number: str
    reported_at: str
    age_days: int
    decay_weight: float
    points: float
    terminal_address: str | None = None
    amount_inr: float | None = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class RiskResult:
    score: float
    label: str
    model_version: str = MODEL_VERSION
    half_life_days: int = 0
    entity_name: str | None = None
    entity_type: str | None = None
    cluster_key: str | None = None
    contributing_case_ids: list[str] = field(default_factory=list)
    contributions: list[dict] = field(default_factory=list)
    factors: list[str] = field(default_factory=list)
    explanation: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def label_for(score: float) -> str:
    if score >= settings.risk_high_threshold:
        return "high"
    if score >= settings.risk_medium_threshold:
        return "medium"
    return "low"


def decay_weight(age_days: float, half_life_days: int) -> float:
    """0.5 ^ (age / half_life). 1.0 today, 0.5 at one half-life, and so on."""
    if half_life_days <= 0:
        return 1.0
    return float(0.5 ** (max(age_days, 0.0) / half_life_days))


def _squash(raw: float) -> float:
    """Map unbounded accumulated points onto 0..100, saturating."""
    return round(100.0 * (1.0 - math.exp(-max(raw, 0.0) / SATURATION)), 2)


# ---------------------------------------------------------------------------
# Which cases terminate at this entity / cluster?
# ---------------------------------------------------------------------------
def cases_terminating_at(
    chain: str, cluster_key: str | None, entity_name: str | None
) -> list[dict]:
    """Cases whose traced flow reaches an address in this cluster or entity.

    Answered in Neo4j because it is a reachability question: does a path exist
    from the address a victim reported to an address belonging to this cluster?
    """
    from app.db.neo4j import get_driver

    if not cluster_key and not entity_name:
        return []

    query = """
    MATCH (k:Case)-[:REPORTED]->(root:Address {chain: $chain})
    CALL (root) {
      MATCH (root)-[:TRANSFERRED*0..8]->(dest:Address)
      OPTIONAL MATCH (dest)-[:MEMBER_OF]->(cl:Cluster)
      OPTIONAL MATCH (dest)-[:TAGGED_AS]->(e:Entity)
      WITH dest, cl, e
      WHERE ($cluster_key IS NOT NULL AND cl.cluster_key = $cluster_key)
         OR ($entity_name IS NOT NULL AND e.name = $entity_name)
      RETURN dest.address_norm AS terminal, 1 AS hit
      LIMIT 1
    }
    RETURN k.case_id AS case_id, k.case_number AS case_number,
           k.reported_at AS reported_at, terminal
    """
    with get_driver().session() as session:
        return [
            dict(r)
            for r in session.run(
                query, chain=chain, cluster_key=cluster_key, entity_name=entity_name
            )
        ]


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def flagged_transactions_for(
    chain: str, cluster_key: str | None, entity_name: str | None
) -> dict:
    """Transactions reaching this cluster that the classifier flagged as illicit.

    Reads the probability stamped onto each :Transaction at ingest, so this
    costs a graph query rather than an indexer call and a re-featurisation.

    Only transactions at or above the model's measured operating point are
    counted. Everything below it is discarded rather than averaged in: the
    0.84 precision figure that justifies using this signal at all was measured
    at that threshold, and a mean over unfiltered probabilities would not carry
    that guarantee.
    """
    from app.db.neo4j import get_driver
    from app.services import illicit_model

    if not cluster_key and not entity_name:
        return {"flagged": 0, "scored": 0, "max_probability": None}

    info = illicit_model.model_info()
    if not info["available"]:
        return {"flagged": 0, "scored": 0, "max_probability": None}

    query = """
    MATCH (t:Transaction {chain: $chain})-[:RECEIVED_BY]->(dest:Address)
    WHERE t.illicit_probability IS NOT NULL
    OPTIONAL MATCH (dest)-[:MEMBER_OF]->(cl:Cluster)
    OPTIONAL MATCH (dest)-[:TAGGED_AS]->(e:Entity)
    WITH DISTINCT t, cl, e
    WHERE ($cluster_key IS NOT NULL AND cl.cluster_key = $cluster_key)
       OR ($entity_name IS NOT NULL AND e.name = $entity_name)
    RETURN count(t) AS scored,
           sum(CASE WHEN t.illicit_probability >= $threshold THEN 1 ELSE 0 END) AS flagged,
           max(t.illicit_probability) AS max_probability
    """
    with get_driver().session() as session:
        row = session.run(
            query,
            chain=chain,
            cluster_key=cluster_key,
            entity_name=entity_name,
            threshold=info["threshold"],
        ).single()

    if row is None:
        return {"flagged": 0, "scored": 0, "max_probability": None}
    return {
        "flagged": int(row["flagged"] or 0),
        "scored": int(row["scored"] or 0),
        "max_probability": row["max_probability"],
    }


def score_entity(
    db: Session,
    chain: str,
    cluster_key: str | None,
    entity_name: str | None = None,
    entity_type: str | None = None,
    mixer_interaction: bool = False,
    persist: bool = True,
) -> RiskResult:
    """Compute the fraud-linkage score for one exchange cluster."""
    half_life = settings.risk_decay_half_life_days
    now = datetime.now(UTC)

    hits = cases_terminating_at(chain, cluster_key, entity_name)

    # Enrich from Postgres: the graph knows reachability, the database knows the
    # reported amount and the canonical case record.
    contributions: list[Contribution] = []
    raw_points = 0.0

    case_numbers = [h["case_number"] for h in hits if h.get("case_number")]
    case_rows = {}
    if case_numbers:
        for case in db.execute(
            select(Case).where(Case.case_number.in_(case_numbers))
        ).scalars():
            case_rows[case.case_number] = case

    for hit in hits:
        case = case_rows.get(hit.get("case_number"))
        if case is None or case.reported_at is None:
            continue
        reported_at = case.reported_at
        if reported_at.tzinfo is None:
            reported_at = reported_at.replace(tzinfo=UTC)

        age_days = max((now - reported_at).days, 0)
        weight = decay_weight(age_days, half_life)
        points = BASE_POINTS_PER_CASE * weight
        raw_points += points

        contributions.append(
            Contribution(
                case_id=str(case.id),
                case_number=case.case_number,
                reported_at=reported_at.isoformat(),
                age_days=age_days,
                decay_weight=round(weight, 4),
                points=round(points, 3),
                terminal_address=hit.get("terminal"),
                amount_inr=float(case.amount_inr) if case.amount_inr is not None else None,
            )
        )

    factors: list[str] = []
    if contributions:
        factors.append(
            f"{len(contributions)} reported case(s) trace to this cluster "
            f"(time-decayed, {half_life}-day half-life)"
        )

    if mixer_interaction:
        raw_points += MIXER_BONUS
        factors.append("traced flow interacted with a tagged mixer (flagged, not unwound)")

    if entity_type == "sanctioned":
        raw_points += SANCTIONED_BONUS
        factors.append("destination is on the OFAC SDN list")

    # The classifier, applied as an advisory nudge and nothing more. At its
    # 0.95 operating point the model is ~0.84 precision, so about one flag in
    # six is wrong - which is useful for ordering two otherwise-equal
    # candidates and useless for deciding one on its own. The cap (6 points
    # against a saturation constant of 60) keeps complaint evidence dominant.
    from app.services import illicit_model

    ml_signal = flagged_transactions_for(chain, cluster_key, entity_name)
    ml_points = float(illicit_model.risk_contribution(ml_signal["flagged"]))
    if ml_points > 0:
        info = illicit_model.model_info()
        raw_points += ml_points
        precision = info["precision_at_threshold"]
        factors.append(
            f"{ml_signal['flagged']} of {ml_signal['scored']} scored transaction(s) "
            f"flagged by the Elliptic classifier at p>={info['threshold']:.2f} "
            f"(model {info['version']}, ~{precision:.2f} precision; advisory, "
            f"capped at {info['max_risk_contribution']} points)"
        )

    score = _squash(raw_points)
    label = label_for(score)

    if not factors:
        factors.append("no reported cases currently trace to this cluster")

    contributions.sort(key=lambda c: c.points, reverse=True)

    explanation = (
        f"Score {score}/100 ({label}). "
        + "; ".join(factors)
        + f". Raw points {round(raw_points, 2)} squashed with saturation {SATURATION}."
    )

    result = RiskResult(
        score=score,
        label=label,
        half_life_days=half_life,
        entity_name=entity_name,
        entity_type=entity_type,
        cluster_key=cluster_key,
        contributing_case_ids=[c.case_id for c in contributions],
        contributions=[c.as_dict() for c in contributions],
        factors=factors,
        explanation=explanation,
    )

    if persist:
        _persist(db, result, chain, entity_name, entity_type, cluster_key, contributions)

    return result


def _persist(
    db: Session,
    result: RiskResult,
    chain: str,
    entity_name: str | None,
    entity_type: str | None,
    cluster_key: str | None,
    contributions: list[Contribution],
) -> None:
    """Write the score and its per-case breakdown so it is auditable later."""
    entity_id = None
    if entity_name:
        entity = db.execute(
            select(Entity).where(
                Entity.name == entity_name,
                Entity.entity_type == (entity_type or "unknown"),
            )
        ).scalar_one_or_none()
        if entity is None:
            entity = Entity(name=entity_name, entity_type=entity_type or "unknown")
            db.add(entity)
            db.flush()
        entity_id = entity.id

    cluster_id = None
    if cluster_key:
        cluster = db.execute(
            select(Cluster).where(Cluster.cluster_key == cluster_key)
        ).scalar_one_or_none()
        if cluster is None:
            cluster = Cluster(
                cluster_key=cluster_key,
                chain=chain,
                heuristic="mirrored",
                size=result.contributions and 1 or 1,
                entity_id=entity_id,
                attribution_method="tagged_db" if entity_name else "none",
            )
            db.add(cluster)
            db.flush()
        else:
            if entity_id is not None:
                cluster.entity_id = entity_id
        cluster_id = cluster.id

    if entity_id is None and cluster_id is None:
        return  # ck_risk_target requires at least one target

    row = RiskScore(
        entity_id=entity_id,
        cluster_id=cluster_id,
        score=result.score,
        label=result.label,
        half_life_days=result.half_life_days,
        model_version=result.model_version,
    )
    db.add(row)
    db.flush()

    for c in contributions:
        db.add(
            RiskContribution(
                risk_score_id=row.id,
                case_id=c.case_id,
                terminal_address=c.terminal_address,
                age_days=c.age_days,
                decay_weight=c.decay_weight,
                points=c.points,
            )
        )
    db.commit()


# ---------------------------------------------------------------------------
# Cross-case ranking (feeds the dashboard's exchange leaderboard)
# ---------------------------------------------------------------------------
def rank_entities(db: Session, chain: str | None = None, limit: int = 20) -> list[dict]:
    """Exchanges ranked by how many distinct cases terminate at them."""
    from app.db.neo4j import get_driver

    query = """
    MATCH (k:Case)-[:REPORTED]->(root:Address)
    WHERE $chain IS NULL OR root.chain = $chain
    MATCH (root)-[:TRANSFERRED*0..8]->(dest:Address)-[:TAGGED_AS]->(e:Entity)
    WHERE e.entity_type IN ['exchange', 'sanctioned', 'payment_processor']
    RETURN e.name AS entity_name, e.entity_type AS entity_type,
           count(DISTINCT k.case_id) AS case_count,
           collect(DISTINCT k.case_number)[0..25] AS case_numbers,
           collect(DISTINCT dest.chain)[0] AS chain
    ORDER BY case_count DESC
    LIMIT $limit
    """
    with get_driver().session() as session:
        rows = [dict(r) for r in session.run(query, chain=chain, limit=limit)]

    out = []
    for row in rows:
        # Score each entity on the chain its own tagged addresses live on.
        # Defaulting to a single chain here silently zeroes every entity that
        # is not on it, since the reachability query is chain-scoped.
        entity_chain = chain or row.get("chain") or "BTC"
        result = score_entity(
            db,
            chain=entity_chain,
            cluster_key=None,
            entity_name=row["entity_name"],
            entity_type=row["entity_type"],
            persist=False,
        )
        out.append(
            {
                "entity_name": row["entity_name"],
                "entity_type": row["entity_type"],
                "chain": entity_chain,
                "case_count": row["case_count"],
                "case_numbers": row["case_numbers"],
                "risk_score": result.score,
                "risk_label": result.label,
            }
        )
    out.sort(key=lambda r: r["risk_score"], reverse=True)
    return out
