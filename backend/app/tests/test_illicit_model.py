"""Serving the illicit-transaction classifier.

Two properties matter more than the arithmetic:

  * the signal is *capped*, so a model that is wrong one time in six can never
    decide a risk rating on its own;
  * an absent model or an unsupported chain yields None, never 0.0 - zero is a
    confident claim of innocence.
"""

from __future__ import annotations

import pytest

from app.services import illicit_model, tx_features
from app.services.connectors.base import Asset
from app.tests.conftest import tx


@pytest.fixture(scope="module")
def model_available() -> bool:
    return illicit_model.model_info()["available"]


# --- operating point -----------------------------------------------------


def test_operating_point_comes_from_the_artifact(model_available):
    if not model_available:
        pytest.skip("servable model not trained")

    info = illicit_model.model_info()
    assert info["threshold"] >= 0.9, (
        "serving at xgboost's 0.5 default would be ~0.51 precision on this model"
    )
    assert info["version"] == "elliptic-xgb-servable-v1"
    assert info["supported_chains"] == ["BTC"]


def test_declared_precision_is_reported(model_available):
    if not model_available:
        pytest.skip("servable model not trained")
    precision = illicit_model.model_info()["precision_at_threshold"]
    assert precision is not None, "the API must be able to disclose how wrong this is"
    assert 0.7 <= precision <= 1.0


# --- scoring -------------------------------------------------------------


def test_scores_a_bitcoin_transaction(model_available):
    if not model_available:
        pytest.skip("servable model not trained")

    probability = illicit_model.score_transaction(
        tx("score-1", [("a", 1.0), ("b", 2.0)], [("c", 2.9)])
    )
    assert probability is not None
    assert 0.0 <= probability <= 1.0


@pytest.mark.parametrize("chain", ["ETH", "TRON"])
def test_unsupported_chain_returns_none_not_zero(chain):
    """Zero would assert innocence. None says we do not know."""
    result = illicit_model.score_transaction(
        tx("score-2", [("a", 1.0)], [("b", 0.9)], chain=chain, asset=Asset.native(chain))
    )
    assert result is None


def test_batch_scoring_skips_unscorable_chains(model_available):
    if not model_available:
        pytest.skip("servable model not trained")

    scores = illicit_model.score_transactions(
        [
            tx("batch-btc", [("a", 1.0)], [("b", 0.9)]),
            tx("batch-eth", [("c", 1.0)], [("d", 0.9)], chain="ETH", asset=Asset.native("ETH")),
        ]
    )
    assert "batch-btc" in scores
    assert "batch-eth" not in scores


def test_batch_and_single_scoring_agree(model_available):
    if not model_available:
        pytest.skip("servable model not trained")

    transaction = tx("agree-1", [("a", 3.0), ("b", 1.0)], [("c", 2.0), ("d", 1.9)])
    single = illicit_model.score_transaction(transaction)
    batched = illicit_model.score_transactions([transaction])["agree-1"]
    assert single == pytest.approx(batched)


def test_empty_batch_is_not_an_error():
    assert illicit_model.score_transactions([]) == {}


# --- the cap -------------------------------------------------------------


def test_no_flags_contribute_nothing():
    assert illicit_model.risk_contribution(0) == 0
    assert illicit_model.risk_contribution(-1) == 0


def test_contribution_saturates_and_never_exceeds_the_cap(model_available):
    if not model_available:
        pytest.skip("servable model not trained")

    cap = illicit_model.model_info()["max_risk_contribution"]
    values = [float(illicit_model.risk_contribution(n)) for n in range(1, 40)]

    assert all(v <= cap for v in values), "the cap must hold at any flag count"
    assert values == sorted(values), "more flags must never contribute less"
    assert values[-1] < cap or values[-1] == pytest.approx(cap, abs=0.01)


def test_cap_is_small_against_the_saturation_constant(model_available):
    """The signal nudges a ranking; it must not be able to decide one.

    At ~0.84 precision roughly one flag in six is wrong. A contribution large
    enough to move a cluster from low to high on its own would make those
    errors consequential.
    """
    if not model_available:
        pytest.skip("servable model not trained")

    from app.services.risk import SATURATION, _squash, label_for

    cap = illicit_model.model_info()["max_risk_contribution"]
    assert cap <= SATURATION * 0.15

    # From nothing, the maximum the model alone can produce stays low.
    assert label_for(_squash(cap)) == "low"


def test_flags_respects_the_threshold(model_available):
    if not model_available:
        pytest.skip("servable model not trained")

    threshold = illicit_model.model_info()["threshold"]
    assert illicit_model.flags(threshold) is True
    assert illicit_model.flags(threshold - 0.01) is False
    assert illicit_model.flags(None) is False


# --- degradation ---------------------------------------------------------


def test_absent_model_degrades_quietly(monkeypatch):
    """Intake must keep working when the artifact was never trained."""
    from pathlib import Path

    monkeypatch.setattr(illicit_model, "ARTIFACT_DIRS", (Path("/nonexistent"),))
    monkeypatch.setattr(illicit_model, "_model", illicit_model._Model())

    info = illicit_model.model_info()
    assert info["available"] is False
    assert "train" in (info["unavailable_reason"] or "").lower()
    assert illicit_model.score_transaction(tx("x", [("a", 1.0)], [("b", 0.9)])) is None
    assert illicit_model.score_transactions([tx("y", [("a", 1.0)], [("b", 0.9)])]) == {}
    assert illicit_model.risk_contribution(5) == 0


def test_feature_order_is_verified_before_the_model_is_used(model_available):
    """Position-bound features: a silent reorder would corrupt every score."""
    if not model_available:
        pytest.skip("servable model not trained")

    directory = next(
        d for d in illicit_model.ARTIFACT_DIRS if (d / illicit_model.MODEL_FILE).exists()
    )
    tx_features.verify_feature_order(directory)
