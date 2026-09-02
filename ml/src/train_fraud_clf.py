#!/usr/bin/env python3
"""Train and validate the illicit-transaction classifier on the Elliptic dataset.

Data: ml/data/elliptic_labeled.csv.gz, produced by ml/src/download_data.py from
the public Elliptic Bitcoin Dataset (Elliptic++ mirror). 46,564 labelled
transactions - 4,545 illicit, 42,019 licit - out of 203,769 total.

Validation protocol: **temporal split**, which is the standard protocol for this
dataset and the only honest one for a fraud problem. Elliptic's `Time step`
column runs 1..49; we train on steps 1-34 and test on 35-49. A random split
would leak future information into training and inflate the reported numbers,
because transactions in the same time step are heavily correlated.

The dataset also contains a well-known distribution shift after time step 43
(the "dark market shutdown"), which is exactly why recall on the held-out tail
is much lower than a random split would suggest. We report the real number.

What this model is and is not:
  * It IS a per-transaction illicit-likelihood signal on Bitcoin.
  * It is NOT an exchange fraud-linkage score. Elliptic labels transactions, not
    exchange culpability. The exchange score lives in app/services/risk.py and
    uses this only as one modifier.

API note: this uses xgboost's **native Booster API**, not the scikit-learn
wrapper. The wrapper couples xgboost to scikit-learn internals that move between
releases - xgboost 2.1's `XGBClassifier.save_model` reads `_estimator_type`,
which scikit-learn removed in 1.9, so the wrapper raises on save. The native API
touches none of that, and the JSON model it writes loads across xgboost 2.x and
3.x alike, so the artifact does not silently break when the image is rebuilt.

Usage:
    python ml/src/train_fraud_clf.py
    python ml/src/train_fraud_clf.py --split-time-step 34
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd
import xgboost as xgb
from sklearn.metrics import (
    average_precision_score,
    classification_report,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "ml" / "data" / "elliptic_labeled.csv.gz"
ARTIFACTS = REPO / "ml" / "artifacts"

# Elliptic encodes 1 = illicit, 2 = licit. We model illicit as the positive class.
LABEL_ILLICIT = 1
DEFAULT_SPLIT_STEP = 34
MODEL_VERSION = "elliptic-xgb-v1"


def load_data(path: Path = DATA) -> pd.DataFrame:
    if not path.exists():
        raise SystemExit(
            f"{path} not found. Run: python ml/src/download_data.py elliptic"
        )
    df = pd.read_csv(path, compression="gzip", low_memory=False)
    df.columns = [c.strip() for c in df.columns]
    return df


def prepare(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series, pd.Series, list[str]]:
    time_col = "Time step" if "Time step" in df.columns else df.columns[1]
    feature_cols = [c for c in df.columns if c not in ("txId", time_col, "label")]

    x = df[feature_cols].apply(pd.to_numeric, errors="coerce").fillna(0.0)
    y = (df["label"].astype(int) == LABEL_ILLICIT).astype(int)
    steps = df[time_col].astype(int)
    return x, y, steps, feature_cols


def train(split_step: int = DEFAULT_SPLIT_STEP) -> dict:
    print(f"loading {DATA} ...", flush=True)
    df = load_data()
    x, y, steps, feature_cols = prepare(df)

    train_mask = steps <= split_step
    test_mask = ~train_mask

    x_train, y_train = x[train_mask], y[train_mask]
    x_test, y_test = x[test_mask], y[test_mask]

    print(
        f"rows={len(df)} features={len(feature_cols)} "
        f"time steps {steps.min()}..{steps.max()}"
    )
    print(
        f"TEMPORAL SPLIT at step {split_step}: "
        f"train={len(x_train)} ({int(y_train.sum())} illicit), "
        f"test={len(x_test)} ({int(y_test.sum())} illicit)"
    )

    if y_test.sum() == 0:
        raise SystemExit("held-out slice contains no illicit samples - bad split")

    # Class imbalance ~1:9. scale_pos_weight rather than resampling, so the
    # held-out distribution stays untouched.
    pos_weight = float((y_train == 0).sum() / max((y_train == 1).sum(), 1))

    params = {
        "objective": "binary:logistic",
        "eval_metric": "aucpr",
        "max_depth": 6,
        "eta": 0.1,
        "subsample": 0.9,
        "colsample_bytree": 0.9,
        "scale_pos_weight": pos_weight,
        "tree_method": "hist",
        "nthread": 4,
        "seed": 26183,
    }
    num_rounds = 400

    dtrain = xgb.DMatrix(x_train.values, label=y_train.values, feature_names=feature_cols)
    dtest = xgb.DMatrix(x_test.values, label=y_test.values, feature_names=feature_cols)

    started = time.time()
    booster = xgb.train(params, dtrain, num_boost_round=num_rounds)
    train_seconds = round(time.time() - started, 1)

    proba = booster.predict(dtest)
    pred = (proba >= 0.5).astype(int)

    metrics = {
        "precision_illicit": round(float(precision_score(y_test, pred, zero_division=0)), 4),
        "recall_illicit": round(float(recall_score(y_test, pred, zero_division=0)), 4),
        "f1_illicit": round(float(f1_score(y_test, pred, zero_division=0)), 4),
        "roc_auc": round(float(roc_auc_score(y_test, proba)), 4),
        "average_precision": round(float(average_precision_score(y_test, proba)), 4),
    }

    tn, fp, fn, tp = confusion_matrix(y_test, pred).ravel()
    report = {
        "model_version": MODEL_VERSION,
        "algorithm": "xgboost.train (native Booster API)",
        "xgboost_version": xgb.__version__,
        "dataset": "Elliptic Bitcoin Dataset (public; Elliptic++ mirror)",
        "validation_protocol": (
            f"temporal split - train on Time step <= {split_step}, "
            f"test on Time step > {split_step}. No random shuffling: "
            "transactions within a time step are correlated, so a random split "
            "would leak future information and inflate these numbers."
        ),
        "rows_total": int(len(df)),
        "features": len(feature_cols),
        "train_rows": int(len(x_train)),
        "train_illicit": int(y_train.sum()),
        "test_rows": int(len(x_test)),
        "test_illicit": int(y_test.sum()),
        "scale_pos_weight": round(pos_weight, 3),
        "num_boost_round": num_rounds,
        "train_seconds": train_seconds,
        "metrics_heldout": metrics,
        "confusion_matrix_heldout": {
            "true_negative": int(tn),
            "false_positive": int(fp),
            "false_negative": int(fn),
            "true_positive": int(tp),
        },
        "top_features": [],
    }

    gains = booster.get_score(importance_type="gain")
    report["top_features"] = [
        {"feature": name, "gain": round(float(gain), 4)}
        for name, gain in sorted(gains.items(), key=lambda kv: kv[1], reverse=True)[:15]
    ]

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    model_path = ARTIFACTS / "fraud_clf.json"
    booster.save_model(str(model_path))
    report["model_path"] = str(model_path.relative_to(REPO))
    (ARTIFACTS / "feature_columns.json").write_text(
        json.dumps(feature_cols, indent=0), encoding="utf-8"
    )
    (ARTIFACTS / "metrics.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("\n--- held-out results (temporal split) ---")
    print(classification_report(y_test, pred, target_names=["licit", "illicit"], digits=4))
    print(json.dumps(metrics, indent=2))
    print(f"\nwrote {ARTIFACTS / 'metrics.json'}")
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--split-time-step", type=int, default=DEFAULT_SPLIT_STEP)
    args = ap.parse_args()
    train(args.split_time_step)
    return 0


if __name__ == "__main__":
    sys.exit(main())
