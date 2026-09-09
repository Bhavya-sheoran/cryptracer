"""PostgreSQL engine and session factory."""

import logging
from collections.abc import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

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


# Schema management lives in app.db.migrations (Alembic). It used to be a
# second mechanism here that re-ran selected idempotent SQL files on every
# boot; that left no history of which schema a database was at and nothing to
# review or reverse, so it was replaced rather than kept alongside.
