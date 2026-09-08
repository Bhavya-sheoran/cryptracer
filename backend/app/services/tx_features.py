"""Compute the servable classifier's features from a ChainTransaction.

The model this feeds (`ml/artifacts/fraud_clf_servable.json`) is trained on the
Elliptic dataset, which is **Bitcoin only**. That is a data limitation, not an
architectural one, and it cannot be coded around: Elliptic's value is its
46,564 analyst-assigned illicit/licit labels, and no comparable public labelled
dataset exists for Ethereum, Tron, or anything else.

Three separate things break if the Bitcoin model is pointed at an account-model
chain:

  * `num_input_addresses` and `num_output_addresses` are the model's 1st and
    3rd features by gain. On Ethereum every transaction has exactly one of
    each, so both become constants and their signal disappears.
  * `total_BTC`, `in_BTC_*` and `out_BTC_*` are Bitcoin value distributions.
    An ETH value fed into them is a different quantity in a different unit
    with a different scale.
  * `size` (transaction bytes, the 2nd feature by gain) has no equivalent on
    an account chain at all.

So this module is deliberately chain-aware and **fails closed**: it extracts
features only for chains a model was actually trained on, and refuses the rest.
A confident number derived from constants and unit mismatches is worse than no
number, because it looks exactly like a real one. When labelled data exists for
another chain, register its model here and the refusal lifts.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from pathlib import Path

from app.services.connectors.base import ChainTransaction

logger = logging.getLogger(__name__)

#: Chains for which a trained, validated model exists. Everything else is
#: refused rather than guessed at.
SUPPORTED_CHAINS = frozenset({"BTC"})

#: Feature order the model expects. Must match feature_columns_servable.json -
#: XGBoost binds by position, so a reordering here silently scores garbage.
FEATURE_ORDER = [
    "in_txs_degree",
    "out_txs_degree",
    "total_BTC",
    "fees",
    "size",
    "num_input_addresses",
    "num_output_addresses",
    "in_BTC_min",
    "in_BTC_max",
    "in_BTC_mean",
    "in_BTC_median",
    "in_BTC_total",
    "out_BTC_min",
    "out_BTC_max",
    "out_BTC_mean",
    "out_BTC_median",
    "out_BTC_total",
]

# A legacy P2PKH input is ~148 bytes and an output ~34, plus ~10 bytes of
# version, counts and locktime. Blockchair does return a real `size`, but it is
# not carried on ChainTransaction, so this estimate stands in.
#
# It is an ESTIMATE, and it is the model's second-strongest feature. Segwit
# inputs are materially smaller, so this overstates size for modern
# transactions - a bias in the same direction for every transaction, which a
# tree model tolerates better than noise, but it is a known weakness and the
# reason `size_is_estimated` is reported alongside every score.
BYTES_PER_INPUT = 148
BYTES_PER_OUTPUT = 34
BYTES_OVERHEAD = 10


class UnsupportedChain(ValueError):
    """Raised when asked to featurise a chain no model was trained on."""


def _stats(values: list[Decimal]) -> dict[str, float]:
    """Five-number summary. Empty input yields zeros, not an exception."""
    if not values:
        return {"min": 0.0, "max": 0.0, "mean": 0.0, "median": 0.0, "total": 0.0}

    numbers = sorted(float(v) for v in values)
    n = len(numbers)
    median = (
        numbers[n // 2] if n % 2 else (numbers[n // 2 - 1] + numbers[n // 2]) / 2.0
    )
    total = sum(numbers)
    return {
        "min": numbers[0],
        "max": numbers[-1],
        "mean": total / n,
        "median": median,
        "total": total,
    }


def estimate_size_bytes(tx: ChainTransaction) -> int:
    return (
        len(tx.inputs) * BYTES_PER_INPUT
        + len(tx.outputs) * BYTES_PER_OUTPUT
        + BYTES_OVERHEAD
    )


def extract(
    tx: ChainTransaction,
    in_degree: int = 0,
    out_degree: int = 0,
) -> dict[str, float]:
    """Feature dict for one transaction, keyed by the model's column names.

    `in_degree` / `out_degree` come from the graph (how many distinct
    counterparties the transaction's addresses have) and default to 0 when the
    caller has not computed them.

    Raises UnsupportedChain for any chain without a trained model.
    """
    if tx.chain not in SUPPORTED_CHAINS:
        raise UnsupportedChain(
            f"no trained model for chain {tx.chain!r}. "
            f"The classifier is trained on the Elliptic Bitcoin dataset; "
            f"scoring {tx.chain} would mean feeding constants and mismatched "
            f"units into a model that would return a confident, meaningless "
            f"number. Supported: {', '.join(sorted(SUPPORTED_CHAINS))}."
        )

    ins = _stats([i.value for i in tx.inputs])
    outs = _stats([o.value for o in tx.outputs])

    return {
        "in_txs_degree": float(in_degree),
        "out_txs_degree": float(out_degree),
        "total_BTC": float(tx.moved_value or 0),
        "fees": float(tx.fee or 0),
        "size": float(estimate_size_bytes(tx)),
        "num_input_addresses": float(len({i.address for i in tx.inputs})),
        "num_output_addresses": float(len({o.address for o in tx.outputs})),
        "in_BTC_min": ins["min"],
        "in_BTC_max": ins["max"],
        "in_BTC_mean": ins["mean"],
        "in_BTC_median": ins["median"],
        "in_BTC_total": ins["total"],
        "out_BTC_min": outs["min"],
        "out_BTC_max": outs["max"],
        "out_BTC_mean": outs["mean"],
        "out_BTC_median": outs["median"],
        "out_BTC_total": outs["total"],
    }


def to_vector(features: dict[str, float]) -> list[float]:
    """Order a feature dict into the model's expected column order."""
    missing = [name for name in FEATURE_ORDER if name not in features]
    if missing:
        raise ValueError(f"missing features: {missing}")
    return [features[name] for name in FEATURE_ORDER]


def verify_feature_order(artifact_dir: Path) -> None:
    """Assert FEATURE_ORDER matches the artifact's own column list.

    XGBoost binds features by position. If the trainer's column order and this
    module's ever diverge, every prediction becomes silently wrong while
    everything still loads, runs and returns plausible probabilities - the
    worst possible failure mode, so it is checked rather than assumed.
    """
    columns_path = artifact_dir / "feature_columns_servable.json"
    if not columns_path.exists():
        raise FileNotFoundError(f"{columns_path} not found - train with --servable first")

    trained = json.loads(columns_path.read_text(encoding="utf-8"))
    if trained != FEATURE_ORDER:
        raise ValueError(
            "feature order mismatch between tx_features.FEATURE_ORDER and "
            f"{columns_path.name}.\n  artifact: {trained}\n  module:   {FEATURE_ORDER}"
        )
