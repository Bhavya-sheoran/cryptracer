"""Application settings, loaded from environment / .env.

Assumptions recorded here rather than blocking on a question (see README):
  * Redis Streams is the event bus, not Kafka - one less container for a demo
    stack, and the publish path is isolated behind services/alerts.py so a
    Kafka swap is a driver change, not a rewrite.
  * Auth is a simplified JWT carrying a role claim rather than Keycloak.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "SIH26183 Fraud-Linked Exchange Identification"
    api_prefix: str = "/api/v1"

    # --- Datastores ---------------------------------------------------------
    database_url: str = "postgresql+psycopg://sih:sihdev@localhost:5432/sih183"
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "sihdevpass"
    redis_url: str = "redis://localhost:6379/0"

    # --- Auth ---------------------------------------------------------------
    jwt_secret: str = "change-me-in-production-please"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 480

    # --- Demo / honesty switch ---------------------------------------------
    # When true, blockchain connectors serve synthetic data and the UI shows a
    # permanent "SYNTHETIC DEMO DATA" banner. This flag is surfaced by
    # /api/v1/health/ready so the frontend cannot silently present synthetic
    # results as live-chain results.
    demo_mode: bool = True

    # --- Blockchain indexer APIs (all optional) -----------------------------
    etherscan_api_key: str = ""
    trongrid_api_key: str = ""
    blockchair_api_key: str = ""

    # --- Tracing ------------------------------------------------------------
    trace_max_depth: int = 8
    trace_max_breadth: int = 25

    # --- Risk scoring -------------------------------------------------------
    risk_decay_half_life_days: int = 90
    risk_medium_threshold: float = 40.0
    risk_high_threshold: float = 70.0

    # --- CORS ---------------------------------------------------------------
    cors_origins: str = "http://localhost:5173"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
