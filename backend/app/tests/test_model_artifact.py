"""The trained model artifact must load and infer in the runtime that ships.

Regression guard. The artifact was once written by xgboost's scikit-learn
wrapper, which encodes `_estimator_type` in the model metadata; scikit-learn
1.9 removed that attribute, so the artifact became unloadable the moment the
image was rebuilt onto a different xgboost. The failure was silent until
inference was attempted.

Training now uses the native Booster API, whose JSON loads across xgboost 2.x
and 3.x. These tests fail loudly if that ever regresses.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ARTIFACT_DIRS = [
    Path("/app/ml/artifacts"),
    Path(__file__).resolve().parents[3] / "ml" / "artifacts",
]


def artifacts() -> Path | None:
    for d in ARTIFACT_DIRS:
        if (d / "fraud_clf.json").exists():
            return d
    return None


@pytest.fixture(scope="module")
def artifact_dir():
    d = artifacts()
    if d is None:
        pytest.skip("model not trained - run ml/src/train_fraud_clf.py")
    return d


def test_model_loads_in_this_runtime(artifact_dir):
    import xgboost as xgb

    booster = xgb.Booster()
    booster.load_model(str(artifact_dir / "fraud_clf.json"))
    assert booster.num_features() > 0


def test_model_can_score_a_row(artifact_dir):
    import numpy as np
    import xgboost as xgb

    booster = xgb.Booster()
    booster.load_model(str(artifact_dir / "fraud_clf.json"))
    cols = json.loads((artifact_dir / "feature_columns.json").read_text())

    proba = booster.predict(xgb.DMatrix(np.zeros((2, len(cols))), feature_names=cols))
    assert len(proba) == 2
    assert all(0.0 <= float(p) <= 1.0 for p in proba)


def test_feature_columns_match_the_model(artifact_dir):
    import xgboost as xgb

    booster = xgb.Booster()
    booster.load_model(str(artifact_dir / "fraud_clf.json"))
    cols = json.loads((artifact_dir / "feature_columns.json").read_text())
    assert booster.num_features() == len(cols)


def test_metrics_report_records_a_temporal_split(artifact_dir):
    """The reported numbers must come from a temporal split, not a random one."""
    report = json.loads((artifact_dir / "metrics.json").read_text())
    assert "temporal split" in report["validation_protocol"].lower()
    assert report["test_illicit"] > 0
    assert report["train_rows"] > 0


def test_metrics_are_plausible_and_present(artifact_dir):
    report = json.loads((artifact_dir / "metrics.json").read_text())
    m = report["metrics_heldout"]
    for key in ("precision_illicit", "recall_illicit", "f1_illicit", "roc_auc"):
        assert 0.0 <= m[key] <= 1.0
    # A perfect score on this dataset would mean the split leaked.
    assert m["f1_illicit"] < 1.0
    assert m["roc_auc"] > 0.5


def test_artifact_was_not_written_by_the_sklearn_wrapper(artifact_dir):
    """`_estimator_type` in the model JSON means the fragile wrapper wrote it."""
    raw = (artifact_dir / "fraud_clf.json").read_text()
    assert "_estimator_type" not in raw
