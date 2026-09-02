"""Live indexer connectors: Etherscan (ETH), TronGrid (TRON), Blockchair (BTC).

Used only when DEMO_MODE=false and the corresponding API key is configured.
Deliberately no full nodes - these are public indexer APIs.

Each connector normalises into the same ChainTransaction shape as the synthetic
connector. Account-model chains (ETH, TRON) produce one input and one output per
transfer; Blockchair returns real UTXO input/output sets for Bitcoin.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal

import httpx

from app.config import get_settings
from app.services.connectors.base import BlockchainConnector, ChainTransaction, ConnectorError, TxIO

logger = logging.getLogger(__name__)
settings = get_settings()

_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


class EtherscanConnector(BlockchainConnector):
    """Ethereum via the Etherscan v2 API."""

    chain = "ETH"
    source_name = "etherscan"
    BASE_URL = "https://api.etherscan.io/v2/api"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or settings.etherscan_api_key
        if not self.api_key:
            raise ConnectorError("ETHERSCAN_API_KEY is not configured")
        self._client = httpx.Client(timeout=_TIMEOUT)

    def get_transactions(self, address: str, limit: int = 50) -> list[ChainTransaction]:
        params = {
            "chainid": 1,
            "module": "account",
            "action": "txlist",
            "address": address,
            "startblock": 0,
            "endblock": 99999999,
            "page": 1,
            "offset": limit,
            "sort": "desc",
            "apikey": self.api_key,
        }
        try:
            resp = self._client.get(self.BASE_URL, params=params)
            resp.raise_for_status()
            payload = resp.json()
        except httpx.HTTPError as exc:
            raise ConnectorError(f"etherscan request failed: {exc}") from exc

        # Etherscan answers "No transactions found" with status "0" - not an error.
        if payload.get("status") != "1":
            message = payload.get("message", "")
            if "No transactions found" in message:
                return []
            raise ConnectorError(f"etherscan error: {message or payload}")

        out: list[ChainTransaction] = []
        for tx in payload.get("result", []):
            if not tx.get("to"):  # contract creation - no recipient to trace
                continue
            value = Decimal(tx.get("value", "0")) / Decimal(10**18)
            out.append(
                ChainTransaction(
                    chain=self.chain,
                    txid=tx["hash"],
                    timestamp=datetime.fromtimestamp(int(tx["timeStamp"]), tz=UTC),
                    block_height=int(tx.get("blockNumber", 0)) or None,
                    fee=Decimal(tx.get("gasUsed", "0")) * Decimal(tx.get("gasPrice", "0"))
                    / Decimal(10**18),
                    asset="ETH",
                    inputs=[TxIO(address=tx["from"].lower(), value=value)],
                    outputs=[TxIO(address=tx["to"].lower(), value=value)],
                )
            )
        return out

    def close(self) -> None:
        self._client.close()


class TronGridConnector(BlockchainConnector):
    """TRON via TronGrid. Covers TRX and TRC-20 (USDT-TRC20 in particular)."""

    chain = "TRON"
    source_name = "trongrid"
    BASE_URL = "https://api.trongrid.io"
    USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or settings.trongrid_api_key
        headers = {"TRON-PRO-API-KEY": self.api_key} if self.api_key else {}
        self._client = httpx.Client(timeout=_TIMEOUT, headers=headers)

    def get_transactions(self, address: str, limit: int = 50) -> list[ChainTransaction]:
        url = f"{self.BASE_URL}/v1/accounts/{address}/transactions/trc20"
        try:
            resp = self._client.get(url, params={"limit": min(limit, 200)})
            resp.raise_for_status()
            payload = resp.json()
        except httpx.HTTPError as exc:
            raise ConnectorError(f"trongrid request failed: {exc}") from exc

        if not payload.get("success", True):
            raise ConnectorError(f"trongrid error: {payload.get('error', payload)}")

        out: list[ChainTransaction] = []
        for tx in payload.get("data", []):
            info = tx.get("token_info", {}) or {}
            decimals = int(info.get("decimals", 6))
            value = Decimal(str(tx.get("value", "0"))) / Decimal(10**decimals)
            out.append(
                ChainTransaction(
                    chain=self.chain,
                    txid=tx["transaction_id"],
                    timestamp=datetime.fromtimestamp(
                        int(tx["block_timestamp"]) / 1000, tz=UTC
                    ),
                    asset=info.get("symbol", "TRC20"),
                    inputs=[TxIO(address=tx["from"], value=value)],
                    outputs=[TxIO(address=tx["to"], value=value)],
                )
            )
        return out

    def close(self) -> None:
        self._client.close()


class BlockchairConnector(BlockchainConnector):
    """Bitcoin via Blockchair. Works keyless at low rates; a key raises limits.

    Returns genuine UTXO input/output sets, which is what the clustering
    heuristics need.
    """

    chain = "BTC"
    source_name = "blockchair"
    BASE_URL = "https://api.blockchair.com/bitcoin"
    SATS = Decimal(10**8)

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or settings.blockchair_api_key
        self._client = httpx.Client(timeout=_TIMEOUT)

    def _params(self, **extra) -> dict:
        params = dict(extra)
        if self.api_key:
            params["key"] = self.api_key
        return params

    def get_transactions(self, address: str, limit: int = 50) -> list[ChainTransaction]:
        try:
            resp = self._client.get(
                f"{self.BASE_URL}/dashboards/address/{address}",
                params=self._params(limit=min(limit, 100)),
            )
            resp.raise_for_status()
            addr_data = resp.json().get("data", {}).get(address, {})
        except httpx.HTTPError as exc:
            raise ConnectorError(f"blockchair request failed: {exc}") from exc

        txids = (addr_data or {}).get("transactions", [])[:limit]
        if not txids:
            return []

        # Blockchair caps the multi-transaction dashboard at 10 hashes per call.
        out: list[ChainTransaction] = []
        for batch_start in range(0, len(txids), 10):
            batch = txids[batch_start : batch_start + 10]
            try:
                resp = self._client.get(
                    f"{self.BASE_URL}/dashboards/transactions/{','.join(batch)}",
                    params=self._params(),
                )
                resp.raise_for_status()
                data = resp.json().get("data", {})
            except httpx.HTTPError as exc:
                raise ConnectorError(f"blockchair tx fetch failed: {exc}") from exc

            for txid, entry in data.items():
                tx = entry.get("transaction", {})
                out.append(
                    ChainTransaction(
                        chain=self.chain,
                        txid=txid,
                        timestamp=datetime.fromisoformat(tx["time"]).replace(tzinfo=UTC),
                        block_height=tx.get("block_id"),
                        fee=Decimal(str(tx.get("fee", 0))) / self.SATS,
                        asset="BTC",
                        inputs=[
                            TxIO(
                                address=i.get("recipient", ""),
                                value=Decimal(str(i.get("value", 0))) / self.SATS,
                                index=idx,
                            )
                            for idx, i in enumerate(entry.get("inputs", []))
                            if i.get("recipient")
                        ],
                        outputs=[
                            TxIO(
                                address=o.get("recipient", ""),
                                value=Decimal(str(o.get("value", 0))) / self.SATS,
                                index=idx,
                            )
                            for idx, o in enumerate(entry.get("outputs", []))
                            if o.get("recipient")
                        ],
                    )
                )
        out.sort(key=lambda t: t.timestamp, reverse=True)
        return out

    def close(self) -> None:
        self._client.close()
