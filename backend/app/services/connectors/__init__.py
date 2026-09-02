"""Connector factory.

DEMO_MODE decides the data source, and the choice is reported back to the caller
so `trace_runs.data_source` records honestly where the evidence came from. If a
live connector is requested but unconfigured, we fall back to synthetic and say
so - we never silently present synthetic data as live-chain data.
"""

from __future__ import annotations

import logging

from app.config import get_settings
from app.services.chain_detect import CHAIN_BTC, CHAIN_ETH, CHAIN_TRON
from app.services.connectors.base import (
    UTXO_CHAINS,
    BlockchainConnector,
    ChainTransaction,
    ConnectorError,
    TxIO,
)
from app.services.connectors.live import (
    BlockchairConnector,
    EtherscanConnector,
    TronGridConnector,
)
from app.services.connectors.synthetic import SyntheticConnector

logger = logging.getLogger(__name__)

_LIVE = {
    CHAIN_ETH: EtherscanConnector,
    CHAIN_TRON: TronGridConnector,
    CHAIN_BTC: BlockchairConnector,
}


def get_connector(chain: str, demo_mode: bool | None = None) -> BlockchainConnector:
    """Return the connector for `chain`. Falls back to synthetic, loudly."""
    settings = get_settings()
    demo = settings.demo_mode if demo_mode is None else demo_mode

    if chain not in _LIVE:
        raise ConnectorError(f"unsupported chain: {chain}")

    if demo:
        return SyntheticConnector(chain)

    try:
        return _LIVE[chain]()
    except ConnectorError as exc:
        logger.warning(
            "live connector for %s unavailable (%s) - falling back to SYNTHETIC data", chain, exc
        )
        return SyntheticConnector(chain)


__all__ = [
    "BlockchainConnector",
    "BlockchairConnector",
    "ChainTransaction",
    "ConnectorError",
    "EtherscanConnector",
    "SyntheticConnector",
    "TronGridConnector",
    "TxIO",
    "UTXO_CHAINS",
    "get_connector",
]
