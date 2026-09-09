"""FastAPI application entrypoint.

Phase 0: app factory, CORS, health probes, Neo4j constraint bootstrap.
Later phases mount their routers alongside `health` in `create_app`.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1 import (
    auth,
    cases,
    exposure,
    freeze,
    health,
    ncrp,
    str_drafts,
    wallet_analysis,
    wallets,
    ws,
)
from app.config import check_secrets, get_settings
from app.db import migrations, redis_client
from app.db import neo4j as neo4j_db
from app.middleware import RateLimitMiddleware, SecurityHeadersMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Before anything else: refuse to serve real data with placeholder secrets.
    # In demo mode this logs warnings instead of raising.
    check_secrets(settings)

    # Neo4j may still be electing itself on first boot; a failure here must not
    # kill the API, since /health/ready is what reports the real state.
    try:
        neo4j_db.apply_constraints()
    except Exception as exc:
        logger.warning("neo4j constraint bootstrap deferred: %s", exc)

    # Alembic, holding an advisory lock so concurrent workers cannot race. A
    # database created before Alembic existed is stamped at the baseline rather
    # than rebuilt. Failure is logged but does not kill the API: /health/ready
    # is what reports the real state, and an API that refuses to start cannot
    # even tell anyone why.
    try:
        result = migrations.upgrade_to_head()
        if result["stamped"]:
            logger.info("stamped pre-alembic database at baseline")
        if result["from_revision"] != result["to_revision"]:
            logger.info(
                "schema migrated: %s -> %s",
                result["from_revision"] or "unversioned",
                result["to_revision"],
            )
    except Exception as exc:
        logger.warning("postgres migration deferred: %s", exc)

    if settings.demo_mode:
        logger.info("DEMO_MODE=true - blockchain connectors will serve SYNTHETIC data")

    yield

    neo4j_db.close_driver()
    redis_client.close_client()


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description=(
            "Prototype for SIH26183. Traces victim-reported suspect wallet addresses "
            "to exchange/VASP clusters and scores their fraud linkage. "
            "Demonstration system built on synthetic complaints and public datasets - "
            "it holds no real NCRP complaint data and no exchange KYC data. "
            "Freeze and disclosure actions are recommendations only and require "
            "explicit human approval."
        ),
        lifespan=lifespan,
    )

    # Middleware is applied outermost-last, so this reads bottom-up: CORS wraps
    # everything (a 429 still needs its headers or the browser reports an
    # opaque network error), then security headers, then the limiter - which
    # means a throttled response is still hardened.
    if settings.rate_limit_enabled:
        app.add_middleware(
            RateLimitMiddleware,
            general_per_minute=settings.rate_limit_per_minute,
            auth_failures=settings.rate_limit_auth_failures,
            auth_window=settings.rate_limit_auth_window_seconds,
        )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router, prefix=settings.api_prefix)
    app.include_router(wallets.router, prefix=settings.api_prefix)
    app.include_router(wallet_analysis.router, prefix=settings.api_prefix)
    app.include_router(exposure.router, prefix=settings.api_prefix)
    app.include_router(auth.router, prefix=settings.api_prefix)
    app.include_router(cases.router, prefix=settings.api_prefix)
    app.include_router(freeze.router, prefix=settings.api_prefix)
    app.include_router(str_drafts.router, prefix=settings.api_prefix)
    app.include_router(ncrp.router, prefix=settings.api_prefix)
    app.include_router(ws.router, prefix=settings.api_prefix)
    return app


app = create_app()


@app.get("/")
def root() -> dict:
    return {
        "app": settings.app_name,
        "docs": "/docs",
        "health": f"{settings.api_prefix}/health/ready",
        "notice": "Synthetic / public-dataset demonstration system.",
    }
