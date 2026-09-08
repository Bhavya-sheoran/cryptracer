"""Liveness and readiness probes.

/health/ready deliberately reports `demo_mode` and `data_source`. The frontend
reads these to decide whether to show the SYNTHETIC DEMO DATA banner, so the
provenance of what is on screen is driven by the server, not by UI copy that
could drift out of date.
"""

from fastapi import APIRouter

from app.config import get_settings
from app.db import neo4j as neo4j_db
from app.db import postgres as pg_db
from app.db import redis_client

router = APIRouter(prefix="/health", tags=["health"])
settings = get_settings()


@router.get("/live")
def live() -> dict:
    """Process is up. No dependency checks."""
    return {"status": "ok", "app": settings.app_name}


@router.get("/ready")
def ready() -> dict:
    """Dependency check across Postgres, Neo4j and Redis."""
    checks: dict[str, dict] = {}

    for name, fn in (
        ("postgres", pg_db.ping),
        ("neo4j", neo4j_db.ping),
        ("redis", redis_client.ping),
    ):
        try:
            fn()
            checks[name] = {"ok": True}
        except Exception as exc:
            checks[name] = {"ok": False, "error": str(exc)[:200]}

    all_ok = all(c["ok"] for c in checks.values())
    return {
        "status": "ready" if all_ok else "degraded",
        "checks": checks,
        "demo_mode": settings.demo_mode,
        # Consumed by the frontend banner. Never claim live-chain provenance
        # while demo_mode is on.
        "data_source": "synthetic" if settings.demo_mode else "live_indexer_apis",
        # The dashboard hides the published-credential panel when this is
        # false, so the UI cannot offer a sign-in route the server refuses.
        "demo_auth_enabled": settings.demo_auth_enabled,
        "phase": "4 - case management, NCRP mock, STR, freeze workflow, alerts",
    }
