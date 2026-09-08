"""Serve the Elliptic-trained illicit-transaction classifier.

Scoring happens at ingest, where real ChainTransaction objects exist with their
full input and output lists. The graph stores only summaries - counts and a
fee, not the per-input values the model needs - so scoring at query time would
mean re-fetching from the indexer, and paying an API call to re-derive
something already known. The probability is written onto the Transaction node
once and read back cheaply thereafter.

Operating point comes from the artifact, not from a constant here. The trainer
measured it (`serving_guidance` in metrics_servable.json) and the number that
governs behaviour must be the number the evidence supports - a threshold
hardcoded in serving code drifts away from the sweep that justified it, and
nothing catches the drift.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from pathlib import Path

from app.services import tx_features
from app.services.connectors.base import ChainTransaction
from app.services.tx_features import UnsupportedChain

logger = logging.getLogger(__name__)

#: Searched in order. The compose stack mounts ml/artifacts read-only at the
#: first path; the second is the repo layout for a local run.
ARTIFACT_DIRS = (Path("/app/ml_artifacts"), Path("/app/ml/artifacts"))

MODEL_FILE = "fraud_clf_servable.json"
METRICS_FILE = "metrics_servable.json"

#: Used only if the artifact somehow lacks serving_guidance. Deliberately the
#: conservative end of the measured sweep rather than xgboost's 0.5 default,
#: which on this model is ~0.51 precision - every second flag wrong.
FALLBACK_THRESHOLD = 0.95
FALLBACK_MAX_CONTRIBUTION = 6.0


class _Model:
    """Lazily-loaded booster plus its operating point."""

    def __init__(self) -> None:
        self._loaded = False
        self.booster = None
        self.threshold = FALLBACK_THRESHOLD
        self.max_contribution = FALLBACK_MAX_CONTRIBUTION
        self.version = "unavailable"
        self.precision_at_threshold: float | None = None
        self.unavailable_reason: str | None = None

    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True

        directory = next(
            (d for d in ARTIFACT_DIRS if (d / MODEL_FILE).exists()), None
        )
        if directory is None:
            self.unavailable_reason = (
                f"{MODEL_FILE} not found. Train it with: "
                "python ml/src/train_fraud_clf.py --servable"
            )
            logger.warning("illicit model unavailable: %s", self.unavailable_reason)
            return

        try:
            import xgboost as xgb

            # Feature order is bound by position. A mismatch between the
            # trainer's columns and ours would score every transaction wrongly
            # while still returning confident-looking probabilities, so it is
            # checked before the model is ever used rather than trusted.
            tx_features.verify_feature_order(directory)

            booster = xgb.Booster()
            booster.load_model(str(directory / MODEL_FILE))

            metrics = json.loads((directory / METRICS_FILE).read_text(encoding="utf-8"))
            guidance = metrics.get("serving_guidance") or {}

            self.booster = booster
            self.version = metrics.get("model_version", "unknown")
            self.threshold = float(guidance.get("decision_threshold", FALLBACK_THRESHOLD))
            self.max_contribution = float(
                guidance.get("max_risk_contribution", FALLBACK_MAX_CONTRIBUTION)
            )
            for row in guidance.get("threshold_sweep", []):
                if abs(float(row.get("threshold", -1)) - self.threshold) < 1e-9:
                    self.precision_at_threshold = float(row.get("precision", 0.0))
                    break

            logger.info(
                "illicit model %s loaded: threshold %.2f (precision %s), cap %.1f",
                self.version,
                self.threshold,
                self.precision_at_threshold,
                self.max_contribution,
            )
        except Exception as exc:  # noqa: BLE001 - never let a model fault break intake
            self.booster = None
            self.unavailable_reason = f"{type(exc).__name__}: {exc}"
            logger.warning("illicit model failed to load: %s", self.unavailable_reason)

    @property
    def available(self) -> bool:
        self.load()
        return self.booster is not None


_model = _Model()


def model_info() -> dict:
    """Describe the serving model, for health checks and API disclosure."""
    _model.load()
    return {
        "available": _model.booster is not None,
        "version": _model.version,
        "threshold": _model.threshold,
        "precision_at_threshold": _model.precision_at_threshold,
        "max_risk_contribution": _model.max_contribution,
        "supported_chains": sorted(tx_features.SUPPORTED_CHAINS),
        "size_feature_is_estimated": True,
        "unavailable_reason": _model.unavailable_reason,
    }


def score_transaction(tx: ChainTransaction) -> float | None:
    """Illicit probability for one transaction, or None if it cannot be scored.

    None is returned - never 0.0 - when the chain is unsupported or the model
    is absent. Zero is a confident claim of innocence; the honest answer in
    both cases is that we do not know.
    """
    if not _model.available:
        return None

    try:
        features = tx_features.extract(tx)
    except UnsupportedChain:
        return None

    import numpy as np
    import xgboost as xgb

    vector = np.array([tx_features.to_vector(features)], dtype=float)
    matrix = xgb.DMatrix(vector, feature_names=tx_features.FEATURE_ORDER)
    return float(_model.booster.predict(matrix)[0])


def score_transactions(transactions: list[ChainTransaction]) -> dict[str, float]:
    """Score a batch, returning {txid: probability} for those that could be scored.

    Batched into a single DMatrix: predicting one row at a time over a few
    hundred transactions is dominated by per-call overhead.
    """
    if not _model.available:
        return {}

    scorable: list[tuple[str, list[float]]] = []
    for tx in transactions:
        try:
            scorable.append((tx.txid, tx_features.to_vector(tx_features.extract(tx))))
        except UnsupportedChain:
            continue

    if not scorable:
        return {}

    import numpy as np
    import xgboost as xgb

    matrix = xgb.DMatrix(
        np.array([vector for _, vector in scorable], dtype=float),
        feature_names=tx_features.FEATURE_ORDER,
    )
    predictions = _model.booster.predict(matrix)
    return {txid: float(p) for (txid, _), p in zip(scorable, predictions, strict=True)}


def flags(probability: float | None) -> bool:
    """Whether a probability clears the measured operating point."""
    if probability is None:
        return False
    _model.load()
    return probability >= _model.threshold


def risk_contribution(flagged_count: int) -> Decimal:
    """Points this signal adds to an entity's risk score.

    Saturating, and capped at the artifact's `max_risk_contribution` (6 points
    against a saturation constant of 60). One flagged transaction earns half
    the cap, two three-quarters, and so on.

    Capped deliberately low. At the 0.95 threshold this model is ~0.84
    precision, so roughly one flag in six is wrong; a signal that wrong must
    nudge a ranking, never decide one. The complaint evidence stays dominant.
    """
    if flagged_count <= 0:
        return Decimal(0)

    # A model that failed to load contributes nothing, even if a caller passes
    # a positive count. The fallback cap exists to keep a *loaded* model off
    # xgboost's dangerous 0.5 default - it must never become points attributed
    # to a classifier that never ran.
    if not _model.available:
        return Decimal(0)

    fraction = 1.0 - (0.5**flagged_count)
    return Decimal(str(round(_model.max_contribution * fraction, 3)))
