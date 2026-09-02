#!/usr/bin/env python3
"""Fetch the public datasets this project trains and seeds from.

Sources, all public:
  * Elliptic Bitcoin Dataset (via the Elliptic++ mirror, git-disl/EllipticPlusPlus)
      Transaction features + illicit/licit labels. Used to train and validate the
      fraud classifier.
  * OFAC SDN list (US Treasury)
      Sanctioned digital-currency addresses. Real tags for the attribution DB.
  * GraphSense TagPacks
      Community-curated address->entity attributions.

Disk note: the Elliptic feature matrix is ~695 MB, but only ~46k of its 203k
transactions carry a label. We stream it over HTTP and keep only the labelled
rows, written gzipped, so the full file never touches disk. On a constrained
machine that is the difference between ~30 MB and ~695 MB of footprint.

Usage:
    python ml/src/download_data.py elliptic
    python ml/src/download_data.py ofac
    python ml/src/download_data.py tagpacks
    python ml/src/download_data.py all
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
SEED_DIR = Path(__file__).resolve().parents[1] / "seeds"

ELLIPTIC_BASE = "https://media.githubusercontent.com/media/git-disl/EllipticPlusPlus/main/"
ELLIPTIC_CLASSES = "Transactions Dataset/txs_classes.csv"
ELLIPTIC_FEATURES = "Transactions Dataset/txs_features.csv"
ELLIPTIC_EDGES = "Transactions Dataset/txs_edgelist.csv"

OFAC_SDN_URL = "https://www.treasury.gov/ofac/downloads/sdn.xml"

TAGPACK_BASE = (
    "https://raw.githubusercontent.com/graphsense/graphsense-tagpacks/master/packs/"
)
# Curated subset of the GraphSense TagPacks repo. These cover the exact public
# sources named in the project brief: Etherscan Label Cloud (the
# "etherscan-wordcloud-*" packs), WalletExplorer, and OFAC. The very large packs
# in that repo (defi-fraud-masterthesis.yaml et al.) are skipped deliberately -
# they add bulk without adding VASP coverage.
TAGPACKS = [
    # --- exchanges / VASPs ---
    "walletexplorer.yaml",
    "etherscan-wordcloud-exchange.yaml",
    "exchange-wallets-binance.yaml",
    "exchange-wallets-huobi.yaml",
    "exchange-wallets-kucoin.yaml",
    "exchange-wallets-okx.yaml",
    "exchange-wallets-bybit.yaml",
    "exchange-wallets-cryptocom.yaml",
    "exchange-wallets-bitfinexcom.yaml",
    "exchange-wallets-deribit.yaml",
    "exchange-wallets-swissborg.yaml",
    "binance.yaml",
    # --- mixers (flagged as a risk signal, never unwound) ---
    "etherscan-wordcloud-mixing_service.yaml",
    "tornado_cash.yaml",
    "blender_io.yaml",
    "wasabi_collector.yaml",
    "sinbad_io.yaml",
    # --- sanctions / other services ---
    "ofac.yaml",
    "etherscan-wordcloud-gambling.yaml",
    "etherscan-wordcloud-market.yaml",
]

# Elliptic label encoding: 1 = illicit, 2 = licit, 3/unknown = unlabelled.
LABEL_ILLICIT, LABEL_LICIT = "1", "2"

_UA = {"User-Agent": "sih183-dataset-fetch"}


def _open(url: str, timeout: int = 120):
    return urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=timeout)


# ---------------------------------------------------------------------------
# Elliptic
# ---------------------------------------------------------------------------
def fetch_elliptic(out_dir: Path = DATA_DIR) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)

    print("[elliptic] fetching class labels ...", flush=True)
    with _open(ELLIPTIC_BASE + urllib.parse.quote(ELLIPTIC_CLASSES)) as r:
        text = r.read().decode("utf-8")
    labels: dict[str, str] = {}
    for row in csv.DictReader(io.StringIO(text)):
        cls = row["class"].strip()
        if cls in (LABEL_ILLICIT, LABEL_LICIT):
            labels[row["txId"].strip()] = cls
    print(
        f"[elliptic] {len(labels)} labelled transactions "
        f"({sum(1 for v in labels.values() if v == LABEL_ILLICIT)} illicit)",
        flush=True,
    )

    out_path = out_dir / "elliptic_labeled.csv.gz"
    print(f"[elliptic] streaming feature matrix -> {out_path} (keeping labelled rows only)",
          flush=True)

    started = time.time()
    kept = seen = 0
    url = ELLIPTIC_BASE + urllib.parse.quote(ELLIPTIC_FEATURES)

    with _open(url, timeout=600) as resp, gzip.open(out_path, "wt", newline="") as gz:
        stream = io.TextIOWrapper(resp, encoding="utf-8", newline="")
        reader = csv.reader(stream)
        writer = csv.writer(gz)

        header = next(reader)
        writer.writerow(header + ["label"])

        for row in reader:
            seen += 1
            label = labels.get(row[0].strip())
            if label is not None:
                writer.writerow(row + [label])
                kept += 1
            if seen % 25_000 == 0:
                print(f"[elliptic]   scanned {seen} rows, kept {kept} "
                      f"({time.time() - started:.0f}s)", flush=True)

    meta = {
        "source": "Elliptic++ mirror of the Elliptic Bitcoin Dataset (public)",
        "source_url": ELLIPTIC_BASE + ELLIPTIC_FEATURES,
        "rows_scanned": seen,
        "rows_kept": kept,
        "feature_columns": len(header) - 2,  # minus txId and Time step
        "illicit": sum(1 for v in labels.values() if v == LABEL_ILLICIT),
        "licit": sum(1 for v in labels.values() if v == LABEL_LICIT),
        "elapsed_seconds": round(time.time() - started, 1),
        "output": str(out_path),
        "output_bytes": out_path.stat().st_size,
    }
    (out_dir / "elliptic_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"[elliptic] done: kept {kept}/{seen} rows in "
          f"{meta['output_bytes'] / 1e6:.1f} MB ({meta['elapsed_seconds']}s)", flush=True)
    return meta


# ---------------------------------------------------------------------------
# OFAC SDN - sanctioned digital currency addresses
# ---------------------------------------------------------------------------
# Currency codes OFAC uses in "Digital Currency Address - XXX" ID types, mapped
# to the chains this system supports. Others are recorded but not chain-tagged.
_OFAC_CHAIN = {"XBT": "BTC", "ETH": "ETH", "USDT": "TRON", "TRX": "TRON"}


def fetch_ofac(out_dir: Path = SEED_DIR) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    print("[ofac] fetching SDN list ...", flush=True)
    with _open(OFAC_SDN_URL, timeout=300) as r:
        raw = r.read()

    root = ET.fromstring(raw)
    ns = {"s": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}

    def find_all(node, tag):
        return node.findall(f"s:{tag}", ns) if ns else node.findall(tag)

    def text_of(node, tag):
        el = node.find(f"s:{tag}", ns) if ns else node.find(tag)
        return el.text.strip() if el is not None and el.text else None

    entries = []
    for entry in find_all(root, "sdnEntry"):
        name = text_of(entry, "lastName") or text_of(entry, "firstName") or "unknown"
        id_list = entry.find("s:idList", ns) if ns else entry.find("idList")
        if id_list is None:
            continue
        for ident in find_all(id_list, "id"):
            id_type = text_of(ident, "idType") or ""
            if not id_type.startswith("Digital Currency Address"):
                continue
            address = text_of(ident, "idNumber")
            if not address:
                continue
            code = id_type.rsplit("-", 1)[-1].strip().upper()
            entries.append(
                {
                    "address": address,
                    "currency_code": code,
                    "chain": _OFAC_CHAIN.get(code),
                    "entity_name": name,
                    "entity_type": "sanctioned",
                    "source": "ofac_sdn",
                    "confidence": 1.0,
                }
            )

    out_path = out_dir / "ofac_sdn_addresses.json"
    payload = {
        "meta": {
            "source": "US Treasury OFAC Specially Designated Nationals list (public)",
            "source_url": OFAC_SDN_URL,
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "count": len(entries),
        },
        "addresses": entries,
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    by_chain: dict[str, int] = {}
    for e in entries:
        by_chain[e["chain"] or e["currency_code"]] = by_chain.get(e["chain"] or e["currency_code"], 0) + 1
    print(f"[ofac] wrote {len(entries)} sanctioned addresses -> {out_path}")
    print(f"[ofac] by chain/code: {by_chain}")
    return payload["meta"]


# ---------------------------------------------------------------------------
# GraphSense TagPacks
# ---------------------------------------------------------------------------
def fetch_tagpacks(out_dir: Path = SEED_DIR) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    fetched = []
    for pack in TAGPACKS:
        url = TAGPACK_BASE + pack
        try:
            with _open(url, timeout=120) as r:
                body = r.read().decode("utf-8")
        except Exception as exc:
            print(f"[tagpacks] skip {pack}: {type(exc).__name__} {exc}")
            continue
        (out_dir / pack).write_text(body, encoding="utf-8")
        fetched.append({"pack": pack, "bytes": len(body), "url": url})
        print(f"[tagpacks] {pack}: {len(body)} bytes")

    meta = {
        "source": "GraphSense TagPacks (public, community-curated)",
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "packs": fetched,
    }
    (out_dir / "tagpacks_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("target", choices=["elliptic", "ofac", "tagpacks", "all"])
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    ap.add_argument("--seed-dir", type=Path, default=SEED_DIR)
    args = ap.parse_args()

    if args.target in ("elliptic", "all"):
        fetch_elliptic(args.data_dir)
    if args.target in ("ofac", "all"):
        fetch_ofac(args.seed_dir)
    if args.target in ("tagpacks", "all"):
        fetch_tagpacks(args.seed_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
