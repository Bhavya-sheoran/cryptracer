"""Hash-chained audit log.

Every approval and export is already recorded. The gap was that the record sat
in an ordinary table: anyone with database access could edit the row naming who
authorised a freeze, delete it, or insert one that never happened, and nothing
would show. For a system whose central safeguard is "a named human approved
this", a forgeable record of that name is the safeguard failing quietly.

Each entry carries the hash of the one before it, so the log forms a chain:

    entry_hash = sha256(prev_hash || canonical fields of this entry)

Editing any entry changes its hash, which breaks every link after it. The
tampering is not prevented - a determined administrator can still rewrite
rows - but it becomes *detectable*, and detectable is what an evidentiary
record needs. Recomputing the chain finds the exact entry where it diverges.

What this is not: it is not a blockchain, and it does not protect against
someone who rewrites the whole chain from the tampered point forward. That
needs the head hash published somewhere outside this database - printed on a
daily report, sent to a separate append-only store - which `chain_head()`
exists to support.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: The first entry has no predecessor. A fixed, recognisable value rather than
#: an empty string, so "chain starts here" is distinguishable from "prev_hash
#: was never populated".
GENESIS_HASH = "0" * 64


def canonical_payload(
    entry_id: int | None,
    actor_id: str | None,
    action: str,
    entity_type: str,
    entity_id: str | None,
    payload: dict[str, Any] | None,
    created_at: str | None,
) -> str:
    """Deterministic string form of one entry.

    `sort_keys` matters more than it looks: dict ordering would otherwise make
    the hash depend on insertion order, and the same entry would verify
    differently after a round-trip through the database. A verification that
    fails for benign reasons is quickly ignored, which is worse than not having
    it.
    """
    return json.dumps(
        {
            "id": entry_id,
            "actor_id": str(actor_id) if actor_id else None,
            "action": action,
            "entity_type": entity_type,
            "entity_id": str(entity_id) if entity_id else None,
            "payload": payload or {},
            "created_at": created_at,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def compute_hash(prev_hash: str, canonical: str) -> str:
    return hashlib.sha256(f"{prev_hash}{canonical}".encode()).hexdigest()


def latest_hash(db) -> str:
    """The current head of the chain, or GENESIS_HASH for an empty log."""
    from sqlalchemy import select

    from app.models import AuditLog

    row = db.execute(
        select(AuditLog.entry_hash)
        .where(AuditLog.entry_hash.isnot(None))
        .order_by(AuditLog.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return row or GENESIS_HASH


def verify_chain(db, limit: int | None = None, start_after_id: int | None = None) -> dict:
    """Recompute the chain and report the first entry that does not match.

    Returns a summary rather than raising: the caller is usually an operator
    asking "is this log intact?", and the useful answer names the entry where
    it stopped being intact.

    `start_after_id` verifies a suffix of the log rather than all of it. Two
    reasons: an operator checking today's activity should not have to walk
    years of history, and a test should verify the entries it wrote rather than
    every entry that happens to be in a shared database.

    The trade-off is honest and worth stating: a partial verification takes the
    first in-range entry's recorded `prev_hash` on trust, because the entry it
    refers to is outside the range. It therefore detects tampering *within* the
    range, not a break at its boundary. Only a full walk proves the whole log.
    """
    from sqlalchemy import select

    from app.models import AuditLog

    query = select(AuditLog).order_by(AuditLog.id.asc())
    if start_after_id is not None:
        query = query.where(AuditLog.id > start_after_id)
    if limit:
        query = query.limit(limit)

    entries = list(db.execute(query).scalars())

    # A full walk starts from the genesis value; a partial one adopts the first
    # in-range entry's recorded predecessor, since the real one is out of scope.
    previous = GENESIS_HASH
    if start_after_id is not None:
        first_chained = next((e for e in entries if e.entry_hash is not None), None)
        if first_chained is not None:
            previous = first_chained.prev_hash or GENESIS_HASH

    checked = 0
    unchained = 0

    for entry in entries:
        # Entries written before chaining existed carry no hash. They are
        # reported rather than treated as tampering - and they are also not
        # trusted, since nothing about them can be verified.
        if entry.entry_hash is None:
            unchained += 1
            continue

        expected = compute_hash(
            previous,
            canonical_payload(
                entry.id,
                entry.actor_id,
                entry.action,
                entry.entity_type,
                entry.entity_id,
                entry.payload,
                entry.created_at.isoformat() if entry.created_at else None,
            ),
        )

        if entry.prev_hash != previous:
            return {
                "intact": False,
                "reason": "broken_link",
                "failed_at_id": entry.id,
                "detail": (
                    f"entry {entry.id} records prev_hash {entry.prev_hash!r} but the "
                    f"entry before it hashes to {previous!r} - an entry was inserted, "
                    f"deleted or reordered"
                ),
                "entries_checked": checked,
                "unchained_legacy_entries": unchained,
            }

        if entry.entry_hash != expected:
            return {
                "intact": False,
                "reason": "content_modified",
                "failed_at_id": entry.id,
                "detail": (
                    f"entry {entry.id} hashes to {expected!r} but stores "
                    f"{entry.entry_hash!r} - its contents were changed after it "
                    f"was written"
                ),
                "entries_checked": checked,
                "unchained_legacy_entries": unchained,
            }

        previous = entry.entry_hash
        checked += 1

    return {
        "intact": True,
        "reason": None,
        "failed_at_id": None,
        "detail": f"{checked} chained entries verify against each other",
        "entries_checked": checked,
        "unchained_legacy_entries": unchained,
        "head": previous,
    }
