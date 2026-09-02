#!/usr/bin/env python3
"""Load the tagged-address database from the fetched public sources.

Inputs (produced by ml/src/download_data.py, all public):
  * ml/seeds/ofac_sdn_addresses.json  - US Treasury OFAC SDN
  * ml/seeds/*.yaml                   - GraphSense TagPacks, which include the
                                        Etherscan Label Cloud and WalletExplorer
                                        derived packs
  * ml/seeds/synthetic_dataset.json   - the fictional demo entities, tagged with
                                        source="synthetic" so they are always
                                        distinguishable from real-world tags

Writes `entities` and `tagged_addresses` in Postgres, and mirrors the tags into
Neo4j as (:Address)-[:TAGGED_AS]->(:Entity) so attribution can be answered by a
graph query during a trace.

Every tag records its `source`. Nothing in this system displays an attribution
without being able to say where the tag came from.

Usage:
    python ml/src/seed_tags.py            # load everything found
    python ml/src/seed_tags.py --dry-run  # parse and report, write nothing
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
SEED_DIR_CANDIDATES = [Path("/app/ml_seeds"), REPO / "ml" / "seeds"]

# TagPack `category` values -> our entity_type_t enum.
CATEGORY_MAP = {
    "exchange": "exchange",
    "centralized_exchange": "exchange",
    "decentralized_exchange": "exchange",
    "mixing_service": "mixer",
    "mixer": "mixer",
    "coinjoin": "mixer",
    "gambling": "gambling",
    "darknet_market": "darknet",
    "market": "darknet",
    "sanctions": "sanctioned",
    "payment_processor": "payment_processor",
    "merchant_services": "payment_processor",
    "miner": "unknown",
    "mining_pool": "unknown",
    "hosted_wallet": "payment_processor",
    "wallet_service": "payment_processor",
}

# TagPack currency codes -> chain_t. Anything else is skipped: we cannot trace
# a chain we have no connector for, and a tag we cannot use is noise.
CURRENCY_MAP = {"BTC": "BTC", "ETH": "ETH", "TRX": "TRON", "USDT": "TRON"}


def seed_dir() -> Path:
    for p in SEED_DIR_CANDIDATES:
        if p.exists():
            return p
    raise SystemExit("no seeds directory found")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def _norm(chain: str, address: str) -> str:
    return address.lower() if chain == "ETH" else address


def parse_ofac(path: Path) -> list[dict]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for row in payload.get("addresses", []):
        chain = row.get("chain")
        if chain not in CURRENCY_MAP.values():
            continue  # sanctioned address on a chain we do not trace
        out.append(
            {
                "chain": chain,
                "address_norm": _norm(chain, row["address"]),
                "entity_name": row["entity_name"],
                "entity_type": "sanctioned",
                "label": f"OFAC SDN: {row['entity_name']}",
                "source": "ofac_sdn",
                "confidence": 1.0,
            }
        )
    return out


def parse_tagpack(path: Path) -> list[dict]:
    """Parse one GraphSense TagPack YAML.

    Tags inherit pack-level defaults (label, category, currency) unless the tag
    overrides them - that inheritance is part of the TagPack spec.
    """
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"  ! {path.name}: unparseable ({type(exc).__name__})")
        return []
    if not isinstance(doc, dict):
        return []

    pack_label = doc.get("label") or path.stem
    pack_category = doc.get("category")
    pack_currency = doc.get("currency")
    pack_source = doc.get("source") or ""

    # Derive a provenance name that names the upstream source honestly.
    stem = path.stem.lower()
    if stem.startswith("etherscan"):
        source = "etherscan_labels"
    elif stem == "walletexplorer":
        source = "walletexplorer"
    elif stem == "ofac":
        source = "graphsense_ofac"
    else:
        source = "graphsense_tagpacks"

    out: list[dict] = []
    for tag in doc.get("tags") or []:
        if not isinstance(tag, dict):
            continue
        address = tag.get("address")
        if not address or not isinstance(address, str):
            continue

        currency = (tag.get("currency") or pack_currency or "").upper()
        chain = CURRENCY_MAP.get(currency)
        if chain is None:
            continue

        category = (tag.get("category") or pack_category or "").lower()
        entity_type = CATEGORY_MAP.get(category, "unknown")

        label = tag.get("label") or pack_label
        entity_name = str(label).strip()
        if not entity_name:
            continue

        out.append(
            {
                "chain": chain,
                "address_norm": _norm(chain, address.strip()),
                "entity_name": entity_name,
                "entity_type": entity_type,
                "label": str(tag.get("label") or label),
                "source": source,
                "confidence": float(tag.get("confidence", 0.8))
                if isinstance(tag.get("confidence"), (int, float))
                else 0.8,
                "pack_source": pack_source,
            }
        )
    return out


def parse_synthetic(path: Path) -> list[dict]:
    """Tag the fictional demo exchanges, clearly marked as synthetic."""
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for entity in data.get("entities", []):
        for addr in entity.get("addresses", []):
            chain = addr["chain"]
            out.append(
                {
                    "chain": chain,
                    "address_norm": _norm(chain, addr["address"]),
                    "entity_name": entity["name"],
                    "entity_type": entity["entity_type"],
                    "label": addr.get("label") or entity["name"],
                    "source": "synthetic",
                    "confidence": 1.0,
                }
            )
    return out


def collect(seeds: Path) -> list[dict]:
    tags: list[dict] = []

    ofac = parse_ofac(seeds / "ofac_sdn_addresses.json")
    print(f"  ofac_sdn_addresses.json -> {len(ofac)} tags")
    tags += ofac

    for yml in sorted(seeds.glob("*.yaml")):
        parsed = parse_tagpack(yml)
        if parsed:
            print(f"  {yml.name} -> {len(parsed)} tags")
        tags += parsed

    synth = parse_synthetic(seeds / "synthetic_dataset.json")
    print(f"  synthetic_dataset.json -> {len(synth)} tags")
    tags += synth

    # Deduplicate on the tagged_addresses unique key.
    seen: set[tuple[str, str, str]] = set()
    unique = []
    for t in tags:
        key = (t["chain"], t["address_norm"], t["source"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(t)
    return unique


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def load(tags: list[dict]) -> dict:
    from sqlalchemy import select

    from app.db.neo4j import get_driver
    from app.db.postgres import SessionLocal
    from app.models import Entity, TaggedAddress

    entity_ids: dict[tuple[str, str], str] = {}
    inserted = skipped = 0

    with SessionLocal() as db:
        # --- entities ---
        for t in tags:
            key = (t["entity_name"], t["entity_type"])
            if key in entity_ids:
                continue
            existing = db.execute(
                select(Entity).where(
                    Entity.name == t["entity_name"], Entity.entity_type == t["entity_type"]
                )
            ).scalar_one_or_none()
            if existing is None:
                existing = Entity(name=t["entity_name"], entity_type=t["entity_type"])
                db.add(existing)
                db.flush()
            entity_ids[key] = str(existing.id)
        db.commit()

        # --- tagged addresses ---
        existing_keys = {
            (c, a, s)
            for c, a, s in db.execute(
                select(TaggedAddress.chain, TaggedAddress.address_norm, TaggedAddress.source)
            ).all()
        }
        for t in tags:
            key = (t["chain"], t["address_norm"], t["source"])
            if key in existing_keys:
                skipped += 1
                continue
            db.add(
                TaggedAddress(
                    address_norm=t["address_norm"],
                    chain=t["chain"],
                    entity_id=entity_ids[(t["entity_name"], t["entity_type"])],
                    label=(t.get("label") or "")[:500],
                    source=t["source"],
                    confidence=min(max(t.get("confidence", 0.8), 0.0), 1.0),
                )
            )
            existing_keys.add(key)
            inserted += 1
        db.commit()

    # --- mirror into Neo4j -------------------------------------------------
    # Attribution runs during a trace, so the tags have to be answerable by a
    # graph query rather than a round trip to Postgres per address.
    rows = [
        {
            "chain": t["chain"],
            "address_norm": t["address_norm"],
            "entity_id": entity_ids[(t["entity_name"], t["entity_type"])],
            "name": t["entity_name"],
            "entity_type": t["entity_type"],
            "source": t["source"],
            "confidence": min(max(t.get("confidence", 0.8), 0.0), 1.0),
        }
        for t in tags
    ]

    query = """
    UNWIND $rows AS row
    MERGE (e:Entity {entity_id: row.entity_id})
      ON CREATE SET e.name = row.name, e.entity_type = row.entity_type
    MERGE (a:Address {chain: row.chain, address_norm: row.address_norm})
      ON CREATE SET a.address = row.address_norm, a.tx_count = 0
    SET a.entity_type = row.entity_type,
        a.is_mixer = (row.entity_type = 'mixer')
    MERGE (a)-[t:TAGGED_AS {source: row.source}]->(e)
      ON CREATE SET t.confidence = row.confidence
    RETURN count(t) AS tagged
    """
    tagged = 0
    with get_driver().session() as session:
        for start in range(0, len(rows), 1000):
            record = session.run(query, rows=rows[start : start + 1000]).single()
            tagged += record["tagged"] if record else 0

    return {
        "entities": len(entity_ids),
        "tags_inserted": inserted,
        "tags_already_present": skipped,
        "neo4j_tag_edges": tagged,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    seeds = seed_dir()
    print(f"reading seeds from {seeds}")
    tags = collect(seeds)

    by_source: dict[str, int] = defaultdict(int)
    by_chain: dict[str, int] = defaultdict(int)
    by_type: dict[str, int] = defaultdict(int)
    for t in tags:
        by_source[t["source"]] += 1
        by_chain[t["chain"]] += 1
        by_type[t["entity_type"]] += 1

    print(f"\n{len(tags)} unique tags")
    print(f"  by source: {dict(sorted(by_source.items(), key=lambda x: -x[1]))}")
    print(f"  by chain:  {dict(by_chain)}")
    print(f"  by type:   {dict(sorted(by_type.items(), key=lambda x: -x[1]))}")

    if args.dry_run:
        print("\n--dry-run: nothing written")
        return 0

    result = load(tags)
    print(f"\nloaded: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
