#!/usr/bin/env python3
"""Change the Neo4j password on a database that already exists.

Neo4j sets its password from NEO4J_AUTH the first time it initialises its data
directory, and never reads it again. So changing the environment variable on a
running deployment does not change the password - it just makes the backend's
credentials wrong, which looks like an outage and is easy to misdiagnose.

This does the other half: it changes the password inside the database, so the
new value in .env is the one that actually works. Data is untouched.

Both values are read from the environment rather than the command line, so
neither appears in shell history or in `ps` output while it runs.

Usage:
    NEO4J_OLD_PASSWORD=... NEO4J_NEW_PASSWORD=... \
        python scripts/rotate_neo4j_password.py
"""

from __future__ import annotations

import os
import sys

from neo4j import GraphDatabase
from neo4j.exceptions import AuthError

from app.config import get_settings


def main() -> int:
    old = os.environ.get("NEO4J_OLD_PASSWORD")
    new = os.environ.get("NEO4J_NEW_PASSWORD")

    if not old or not new:
        print(
            "set NEO4J_OLD_PASSWORD and NEO4J_NEW_PASSWORD in the environment",
            file=sys.stderr,
        )
        return 2
    if old == new:
        print("old and new passwords are identical - nothing to do", file=sys.stderr)
        return 2
    if len(new) < 8:
        # Neo4j enforces this itself; failing here gives a clearer message.
        print("new password must be at least 8 characters", file=sys.stderr)
        return 2

    settings = get_settings()
    uri = settings.neo4j_uri
    user = settings.neo4j_user

    driver = GraphDatabase.driver(uri, auth=(user, old))
    try:
        with driver.session(database="system") as session:
            session.run(
                "ALTER CURRENT USER SET PASSWORD FROM $old TO $new",
                old=old,
                new=new,
            )
    except AuthError:
        print(
            f"could not authenticate to {uri} as {user!r} with the old password.\n"
            "If the password was already rotated, NEO4J_OLD_PASSWORD is stale.",
            file=sys.stderr,
        )
        return 1
    finally:
        driver.close()

    # Prove the new password works before reporting success. A rotation that
    # silently half-applied would lock the backend out of its own database.
    check = GraphDatabase.driver(uri, auth=(user, new))
    try:
        with check.session() as session:
            addresses = session.run("MATCH (a:Address) RETURN count(a) AS c").single()["c"]
            tags = session.run(
                "MATCH ()-[t:TAGGED_AS]->() RETURN count(t) AS c"
            ).single()["c"]
    finally:
        check.close()

    print("neo4j password rotated and verified")
    print(f"  reachable with the new password: {addresses} addresses, {tags} tags intact")
    print("\nSet NEO4J_PASSWORD in .env to the new value, then restart the backend.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
