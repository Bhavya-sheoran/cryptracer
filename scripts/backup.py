#!/usr/bin/env python3
"""Back up both databases, and verify the backup by restoring it.

Run from the HOST, where `docker compose` is available - not inside a
container.

Two datastores, two mechanisms, because they are different problems:

  * Postgres holds the case records, users and audit log. `pg_dump` produces a
    plain SQL dump that restores into any comparable server.
  * Neo4j Community has no online backup - `neo4j-admin database dump` needs
    the database stopped, which means downtime for a backup. APOC's Cypher
    export runs against a live database instead, which is the whole reason
    APOC is loaded in the first place.

The verify step is not optional decoration. An untested backup is a belief, not
a backup, and the moment you discover an unrestorable dump is the moment you
needed it. `--verify` restores the Postgres dump into a scratch database and
compares row counts table by table, then drops it.

Usage:
    python scripts/backup.py                    # back up
    python scripts/backup.py --verify           # back up, then prove it restores
    python scripts/backup.py --list             # what is on disk
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKUP_DIR = REPO / "backups"

PG_SERVICE = "postgres"
PG_USER = "sih"
PG_DB = "sih183"
NEO4J_SERVICE = "neo4j"

#: Tables whose row counts are compared after a restore. Chosen as the ones
#: whose loss would actually matter: an investigation is its cases, the
#: evidence attached to them, and the record of who approved what.
VERIFY_TABLES = ("cases", "wallets", "case_wallets", "evidence", "audit_log", "users")


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=REPO, capture_output=True, text=True, **kwargs)


def compose(*args: str) -> list[str]:
    return ["docker", "compose", *args]


def timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


# --- backup --------------------------------------------------------------


def backup_postgres(stamp: str) -> Path:
    target = BACKUP_DIR / f"postgres_{stamp}.sql"
    print(f"  postgres -> {target.name}", flush=True)

    result = run(compose("exec", "-T", PG_SERVICE, "pg_dump", "-U", PG_USER, PG_DB))
    if result.returncode != 0:
        raise SystemExit(f"pg_dump failed:\n{result.stderr[:500]}")

    target.write_text(result.stdout, encoding="utf-8")
    print(f"    {target.stat().st_size:,} bytes")
    return target


def backup_neo4j(stamp: str) -> Path:
    target = BACKUP_DIR / f"neo4j_{stamp}.cypher"
    print(f"  neo4j    -> {target.name}", flush=True)

    # Exported through the driver rather than by copying the data directory,
    # so the database keeps serving while this runs.
    script = (
        "from app.db.neo4j import get_driver\n"
        "with get_driver().session() as s:\n"
        "    rows = s.run('CALL apoc.export.cypher.all(null, "
        "{stream:true, format:\"cypher-shell\", "
        "useOptimizations:{type:\"UNWIND_BATCH\", unwindBatchSize:100}}) "
        "YIELD cypherStatements RETURN cypherStatements')\n"
        "    for r in rows:\n"
        "        print(r['cypherStatements'], end='')\n"
    )
    result = run(compose("exec", "-T", "backend", "python", "-c", script))
    if result.returncode != 0:
        raise SystemExit(f"neo4j export failed:\n{result.stderr[:500]}")

    target.write_text(result.stdout, encoding="utf-8")
    print(f"    {target.stat().st_size:,} bytes")
    return target


# --- verify --------------------------------------------------------------


def table_counts(database: str) -> dict[str, int]:
    counts = {}
    for table in VERIFY_TABLES:
        result = run(
            compose(
                "exec", "-T", PG_SERVICE,
                "psql", "-U", PG_USER, "-d", database, "-Atc",
                f"SELECT count(*) FROM {table}",
            )
        )
        counts[table] = int(result.stdout.strip()) if result.returncode == 0 else -1
    return counts


def verify_postgres(dump: Path) -> bool:
    """Restore into a scratch database and compare row counts."""
    scratch = f"verify_{timestamp().lower()}"
    print(f"\n  restoring into scratch database {scratch}")

    run(compose("exec", "-T", PG_SERVICE, "psql", "-U", PG_USER, "-d", PG_DB,
                "-c", f'CREATE DATABASE "{scratch}"'))

    restore = subprocess.run(
        compose("exec", "-T", PG_SERVICE, "psql", "-U", PG_USER, "-d", scratch),
        cwd=REPO, input=dump.read_text(encoding="utf-8"),
        capture_output=True, text=True,
    )

    ok = True
    if restore.returncode != 0:
        print(f"    restore reported errors:\n{restore.stderr[:400]}")
        ok = False

    original = table_counts(PG_DB)
    restored = table_counts(scratch)

    print(f"    {'table':<16} {'original':>10} {'restored':>10}")
    for table in VERIFY_TABLES:
        match = "ok" if original[table] == restored[table] else "MISMATCH"
        if original[table] != restored[table]:
            ok = False
        print(f"    {table:<16} {original[table]:>10} {restored[table]:>10}  {match}")

    run(compose("exec", "-T", PG_SERVICE, "psql", "-U", PG_USER, "-d", PG_DB,
                "-c", f'DROP DATABASE IF EXISTS "{scratch}"'))
    print("    scratch database dropped")
    return ok


# --- entry points --------------------------------------------------------


def do_list() -> int:
    if not BACKUP_DIR.exists() or not any(BACKUP_DIR.iterdir()):
        print("no backups on disk")
        return 0
    for path in sorted(BACKUP_DIR.iterdir()):
        age = datetime.now(UTC) - datetime.fromtimestamp(path.stat().st_mtime, UTC)
        print(f"  {path.name:<40} {path.stat().st_size:>12,} bytes   {age.days}d old")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--verify", action="store_true",
                        help="restore the dump into a scratch database and compare")
    parser.add_argument("--list", action="store_true", help="show existing backups")
    args = parser.parse_args()

    if args.list:
        return do_list()

    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = timestamp()
    print(f"backup {stamp}")

    pg_dump = backup_postgres(stamp)
    backup_neo4j(stamp)

    if not args.verify:
        print("\nBacked up. Run with --verify to prove it restores - an untested")
        print("backup is a belief, not a backup.")
        return 0

    if verify_postgres(pg_dump):
        print("\nVERIFIED: the dump restores and every checked table matches.")
        return 0

    print("\nVERIFICATION FAILED. This backup should not be relied on.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
