"""Service exposure: which exchange, mixer or VASP did this money actually reach?

This is the question an investigator asks first, and it has two very different
answers depending on distance.

**Direct exposure** - the reported wallet sent straight to a labelled service.
That is a one-hop, indexed lookup and it is nearly free, so it runs first and
short-circuits. It also carries the strongest evidence: a single transaction
hash, amount, asset and timestamp that go straight into a case file.

**Indirect exposure** - no direct hit, so the funds went through intermediaries.
Here "which service" stops being a lookup and becomes a ranking problem: several
services may be reachable, and hop count alone is a poor way to choose between
them. A dust transfer two hops away is less interesting than eighteen lakh
rupees three hops away. Stage 4 extracts the features that separate them; this
module owns the traversal and the direct case.

Design notes:

  * Mixers and darknet services are first-class candidates here. An earlier
    revision filtered them out of attribution entirely, which meant a flow into
    a tumbler could be reported as reaching an exchange with no mention of the
    mixer in between.
  * Value totals use `value_attributed`, never `value`. On a UTXO chain the raw
    output amount repeats across every input edge, so summing it overstates
    volume by the input count.
  * Failed transactions never created transfer edges (see graph_writer), so they
    cannot produce an exposure.
"""

from __future__ import annotations

import logging
import math
import re
import statistics
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from app.db.neo4j import get_driver
from app.services import pricing

logger = logging.getLogger(__name__)

#: Entity types that count as a "service" for exposure purposes. Deliberately
#: broader than the old attribution filter: reaching a mixer *is* an exposure,
#: and arguably a more urgent one than reaching an exchange.
SERVICE_TYPES = [
    "exchange",
    "mixer",
    "sanctioned",
    "payment_processor",
    "gambling",
    "darknet",
]

KIND_DIRECT = "direct"
KIND_INDIRECT = "indirect"
KIND_NONE = "none"


@dataclass
class Evidence:
    """What an investigator writes into the case file."""

    txid: str
    amount: float
    amount_attributed: float
    asset: str
    token_contract: str | None
    transfer_type: str
    timestamp: str | None
    from_address: str
    to_address: str


@dataclass
class LabelProvenance:
    """Where the service name came from, and how much to trust it."""

    source: str
    confidence: float


@dataclass
class ServiceExposure:
    """One service the traced funds reached."""

    service: str
    service_type: str
    hop: int
    address: str
    label: LabelProvenance
    evidence: Evidence | None = None
    path: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Direct exposure - one hop, with full evidence
# ---------------------------------------------------------------------------
_DIRECT = """
MATCH (root:Address {chain: $chain, address_norm: $address_norm})
      -[tr:TRANSFERRED]->(svc:Address)
MATCH (svc)-[tag:TAGGED_AS]->(e:Entity)
WHERE e.entity_type IN $service_types
RETURN e.name              AS service,
       e.entity_type       AS service_type,
       svc.address_norm    AS address,
       tag.source          AS label_source,
       tag.confidence      AS label_confidence,
       tr.txid             AS txid,
       tr.value            AS amount,
       tr.value_attributed AS amount_attributed,
       tr.asset            AS asset,
       tr.token_contract   AS token_contract,
       tr.transfer_type    AS transfer_type,
       toString(tr.timestamp) AS timestamp
ORDER BY tag.confidence DESC, tr.timestamp DESC
"""


def detect_direct_exposure(chain: str, address_norm: str) -> list[ServiceExposure]:
    """Services this address paid directly, newest and best-labelled first.

    One hop from an indexed node, so this is cheap enough to run on every
    request before considering any traversal.
    """
    with get_driver().session() as session:
        rows = [
            dict(r)
            for r in session.run(
                _DIRECT,
                chain=chain,
                address_norm=address_norm,
                service_types=SERVICE_TYPES,
            )
        ]

    exposures: list[ServiceExposure] = []
    for row in rows:
        exposures.append(
            ServiceExposure(
                service=row["service"],
                service_type=row["service_type"],
                hop=1,
                address=row["address"],
                label=LabelProvenance(
                    source=row["label_source"] or "unknown",
                    confidence=float(row["label_confidence"] or 0.0),
                ),
                evidence=Evidence(
                    txid=row["txid"],
                    amount=float(row["amount"] or 0.0),
                    amount_attributed=float(row["amount_attributed"] or 0.0),
                    asset=row["asset"] or "",
                    token_contract=row["token_contract"],
                    transfer_type=row["transfer_type"] or "native",
                    timestamp=row["timestamp"],
                    from_address=address_norm,
                    to_address=row["address"],
                ),
                path=[address_norm, row["address"]],
            )
        )
    return exposures


# ---------------------------------------------------------------------------
# Indirect exposure - k-hop traversal to labelled services
# ---------------------------------------------------------------------------
# Every transfer along the shortest path is returned so Stage 4 can derive
# timing, volume and continuity without a second round trip. `shortestPath`
# bounds the work: without it a dense subgraph enumerates every permutation.
_INDIRECT = """
MATCH (root:Address {chain: $chain, address_norm: $address_norm})
CALL (root) {
  MATCH p = shortestPath((root)-[:TRANSFERRED*1..%(depth)d]->(svc:Address))
  WHERE svc <> root
  RETURN p, svc
  LIMIT $max_paths
}
WITH root, p, svc
MATCH (svc)-[tag:TAGGED_AS]->(e:Entity)
WHERE e.entity_type IN $service_types
RETURN e.name           AS service,
       e.entity_type    AS service_type,
       svc.address_norm AS address,
       tag.source       AS label_source,
       tag.confidence   AS label_confidence,
       length(p)        AS hop,
       [n IN nodes(p) | n.address_norm] AS path,
       [r IN relationships(p) | {
          txid: r.txid,
          value: r.value,
          value_attributed: r.value_attributed,
          asset: r.asset,
          token_contract: r.token_contract,
          transfer_type: r.transfer_type,
          timestamp: toString(r.timestamp)
       }] AS transfers
ORDER BY hop ASC
"""


# Every transfer landing on a service address from anywhere inside the traced
# subgraph.
#
# `shortestPath` above deliberately returns ONE route per service, which is
# right for "how far away is it" but wrong for "how much arrived". Three
# separate payments into an exchange are three arrivals; the shortest path sees
# only one of them, so volume and frequency would both read 1. Arrivals are
# therefore collected separately, restricted to sources reachable from the
# reported address so unrelated traffic into a busy exchange is excluded.
_ARRIVALS = """
MATCH (root:Address {chain: $chain, address_norm: $address_norm})
MATCH (root)-[:TRANSFERRED*0..%(depth)d]->(n:Address)
WITH root, collect(DISTINCT n) + root AS reachable
UNWIND reachable AS src
MATCH (src)-[tr:TRANSFERRED]->(svc:Address)
WHERE svc.address_norm IN $service_addresses AND src IN reachable
RETURN DISTINCT
       svc.address_norm    AS service_address,
       src.address_norm    AS from_address,
       tr.txid             AS txid,
       tr.value            AS value,
       tr.value_attributed AS value_attributed,
       tr.asset            AS asset,
       tr.token_contract   AS token_contract,
       tr.transfer_type    AS transfer_type,
       toString(tr.timestamp) AS timestamp
"""


def find_service_paths(
    chain: str, address_norm: str, max_hops: int = 5, max_paths: int = 400
) -> list[dict]:
    """Raw paths to labelled services, each with every transfer that arrived.

    Two queries on purpose: one for reachability and hop distance, one for the
    arriving transfers. Grouping and scoring is Stage 4/5's job - this stays
    pure traversal so it can be tested on its own.
    """
    depth = max(1, min(max_hops, 8))
    with get_driver().session() as session:
        rows = [
            dict(r)
            for r in session.run(
                _INDIRECT % {"depth": depth},
                chain=chain,
                address_norm=address_norm,
                service_types=SERVICE_TYPES,
                max_paths=max_paths,
            )
        ]
        if not rows:
            return []

        service_addresses = sorted({r["address"] for r in rows})
        arrivals = [
            dict(r)
            for r in session.run(
                _ARRIVALS % {"depth": depth},
                chain=chain,
                address_norm=address_norm,
                service_addresses=service_addresses,
            )
        ]

    by_address: dict[str, list[dict]] = {}
    for arrival in arrivals:
        by_address.setdefault(arrival["service_address"], []).append(arrival)

    for row in rows:
        row["arrivals"] = by_address.get(row["address"], [])
        # Carried so feature extraction can price assets without being handed
        # the chain separately.
        row["chain"] = chain
    return rows


# ---------------------------------------------------------------------------
# Stage 4: path features
# ---------------------------------------------------------------------------
# Neo4j renders a datetime with up to 9 fractional digits; datetime.fromisoformat
# accepts only 3 or 6, so the tail is trimmed rather than letting the parse fail
# silently and lose every timing feature.
_FRACTION = re.compile(r"\.(\d{1,9})")


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    match = _FRACTION.search(text)
    if match and len(match.group(1)) not in (3, 6):
        text = text[: match.start()] + "." + match.group(1)[:6] + text[match.end() :]
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        logger.debug("unparseable timestamp from graph: %r", value)
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _chain_of(rows: list[dict]) -> str:
    """Chain for these paths. Traversal is chain-scoped, so any row will do."""
    for row in rows:
        if row.get("chain"):
            return row["chain"]
    return ""


def _asset_key_of(arrival: dict, chain: str) -> str:
    """Rebuild the Asset.key that pricing expects from a stored edge.

    Edges carry the display symbol plus the contract; the key is derived rather
    than stored twice, so the two can never disagree.
    """
    contract = arrival.get("token_contract")
    if not contract:
        return f"{chain}:native"
    normalised = contract.lower() if chain == "ETH" else contract
    return f"{chain}:{normalised}"


@dataclass
class PathFeatures:
    """Everything known about how funds reached one service.

    Deliberately raw: no normalisation and no weighting happens here, so the
    numbers can be shown to an investigator as measurements and the scoring
    layer can be replaced without touching extraction.
    """

    service: str
    service_type: str
    hop: int                        # shortest hop distance to this service
    path_count: int                 # distinct routes found
    shortest_path: list[str]

    total_volume: float             # attributed value landing on the service
    max_transfer: float             # largest single arriving transfer
    transfer_count: int             # arriving transfers
    unique_counterparties: int      # distinct addresses paying the service

    first_seen: str | None
    last_seen: str | None
    seconds_since_last: float | None
    median_inter_hop_seconds: float | None
    continuity_ok: bool             # do path timestamps move forward in time?

    label_confidence: float
    label_sources: list[str]

    asset: str                      # unit total_volume is denominated in
    mixed_assets: bool              # more than one asset arrived

    # Fiat valuation. `None` means the asset could not be priced, in which case
    # scoring falls back to relative normalisation rather than treating the
    # exposure as worthless.
    total_volume_inr: float | None = None
    max_transfer_inr: float | None = None
    price_sources: list[str] = field(default_factory=list)

    @property
    def priced(self) -> bool:
        return self.total_volume_inr is not None

    def as_dict(self) -> dict:
        payload = asdict(self)
        payload["priced"] = self.priced
        return payload


def _terminal_transfers(rows: list[dict]) -> dict[tuple, dict]:
    """Distinct transfers that land ON the service address.

    "Volume reaching the service" means exactly this - what arrived. Summing
    every transfer along every path would count the same money once per hop it
    travelled, and converging routes would count it again, so this keys on
    (txid, from, to) and deduplicates.

    Arrivals come from the dedicated query rather than the shortest path, which
    only ever exposes one of them. The final-hop fallback keeps the function
    usable for callers that supply paths without arrivals.
    """
    terminal: dict[tuple, dict] = {}

    for row in rows:
        for arrival in row.get("arrivals") or []:
            key = (arrival.get("txid"), arrival["from_address"], arrival["service_address"])
            terminal.setdefault(
                key, {**arrival, "to_address": arrival["service_address"]}
            )

    if terminal:
        return terminal

    for row in rows:
        transfers = row.get("transfers") or []
        path = row.get("path") or []
        if not transfers or len(path) < 2:
            continue
        last = transfers[-1]
        key = (last.get("txid"), path[-2], path[-1])
        terminal.setdefault(key, {**last, "from_address": path[-2], "to_address": path[-1]})
    return terminal


def extract_path_features(rows: list[dict]) -> PathFeatures:
    """Turn the raw paths for ONE service into comparable measurements."""
    if not rows:
        raise ValueError("extract_path_features needs at least one path")

    rows = sorted(rows, key=lambda r: r["hop"])
    first = rows[0]

    terminal = _terminal_transfers(rows)
    arrivals = list(terminal.values())

    volumes = [float(t.get("value_attributed") or 0.0) for t in arrivals]
    total_volume = sum(volumes)
    max_transfer = max(volumes, default=0.0)

    assets = {t.get("asset") or "" for t in arrivals}
    counterparties = {t["from_address"] for t in arrivals}

    # --- fiat valuation --------------------------------------------------
    # Priced per arrival at the time it happened, so a two-year-old transfer is
    # not revalued at today's rate. A single unpriceable arrival makes the whole
    # total unusable - a partial sum would understate the exposure and look
    # authoritative doing it.
    chain = _chain_of(rows)
    inr_values: list[float] = []
    price_sources: set[str] = set()
    priced_all = bool(arrivals)
    for arrival in arrivals:
        asset_key = _asset_key_of(arrival, chain)
        value, quote = pricing.to_inr(
            float(arrival.get("value_attributed") or 0.0),
            asset_key,
            chain,
            at=_parse_ts(arrival.get("timestamp")),
        )
        if value is None or quote is None:
            priced_all = False
            break
        inr_values.append(value)
        price_sources.add(quote.source)

    arrival_times = sorted(t for t in (_parse_ts(a.get("timestamp")) for a in arrivals) if t)
    first_seen = arrival_times[0] if arrival_times else None
    last_seen = arrival_times[-1] if arrival_times else None
    seconds_since_last = (
        (datetime.now(UTC) - last_seen).total_seconds() if last_seen else None
    )

    # Inter-hop delay and continuity, measured per route then pooled.
    deltas: list[float] = []
    continuity_ok = True
    for row in rows:
        stamps = [_parse_ts(t.get("timestamp")) for t in (row.get("transfers") or [])]
        stamps = [s for s in stamps if s]
        for earlier, later in zip(stamps, stamps[1:], strict=False):
            gap = (later - earlier).total_seconds()
            deltas.append(abs(gap))
            # Funds cannot arrive before they left. A route that goes backwards
            # in time is an artefact of graph shape, not a real money flow.
            if gap < 0:
                continuity_ok = False

    return PathFeatures(
        service=first["service"],
        service_type=first["service_type"],
        hop=int(first["hop"]),
        path_count=len(rows),
        shortest_path=list(first.get("path") or []),
        total_volume=total_volume,
        max_transfer=max_transfer,
        transfer_count=len(arrivals),
        unique_counterparties=len(counterparties),
        first_seen=first_seen.isoformat() if first_seen else None,
        last_seen=last_seen.isoformat() if last_seen else None,
        seconds_since_last=seconds_since_last,
        median_inter_hop_seconds=statistics.median(deltas) if deltas else None,
        continuity_ok=continuity_ok,
        label_confidence=max(float(r.get("label_confidence") or 0.0) for r in rows),
        label_sources=sorted({r["label_source"] for r in rows if r.get("label_source")}),
        asset=(sorted(assets)[0] if assets else ""),
        mixed_assets=len({a for a in assets if a}) > 1,
        total_volume_inr=sum(inr_values) if priced_all else None,
        max_transfer_inr=max(inr_values, default=0.0) if priced_all else None,
        price_sources=sorted(price_sources) if priced_all else [],
    )


def group_by_service(rows: list[dict]) -> dict[str, list[dict]]:
    """Group raw paths by the service they terminate at."""
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["service"], []).append(row)
    return grouped


# ---------------------------------------------------------------------------
# Stage 5: scoring
# ---------------------------------------------------------------------------
SCORING_VERSION = "exposure-v1"

#: Weights are a **defensible prior, not a fitted result**. They encode the
#: judgement that a large, recent, repeated flow into a well-labelled service
#: matters more than proximity alone - which is the whole reason hop count is
#: not the ranking. Retune them here; the version string above must change with
#: them so a stored score can always be traced to the weights that produced it.
#:
#: To fit them properly: have investigators rank pairs of real candidates, then
#: solve for weights reproducing those rankings. Until that data exists, a
#: learned ranker would only be an opaque re-encoding of these same guesses.
WEIGHTS = {
    "hop": 0.22,
    "volume": 0.24,
    "recency": 0.16,
    "frequency": 0.12,
    "continuity": 0.10,
    "label": 0.16,
}

#: How much trust each labelling source earns. OFAC is a legal designation;
#: a community tagpack is a best effort.
SOURCE_TRUST = {
    "ofac_sdn": 1.0,
    "graphsense_ofac": 0.95,
    "etherscan_labels": 0.9,
    "walletexplorer": 0.85,
    "graphsense_tagpacks": 0.8,
    "synthetic": 1.0,          # demo data is definitionally correct
    "behavioural_classifier": 0.4,
}
DEFAULT_SOURCE_TRUST = 0.6

HOP_HALF_LIFE = 2.0        # hops at which proximity weight halves
RECENCY_HALF_LIFE_DAYS = 7.0
FREQUENCY_SATURATION = 10  # transfers beyond which more adds nothing
CONTINUITY_PENALTY = 0.4   # score for a route that runs backwards in time

#: Absolute volume scale, in rupees. Below the dust floor an exposure scores
#: zero on volume: a few hundred rupees reaching an exchange is not evidence of
#: laundering, and letting it score on proximity alone is exactly the
#: false positive this guards against. Above the floor the curve is logarithmic,
#: because the step from 1 lakh to 10 lakh matters more than 90 lakh to 1 crore.
VOLUME_DUST_INR = 1_000.0
VOLUME_SATURATION_INR = 10_000_000.0   # 1 crore


@dataclass
class ScoredExposure:
    """A ranked candidate, carrying the arithmetic that produced its rank."""

    features: dict
    score: float
    rank: int
    explanation: list[dict]
    scoring_version: str = SCORING_VERSION

    def as_dict(self) -> dict:
        return asdict(self)


def _source_trust(sources: list[str]) -> float:
    if not sources:
        return DEFAULT_SOURCE_TRUST
    return max(SOURCE_TRUST.get(s, DEFAULT_SOURCE_TRUST) for s in sources)


def score_candidates(candidates: list[PathFeatures]) -> list[ScoredExposure]:
    """Rank services by weighted, normalised path features.

    Every feature is mapped to 0-1 before weighting, or units dominate: raw
    volume would swamp a hop count purely by magnitude.

    **Volume is scored in rupees on an absolute scale** when the assets can be
    priced, so a score means the same thing in every investigation and a dust
    transfer scores zero on volume however close it sits. Where an asset cannot
    be priced the candidate falls back to normalisation relative to the other
    candidates - still correct for ordering within this analysis, but flagged
    via `volume_basis` so nobody reads it as an absolute figure.
    """
    if not candidates:
        return []

    # Only used for the unpriced fallback.
    max_volume = max((c.total_volume for c in candidates), default=0.0)

    scored: list[ScoredExposure] = []
    for feature in candidates:
        # --- normalise -----------------------------------------------------
        hop_n = 0.5 ** ((max(feature.hop, 1) - 1) / HOP_HALF_LIFE)

        if feature.priced:
            inr = feature.total_volume_inr or 0.0
            if inr < VOLUME_DUST_INR:
                volume_n = 0.0
            else:
                volume_n = min(
                    math.log1p(inr / VOLUME_DUST_INR)
                    / math.log1p(VOLUME_SATURATION_INR / VOLUME_DUST_INR),
                    1.0,
                )
            volume_raw: float | None = inr
            volume_basis = "INR"
        else:
            volume_n = (feature.total_volume / max_volume) if max_volume > 0 else 0.0
            volume_raw = feature.total_volume
            volume_basis = f"{feature.asset or 'native'} (relative - asset not priceable)"

        if feature.seconds_since_last is None:
            recency_n = 0.0
        else:
            days = max(feature.seconds_since_last, 0.0) / 86400.0
            recency_n = 0.5 ** (days / RECENCY_HALF_LIFE_DAYS)

        frequency_n = min(feature.transfer_count / FREQUENCY_SATURATION, 1.0)
        continuity_n = 1.0 if feature.continuity_ok else CONTINUITY_PENALTY
        label_n = feature.label_confidence * _source_trust(feature.label_sources)

        parts = {
            "hop": hop_n,
            "volume": volume_n,
            "recency": recency_n,
            "frequency": frequency_n,
            "continuity": continuity_n,
            "label": label_n,
        }
        raw_values = {
            "hop": feature.hop,
            "volume": volume_raw,
            "recency": feature.seconds_since_last,
            "frequency": feature.transfer_count,
            "continuity": feature.continuity_ok,
            "label": feature.label_confidence,
        }

        score = sum(WEIGHTS[k] * parts[k] for k in WEIGHTS)

        explanation = [
            {
                "feature": k,
                "raw": raw_values[k],
                "normalised": round(parts[k], 4),
                "weight": WEIGHTS[k],
                "contribution": round(WEIGHTS[k] * parts[k], 4),
            }
            for k in sorted(WEIGHTS, key=lambda k: WEIGHTS[k] * parts[k], reverse=True)
        ]

        payload = feature.as_dict()
        payload["volume_basis"] = volume_basis
        scored.append(
            ScoredExposure(
                features=payload,
                score=round(score, 4),
                rank=0,
                explanation=explanation,
            )
        )

    # Deterministic order: score desc, then fewer hops, then name - so two runs
    # over identical data never disagree.
    scored.sort(key=lambda s: (-s.score, s.features["hop"], s.features["service"]))
    for i, item in enumerate(scored, start=1):
        item.rank = i
    return scored


def explain_ranking(scored: list[ScoredExposure]) -> str:
    """One sentence an investigator can read, naming what actually decided it."""
    if not scored:
        return "No service exposure found within the searched depth."
    top = scored[0]
    name = top.features["service"]
    if len(scored) == 1:
        return f"{name} is the only service reached within the searched depth."

    runner = scored[1]
    lead = max(
        top.explanation,
        key=lambda e: e["contribution"]
        - next(
            (x["contribution"] for x in runner.explanation if x["feature"] == e["feature"]),
            0.0,
        ),
    )
    return (
        f"{name} ranks above {runner.features['service']} mainly on "
        f"{lead['feature']} (contributing {lead['contribution']} of "
        f"{top.score}); {name} is at hop {top.features['hop']} versus "
        f"{runner.features['hop']}."
    )


def analyse_exposure(chain: str, address_norm: str, max_hops: int = 5) -> dict:
    """Direct exposure if it exists, otherwise ranked multi-hop candidates.

    Direct exposure short-circuits: it is a one-hop indexed lookup, it carries
    the strongest possible evidence, and there is nothing to rank when the
    suspect paid the service itself.
    """
    direct = detect_direct_exposure(chain, address_norm)
    if direct:
        return {
            "kind": KIND_DIRECT,
            "chain": chain,
            "address": address_norm,
            "top": direct[0].as_dict(),
            "candidates": [d.as_dict() for d in direct],
            "searched_to_hop": 1,
            "scoring_version": SCORING_VERSION,
            "explanation": (
                f"Direct exposure: {direct[0].service} received funds from this "
                f"address in one hop. No ranking is required."
            ),
        }

    paths = find_service_paths(chain, address_norm, max_hops=max_hops)
    if not paths:
        return {
            "kind": KIND_NONE,
            "chain": chain,
            "address": address_norm,
            "top": None,
            "candidates": [],
            "searched_to_hop": max_hops,
            "scoring_version": SCORING_VERSION,
            "explanation": "No service exposure found within the searched depth.",
        }

    features = [extract_path_features(rows) for rows in group_by_service(paths).values()]
    scored = score_candidates(features)

    return {
        "kind": KIND_INDIRECT,
        "chain": chain,
        "address": address_norm,
        "top": scored[0].as_dict() if scored else None,
        "candidates": [s.as_dict() for s in scored],
        "searched_to_hop": max_hops,
        "scoring_version": SCORING_VERSION,
        "weights": WEIGHTS,
        "explanation": explain_ranking(scored),
    }
