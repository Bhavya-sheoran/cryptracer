#!/usr/bin/env python3
"""Load the demonstration state: seed tags, then file every synthetic complaint.

Run this after a fresh `docker compose up`, and again after running the test
suite - the test fixtures deliberately clear graph transaction data, so the
traced money flow has to be rebuilt (the seeded tag database survives).

Everything loaded here is synthetic or public-dataset derived. No real NCRP
complaint data and no exchange KYC data is involved.

Usage:
    docker compose exec backend python scripts/load_demo.py
    docker compose exec backend python scripts/load_demo.py --skip-tags
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def seed_tags() -> None:
    print("=== seeding tagged-address database (public sources) ===", flush=True)
    script = Path("/app/ml/src/seed_tags.py")
    if not script.exists():
        script = REPO / "ml" / "src" / "seed_tags.py"
    subprocess.run([sys.executable, str(script)], check=True)


def file_complaints(depth: int | None) -> int:
    from sqlalchemy.orm import Session

    from app.db.postgres import SessionLocal
    from app.services.connectors.synthetic import get_complaints
    from app.services.ingest import intake_wallet

    complaints = get_complaints()
    print(f"\n=== filing {len(complaints)} synthetic complaints ===", flush=True)

    filed = 0
    db: Session = SessionLocal()
    try:
        for c in complaints:
            result = intake_wallet(
                db,
                address=c["address"],
                victim_ref=c["victim_ref"],
                amount_inr=c.get("amount_inr"),
                narrative=c.get("narrative"),
                source="synthetic",
                trace_depth=depth,
            )
            trace = result["trace"]
            print(
                f"  {result['case_number']}  {result['chain']:4s} "
                f"hops={trace['hops_discovered']} addrs={trace['addresses_touched']} "
                f"txs={trace['transactions_ingested']}"
                + ("  [mixer]" if trace["mixer_interaction"] else ""),
                flush=True,
            )
            filed += 1
    finally:
        db.close()
    return filed


def summarise() -> None:
    from app.db.postgres import SessionLocal
    from app.services.risk import rank_entities

    print("\n=== exchange fraud-linkage ranking ===", flush=True)
    db = SessionLocal()
    try:
        for e in rank_entities(db, limit=10):
            print(
                f"  {e['risk_label'].upper():6s} {e['risk_score']:6.2f}  "
                f"{e['case_count']:3d} cases  {e['chain']:4s}  {e['entity_name']}"
            )
    finally:
        db.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--skip-tags", action="store_true", help="do not re-seed the tag DB")
    ap.add_argument("--depth", type=int, default=None, help="trace depth override")
    args = ap.parse_args()

    if not args.skip_tags:
        seed_tags()

    filed = file_complaints(args.depth)
    summarise()

    print(f"\ndemo loaded: {filed} complaints filed.")
    print("All data is SYNTHETIC. Try: curl 'localhost:8001/api/v1/exchanges/ranked'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
