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
    freeze,
    health,
    ncrp,
    str_drafts,
    wallet_analysis,
    wallets,
    ws,
)
from app.config import get_settings
from app.db import neo4j as neo4j_db
from app.db import redis_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Neo4j may still be electing itself on first boot; a failure here must not
    # kill the API, since /health/ready is what reports the real state.
    try:
        neo4j_db.apply_constraints()
    except Exception as exc:
        logger.warning("neo4j constraint bootstrap deferred: %s", exc)

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
