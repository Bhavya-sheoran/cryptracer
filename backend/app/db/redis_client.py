"""Redis connection used for the alert stream (see services/alerts.py, Phase 4)."""

import redis

from app.config import get_settings

settings = get_settings()

# Stream key that Phase 4 alert fan-out publishes to.
ALERT_STREAM = "sih183:alerts"

_client: redis.Redis | None = None


def get_client() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(settings.redis_url, decode_responses=True)
    return _client


def close_client() -> None:
    global _client
    if _client is not None:
        _client.close()
        _client = None


def ping() -> bool:
    return bool(get_client().ping())
