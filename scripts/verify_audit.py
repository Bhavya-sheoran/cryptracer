#!/usr/bin/env python3
"""Verify the audit log has not been altered.

Recomputes the hash chain over every entry. If any record of who approved a
freeze, exported a report or dispatched a request has been edited, deleted or
inserted, the chain breaks and this names the entry where it broke.

This does not prevent tampering - a determined administrator with database
access can rewrite rows. It makes tampering *detectable*, which is what an
evidentiary record needs.

Run it on a schedule, and record the head hash somewhere outside this database
(printed on a daily report, or an append-only store). Without an external
anchor, someone who rewrites the chain from a tampered entry forward produces a
log that verifies internally. The head hash is what makes that detectable too.

Usage:
    python scripts/verify_audit.py
    python scripts/verify_audit.py --quiet    # exit code only, for cron
"""

from __future__ import annotations

import argparse
import sys

from app.db.postgres import SessionLocal
from app.services import audit_chain

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--quiet", action="store_true", help="exit code only")
    args = parser.parse_args()

    with SessionLocal() as db:
        result = audit_chain.verify_chain(db)

    if args.quiet:
        return 0 if result["intact"] else 1

    print(f"{BOLD}Audit log verification{RESET}")
    print(f"{DIM}hash-chained; each entry commits to the one before it{RESET}\n")

    if result["intact"]:
        print(f"  {GREEN}{BOLD}INTACT{RESET}  {result['entries_checked']} entries verify")
        print(f"  head  {DIM}{result.get('head')}{RESET}")
        if result["unchained_legacy_entries"]:
            print(
                f"\n  {YELLOW}{result['unchained_legacy_entries']} entr(ies) predate chaining "
                f"and cannot be verified.{RESET}"
            )
            print(
                f"  {DIM}Not evidence of tampering - but not evidence of "
                f"integrity either.{RESET}"
            )
        print(
            f"\n  {DIM}Record the head hash outside this database. Without an "
            f"external{RESET}"
        )
        print(f"  {DIM}anchor, a chain rewritten from a tampered entry still verifies.{RESET}")
        return 0

    print(f"  {RED}{BOLD}TAMPERING DETECTED{RESET}")
    print(f"  reason        {result['reason']}")
    print(f"  first bad id  {result['failed_at_id']}")
    print(f"  verified up to {result['entries_checked']} entries before this\n")
    print(f"  {result['detail']}")
    print(
        f"\n  {RED}Treat every entry from id {result['failed_at_id']} onward "
        f"as unreliable.{RESET}"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
