"""Shared test fixtures.

Clustering is Cypher and GDS, so testing it against a mock would test the mock.
These tests run against the real Neo4j from the compose stack and skip cleanly
when it is not reachable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.connectors.base import ChainTransaction, TxIO

BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture(scope="session")
def neo4j_available() -> bool:
    from app.db import neo4j as neo4j_db

    try:
        neo4j_db.ping()
        return True
    except Exception:
        return False


@pytest.fixture
def graph(neo4j_available):
    """Empty graph before and after each test that uses it."""
    if not neo4j_available:
        pytest.skip("Neo4j not reachable - start the compose stack to run graph tests")

    from app.services import graph_writer

    # Scoped cleanup: a full wipe would destroy the seeded tagged-address
    # database (OFAC / Etherscan labels / WalletExplorer / TagPacks), which is
    # reference data rather than test data.
    graph_writer.clear_test_data()
    yield graph_writer
    graph_writer.clear_test_data()


def tx(
    txid: str,
    inputs: list[tuple[str, float]],
    outputs: list[tuple[str, float]],
    minutes: int = 0,
    chain: str = "BTC",
) -> ChainTransaction:
    """Build a ChainTransaction concisely. `minutes` offsets from BASE_TIME."""
    return ChainTransaction(
        chain=chain,
        txid=txid,
        timestamp=BASE_TIME + timedelta(minutes=minutes),
        block_height=800_000 + minutes,
        fee=Decimal("0.0001"),
        asset=chain,
        inputs=[TxIO(address=a, value=Decimal(str(v)), index=i) for i, (a, v) in enumerate(inputs)],
        outputs=[
            TxIO(address=a, value=Decimal(str(v)), index=i) for i, (a, v) in enumerate(outputs)
        ],
    )
