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

Two models come out of this file, and the distinction matters:

  * **Default (182 features)** - the benchmark. Validated, citable, and
    impossible to serve: 165 of its features are anonymised z-scores whose
    definitions Elliptic never published, so they cannot be computed for a live
    transaction.
  * **`--servable` (17 features)** - trained only on the human-readable columns
    that are reproducible from raw chain data. Weaker, and the one that can
    actually score a transaction the system traced itself.

They are written to separate artifacts. Never overwrite one with the other.

Usage:
    python ml/src/train_fraud_clf.py
    python ml/src/train_fraud_clf.py --split-time-step 34
    python ml/src/train_fraud_clf.py --servable
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
SERVABLE_MODEL_VERSION = "elliptic-xgb-servable-v1"

# The 17 Elliptic features that are human-readable and reproducible from raw
# chain data. The other 165 (Local_feature_N, Aggregate_feature_N) are
# anonymised z-scores whose definitions Elliptic never published - they cannot
# be computed for a live transaction, which is why a model using them can be
# validated but never served.
SERVABLE_FEATURES = [
    "in_txs_degree", "out_txs_degree", "total_BTC", "fees", "size",
    "num_input_addresses", "num_output_addresses",
    "in_BTC_min", "in_BTC_max", "in_BTC_mean", "in_BTC_median", "in_BTC_total",
    "out_BTC_min", "out_BTC_max", "out_BTC_mean", "out_BTC_median", "out_BTC_total",
]

# Thresholds reported in the servable artifact so the serving code can read its
# operating point from the same file that evidences it.
SERVING_THRESHOLDS = (0.5, 0.7, 0.8, 0.9, 0.95, 0.98)
SERVING_THRESHOLD = 0.95
SERVING_MAX_RISK_CONTRIBUTION = 6.0


def load_data(path: Path = DATA) -> pd.DataFrame:
    if not path.exists():
        raise SystemExit(
            f"{path} not found. Run: python ml/src/download_data.py elliptic"
        )
    df = pd.read_csv(path, compression="gzip", low_memory=False)
    df.columns = [c.strip() for c in df.columns]
    return df


def prepare(
    df: pd.DataFrame, servable: bool = False
) -> tuple[pd.DataFrame, pd.Series, pd.Series, list[str]]:
    time_col = "Time step" if "Time step" in df.columns else df.columns[1]
    feature_cols = [c for c in df.columns if c not in ("txId", time_col, "label")]

    if servable:
        # Fail loudly rather than quietly training on whatever survived. If the
        # upstream mirror renames a column, a model trained on 14 features would
        # still save and still load - and be wrong in a way nothing catches.
        missing = [c for c in SERVABLE_FEATURES if c not in df.columns]
        if missing:
            raise SystemExit(f"servable columns absent from the dataset: {missing}")
        feature_cols = list(SERVABLE_FEATURES)

    x = df[feature_cols].apply(pd.to_numeric, errors="coerce").fillna(0.0)
    y = (df["label"].astype(int) == LABEL_ILLICIT).astype(int)
    steps = df[time_col].astype(int)
    return x, y, steps, feature_cols


def train(split_step: int = DEFAULT_SPLIT_STEP, servable: bool = False) -> dict:
    print(f"loading {DATA} ...", flush=True)
    df = load_data()
    x, y, steps, feature_cols = prepare(df, servable=servable)
    if servable:
        print(f"SERVABLE MODE: restricted to {len(feature_cols)} reproducible features")

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
        "model_version": SERVABLE_MODEL_VERSION if servable else MODEL_VERSION,
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

    # The servable model is dangerous at the default 0.5 threshold, so the
    # artifact carries its own operating point and the sweep that justifies it.
    # Serving code reads the threshold from here rather than hardcoding one,
    # which keeps the number that governs behaviour and the evidence for it in
    # the same file - they cannot drift apart.
    if servable:
        report["serving_guidance"] = {
            "decision_threshold": SERVING_THRESHOLD,
            "reason": (
                "At the default 0.5 this model is roughly 0.53 precision - about "
                "every second flag would be wrong, which is indefensible for a "
                "signal contributing to a freeze recommendation. At 0.95, "
                "precision recovers to roughly 0.86 while recall falls to about "
                "0.21. For an advisory modifier, being right when it speaks "
                "matters far more than speaking often."
            ),
            "max_risk_contribution": SERVING_MAX_RISK_CONTRIBUTION,
            "threshold_sweep": [
                {
                    "threshold": t,
                    "flagged": int((proba >= t).sum()),
                    "precision": round(
                        float(precision_score(y_test, (proba >= t).astype(int),
                                              zero_division=0)), 4
                    ),
                    "recall": round(
                        float(recall_score(y_test, (proba >= t).astype(int),
                                           zero_division=0)), 4
                    ),
                }
                for t in SERVING_THRESHOLDS
            ],
        }

    suffix = "_servable" if servable else ""
    model_path = ARTIFACTS / f"fraud_clf{suffix}.json"
    columns_path = ARTIFACTS / f"feature_columns{suffix}.json"
    metrics_path = ARTIFACTS / f"metrics{suffix}.json"

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    booster.save_model(str(model_path))
    report["model_path"] = str(model_path.relative_to(REPO))
    columns_path.write_text(json.dumps(feature_cols, indent=0), encoding="utf-8")
    metrics_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("\n--- held-out results (temporal split) ---")
    print(classification_report(y_test, pred, target_names=["licit", "illicit"], digits=4))
    print(json.dumps(metrics, indent=2))
    if servable:
        print("\n--- threshold sweep (serve at 0.95, not 0.5) ---")
        for row in report["serving_guidance"]["threshold_sweep"]:
            print(
                f"  {row['threshold']:.2f}  flagged={row['flagged']:>5}  "
                f"precision={row['precision']:.4f}  recall={row['recall']:.4f}"
            )
    print(f"\nwrote {metrics_path}")
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--split-time-step", type=int, default=DEFAULT_SPLIT_STEP)
    ap.add_argument(
        "--servable",
        action="store_true",
        help=(
            "train on the 17 reproducible features only - the model that can "
            "actually score a live transaction. Writes *_servable artifacts."
        ),
    )
    args = ap.parse_args()
    train(args.split_time_step, servable=args.servable)
    return 0


if __name__ == "__main__":
    sys.exit(main())
