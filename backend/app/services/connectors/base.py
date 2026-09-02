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
    asset: str = ""

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
