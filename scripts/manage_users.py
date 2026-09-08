#!/usr/bin/env python3
"""Create and manage officer accounts from the command line.

This exists because `seed_demo_users` was the only code in the project that
created a User, and it is disabled outside demo mode - so with DEMO_MODE=false
there was no way to make a real account at all.

Deliberately a CLI and not an HTTP endpoint. An account-creation route is the
single most valuable thing for an attacker to reach; requiring shell access to
the container is a meaningful barrier, and account creation is rare enough that
convenience is not worth trading for it.

Passwords are generated here rather than chosen. They are printed exactly once
and only their bcrypt hash is stored, so there is no way to recover one later -
if it is lost, reset it.

Usage:
    python scripts/manage_users.py list
    python scripts/manage_users.py create SIHtesting --role admin
    python scripts/manage_users.py create asha --role investigator --full-name "Asha Nair"
    python scripts/manage_users.py deactivate investigator
    python scripts/manage_users.py reset-password SIHtesting
"""

from __future__ import annotations

import argparse
import secrets
import string
import sys

from sqlalchemy import select

from app.db.postgres import SessionLocal
from app.models import User
from app.services.auth import (
    ROLE_ADMIN,
    ROLE_INVESTIGATOR,
    ROLE_SUPERVISOR,
    hash_password,
)

ROLES = (ROLE_INVESTIGATOR, ROLE_SUPERVISOR, ROLE_ADMIN)

# Ambiguous glyphs removed: these get read off a screen and typed by hand, and
# an account nobody can log into is not more secure, just broken.
ALPHABET = "".join(c for c in string.ascii_letters + string.digits if c not in "O0oIl1")
PASSWORD_LENGTH = 20


def generate_password() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(PASSWORD_LENGTH))


def cmd_list(args) -> int:
    with SessionLocal() as db:
        users = db.execute(select(User).order_by(User.username)).scalars().all()
        if not users:
            print("no accounts exist")
            return 0
        print(f"{'username':20} {'role':14} {'active':8} full name")
        print("-" * 68)
        for u in users:
            print(f"{u.username:20} {u.role:14} {str(u.is_active):8} {u.full_name}")
    return 0


def cmd_create(args) -> int:
    with SessionLocal() as db:
        existing = db.execute(
            select(User).where(User.username == args.username)
        ).scalar_one_or_none()
        if existing is not None:
            print(f"error: an account named {args.username!r} already exists", file=sys.stderr)
            return 1

        password = args.password or generate_password()
        user = User(
            username=args.username,
            full_name=args.full_name or args.username,
            role=args.role,
            hashed_password=hash_password(password),
        )
        db.add(user)
        db.commit()

        print(f"created {args.username!r} with role {args.role!r}")
        if args.role in (ROLE_SUPERVISOR, ROLE_ADMIN):
            print("  this role can APPROVE freeze requests and STR drafts")
        if not args.password:
            print(f"\n  password: {password}\n")
            print("  Shown once. Only its bcrypt hash is stored - it cannot be recovered.")
    return 0


def cmd_deactivate(args) -> int:
    with SessionLocal() as db:
        user = db.execute(
            select(User).where(User.username == args.username)
        ).scalar_one_or_none()
        if user is None:
            print(f"error: no account named {args.username!r}", file=sys.stderr)
            return 1

        # Deactivated, never deleted: the audit log references the actor by id,
        # and removing the row would orphan the record of who approved what.
        user.is_active = False
        db.commit()
        print(f"deactivated {args.username!r} - sign-in refused, audit history intact")
    return 0


def cmd_reset_password(args) -> int:
    with SessionLocal() as db:
        user = db.execute(
            select(User).where(User.username == args.username)
        ).scalar_one_or_none()
        if user is None:
            print(f"error: no account named {args.username!r}", file=sys.stderr)
            return 1

        password = generate_password()
        user.hashed_password = hash_password(password)
        db.commit()
        print(f"reset password for {args.username!r}")
        print(f"\n  password: {password}\n")
        print("  Any token issued before now stays valid until it expires -")
        print("  there is no revocation path yet (Tier 3 of the roadmap).")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="show all accounts")

    create = sub.add_parser("create", help="create an account")
    create.add_argument("username")
    create.add_argument("--role", choices=ROLES, required=True)
    create.add_argument("--full-name", default=None)
    create.add_argument(
        "--password",
        default=None,
        help="set explicitly instead of generating one (avoid: it lands in shell history)",
    )

    deact = sub.add_parser("deactivate", help="disable an account without deleting it")
    deact.add_argument("username")

    reset = sub.add_parser("reset-password", help="issue a new generated password")
    reset.add_argument("username")

    args = ap.parse_args()
    return {
        "list": cmd_list,
        "create": cmd_create,
        "deactivate": cmd_deactivate,
        "reset-password": cmd_reset_password,
    }[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
