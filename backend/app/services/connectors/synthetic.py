"""Synthetic connector - the DEMO_MODE data source.

Reads the dataset produced by scripts/generate_synthetic_complaints.py and
answers address queries from it. Serves fictional data only; `source_name` is
"synthetic" so every trace_run it feeds records that provenance in the database.

The dataset is loaded once and indexed by address, since the demo re-queries the
same small graph repeatedly.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from app.services.connectors.base import BlockchainConnector, ChainTransaction, ConnectorError, TxIO

logger = logging.getLogger(__name__)

# Docker mount first, then the repo-relative path for running outside a container.
_DATASET_CANDIDATES = (
    Path("/app/ml_seeds/synthetic_dataset.json"),
    Path(__file__).resolve().parents[4] / "ml" / "seeds" / "synthetic_dataset.json",
)

_cache: dict | None = None
_index: dict[tuple[str, str], list[ChainTransaction]] | None = None


# Shared normalisation - see chain_detect.normalize_address. The index MUST use
# it, or an ETH lookup by normalised address misses every checksummed key.
from app.services.chain_detect import normalize_address as _norm  # noqa: E402


def dataset_path() -> Path | None:
    for p in _DATASET_CANDIDATES:
        if p.exists():
            return p
    return None


def _parse_tx(raw: dict) -> ChainTransaction:
    return ChainTransaction(
        chain=raw["chain"],
        txid=raw["txid"],
        timestamp=datetime.fromisoformat(raw["timestamp"]),
        block_height=raw.get("block_height"),
        fee=Decimal(str(raw.get("fee", 0))),
        asset=raw.get("asset", ""),
        inputs=[
            TxIO(address=i["address"], value=Decimal(str(i["value"])), index=i.get("index", 0))
            for i in raw["inputs"]
        ],
        outputs=[
            TxIO(
                address=o["address"],
                value=Decimal(str(o["value"])),
                index=o.get("index", 0),
                is_change=bool(o.get("is_change", False)),
            )
            for o in raw["outputs"]
        ],
    )


def load_dataset(force: bool = False) -> dict:
    """Load and cache the synthetic dataset."""
    global _cache, _index
    if _cache is not None and not force:
        return _cache

    path = dataset_path()
    if path is None:
        raise ConnectorError(
            "synthetic dataset not found - run: "
            "docker compose exec backend python scripts/generate_synthetic_complaints.py "
            "--out /app/ml_seeds/synthetic_dataset.json"
        )

    data = json.loads(path.read_text(encoding="utf-8"))
    idx: dict[tuple[str, str], list[ChainTransaction]] = defaultdict(list)
    for raw in data["transactions"]:
        tx = _parse_tx(raw)
        for io in tx.inputs + tx.outputs:
            idx[(tx.chain, _norm(tx.chain, io.address))].append(tx)

    # Newest first, matching what the live indexers return.
    for key in idx:
        idx[key].sort(key=lambda t: t.timestamp, reverse=True)

    _cache, _index = data, dict(idx)
    logger.info(
        "synthetic dataset loaded from %s: %d transactions, %d addresses",
        path,
        len(data["transactions"]),
        len(idx),
    )
    return _cache


def get_complaints() -> list[dict]:
    return load_dataset().get("complaints", [])


def get_entities() -> list[dict]:
    return load_dataset().get("entities", [])


class SyntheticConnector(BlockchainConnector):
    """Serves the generated fraud-ring dataset for a single chain."""

    source_name = "synthetic"

    def __init__(self, chain: str):
        self.chain = chain
        load_dataset()

    def get_transactions(self, address: str, limit: int = 50) -> list[ChainTransaction]:
        assert _index is not None  # load_dataset() populates it
        return _index.get((self.chain, _norm(self.chain, address)), [])[:limit]
