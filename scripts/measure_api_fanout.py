#!/usr/bin/env python3
"""How many upstream indexer calls does ONE user request actually make?

A free Etherscan key allows 100,000 calls a day. If a single traced address
costs 1,400 of them, the quota is gone after seventy investigations - and the
person who spends it has no idea they did, because from their side it was one
click.

This counts real outbound HTTP calls during one expansion, without writing
anything to the graph.

Usage:
    python scripts/measure_api_fanout.py 0x899ac9...
    python scripts/measure_api_fanout.py 0x899ac9... --depth 8 --breadth 25
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter

import httpx

from app.config import get_settings
from app.services import chain_detect
from app.services.connectors import get_connector
from app.services.ingest import expand_money_flow


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("address")
    ap.add_argument("--depth", type=int, default=None)
    ap.add_argument("--breadth", type=int, default=None)
    ap.add_argument("--no-cache", action="store_true", help="measure the cold cost")
    args = ap.parse_args()

    settings = get_settings()
    depth = args.depth if args.depth is not None else settings.trace_max_depth
    breadth = args.breadth if args.breadth is not None else settings.trace_max_breadth

    info = chain_detect.detect(args.address)
    if not info.valid:
        print(f"invalid address: {args.address}", file=sys.stderr)
        return 1

    calls = Counter()
    original = httpx.Client.get

    def counting_get(self, url, **kwargs):
        calls[str(url).split("?")[0]] += 1
        return original(self, url, **kwargs)

    httpx.Client.get = counting_get  # noqa: B010

    if args.no_cache:
        from app.services.connectors import http as http_mod

        http_mod._cache_get = lambda key: None  # noqa: SLF001

    print(f"DEMO_MODE={settings.demo_mode}  depth={depth}  breadth={breadth}")
    print(f"tracing {args.address} ...\n", flush=True)

    started = time.time()
    try:
        result = expand_money_flow(
            get_connector(info.chain), info.chain, info.address_norm, depth, breadth
        )
    finally:
        httpx.Client.get = original  # noqa: B010

    elapsed = time.time() - started
    total = sum(calls.values())
    touched = result["addresses_touched"]

    print("=" * 62)
    print("Cost of ONE user request")
    print("=" * 62)
    print(f"  upstream HTTP calls   {total}")
    print(f"  addresses touched     {touched}")
    print(f"  transactions returned {len(result['transactions'])}")
    print(f"  wall clock            {elapsed:.1f}s")
    if total:
        print(f"  amplification         1 request -> {total} upstream calls")

    print("\n  by endpoint:")
    for url, n in calls.most_common(5):
        print(f"    {n:>5}  {url}")

    # What that costs against the free tiers.
    print("\n" + "=" * 62)
    print("Quota impact")
    print("=" * 62)
    for name, daily in (("Etherscan free", 100_000), ("TronGrid free (est/day)", 1_296_000)):
        if total:
            print(f"  {name:26} {daily // total:>7,} traces/day before exhaustion")
    if total > 100:
        print("\n  WARNING: this is amplification, not usage. A per-request")
        print("  budget should bound it regardless of graph shape.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
