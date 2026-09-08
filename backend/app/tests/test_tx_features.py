"""Feature extraction for the servable classifier.

The load-bearing test here is the chain refusal. Everything else is arithmetic;
that one is the guard against the system producing a confident illicit score
for an Ethereum transaction from a model that has never seen one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services import tx_features
from app.services.connectors.base import Asset
from app.services.tx_features import UnsupportedChain, extract, to_vector
from app.tests.conftest import tx

# --- the refusal ---------------------------------------------------------


def test_bitcoin_is_supported():
    features = extract(tx("btc-1", [("a", 1.0)], [("b", 0.9)], chain="BTC"))
    assert set(features) == set(tx_features.FEATURE_ORDER)


@pytest.mark.parametrize("chain", ["ETH", "TRON"])
def test_account_chains_are_refused(chain):
    """Fail closed. A number from constants and mismatched units is worse than none."""
    with pytest.raises(UnsupportedChain) as exc:
        extract(tx("x-1", [("a", 1.0)], [("b", 0.9)], chain=chain, asset=Asset.native(chain)))

    message = str(exc.value)
    assert chain in message
    assert "Elliptic" in message, "the refusal must say why, not just that"


def test_refusal_names_the_supported_chains():
    with pytest.raises(UnsupportedChain, match="BTC"):
        extract(tx("x-2", [("a", 1.0)], [("b", 0.9)], chain="ETH", asset=Asset.native("ETH")))


def test_supported_chains_is_not_silently_widened():
    """A guard on the guard.

    Adding a chain here without training a model for it would re-open exactly
    the hole this module exists to close, so the set is pinned.
    """
    assert tx_features.SUPPORTED_CHAINS == frozenset({"BTC"})


# --- arithmetic ----------------------------------------------------------


def test_counts_distinct_addresses_not_entries():
    """Two inputs from one address is one address, not two."""
    features = extract(tx("btc-2", [("same", 1.0), ("same", 2.0)], [("out", 2.9)]))
    assert features["num_input_addresses"] == 1.0
    assert features["num_output_addresses"] == 1.0


def test_input_and_output_statistics():
    features = extract(tx("btc-3", [("a", 1.0), ("b", 3.0)], [("c", 2.0), ("d", 1.9)]))

    assert features["in_BTC_min"] == 1.0
    assert features["in_BTC_max"] == 3.0
    assert features["in_BTC_mean"] == 2.0
    assert features["in_BTC_total"] == 4.0
    assert features["out_BTC_min"] == 1.9
    assert features["out_BTC_max"] == 2.0
    assert features["out_BTC_total"] == pytest.approx(3.9)


def test_median_of_an_even_count_is_the_midpoint():
    features = extract(tx("btc-4", [("a", 1.0), ("b", 2.0), ("c", 3.0), ("d", 6.0)], [("e", 11.0)]))
    assert features["in_BTC_median"] == 2.5


def test_median_of_an_odd_count_is_the_middle_value():
    features = extract(tx("btc-5", [("a", 1.0), ("b", 5.0), ("c", 9.0)], [("d", 14.0)]))
    assert features["in_BTC_median"] == 5.0


def test_size_grows_with_inputs_and_outputs():
    small = extract(tx("btc-6", [("a", 1.0)], [("b", 0.9)]))["size"]
    large = extract(tx("btc-7", [("a", 1.0), ("b", 1.0)], [("c", 1.0), ("d", 0.9)]))["size"]
    assert large > small


def test_degrees_default_to_zero_when_not_supplied():
    features = extract(tx("btc-8", [("a", 1.0)], [("b", 0.9)]))
    assert features["in_txs_degree"] == 0.0
    assert features["out_txs_degree"] == 0.0


def test_degrees_are_carried_through():
    features = extract(tx("btc-9", [("a", 1.0)], [("b", 0.9)]), in_degree=7, out_degree=3)
    assert features["in_txs_degree"] == 7.0
    assert features["out_txs_degree"] == 3.0


# --- vector ordering -----------------------------------------------------


def test_vector_follows_the_declared_order():
    features = extract(tx("btc-10", [("a", 1.0)], [("b", 0.9)]))
    vector = to_vector(features)

    assert len(vector) == 17
    assert vector == [features[name] for name in tx_features.FEATURE_ORDER]


def test_missing_feature_is_an_error_not_a_zero():
    features = extract(tx("btc-11", [("a", 1.0)], [("b", 0.9)]))
    del features["fees"]
    with pytest.raises(ValueError, match="fees"):
        to_vector(features)


def test_feature_order_matches_the_trained_artifact():
    """The silent-corruption guard.

    XGBoost binds by position. If the trainer's column order and this module's
    diverge, every prediction is wrong while everything still loads and returns
    plausible probabilities.
    """
    for candidate in (Path("/app/ml_artifacts"), Path("/app/ml/artifacts")):
        if (candidate / "feature_columns_servable.json").exists():
            tx_features.verify_feature_order(candidate)
            trained = json.loads(
                (candidate / "feature_columns_servable.json").read_text(encoding="utf-8")
            )
            assert trained == tx_features.FEATURE_ORDER
            return
    pytest.skip("servable model not trained - run train_fraud_clf.py --servable")
