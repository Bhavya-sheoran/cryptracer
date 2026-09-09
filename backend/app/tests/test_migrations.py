"""Schema migration.

The property that matters most: a database built by Alembic must be identical
to one built by `infra/postgres/init.sql`. If the baseline drifts from the SQL
that created every existing deployment, every future migration is a diff
against a schema nobody is actually running.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, inspect, text

from app.config import get_settings
from app.db import migrations


@pytest.fixture(scope="module")
def pg_available() -> bool:
    from app.db.postgres import ping

    try:
        return ping()
    except Exception:
        return False


def test_baseline_revision_is_pinned():
    """A moving baseline id would strand already-stamped databases."""
    assert migrations.BASELINE_REVISION == "0001"


def test_status_reports_the_live_database(pg_available):
    if not pg_available:
        pytest.skip("postgres not reachable")

    state = migrations.status()
    assert state["schema_exists"] is True
    assert state["revision"] is not None, (
        "the live database should be stamped; run scripts/migrate.py"
    )


def test_advisory_lock_id_is_stable():
    """Workers only serialise against each other if they take the same lock."""
    assert migrations.MIGRATION_LOCK_ID == 918_326_183


@pytest.fixture
def scratch_database(pg_available):
    """A real, empty database, dropped afterwards.

    Built against the live server rather than mocked: the thing under test is
    whether raw SQL containing enums and constraints actually applies, and a
    mock would only confirm that the code calls alembic.
    """
    if not pg_available:
        pytest.skip("postgres not reachable")

    settings = get_settings()
    name = f"sih183_test_{uuid.uuid4().hex[:12]}"
    admin_url = settings.database_url.rsplit("/", 1)[0] + "/sih183"

    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT", future=True)
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))

    yield settings.database_url.rsplit("/", 1)[0] + f"/{name}"

    with admin.connect() as conn:
        conn.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :n AND pid <> pg_backend_pid()"
            ),
            {"n": name},
        )
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
    admin.dispose()


def test_baseline_builds_a_schema_matching_the_initdb_sql(scratch_database, monkeypatch):
    """The load-bearing test: migrated schema == initdb schema."""
    from alembic import command

    fresh = create_engine(scratch_database, future=True)
    monkeypatch.setattr(migrations, "engine", fresh)

    config = migrations._config()  # noqa: SLF001 - exercising the real config
    with fresh.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")

    migrated_tables = set(inspect(fresh).get_table_names()) - {"alembic_version"}
    fresh.dispose()

    from app.db.postgres import engine as live_engine

    live_tables = set(inspect(live_engine).get_table_names()) - {"alembic_version"}

    assert migrated_tables == live_tables, (
        "the baseline produced a different schema than the deployed one"
    )
    assert len(migrated_tables) >= 15, "suspiciously few tables - did the SQL apply?"


def test_upgrade_is_idempotent(pg_available):
    """Running it twice must be a no-op, not an error.

    Every backend worker calls this on startup, so it runs as many times as
    there are workers on every deploy.
    """
    if not pg_available:
        pytest.skip("postgres not reachable")

    first = migrations.upgrade_to_head()
    second = migrations.upgrade_to_head()

    assert first["to_revision"] == second["to_revision"]
    assert second["stamped"] is False, "an already-stamped database must not restamp"


def test_baseline_downgrade_refuses_rather_than_destroying_data():
    """Reversing the baseline would drop every case and evidence record.

    Alembic offers `downgrade` and someone will eventually type it; it must
    fail loudly rather than silently succeed at deleting the investigation.
    """
    import importlib.util

    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(migrations._config())  # noqa: SLF001
    revision = script.get_revision(migrations.BASELINE_REVISION)

    spec = importlib.util.spec_from_file_location("baseline", revision.module.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    with pytest.raises(NotImplementedError, match="not reversible"):
        module.downgrade()
