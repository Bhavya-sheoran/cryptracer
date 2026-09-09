"""Schema migration, shared by the application's startup and the CLI.

Previously there were two mechanisms: `infra/postgres/init.sql` applied once by
docker-entrypoint-initdb.d, and an `apply_pending_ddl()` that re-ran selected
idempotent files on every boot. That worked, but it left no history - nothing
recorded which schema a given database was at, and nothing could be reviewed or
reversed. Alembic replaces both.

The awkward case is a database that already existed before Alembic did. Running
the baseline against one would try to CREATE TYPE over enums already present
and fail, so such a database is *stamped* instead: its existing schema is
recorded as revision 0001, and later revisions apply normally on top.

An advisory lock wraps the whole operation. Under the production overlay
uvicorn runs four workers, each with its own lifespan, and two of them applying
the same CREATE TABLE is a startup crash that reads as a database fault.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import inspect, text

from app.db.postgres import engine

logger = logging.getLogger(__name__)

#: Fixed and arbitrary. Any process holding it is doing schema work.
MIGRATION_LOCK_ID = 918_326_183

#: Presence of this table means the core schema exists, however it was built.
SENTINEL_TABLE = "cases"

BASELINE_REVISION = "0001"

_CONFIG_PATHS = (
    Path("/app/alembic.ini"),
    Path(__file__).resolve().parents[2] / "alembic.ini",
)


def _config():
    from alembic.config import Config

    path = next((p for p in _CONFIG_PATHS if p.exists()), None)
    if path is None:
        raise RuntimeError(f"alembic.ini not found in {[str(p) for p in _CONFIG_PATHS]}")
    config = Config(str(path))
    config.set_main_option("script_location", str(path.parent / "alembic"))
    return config


def current_revision() -> str | None:
    from alembic.runtime.migration import MigrationContext

    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def schema_exists() -> bool:
    return SENTINEL_TABLE in set(inspect(engine).get_table_names())


def status() -> dict:
    return {
        "schema_exists": schema_exists(),
        "revision": current_revision(),
        "predates_alembic": schema_exists() and current_revision() is None,
    }


def upgrade_to_head() -> dict:
    """Migrate to head, stamping a pre-Alembic database rather than rebuilding it.

    Returns what happened, so a caller can log it meaningfully instead of
    reporting a bare success.
    """
    from alembic.runtime.migration import MigrationContext

    from alembic import command

    config = _config()
    result = {"stamped": False, "from_revision": None, "to_revision": None}

    with engine.begin() as connection:
        # Blocks rather than erroring: a worker that loses the race waits for
        # the winner, then finds nothing left to do.
        connection.execute(
            text("SELECT pg_advisory_lock(:lock_id)"), {"lock_id": MIGRATION_LOCK_ID}
        )
        try:
            revision = MigrationContext.configure(connection).get_current_revision()
            already_built = SENTINEL_TABLE in set(inspect(connection).get_table_names())
            result["from_revision"] = revision

            if revision is None and already_built:
                logger.info(
                    "database predates alembic; stamping baseline %s", BASELINE_REVISION
                )
                command.stamp(config, BASELINE_REVISION)
                result["stamped"] = True

            command.upgrade(config, "head")
            result["to_revision"] = MigrationContext.configure(
                connection
            ).get_current_revision()
        finally:
            connection.execute(
                text("SELECT pg_advisory_unlock(:lock_id)"),
                {"lock_id": MIGRATION_LOCK_ID},
            )

    return result
