#!/usr/bin/env python3
"""Fetch real transactions from a live indexer and report what actually arrives.

Written for one specific question: the servable fraud classifier needs 17
features computed from a ChainTransaction, and those features were chosen by
looking at the Elliptic dataset, not at what our own connectors return. If the
live connectors leave a field empty that the synthetic ones populate, the
extractor would compute a feature from nothing and the model would score
confidently on a zero.

So this prints, per transaction, which fields are present and which are None -
before any extractor is written against them.

Usage:
    python scripts/inspect_live_tx.py 0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045
    python scripts/inspect_live_tx.py <btc address> --chain BTC
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import fields as dataclass_fields

from app.config import get_settings
from app.services import chain_detect
from app.services.connectors import get_connector

# The features the servable model consumes, and where each would come from.
SERVABLE_SOURCES = {
    "in_txs_degree": "graph (neighbours)",
    "out_txs_degree": "graph (neighbours)",
    "total_BTC": "tx.moved_value",
    "fees": "tx.fee",
    "size": "NOT ON ChainTransaction",
    "num_input_addresses": "len(tx.inputs)",
    "num_output_addresses": "len(tx.outputs)",
    "in_BTC_*": "tx.inputs[].value",
    "out_BTC_*": "tx.outputs[].value",
}


def describe(value) -> str:
    if value is None:
        return "None"
    if isinstance(value, list):
        return f"list[{len(value)}]"
    text = str(value)
    return text if len(text) <= 46 else text[:43] + "..."


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("address")
    ap.add_argument("--chain", default=None, help="override chain detection")
    ap.add_argument("--limit", type=int, default=3, help="transactions to show")
    args = ap.parse_args()

    settings = get_settings()
    print(f"DEMO_MODE={settings.demo_mode}  ->  ", end="")
    print("SYNTHETIC connectors" if settings.demo_mode else "LIVE indexer APIs")
    if settings.demo_mode:
        print("\nWARNING: this is not testing the live path. Set DEMO_MODE=false.")

    info = chain_detect.detect(args.address)
    if not info.valid:
        print(f"invalid address: {args.address}", file=sys.stderr)
        return 1
    chain = args.chain or info.chain
    print(f"address {args.address}\nchain   {chain} ({info.address_kind})\n")

    connector = get_connector(chain)
    print(f"connector {type(connector).__name__}, fetching...", flush=True)
    txs = connector.get_transactions(info.address_norm)
    print(f"returned {len(txs)} transactions\n")

    if not txs:
        print("no transactions - pick a busier address")
        return 0

    print("=" * 68)
    print("ChainTransaction fields on live data")
    print("=" * 68)
    for tx in txs[: args.limit]:
        print(f"\n--- {tx.txid[:24]}... ---")
        for f in dataclass_fields(tx):
            print(f"  {f.name:16} = {describe(getattr(tx, f.name))}")
        print(f"  {'moved_value':16} = {describe(tx.moved_value)}   (property)")
        print(f"  {'transfer_type':16} = {describe(tx.transfer_type)}   (property)")
        if tx.inputs:
            print(f"  inputs[0]        = {tx.inputs[0]}")
        if tx.outputs:
            print(f"  outputs[0]       = {tx.outputs[0]}")

    # The part that decides whether the feature extractor is viable.
    print("\n" + "=" * 68)
    print("Servable-feature availability across all returned transactions")
    print("=" * 68)
    n = len(txs)
    checks = {
        "tx.fee is not None": sum(1 for t in txs if t.fee is not None),
        "tx.inputs non-empty": sum(1 for t in txs if t.inputs),
        "tx.outputs non-empty": sum(1 for t in txs if t.outputs),
        "tx.moved_value truthy": sum(1 for t in txs if t.moved_value),
        "tx.timestamp is not None": sum(1 for t in txs if t.timestamp is not None),
        "tx.block_height is not None": sum(1 for t in txs if t.block_height is not None),
        "input values non-null": sum(
            1 for t in txs if t.inputs and all(i.value is not None for i in t.inputs)
        ),
        "output values non-null": sum(
            1 for t in txs if t.outputs and all(o.value is not None for o in t.outputs)
        ),
    }
    for label, count in checks.items():
        mark = "ok  " if count == n else "GAP "
        print(f"  [{mark}] {label:30} {count}/{n}")

    print("\nFeature sources:")
    for feature, source in SERVABLE_SOURCES.items():
        flag = "  <-- must be derived or dropped" if "NOT ON" in source else ""
        print(f"  {feature:22} {source}{flag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
