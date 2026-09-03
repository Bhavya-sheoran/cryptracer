"""Application settings, loaded from environment / .env.

Assumptions recorded here rather than blocking on a question (see README):
  * Redis Streams is the event bus, not Kafka - one less container for a demo
    stack, and the publish path is isolated behind services/alerts.py so a
    Kafka swap is a driver change, not a rewrite.
  * Auth is a simplified JWT carrying a role claim rather than Keycloak.
"""

import logging
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Named rather than inlined so `check_secrets()` can recognise it, and so the
# comparison cannot drift from the value it is meant to catch.
DEFAULT_JWT_SECRET = "insecure-demo-secret-replace-before-any-real-deployment"

# RFC 7518 §3.2: an HMAC key for HS256 should be at least as long as the hash
# output. PyJWT warns below this; we would rather fail than warn.
MIN_JWT_SECRET_BYTES = 32


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
    # The default is deliberately long enough to satisfy RFC 7518 §3.2 (HMAC
    # keys for SHA-256 should be at least 32 bytes) and deliberately obvious
    # about being a placeholder. `check_secrets()` below refuses to start with
    # it outside demo mode.
    jwt_secret: str = DEFAULT_JWT_SECRET
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

    # --- Live indexer HTTP behaviour ---------------------------------------
    # Applies only when DEMO_MODE=false. Four attempts covers the brief 429s
    # the free tiers hand out without leaving an investigator watching a
    # spinner. The cache is short because chain data is append-only but a trace
    # started a minute later should still see a new transaction.
    connector_max_attempts: int = 4
    connector_cache_ttl_seconds: int = 300

    # --- Risk scoring -------------------------------------------------------
    risk_decay_half_life_days: int = 90
    risk_medium_threshold: float = 40.0
    risk_high_threshold: float = 70.0

    # --- CORS ---------------------------------------------------------------
    cors_origins: str = "http://localhost:5173"

    # --- Rate limiting ------------------------------------------------------
    # The general ceiling is set well above what the dashboard or the e2e
    # script produce; it is there to stop scripted abuse, not to pace normal
    # use. The auth limit counts failed sign-ins only, so an officer signing in
    # repeatedly during a demonstration is never locked out.
    rate_limit_enabled: bool = True
    rate_limit_per_minute: int = 240
    rate_limit_auth_failures: int = 8
    rate_limit_auth_window_seconds: int = 300

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


class InsecureConfiguration(RuntimeError):
    """Raised when the process is configured in a way that must not run for real."""


def check_secrets(settings: Settings) -> list[str]:
    """Validate secrets at startup. Returns the problems found.

    In demo mode these are logged as warnings: the whole point of the demo
    stack is that it runs with `docker compose up` and no secret management.
    With DEMO_MODE=false the same problems raise instead, because that flag is
    the system's own claim to be handling real data - and a placeholder signing
    key underneath that claim would let anyone mint a supervisor token and
    approve a freeze.
    """
    problems: list[str] = []

    if settings.jwt_secret == DEFAULT_JWT_SECRET:
        problems.append(
            "JWT_SECRET is the shipped default. Anyone with the source can forge "
            "a token for any role, including one that approves a freeze."
        )
    if len(settings.jwt_secret.encode()) < MIN_JWT_SECRET_BYTES:
        problems.append(
            f"JWT_SECRET is {len(settings.jwt_secret.encode())} bytes; HS256 needs at "
            f"least {MIN_JWT_SECRET_BYTES} (RFC 7518 section 3.2)."
        )
    if not settings.demo_mode and settings.neo4j_password == "sihdevpass":
        problems.append("NEO4J_PASSWORD is the shipped development password.")

    if not problems:
        return problems

    if settings.demo_mode:
        for problem in problems:
            logger.warning("INSECURE CONFIG (tolerated because DEMO_MODE=true): %s", problem)
    else:
        how_to_fix = 'python -c "import secrets; print(secrets.token_urlsafe(48))"'
        raise InsecureConfiguration(
            "Refusing to start with DEMO_MODE=false and insecure configuration:\n  - "
            + "\n  - ".join(problems)
            + f"\nGenerate a key with: {how_to_fix}"
        )
    return problems
