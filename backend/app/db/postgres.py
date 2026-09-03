"""PostgreSQL engine and session factory."""

import logging
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

#: Idempotent DDL applied on every startup.
#:
#: `infra/postgres/init.sql` runs only via docker-entrypoint-initdb.d, which
#: fires once on an empty volume. Any schema added after a database exists would
#: therefore never appear without wiping it. These files use IF NOT EXISTS
#: throughout and are safe to re-run, which is enough of a migration path for a
#: prototype - Alembic remains the right answer before production.
_MIGRATION_CANDIDATES = [
    Path("/app/infra/postgres"),
    Path(__file__).resolve().parents[3] / "infra" / "postgres",
]
_APPLY_ON_STARTUP = ("002_exposure.sql",)

engine = create_engine(settings.database_url, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ping() -> bool:
    """Cheap liveness check used by the readiness probe."""
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return True


def apply_pending_ddl() -> list[str]:
    """Apply the idempotent DDL files. Returns the names applied."""
    directory = next((p for p in _MIGRATION_CANDIDATES if p.exists()), None)
    if directory is None:
        logger.warning("no postgres DDL directory found; skipping")
        return []

    applied: list[str] = []
    for name in _APPLY_ON_STARTUP:
        path = directory / name
        if not path.exists():
            logger.warning("expected DDL file missing: %s", path)
            continue
        try:
            with engine.begin() as conn:
                conn.execute(text(path.read_text(encoding="utf-8")))
            applied.append(name)
        except Exception as exc:  # noqa: BLE001 - never block startup on this
            logger.error("failed applying %s: %s", name, exc)
    if applied:
        logger.info("postgres DDL applied: %s", ", ".join(applied))
    return applied
