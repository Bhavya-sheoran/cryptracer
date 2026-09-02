"""Neo4j driver lifecycle plus idempotent constraint bootstrap."""

import logging
import re
from pathlib import Path

from neo4j import Driver, GraphDatabase

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_driver: Driver | None = None

# Mounted read-only into the backend container by docker-compose (the Neo4j
# container cannot take a read-only import mount - its entrypoint chowns that
# dir). Falls back to the repo-relative path when running outside Docker.
_CYPHER_CANDIDATES = (
    Path("/app/infra/neo4j/init.cypher"),
    Path(__file__).resolve().parents[3] / "infra" / "neo4j" / "init.cypher",
)


def get_driver() -> Driver:
    global _driver
    if _driver is None:
        _driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
        )
    return _driver


def close_driver() -> None:
    global _driver
    if _driver is not None:
        _driver.close()
        _driver = None


def ping() -> bool:
    get_driver().verify_connectivity()
    return True


def _load_statements() -> list[str]:
    for path in _CYPHER_CANDIDATES:
        if path.exists():
            raw = path.read_text(encoding="utf-8")
            # Strip // comments, then split on statement terminators.
            stripped = re.sub(r"^\s*//.*$", "", raw, flags=re.MULTILINE)
            return [s.strip() for s in stripped.split(";") if s.strip()]
    logger.warning("init.cypher not found in %s", [str(p) for p in _CYPHER_CANDIDATES])
    return []


def apply_constraints() -> int:
    """Apply the CREATE CONSTRAINT / CREATE INDEX statements. Safe to re-run."""
    statements = _load_statements()
    applied = 0
    with get_driver().session() as session:
        for stmt in statements:
            try:
                session.run(stmt)
                applied += 1
            except Exception as exc:  # pragma: no cover - surfaced in logs only
                logger.warning("constraint statement failed: %s | %s", stmt[:60], exc)
    logger.info("neo4j: applied %d/%d schema statements", applied, len(statements))
    return applied
