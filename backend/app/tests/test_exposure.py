"""Service exposure detection.

These cover the capability the whole feature exists for: given a suspect wallet,
did the money reach a known service, and can we prove it with a transaction an
investigator could look up independently.
"""

from __future__ import annotations

import pytest

from app.services import exposure
from app.tests.conftest import tx

SUSPECT = "suspectA"
MULE1 = "muleB"
MULE2 = "muleC"
EXCHANGE = "exchangeHot"
MIXER = "mixerPool"
FUNDER = "funderX"


def tag_service(address: str, name: str, entity_type: str, source: str, confidence: float):
    """Attach a service label the way seed_tags.py does."""
    from app.db.neo4j import get_driver

    with get_driver().session() as session:
        session.run(
            """
            MERGE (e:Entity {entity_id: $eid})
              SET e.name = $name, e.entity_type = $etype
            WITH e
            MATCH (a:Address {chain: 'BTC', address_norm: $addr})
            MERGE (a)-[t:TAGGED_AS {source: $source}]->(e)
              SET t.confidence = $confidence
            """,
            eid=f"test-{name.lower().replace(' ', '-')}",
            name=name,
            etype=entity_type,
            addr=address,
            source=source,
            confidence=confidence,
        )


# ---------------------------------------------------------------------------
# Direct exposure
# ---------------------------------------------------------------------------
def test_direct_exposure_is_detected_with_evidence(graph):
    """Wallet -> Exchange in one hop returns the transaction that proves it."""
    graph.write_transactions(
        [
            tx("fund", [(FUNDER, 5.0)], [(SUSPECT, 5.0)], minutes=0),
            tx("cashout", [(SUSPECT, 5.0)], [(EXCHANGE, 4.9)], minutes=30),
        ]
    )
    tag_service(EXCHANGE, "Test Exchange", "exchange", "walletexplorer", 0.9)

    result = exposure.analyse_exposure("BTC", SUSPECT, max_hops=5)

    assert result["kind"] == exposure.KIND_DIRECT
    top = result["top"]
    assert top["service"] == "Test Exchange"
    assert top["service_type"] == "exchange"
    assert top["hop"] == 1

    # The evidence is the point - it must be independently checkable.
    ev = top["evidence"]
    assert ev["txid"] == "cashout"
    assert ev["amount"] == pytest.approx(4.9)
    assert ev["from_address"] == SUSPECT
    assert ev["to_address"] == EXCHANGE
    assert ev["timestamp"]
    assert top["label"]["source"] == "walletexplorer"
    assert top["label"]["confidence"] == pytest.approx(0.9)


def test_direct_exposure_short_circuits_before_traversal(graph):
    """A direct hit must not be reported at some deeper hop instead."""
    graph.write_transactions(
        [
            tx("fund", [(FUNDER, 9.0)], [(SUSPECT, 9.0)], minutes=0),
            tx("direct", [(SUSPECT, 4.0)], [(EXCHANGE, 4.0)], minutes=10),
            # A longer route to the same service also exists.
            tx("hop1", [(SUSPECT, 5.0)], [(MULE1, 5.0)], minutes=20),
            tx("hop2", [(MULE1, 5.0)], [(EXCHANGE, 4.9)], minutes=30),
        ]
    )
    tag_service(EXCHANGE, "Test Exchange", "exchange", "walletexplorer", 0.9)

    result = exposure.analyse_exposure("BTC", SUSPECT, max_hops=5)
    assert result["kind"] == exposure.KIND_DIRECT
    assert result["top"]["hop"] == 1
    assert result["searched_to_hop"] == 1, "traversal should not have been needed"


def test_unlabelled_counterparty_is_not_a_direct_exposure(graph):
    """Paying an unknown wallet is not exposure to a service."""
    graph.write_transactions(
        [
            tx("fund", [(FUNDER, 5.0)], [(SUSPECT, 5.0)], minutes=0),
            tx("send", [(SUSPECT, 5.0)], [(MULE1, 4.9)], minutes=10),
        ]
    )
    assert exposure.detect_direct_exposure("BTC", SUSPECT) == []


def test_mixer_is_a_first_class_exposure(graph):
    """Reaching a tumbler is an exposure, and arguably a more urgent one.

    An earlier revision filtered mixers out of attribution entirely, so a flow
    into a mixer could be reported as reaching an exchange with no mention of
    the mixer in between.
    """
    graph.write_transactions(
        [
            tx("fund", [(FUNDER, 5.0)], [(SUSPECT, 5.0)], minutes=0),
            tx("tomixer", [(SUSPECT, 5.0)], [(MIXER, 4.9)], minutes=10),
        ]
    )
    tag_service(MIXER, "Test Mixer", "mixer", "graphsense_tagpacks", 0.85)

    result = exposure.analyse_exposure("BTC", SUSPECT, max_hops=5)
    assert result["kind"] == exposure.KIND_DIRECT
    assert result["top"]["service_type"] == "mixer"


def test_failed_transaction_creates_no_exposure(graph):
    """A reverted transaction moved nothing, so it is not exposure."""
    graph.write_transactions(
        [
            tx("fund", [(FUNDER, 5.0)], [(SUSPECT, 5.0)], minutes=0),
            tx("reverted", [(SUSPECT, 5.0)], [(EXCHANGE, 5.0)], minutes=10, status="failed"),
        ]
    )
    tag_service(EXCHANGE, "Test Exchange", "exchange", "walletexplorer", 0.9)
    assert exposure.detect_direct_exposure("BTC", SUSPECT) == []


# ---------------------------------------------------------------------------
# Indirect exposure
# ---------------------------------------------------------------------------
def test_multi_hop_exposure_returns_the_full_path(graph):
    """Three hops to an exchange, with every transfer along the way."""
    graph.write_transactions(
        [
            tx("fund", [(FUNDER, 5.0)], [(SUSPECT, 5.0)], minutes=0),
            tx("h1", [(SUSPECT, 5.0)], [(MULE1, 4.9)], minutes=10),
            tx("h2", [(MULE1, 4.9)], [(MULE2, 4.8)], minutes=20),
            tx("h3", [(MULE2, 4.8)], [(EXCHANGE, 4.7)], minutes=30),
        ]
    )
    tag_service(EXCHANGE, "Test Exchange", "exchange", "etherscan_labels", 0.95)

    result = exposure.analyse_exposure("BTC", SUSPECT, max_hops=5)
    assert result["kind"] == exposure.KIND_INDIRECT

    features = result["candidates"][0]["features"]
    assert features["service"] == "Test Exchange"
    assert features["hop"] == 3
    assert features["shortest_path"] == [SUSPECT, MULE1, MULE2, EXCHANGE]

    # The traversal itself must expose every transfer along the route, so
    # timing features can be derived without a second round trip.
    paths = exposure.find_service_paths("BTC", SUSPECT, max_hops=5)
    assert len(paths[0]["transfers"]) == 3
    for transfer in paths[0]["transfers"]:
        assert transfer["txid"]
        assert transfer["timestamp"]
        assert transfer["value_attributed"] is not None


def test_no_reachable_service_is_reported_honestly(graph):
    """Finding nothing is a legitimate answer, not an error."""
    graph.write_transactions(
        [
            tx("fund", [(FUNDER, 5.0)], [(SUSPECT, 5.0)], minutes=0),
            tx("h1", [(SUSPECT, 5.0)], [(MULE1, 4.9)], minutes=10),
        ]
    )
    result = exposure.analyse_exposure("BTC", SUSPECT, max_hops=5)
    assert result["kind"] == exposure.KIND_NONE
    assert result["top"] is None
    assert result["candidates"] == []


def test_competing_services_are_all_returned(graph):
    """Two exchanges reachable: both must be candidates, not just the nearest."""
    graph.write_transactions(
        [
            tx("fund", [(FUNDER, 10.0)], [(SUSPECT, 10.0)], minutes=0),
            tx("a1", [(SUSPECT, 5.0)], [(MULE1, 5.0)], minutes=10),
            tx("a2", [(MULE1, 5.0)], [(EXCHANGE, 4.9)], minutes=20),
            tx("b1", [(SUSPECT, 5.0)], [(MULE2, 5.0)], minutes=15),
            tx("b2", [(MULE2, 5.0)], [(MIXER, 4.9)], minutes=25),
        ]
    )
    tag_service(EXCHANGE, "Exchange One", "exchange", "walletexplorer", 0.9)
    tag_service(MIXER, "Mixer Two", "mixer", "graphsense_tagpacks", 0.8)

    result = exposure.analyse_exposure("BTC", SUSPECT, max_hops=5)
    services = {c["features"]["service"] for c in result["candidates"]}
    assert services == {"Exchange One", "Mixer Two"}


def test_max_hops_bounds_the_search(graph):
    """A service beyond the requested depth must not be reported."""
    graph.write_transactions(
        [
            tx("fund", [(FUNDER, 5.0)], [(SUSPECT, 5.0)], minutes=0),
            tx("h1", [(SUSPECT, 5.0)], [(MULE1, 4.9)], minutes=10),
            tx("h2", [(MULE1, 4.9)], [(MULE2, 4.8)], minutes=20),
            tx("h3", [(MULE2, 4.8)], [(EXCHANGE, 4.7)], minutes=30),
        ]
    )
    tag_service(EXCHANGE, "Test Exchange", "exchange", "walletexplorer", 0.9)

    assert exposure.analyse_exposure("BTC", SUSPECT, max_hops=2)["kind"] == exposure.KIND_NONE
    assert exposure.analyse_exposure("BTC", SUSPECT, max_hops=3)["kind"] == exposure.KIND_INDIRECT


def test_circular_path_terminates(graph):
    """Funds cycling between wallets must not hang the traversal."""
    graph.write_transactions(
        [
            tx("fund", [(FUNDER, 5.0)], [(SUSPECT, 5.0)], minutes=0),
            tx("h1", [(SUSPECT, 5.0)], [(MULE1, 4.9)], minutes=10),
            tx("h2", [(MULE1, 4.9)], [(MULE2, 4.8)], minutes=20),
            tx("loop", [(MULE2, 4.8)], [(SUSPECT, 4.7)], minutes=30),
            tx("out", [(MULE2, 4.8)], [(EXCHANGE, 4.6)], minutes=40),
        ]
    )
    tag_service(EXCHANGE, "Test Exchange", "exchange", "walletexplorer", 0.9)

    result = exposure.analyse_exposure("BTC", SUSPECT, max_hops=5)
    assert result["kind"] == exposure.KIND_INDIRECT
    assert result["candidates"][0]["features"]["hop"] == 3


def test_service_types_include_every_category_that_matters():
    """Guard against a filter silently dropping a whole class of service."""
    for required in ("exchange", "mixer", "sanctioned", "darknet"):
        assert required in exposure.SERVICE_TYPES


# ---------------------------------------------------------------------------
# Path features
# ---------------------------------------------------------------------------
def _features_for(graph, transfers, tags):
    graph.write_transactions(transfers)
    for addr, name, etype, source, conf in tags:
        tag_service(addr, name, etype, source, conf)
    paths = exposure.find_service_paths("BTC", SUSPECT, max_hops=6)
    grouped = exposure.group_by_service(paths)
    return {k: exposure.extract_path_features(v) for k, v in grouped.items()}


def test_volume_counts_only_what_arrives_at_the_service(graph):
    """Volume must be the arriving transfer, not every hop summed.

    Summing all transfers along a 3-hop path would count the same money three
    times, and converging routes would count it again.
    """
    feats = _features_for(
        graph,
        [
            tx("fund", [(FUNDER, 10.0)], [(SUSPECT, 10.0)], minutes=0),
            tx("h1", [(SUSPECT, 10.0)], [(MULE1, 9.0)], minutes=10),
            tx("h2", [(MULE1, 9.0)], [(MULE2, 8.0)], minutes=20),
            tx("h3", [(MULE2, 8.0)], [(EXCHANGE, 7.0)], minutes=30),
        ],
        [(EXCHANGE, "Test Exchange", "exchange", "walletexplorer", 0.9)],
    )
    f = feats["Test Exchange"]
    # Only the 7.0 that actually landed on the exchange.
    assert f.total_volume == pytest.approx(7.0)
    assert f.max_transfer == pytest.approx(7.0)
    assert f.transfer_count == 1
    assert f.hop == 3


def test_frequency_and_counterparties_are_counted(graph):
    """Several addresses paying the same service, several times."""
    feats = _features_for(
        graph,
        [
            tx("fund", [(FUNDER, 30.0)], [(SUSPECT, 30.0)], minutes=0),
            tx("s1", [(SUSPECT, 10.0)], [(MULE1, 10.0)], minutes=5),
            tx("s2", [(SUSPECT, 10.0)], [(MULE2, 10.0)], minutes=6),
            tx("m1", [(MULE1, 5.0)], [(EXCHANGE, 5.0)], minutes=20),
            tx("m2", [(MULE1, 4.0)], [(EXCHANGE, 4.0)], minutes=25),
            tx("m3", [(MULE2, 8.0)], [(EXCHANGE, 8.0)], minutes=30),
        ],
        [(EXCHANGE, "Test Exchange", "exchange", "walletexplorer", 0.9)],
    )
    f = feats["Test Exchange"]
    assert f.transfer_count == 3
    assert f.unique_counterparties == 2, "MULE1 and MULE2 both paid the service"
    assert f.total_volume == pytest.approx(17.0)
    assert f.max_transfer == pytest.approx(8.0)


def test_timing_features_are_derived(graph):
    """Inter-hop delay and recency come from the edge timestamps."""
    feats = _features_for(
        graph,
        [
            tx("fund", [(FUNDER, 5.0)], [(SUSPECT, 5.0)], minutes=0),
            tx("h1", [(SUSPECT, 5.0)], [(MULE1, 5.0)], minutes=10),
            tx("h2", [(MULE1, 5.0)], [(EXCHANGE, 5.0)], minutes=40),
        ],
        [(EXCHANGE, "Test Exchange", "exchange", "walletexplorer", 0.9)],
    )
    f = feats["Test Exchange"]
    assert f.last_seen is not None
    assert f.seconds_since_last is not None and f.seconds_since_last > 0
    # 30 minutes between the two hops.
    assert f.median_inter_hop_seconds == pytest.approx(1800, abs=1)
    assert f.continuity_ok is True


def test_backwards_in_time_path_fails_continuity(graph):
    """Funds cannot arrive before they left; such a route must be flagged."""
    feats = _features_for(
        graph,
        [
            tx("fund", [(FUNDER, 5.0)], [(SUSPECT, 5.0)], minutes=0),
            tx("h1", [(SUSPECT, 5.0)], [(MULE1, 5.0)], minutes=100),
            # Second hop dated BEFORE the first - impossible in reality.
            tx("h2", [(MULE1, 5.0)], [(EXCHANGE, 5.0)], minutes=20),
        ],
        [(EXCHANGE, "Test Exchange", "exchange", "walletexplorer", 0.9)],
    )
    assert feats["Test Exchange"].continuity_ok is False


def test_label_provenance_is_carried_into_features(graph):
    feats = _features_for(
        graph,
        [
            tx("fund", [(FUNDER, 5.0)], [(SUSPECT, 5.0)], minutes=0),
            tx("h1", [(SUSPECT, 5.0)], [(MULE1, 5.0)], minutes=10),
            tx("h2", [(MULE1, 5.0)], [(EXCHANGE, 5.0)], minutes=20),
        ],
        [(EXCHANGE, "Test Exchange", "exchange", "ofac_sdn", 1.0)],
    )
    f = feats["Test Exchange"]
    assert f.label_sources == ["ofac_sdn"]
    assert f.label_confidence == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def _synthetic_features(name, hop, volume, transfers, minutes_ago, confidence, source):
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    return exposure.PathFeatures(
        service=name,
        service_type="exchange",
        hop=hop,
        path_count=1,
        shortest_path=["a"] * (hop + 1),
        total_volume=volume,
        max_transfer=volume,
        transfer_count=transfers,
        unique_counterparties=transfers,
        first_seen=None,
        last_seen=(now - timedelta(minutes=minutes_ago)).isoformat(),
        seconds_since_last=minutes_ago * 60,
        median_inter_hop_seconds=250.0,
        continuity_ok=True,
        label_confidence=confidence,
        label_sources=[source],
        asset="INR",
        mixed_assets=False,
    )


def test_high_volume_recent_path_outranks_a_closer_trivial_one():
    """The thesis of the whole feature.

    A sits one hop further away but carries 18.4 lakh across five recent
    transfers with a high-confidence label. B is closer and otherwise
    negligible. Ranking on hop count alone - the previous behaviour - would
    return B, which is the wrong answer.
    """
    a = _synthetic_features("Candidate A", 3, 1_840_000, 5, 14, 0.95, "etherscan_labels")
    b = _synthetic_features("Candidate B", 2, 500, 1, 8 * 24 * 60, 0.60, "graphsense_tagpacks")

    scored = exposure.score_candidates([a, b])
    assert scored[0].features["service"] == "Candidate A"
    assert scored[0].score > scored[1].score

    # B must still win on the hop term specifically - the point is that the
    # other terms outweigh it, not that proximity stopped counting.
    hop_a = next(e for e in scored[0].explanation if e["feature"] == "hop")
    hop_b = next(e for e in scored[1].explanation if e["feature"] == "hop")
    assert hop_b["contribution"] > hop_a["contribution"]


def test_explanation_accounts_for_the_whole_score():
    """Contributions must sum to the score, or the explanation is decoration."""
    a = _synthetic_features("A", 2, 1000, 3, 30, 0.9, "walletexplorer")
    scored = exposure.score_candidates([a])[0]
    assert sum(e["contribution"] for e in scored.explanation) == pytest.approx(
        scored.score, abs=0.001
    )


def test_weights_sum_to_one():
    """Otherwise scores are not on a 0-1 scale and thresholds mean nothing."""
    assert sum(exposure.WEIGHTS.values()) == pytest.approx(1.0)


def test_ranking_is_deterministic():
    """Two runs over identical data must not disagree."""
    items = [
        _synthetic_features("Alpha", 3, 1000, 2, 60, 0.9, "walletexplorer"),
        _synthetic_features("Beta", 3, 1000, 2, 60, 0.9, "walletexplorer"),
    ]
    first = [s.features["service"] for s in exposure.score_candidates(list(items))]
    second = [s.features["service"] for s in exposure.score_candidates(list(reversed(items)))]
    assert first == second


def test_continuity_violation_is_penalised():
    good = _synthetic_features("Good", 3, 1000, 2, 60, 0.9, "walletexplorer")
    bad = _synthetic_features("Bad", 3, 1000, 2, 60, 0.9, "walletexplorer")
    bad.continuity_ok = False
    scored = {s.features["service"]: s.score for s in exposure.score_candidates([good, bad])}
    assert scored["Good"] > scored["Bad"]


def test_ofac_label_outweighs_a_community_tag_at_equal_confidence():
    """Source trust is part of label quality, not just the confidence number."""
    legal = _synthetic_features("Sanctioned", 3, 1000, 2, 60, 1.0, "ofac_sdn")
    community = _synthetic_features("Community", 3, 1000, 2, 60, 1.0, "graphsense_tagpacks")
    scored = {s.features["service"]: s.score for s in exposure.score_candidates([legal, community])}
    assert scored["Sanctioned"] > scored["Community"]


def test_dust_transfer_does_not_win_on_proximity_alone(graph):
    """False-positive guard: a token payment to an exchange next door must not
    outrank a substantial flow slightly further away."""
    feats = _features_for(
        graph,
        [
            tx("fund", [(FUNDER, 100.0)], [(SUSPECT, 100.0)], minutes=0),
            # Dust, one hop.
            tx("dust", [(SUSPECT, 0.0001)], [(MIXER, 0.0001)], minutes=5),
            # Substantial, two hops.
            tx("big1", [(SUSPECT, 90.0)], [(MULE1, 90.0)], minutes=10),
            tx("big2", [(MULE1, 90.0)], [(EXCHANGE, 89.0)], minutes=20),
        ],
        [
            (MIXER, "Dust Sink", "mixer", "graphsense_tagpacks", 0.8),
            (EXCHANGE, "Real Exchange", "exchange", "walletexplorer", 0.9),
        ],
    )
    scored = exposure.score_candidates(list(feats.values()))
    assert scored[0].features["service"] == "Real Exchange", (
        "89.0 at two hops must outrank 0.0001 at one"
    )


# ---------------------------------------------------------------------------
# Fiat valuation
# ---------------------------------------------------------------------------
def test_volume_is_valued_in_rupees(graph):
    """Absolute valuation is what makes a score mean the same thing twice.

    Native units cannot be compared across assets, so without this the volume
    term could only ever be relative to the other candidates in the same
    analysis - and 0.68 in one investigation would say nothing about 0.68 in
    another.
    """
    feats = _features_for(
        graph,
        [
            tx("fund", [(FUNDER, 1.0)], [(SUSPECT, 1.0)], minutes=0),
            tx("h1", [(SUSPECT, 1.0)], [(MULE1, 1.0)], minutes=10),
            tx("h2", [(MULE1, 1.0)], [(EXCHANGE, 1.0)], minutes=20),
        ],
        [(EXCHANGE, "Test Exchange", "exchange", "walletexplorer", 0.9)],
    )
    f = feats["Test Exchange"]
    assert f.priced is True
    # 1 BTC at the demo rate.
    assert f.total_volume_inr == pytest.approx(5_500_000.0, rel=0.01)
    assert f.price_sources == ["synthetic"]

    scored = exposure.score_candidates([f])[0]
    assert scored.features["volume_basis"] == "INR"
    volume_term = next(e for e in scored.explanation if e["feature"] == "volume")
    assert volume_term["raw"] == pytest.approx(5_500_000.0, rel=0.01)


def test_dust_exposure_scores_zero_on_volume():
    """A few hundred rupees reaching an exchange is not evidence of laundering.

    Letting it score on proximity alone is exactly the false positive the dust
    floor exists to prevent.
    """
    dust = _synthetic_features("Dust", 1, 0.0, 1, 5, 0.9, "walletexplorer")
    dust.total_volume_inr = 500.0        # below the floor
    substantial = _synthetic_features("Real", 4, 0.0, 1, 5, 0.9, "walletexplorer")
    substantial.total_volume_inr = 1_800_000.0

    scored = exposure.score_candidates([dust, substantial])
    assert scored[0].features["service"] == "Real", (
        "a large exposure four hops away must beat dust next door"
    )
    dust_volume = next(
        e for e in scored[1].explanation if e["feature"] == "volume"
    )
    assert dust_volume["contribution"] == 0.0


def test_score_is_comparable_across_investigations():
    """The same rupee volume must produce the same volume term either time.

    This is the property relative normalisation could not offer.
    """
    a = _synthetic_features("Solo", 3, 0.0, 2, 60, 0.9, "walletexplorer")
    a.total_volume_inr = 1_000_000.0

    b = _synthetic_features("WithRival", 3, 0.0, 2, 60, 0.9, "walletexplorer")
    b.total_volume_inr = 1_000_000.0
    rival = _synthetic_features("Rival", 2, 0.0, 9, 5, 0.95, "ofac_sdn")
    rival.total_volume_inr = 90_000_000.0

    alone = exposure.score_candidates([a])[0]
    contested = next(
        s for s in exposure.score_candidates([b, rival]) if s.features["service"] == "WithRival"
    )
    term_alone = next(e for e in alone.explanation if e["feature"] == "volume")
    term_contested = next(e for e in contested.explanation if e["feature"] == "volume")
    assert term_alone["contribution"] == pytest.approx(term_contested["contribution"])


def test_unpriceable_asset_falls_back_and_says_so():
    """A missing price must not be treated as zero value."""
    f = _synthetic_features("Unknown Token", 2, 1234.0, 3, 30, 0.9, "walletexplorer")
    f.total_volume_inr = None
    f.asset = "WEIRDTOKEN"

    scored = exposure.score_candidates([f])[0]
    assert "relative" in scored.features["volume_basis"]
    assert scored.features["priced"] is False
    # Still ranked, not discarded.
    assert scored.score > 0


def test_price_quote_carries_provenance():
    """A valuation without a source and an as-of time is not evidence."""
    from app.services import pricing

    quote = pricing.get_price("BTC:native", "BTC")
    assert quote is not None
    assert quote.source
    assert quote.as_of
    assert quote.is_estimate is True


def test_unmappable_asset_has_no_price():
    """Better to return nothing than to invent a rate."""
    from app.services import pricing

    assert pricing.get_price("ETH:0xnotarealtoken", "ETH") is None


def test_analyse_exposure_reports_its_scoring_version(graph):
    graph.write_transactions(
        [
            tx("fund", [(FUNDER, 5.0)], [(SUSPECT, 5.0)], minutes=0),
            tx("h1", [(SUSPECT, 5.0)], [(MULE1, 5.0)], minutes=10),
            tx("h2", [(MULE1, 5.0)], [(EXCHANGE, 5.0)], minutes=20),
        ]
    )
    tag_service(EXCHANGE, "Test Exchange", "exchange", "walletexplorer", 0.9)
    result = exposure.analyse_exposure("BTC", SUSPECT, max_hops=5)
    assert result["scoring_version"] == exposure.SCORING_VERSION
    assert result["weights"] == exposure.WEIGHTS
    assert result["explanation"]
