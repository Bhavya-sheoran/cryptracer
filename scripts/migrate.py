#!/usr/bin/env python3
"""Bring the database schema up to date.

A thin CLI over app.db.migrations, which is the same code the application runs
at startup - so this cannot drift from what actually happens on boot.

Usage:
    python scripts/migrate.py            # upgrade to head
    python scripts/migrate.py --status   # report only, change nothing
"""

from __future__ import annotations

import argparse
import logging
import sys

from app.db import migrations


def show_status() -> int:
    state = migrations.status()
    print(f"  schema present:   {state['schema_exists']}")
    print(f"  alembic revision: {state['revision'] or '(none - never migrated)'}")
    if state["predates_alembic"]:
        print("\n  This database predates Alembic. Upgrading will stamp it at the")
        print("  baseline rather than trying to rebuild a schema it already has.")
    return 0


def run_upgrade() -> int:
    result = migrations.upgrade_to_head()
    if result["stamped"]:
        print(f"stamped this database at baseline {migrations.BASELINE_REVISION}")
    print(
        f"schema at {result['to_revision']} "
        f"(was {result['from_revision'] or 'unversioned'})"
    )
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--status", action="store_true", help="report only")
    args = parser.parse_args()
    return show_status() if args.status else run_upgrade()


if __name__ == "__main__":
    sys.exit(main())
