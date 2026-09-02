"""Connector factory and synthetic dataset tests.

The provenance assertions here are the code-level enforcement of the project's
honesty rule: synthetic data must never be reported as live-chain data.
"""

from __future__ import annotations

import pytest

from app.services.chain_detect import detect
from app.services.connectors import ConnectorError, get_connector
from app.services.connectors.live import (
    BlockchairConnector,
    EtherscanConnector,
    TronGridConnector,
)
from app.services.connectors.synthetic import (
    SyntheticConnector,
    dataset_path,
    get_complaints,
    get_entities,
    load_dataset,
)


@pytest.fixture(scope="module")
def dataset():
    if dataset_path() is None:
        pytest.skip("synthetic dataset not generated")
    return load_dataset()


# ---------------------------------------------------------------------------
# Factory / provenance
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("chain", ["BTC", "ETH", "TRON"])
def test_demo_mode_returns_synthetic_connector(chain, dataset):
    connector = get_connector(chain, demo_mode=True)
    assert isinstance(connector, SyntheticConnector)
    assert connector.source_name == "synthetic"


def test_live_connector_falls_back_to_synthetic_when_unconfigured(dataset, monkeypatch):
    """Without an API key we must fall back to synthetic AND say so.

    Silently serving synthetic data under a live source name would misrepresent
    the provenance of evidence in a case file.
    """
    monkeypatch.setattr(
        "app.services.connectors.live.settings.etherscan_api_key", "", raising=False
    )
    connector = get_connector("ETH", demo_mode=False)
    assert connector.source_name == "synthetic"
    assert not isinstance(connector, EtherscanConnector)


def test_unsupported_chain_is_rejected():
    with pytest.raises(ConnectorError):
        get_connector("DOGE", demo_mode=True)


def test_live_connectors_declare_distinct_source_names():
    """trace_runs.data_source must identify which indexer produced the evidence."""
    names = {
        EtherscanConnector.source_name,
        TronGridConnector.source_name,
        BlockchairConnector.source_name,
        SyntheticConnector.source_name,
    }
    assert names == {"etherscan", "trongrid", "blockchair", "synthetic"}


def test_etherscan_connector_refuses_to_start_without_a_key():
    with pytest.raises(ConnectorError):
        EtherscanConnector(api_key="")


# ---------------------------------------------------------------------------
# Synthetic dataset integrity
# ---------------------------------------------------------------------------
def test_dataset_declares_itself_synthetic(dataset):
    assert dataset["meta"]["is_synthetic"] is True
    notice = dataset["meta"]["notice"].lower()
    assert "synthetic" in notice
    assert "no real" in notice


def test_every_generated_address_passes_validation(dataset):
    """Demo data its own intake validator would reject is worthless."""
    bad = []
    for tx in dataset["transactions"]:
        for io in tx["inputs"] + tx["outputs"]:
            info = detect(io["address"])
            if not info.valid or info.chain != tx["chain"]:
                bad.append((tx["chain"], io["address"], info.reason))
    assert bad == [], f"{len(bad)} synthetic addresses fail validation"


def test_complaints_carry_no_personal_data(dataset):
    """victim_ref is a pseudonymous handle; the generator must not invent PII."""
    for complaint in dataset["complaints"]:
        assert complaint["victim_ref"].startswith("SYN-")
        assert complaint["source"] == "synthetic"


def test_dataset_contains_exchange_and_mixer_entities(dataset):
    types = {e["entity_type"] for e in get_entities()}
    assert "exchange" in types
    assert "mixer" in types, "the ETH ring routes through a mixer to exercise the flag"


def test_dataset_covers_all_three_chains(dataset):
    assert {t["chain"] for t in dataset["transactions"]} == {"BTC", "ETH", "TRON"}
    assert {c["chain"] for c in get_complaints()} == {"BTC", "ETH", "TRON"}


def test_btc_ring_contains_multi_input_transactions(dataset):
    """Common-input-ownership needs co-spends to find."""
    multi = [
        t for t in dataset["transactions"] if t["chain"] == "BTC" and len(t["inputs"]) > 1
    ]
    assert multi, "BTC ring must include consolidation transactions"


def test_btc_ring_contains_change_outputs(dataset):
    marked = [
        t
        for t in dataset["transactions"]
        if t["chain"] == "BTC" and any(o.get("is_change") for o in t["outputs"])
    ]
    assert marked, "BTC ring must include 2-output payments leaving change"


# ---------------------------------------------------------------------------
# Lookup behaviour
# ---------------------------------------------------------------------------
def test_connector_returns_transactions_for_a_known_address(dataset):
    complaint = get_complaints()[0]
    connector = get_connector(complaint["chain"], demo_mode=True)
    txs = connector.get_transactions(complaint["address"])
    assert txs, "a reported collection address must have transactions"
    assert all(t.chain == complaint["chain"] for t in txs)


def test_connector_returns_empty_for_unknown_address(dataset):
    connector = get_connector("BTC", demo_mode=True)
    assert connector.get_transactions("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa") == []


def test_transactions_are_returned_newest_first(dataset):
    complaint = next(c for c in get_complaints() if c["chain"] == "TRON")
    txs = get_connector("TRON", demo_mode=True).get_transactions(complaint["address"])
    timestamps = [t.timestamp for t in txs]
    assert timestamps == sorted(timestamps, reverse=True)


def test_limit_is_respected(dataset):
    complaint = get_complaints()[0]
    connector = get_connector(complaint["chain"], demo_mode=True)
    assert len(connector.get_transactions(complaint["address"], limit=1)) <= 1


def test_eth_lookup_works_by_normalised_address(dataset):
    """Regression: the index must be keyed on the normalised address.

    ETH addresses are traced in lowercase, but the dataset stores them
    EIP-55 checksummed. Keying the index on the raw form made every ETH
    lookup during a trace silently return nothing, so the ETH graph was
    never built.
    """
    from app.services.chain_detect import detect

    complaint = next(c for c in get_complaints() if c["chain"] == "ETH")
    info = detect(complaint["address"])
    connector = get_connector("ETH", demo_mode=True)

    by_raw = connector.get_transactions(complaint["address"])
    by_norm = connector.get_transactions(info.address_norm)

    assert by_raw, "checksummed lookup must work"
    assert by_norm, "normalised (lowercase) lookup must work - this is what tracing uses"
    assert len(by_raw) == len(by_norm)
