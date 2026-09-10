"""Tamper-evident audit log.

The safeguard this protects is the project's central one: a named human
approved this freeze. That claim was recorded in an ordinary table, so anyone
with database access could change the name, delete the row, or insert an
approval that never happened - and nothing would show.

Chaining does not prevent that. It makes it detectable, which is what an
evidentiary record needs. The tests that matter are the three that actually
tamper with a row and assert the chain notices.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text

from app.db.postgres import SessionLocal
from app.models import AuditLog
from app.services import audit_chain


@pytest.fixture(scope="module")
def pg_available() -> bool:
    from app.db.postgres import ping

    try:
        return ping()
    except Exception:
        return False


@pytest.fixture
def db(pg_available):
    if not pg_available:
        pytest.skip("postgres not reachable")
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()


@pytest.fixture
def baseline(db) -> int:
    """Highest audit id before this test wrote anything.

    Tests verify only the entries they created. Verifying the whole table makes
    them depend on the state of a shared database - they passed while it was
    clean and failed the moment an unrelated entry was tampered with during a
    manual check, which is a test asserting something it does not control.
    """
    from sqlalchemy import func

    return db.execute(select(func.coalesce(func.max(AuditLog.id), 0))).scalar_one()


def write_entry(db, action: str, entity_id: str) -> AuditLog:
    """Append a chained entry the same way record_audit does."""
    entry = AuditLog(
        actor_id=None, action=action, entity_type="test", entity_id=entity_id, payload={}
    )
    db.add(entry)
    db.flush()
    entry.prev_hash = audit_chain.latest_hash(db)
    entry.entry_hash = audit_chain.compute_hash(
        entry.prev_hash,
        audit_chain.canonical_payload(
            entry.id, None, action, "test", entity_id, {},
            entry.created_at.isoformat() if entry.created_at else None,
        ),
    )
    db.flush()
    return entry


# --- hashing ------------------------------------------------------------


def test_canonical_form_is_order_independent():
    """Dict ordering must not change the hash.

    Otherwise the same entry verifies differently after a round-trip through
    the database, and a verification that fails for benign reasons is one
    people learn to ignore.
    """
    a = audit_chain.canonical_payload(1, None, "x", "t", "e", {"b": 2, "a": 1}, "2026-01-01")
    b = audit_chain.canonical_payload(1, None, "x", "t", "e", {"a": 1, "b": 2}, "2026-01-01")
    assert a == b


def test_any_field_change_changes_the_hash():
    base = dict(
        entry_id=1, actor_id=None, action="approve", entity_type="freeze_request",
        entity_id="e1", payload={"note": "ok"}, created_at="2026-01-01T00:00:00",
    )
    original = audit_chain.compute_hash("prev", audit_chain.canonical_payload(**base))

    for field, altered in (
        ("action", "reject"),
        ("entity_id", "e2"),
        ("payload", {"note": "tampered"}),
        ("created_at", "2026-01-02T00:00:00"),
        ("actor_id", "someone-else"),
    ):
        changed = audit_chain.compute_hash(
            "prev", audit_chain.canonical_payload(**{**base, field: altered})
        )
        assert changed != original, f"changing {field} did not change the hash"


def test_the_same_entry_under_a_different_predecessor_hashes_differently():
    """This is what makes it a chain rather than a set of independent hashes."""
    canonical = audit_chain.canonical_payload(1, None, "x", "t", "e", {}, "2026-01-01")
    assert audit_chain.compute_hash("aaa", canonical) != audit_chain.compute_hash("bbb", canonical)


# --- the chain, end to end ----------------------------------------------


def test_a_freshly_written_run_verifies(db, baseline):
    for i in range(3):
        write_entry(db, "test.write", f"entity-{i}")
    db.flush()

    result = audit_chain.verify_chain(db, start_after_id=baseline)
    assert result["intact"] is True, result["detail"]


def test_editing_an_entry_is_detected(db, baseline):
    """The case this exists for: someone rewrites who approved a freeze."""
    write_entry(db, "freeze.approve", "case-1")
    target = write_entry(db, "freeze.approve", "case-2")
    write_entry(db, "freeze.approve", "case-3")
    db.flush()

    assert audit_chain.verify_chain(db, start_after_id=baseline)["intact"] is True

    # Change the record without touching its hash - what an edit looks like.
    db.execute(
        text("UPDATE audit_log SET entity_id = :new WHERE id = :id"),
        {"new": "case-2-TAMPERED", "id": target.id},
    )
    db.flush()
    db.expire_all()

    result = audit_chain.verify_chain(db, start_after_id=baseline)
    assert result["intact"] is False
    assert result["reason"] == "content_modified"
    assert result["failed_at_id"] == target.id


def test_deleting_an_entry_is_detected(db, baseline):
    """A deletion leaves a gap the surrounding links no longer span."""
    write_entry(db, "freeze.approve", "keep-1")
    victim = write_entry(db, "freeze.approve", "delete-me")
    write_entry(db, "freeze.approve", "keep-2")
    db.flush()

    db.execute(text("DELETE FROM audit_log WHERE id = :id"), {"id": victim.id})
    db.flush()
    db.expire_all()

    result = audit_chain.verify_chain(db, start_after_id=baseline)
    assert result["intact"] is False
    assert result["reason"] == "broken_link"


def test_an_inserted_entry_is_detected(db, baseline):
    """An approval that never happened, written straight into the table."""
    write_entry(db, "freeze.approve", "real-1")
    db.flush()
    head_before = audit_chain.verify_chain(db, start_after_id=baseline)
    assert head_before["intact"] is True

    # Forged: plausible content, and a hash that cannot be right because the
    # forger cannot produce one consistent with the entries around it.
    db.execute(
        text(
            "INSERT INTO audit_log (action, entity_type, entity_id, payload,"
            " prev_hash, entry_hash) VALUES ('freeze.approve', 'test', 'forged',"
            " '{}'::jsonb, :prev, :fake)"
        ),
        {"prev": audit_chain.GENESIS_HASH, "fake": "f" * 64},
    )
    db.flush()
    db.expire_all()

    assert audit_chain.verify_chain(db, start_after_id=baseline)["intact"] is False


def test_legacy_entries_are_reported_not_treated_as_tampering(db, baseline):
    """Entries predating chaining are unverifiable, which is not the same as forged.

    Backfilling them would be worse: a computed hash on a historical row
    asserts it was verified when it never was.
    """
    db.execute(
        text(
            "INSERT INTO audit_log (action, entity_type, entity_id, payload)"
            " VALUES ('legacy.action', 'test', 'old', '{}'::jsonb)"
        )
    )
    write_entry(db, "test.after", "new")
    db.flush()

    result = audit_chain.verify_chain(db, start_after_id=baseline)
    assert result["intact"] is True
    assert result["unchained_legacy_entries"] >= 1


def test_verification_names_the_entry_that_failed(db, baseline):
    """An operator needs to know where the log stopped being trustworthy."""
    write_entry(db, "a", "1")
    target = write_entry(db, "b", "2")
    db.flush()

    db.execute(
        text("UPDATE audit_log SET action = 'forged' WHERE id = :id"), {"id": target.id}
    )
    db.flush()
    db.expire_all()

    result = audit_chain.verify_chain(db, start_after_id=baseline)
    assert result["failed_at_id"] == target.id
    assert str(target.id) in result["detail"]


# --- integration with record_audit --------------------------------------


def test_record_audit_chains_what_it_writes(db, baseline):
    """The real write path, not just the helper in this file."""
    from app.deps import record_audit

    record_audit(db, None, "test.record_audit", "test", "entity-x", {"k": "v"})
    db.flush()

    entry = db.execute(
        select(AuditLog).where(AuditLog.action == "test.record_audit").order_by(AuditLog.id.desc())
    ).scalars().first()

    assert entry is not None
    assert entry.entry_hash, "record_audit wrote an unchained entry"
    assert entry.prev_hash, "record_audit did not link to a predecessor"
    assert audit_chain.verify_chain(db, start_after_id=baseline)["intact"] is True
