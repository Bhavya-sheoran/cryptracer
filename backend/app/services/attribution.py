"""VASP attribution: which exchange or service does a cluster belong to?

Two tiers, and the tier used is always reported so an investigator knows whether
they are looking at a curated fact or a model's guess:

  1. `tagged_db`   - a curated tag from a public source (OFAC SDN, Etherscan
                     Label Cloud, WalletExplorer, GraphSense TagPacks). Any
                     address in the cluster carrying a tag attributes the whole
                     cluster, because clustering already asserted shared
                     ownership. Highest-confidence tag wins.
  2. `classifier`  - no tag anywhere in the cluster. Predict the likely *service
                     category* from behavioural features (deposit frequency,
                     wallet age, counterparty diversity). This yields a category
                     and a confidence, never a named company - inventing an
                     exchange name from behaviour alone would be fabrication.

Returning `none` is a legitimate outcome. An unattributed terminal cluster is
useful information; a fabricated attribution is not.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field

from app.db.neo4j import get_driver

logger = logging.getLogger(__name__)

METHOD_TAGGED = "tagged_db"
METHOD_CLASSIFIER = "classifier"
METHOD_NONE = "none"

# Behavioural thresholds for the fallback classifier. Deliberately simple and
# inspectable: an investigator can read these and understand why a category was
# suggested. Tuned against the synthetic ring, and reported as low confidence
# precisely because they are heuristics, not a trained model.
_EXCHANGE_MIN_COUNTERPARTIES = 8
_EXCHANGE_MIN_TX = 10


@dataclass
class AttributionResult:
    method: str = METHOD_NONE
    entity_name: str | None = None
    entity_type: str | None = None
    confidence: float | None = None
    source: str | None = None
    label: str | None = None
    matched_address: str | None = None
    cluster_key: str | None = None
    cluster_size: int = 0
    evidence: list[dict] = field(default_factory=list)
    features: dict | None = None
    note: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Tier 1: curated tags
# ---------------------------------------------------------------------------
_TAG_LOOKUP = """
MATCH (a:Address {chain: $chain, address_norm: $address_norm})
OPTIONAL MATCH (a)-[:MEMBER_OF]->(cl:Cluster)
WITH a, cl
OPTIONAL MATCH (m:Address)-[:MEMBER_OF]->(cl)
WITH a, cl, collect(DISTINCT m) AS members
WITH cl, CASE WHEN size(members) = 0 THEN [a] ELSE members END AS members
UNWIND members AS member
MATCH (member)-[t:TAGGED_AS]->(e:Entity)
RETURN member.address_norm AS matched_address,
       e.name              AS entity_name,
       e.entity_type       AS entity_type,
       t.source            AS source,
       t.confidence        AS confidence,
       cl.cluster_key      AS cluster_key,
       size(members)       AS cluster_size
ORDER BY t.confidence DESC, e.name
"""


def lookup_tags(chain: str, address_norm: str) -> list[dict]:
    """All curated tags on any address in this address's cluster."""
    with get_driver().session() as session:
        return [dict(r) for r in session.run(_TAG_LOOKUP, chain=chain, address_norm=address_norm)]


# ---------------------------------------------------------------------------
# Tier 2: behavioural features + category fallback
# ---------------------------------------------------------------------------
_FEATURES = """
MATCH (a:Address {chain: $chain, address_norm: $address_norm})
OPTIONAL MATCH (a)-[:MEMBER_OF]->(cl:Cluster)
OPTIONAL MATCH (m:Address)-[:MEMBER_OF]->(cl)
WITH a, cl, collect(DISTINCT m) AS members
WITH a, cl, CASE WHEN size(members) = 0 THEN [a] ELSE members END AS members
UNWIND members AS member
OPTIONAL MATCH (member)<-[inc:TRANSFERRED]-(src:Address)
WITH a, cl, members, member,
     count(DISTINCT inc) AS in_tx, collect(DISTINCT src.address_norm) AS senders
OPTIONAL MATCH (member)-[out:TRANSFERRED]->(dst:Address)
WITH a, cl, members, member, in_tx, senders,
     count(DISTINCT out) AS out_tx, collect(DISTINCT dst.address_norm) AS receivers
RETURN cl.cluster_key                              AS cluster_key,
       size(members)                               AS cluster_size,
       sum(in_tx)                                  AS incoming_tx,
       sum(out_tx)                                 AS outgoing_tx,
       size(apoc.coll.toSet(apoc.coll.flatten(collect(senders))))   AS distinct_senders,
       size(apoc.coll.toSet(apoc.coll.flatten(collect(receivers)))) AS distinct_receivers,
       min(member.first_seen)                      AS first_seen,
       max(member.last_seen)                       AS last_seen
"""

# APOC-free fallback, in case the plugin is unavailable.
_FEATURES_PLAIN = """
MATCH (a:Address {chain: $chain, address_norm: $address_norm})
OPTIONAL MATCH (a)-[:MEMBER_OF]->(cl:Cluster)
OPTIONAL MATCH (m:Address)-[:MEMBER_OF]->(cl)
WITH a, cl, collect(DISTINCT m) AS members
WITH a, cl, CASE WHEN size(members) = 0 THEN [a] ELSE members END AS members
UNWIND members AS member
OPTIONAL MATCH (member)<-[inc:TRANSFERRED]-(src:Address)
WITH cl, members, member, count(DISTINCT inc) AS in_tx,
     collect(DISTINCT src.address_norm) AS s
OPTIONAL MATCH (member)-[out:TRANSFERRED]->(dst:Address)
WITH cl, members, member, in_tx, s, count(DISTINCT out) AS out_tx,
     collect(DISTINCT dst.address_norm) AS r
WITH cl, members,
     sum(in_tx) AS incoming_tx, sum(out_tx) AS outgoing_tx,
     reduce(acc = [], x IN collect(s) | acc + x) AS all_senders,
     reduce(acc = [], x IN collect(r) | acc + x) AS all_receivers,
     min(member.first_seen) AS first_seen, max(member.last_seen) AS last_seen
RETURN cl.cluster_key AS cluster_key,
       size(members)  AS cluster_size,
       incoming_tx, outgoing_tx,
       size([x IN all_senders   WHERE x IS NOT NULL]) AS distinct_senders,
       size([x IN all_receivers WHERE x IS NOT NULL]) AS distinct_receivers,
       first_seen, last_seen
"""


def behavioural_features(chain: str, address_norm: str) -> dict:
    """Features describing how a cluster behaves, for the fallback classifier."""
    with get_driver().session() as session:
        try:
            record = session.run(_FEATURES, chain=chain, address_norm=address_norm).single()
        except Exception:
            record = session.run(_FEATURES_PLAIN, chain=chain, address_norm=address_norm).single()

    if record is None:
        return {}

    data = dict(record)
    first_seen, last_seen = data.pop("first_seen", None), data.pop("last_seen", None)
    age_days = 0.0
    if first_seen is not None and last_seen is not None:
        try:
            age_days = round((last_seen.to_native() - first_seen.to_native()).days, 2)
        except Exception:
            age_days = 0.0

    incoming = data.get("incoming_tx") or 0
    senders = data.get("distinct_senders") or 0
    receivers = data.get("distinct_receivers") or 0

    return {
        "cluster_key": data.get("cluster_key"),
        "cluster_size": data.get("cluster_size") or 1,
        "incoming_tx": incoming,
        "outgoing_tx": data.get("outgoing_tx") or 0,
        "distinct_senders": senders,
        "distinct_receivers": receivers,
        "counterparty_diversity": round(senders + receivers, 2),
        "active_days": age_days,
        "deposit_frequency": round(incoming / age_days, 3) if age_days > 0 else float(incoming),
    }


def classify_category(features: dict) -> tuple[str, float, str]:
    """Predict a service *category* from behaviour. Never invents a name.

    Returns (entity_type, confidence, reasoning).
    """
    if not features:
        return "unknown", 0.0, "no behavioural data available for this cluster"

    senders = features.get("distinct_senders", 0)
    receivers = features.get("distinct_receivers", 0)
    diversity = features.get("counterparty_diversity", 0)
    incoming = features.get("incoming_tx", 0)

    # A deposit-taking service: many distinct senders funnelling into few
    # outputs is the classic exchange hot-wallet shape.
    if senders >= _EXCHANGE_MIN_COUNTERPARTIES and incoming >= _EXCHANGE_MIN_TX:
        confidence = min(0.35 + 0.02 * senders, 0.75)
        return (
            "exchange",
            round(confidence, 2),
            f"{senders} distinct senders across {incoming} incoming transfers - "
            "consistent with a deposit-taking service",
        )

    if diversity >= _EXCHANGE_MIN_COUNTERPARTIES:
        return (
            "payment_processor",
            0.3,
            f"counterparty diversity {diversity} without a strong deposit skew",
        )

    if receivers <= 2 and senders <= 2:
        return (
            "unknown",
            0.15,
            "low counterparty diversity - looks like a pass-through or personal wallet",
        )

    return "unknown", 0.1, "behaviour does not match a known service profile"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def attribute(chain: str, address_norm: str) -> AttributionResult:
    """Attribute the cluster containing `address_norm`."""
    tags = lookup_tags(chain, address_norm)

    if tags:
        best = tags[0]
        return AttributionResult(
            method=METHOD_TAGGED,
            entity_name=best["entity_name"],
            entity_type=best["entity_type"],
            confidence=float(best["confidence"]) if best["confidence"] is not None else 1.0,
            source=best["source"],
            matched_address=best["matched_address"],
            cluster_key=best["cluster_key"],
            cluster_size=best["cluster_size"] or 1,
            evidence=[
                {
                    "address": t["matched_address"],
                    "entity": t["entity_name"],
                    "entity_type": t["entity_type"],
                    "source": t["source"],
                    "confidence": t["confidence"],
                }
                for t in tags[:10]
            ],
            note=(
                f"Attributed from a curated tag ({best['source']}). "
                "Tag applies to the cluster because clustering asserted shared ownership."
            ),
        )

    features = behavioural_features(chain, address_norm)
    entity_type, confidence, reasoning = classify_category(features)

    if confidence <= 0.0:
        return AttributionResult(
            method=METHOD_NONE,
            cluster_key=features.get("cluster_key"),
            cluster_size=features.get("cluster_size", 0),
            features=features or None,
            note="No curated tag and insufficient behavioural data to suggest a category.",
        )

    return AttributionResult(
        method=METHOD_CLASSIFIER,
        entity_name=None,  # deliberately unnamed - see module docstring
        entity_type=entity_type,
        confidence=confidence,
        source="behavioural_classifier",
        cluster_key=features.get("cluster_key"),
        cluster_size=features.get("cluster_size", 1),
        features=features,
        note=(
            "No curated tag matched this cluster. Category predicted from behaviour: "
            f"{reasoning}. This is a suggestion, not an identification - the service "
            "is not named because behaviour alone cannot name it."
        ),
    )
