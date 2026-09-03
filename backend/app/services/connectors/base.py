"""Normalised blockchain data model and the connector interface.

Every connector - live indexer or synthetic - returns the same shape, so the
graph writer and the clustering heuristics never learn which chain or which API
the data came from.

The model keeps full input/output sets rather than flat transfers. UTXO
clustering (common-input-ownership, change-address) is impossible without them,
and account-model chains express cleanly as a single-input/single-output
transaction, so one shape covers both.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

# Chains whose transactions carry real UTXO structure worth clustering on.
UTXO_CHAINS = frozenset({"BTC"})

# Native currency symbol per chain, used when no token contract is involved.
NATIVE_SYMBOL = {"BTC": "BTC", "ETH": "ETH", "TRON": "TRX"}
NATIVE_DECIMALS = {"BTC": 8, "ETH": 18, "TRON": 6}

# Transaction outcome. Only `success` moved value.
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_UNKNOWN = "unknown"

# What kind of movement an entry represents.
TRANSFER_NATIVE = "native"
TRANSFER_TOKEN = "token"


@dataclass(frozen=True)
class Asset:
    """What actually moved.

    A bare symbol string is not an identity: on Ethereum anyone can deploy a
    contract calling itself "USDT". The contract address is the identity, and
    an investigator writing a case file needs it - "100 USDT" is only meaningful
    alongside the contract that issued it.

    `contract is None` means the chain's native currency (BTC, ETH, TRX), which
    has no contract by definition.
    """

    chain: str
    symbol: str
    contract: str | None = None
    decimals: int = 18

    @property
    def is_native(self) -> bool:
        return self.contract is None

    @property
    def key(self) -> str:
        """Stable identity, safe to group and compare on."""
        if self.contract is None:
            return f"{self.chain}:native"
        # ETH contracts are case-insensitive; normalise so the same token does
        # not appear twice under different capitalisation.
        contract = self.contract.lower() if self.chain == "ETH" else self.contract
        return f"{self.chain}:{contract}"

    @classmethod
    def native(cls, chain: str) -> Asset:
        return cls(
            chain=chain,
            symbol=NATIVE_SYMBOL.get(chain, chain),
            contract=None,
            decimals=NATIVE_DECIMALS.get(chain, 18),
        )


@dataclass(frozen=True)
class TxIO:
    """One side of a transaction: a spent input or a created output."""

    address: str
    value: Decimal
    index: int = 0
    is_change: bool = False


@dataclass
class ChainTransaction:
    """A single on-chain transaction, chain-agnostic."""

    chain: str
    txid: str
    timestamp: datetime
    inputs: list[TxIO] = field(default_factory=list)
    outputs: list[TxIO] = field(default_factory=list)
    block_height: int | None = None
    fee: Decimal = Decimal(0)
    asset: Asset | None = None
    #: `failed` transactions are recorded as attempts but move no value.
    status: str = STATUS_SUCCESS

    def __post_init__(self) -> None:
        if self.asset is None:
            self.asset = Asset.native(self.chain)

    @property
    def transfer_type(self) -> str:
        return TRANSFER_NATIVE if self.asset.is_native else TRANSFER_TOKEN

    @property
    def moved_value(self) -> bool:
        """A reverted transaction consumed gas but transferred nothing."""
        return self.status == STATUS_SUCCESS

    @property
    def is_utxo(self) -> bool:
        return self.chain in UTXO_CHAINS

    @property
    def input_addresses(self) -> list[str]:
        return [i.address for i in self.inputs]

    @property
    def output_addresses(self) -> list[str]:
        return [o.address for o in self.outputs]

    def total_out(self) -> Decimal:
        return sum((o.value for o in self.outputs), Decimal(0))


class ConnectorError(RuntimeError):
    """Raised when an upstream indexer fails or is misconfigured."""


class BlockchainConnector(ABC):
    """Fetches transactions for an address on one chain."""

    #: chain_t value this connector serves
    chain: str = ""
    #: recorded on trace_runs.data_source so provenance is always auditable
    source_name: str = ""

    @abstractmethod
    def get_transactions(self, address: str, limit: int = 50) -> list[ChainTransaction]:
        """Return transactions involving `address`, newest first."""

    def close(self) -> None:  # noqa: B027 - optional hook, not every connector holds a client
        """Release any underlying HTTP client. Override only if one is held."""
