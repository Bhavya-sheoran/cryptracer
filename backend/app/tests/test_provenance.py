"""Provenance is a property of the records, not of a configuration flag.

The defect these tests exist to prevent: `data_provenance` used to be computed
as `"synthetic" if settings.demo_mode else "live_indexer_apis"`. That reports
how the system is configured *at query time*, which says nothing about where
records already in the graph came from. Switching DEMO_MODE on a populated
graph therefore made every synthetic path start claiming to be live data - in a
system whose central promise is that it never misrepresents its data source.
"""

from __future__ import annotations

import pytest

from app.api.v1.wallet_analysis import _provenance_of
from app.services.connectors.base import SOURCE_SYNTHETIC

# --- the classifier, in isolation ---------------------------------------


def path(sources: list[str]) -> dict:
    return {
        "edge_sources": sorted(set(sources)),
        "contains_synthetic": SOURCE_SYNTHETIC in sources,
        "provenance_complete": "unknown" not in sources,
    }


def test_all_live_edges_report_live():
    assert _provenance_of(path(["etherscan"])) == "live_indexer_apis"
    assert _provenance_of(path(["etherscan", "trongrid"])) == "live_indexer_apis"


def test_all_synthetic_edges_report_synthetic():
    assert _provenance_of(path(["synthetic"])) == "synthetic"


def test_one_synthetic_edge_contaminates_the_whole_path():
    """A path is only as trustworthy as its weakest link.

    "Mostly real" is not a category an investigator can act on, so a single
    synthetic edge must downgrade the entire finding rather than being
    averaged away.
    """
    result = _provenance_of(path(["etherscan", "synthetic"]))
    assert result == "mixed_contains_synthetic"
    assert result != "live_indexer_apis"


def test_unstamped_edges_report_unverified_never_live():
    """Records written before stamping existed must not be assumed live.

    An unknown origin is exactly the case where a wrong guess is most
    damaging, so it degrades to "unverified" rather than being upgraded.
    """
    assert _provenance_of(path(["unknown"])) == "unverified"
    assert _provenance_of(path(["etherscan", "unknown"])) == "unverified"


def test_synthetic_outranks_unverified():
    """Both are bad; the more specific warning is the more useful one."""
    assert _provenance_of(path(["synthetic", "unknown"])) == "mixed_contains_synthetic"


def test_empty_path_does_not_claim_anything():
    assert _provenance_of(path([])) == "no_traced_edges"


def test_demo_mode_cannot_change_the_answer(monkeypatch):
    """The regression guard.

    Whatever DEMO_MODE says, the classifier reads only the edges.
    """
    from app.api.v1 import wallet_analysis

    live = path(["etherscan"])
    synthetic = path(["synthetic"])

    for demo in (True, False):
        monkeypatch.setattr(wallet_analysis.settings, "demo_mode", demo, raising=False)
        assert _provenance_of(live) == "live_indexer_apis"
        assert _provenance_of(synthetic) == "synthetic"


# --- end to end through the graph ---------------------------------------


def test_writer_stamps_every_record(graph):
    """Nodes, transactions and transfer edges all carry the source."""
    from app.db.neo4j import get_driver
    from app.tests.conftest import tx

    graph.write_transactions(
        [tx("prov-tx-1", [("addr-a", 1.0)], [("addr-b", 0.9)])],
        data_source="etherscan",
    )

    with get_driver().session() as session:
        assert session.run(
            "MATCH (t:Transaction {txid:'prov-tx-1'}) RETURN t.data_source AS s"
        ).single()["s"] == "etherscan"
        assert session.run(
            "MATCH ()-[r:TRANSFERRED {txid:'prov-tx-1'}]->() RETURN r.data_source AS s"
        ).single()["s"] == "etherscan"
        assert session.run(
            "MATCH (a:Address {address_norm:'addr-a'}) RETURN a.data_sources AS s"
        ).single()["s"] == ["etherscan"]


def test_unspecified_source_is_unknown_not_assumed(graph):
    """A caller that does not declare a source gets "unknown", never a guess.

    Filling the gap from DEMO_MODE would make the guess indistinguishable from
    a fact, which is the whole failure mode being designed out.
    """
    from app.db.neo4j import get_driver
    from app.tests.conftest import tx

    graph.write_transactions([tx("prov-tx-2", [("addr-c", 1.0)], [("addr-d", 0.9)])])

    with get_driver().session() as session:
        assert session.run(
            "MATCH (t:Transaction {txid:'prov-tx-2'}) RETURN t.data_source AS s"
        ).single()["s"] == "unknown"


def test_address_seen_by_two_sources_records_both(graph):
    """Mixed history must stay visible rather than collapsing to one value."""
    from app.db.neo4j import get_driver
    from app.tests.conftest import tx

    graph.write_transactions(
        [tx("prov-tx-3", [("shared", 1.0)], [("out-1", 0.9)])], data_source="etherscan"
    )
    graph.write_transactions(
        [tx("prov-tx-4", [("shared", 1.0)], [("out-2", 0.9)])], data_source="synthetic"
    )

    with get_driver().session() as session:
        sources = session.run(
            "MATCH (a:Address {address_norm:'shared'}) RETURN a.data_sources AS s"
        ).single()["s"]

    assert sorted(sources) == ["etherscan", "synthetic"]


def test_trace_reports_the_sources_it_crossed(graph):
    """The read side surfaces what the write side stamped."""
    from app.services import tracing
    from app.tests.conftest import tx

    graph.write_transactions(
        [
            tx("prov-tx-5", [("root-x", 2.0)], [("mid-x", 1.9)]),
            tx("prov-tx-6", [("mid-x", 1.9)], [("end-x", 1.8)], minutes=10),
        ],
        data_source="synthetic",
    )

    result = tracing.trace_path("BTC", "root-x", depth=4)

    assert result["edge_sources"] == ["synthetic"]
    assert result["contains_synthetic"] is True
    assert result["provenance_complete"] is True
    assert _provenance_of(result) == "synthetic"


@pytest.mark.parametrize("declared", ["etherscan", "trongrid", "blockchair", "synthetic"])
def test_every_connector_source_round_trips(graph, declared):
    from app.services import tracing
    from app.tests.conftest import tx

    graph.write_transactions(
        [tx(f"rt-{declared}", [(f"r-{declared}", 1.0)], [(f"e-{declared}", 0.9)])],
        data_source=declared,
    )

    result = tracing.trace_path("BTC", f"r-{declared}", depth=2)
    assert result["edge_sources"] == [declared]
