"""Shared HTTP fetch for the live indexer connectors.

Every connector previously issued a bare `client.get()`. That is fine until the
first 429 - and the free tiers these connectors use are tight (Etherscan allows
5 calls/second, Blockchair less without a key). A single throttled response
would surface to the investigator as "trace failed", with the funds still
un-traced and no indication that waiting two seconds would have worked.

Two things fix that, and they compound:

  * **Retry with backoff** on the failures that are actually transient - 429,
    5xx, timeouts, connection resets. Never on a 4xx like 400 or 404, because a
    malformed address does not become well-formed on the second attempt.
  * **A short-lived response cache.** A 6-hop trace fans out across addresses
    and revisits the same ones repeatedly; without a cache the same address is
    fetched once per path that reaches it. Caching collapses that to one call,
    which is usually the difference between staying inside a rate limit and
    hammering into it.
"""

from __future__ import annotations

import email.utils
import hashlib
import json
import logging
import random
import time
from datetime import UTC, datetime

import httpx

from app.config import get_settings
from app.services.connectors.base import ConnectorError

logger = logging.getLogger(__name__)
settings = get_settings()

CACHE_PREFIX = "sih183:indexer:"

# Retried: the server asked us to slow down, or failed in a way that is not
# about our request. Everything else is our problem and retrying just wastes
# the caller's time and our rate-limit budget.
RETRY_STATUS = frozenset({408, 429, 500, 502, 503, 504})

# A server-supplied Retry-After is authoritative, but an unbounded one would
# hang the request. Past this we give up rather than sit on the connection.
MAX_RETRY_AFTER_SECONDS = 30.0


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Parse Retry-After, which is either a delta in seconds or an HTTP date."""
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _backoff(attempt: int, base: float = 0.5, cap: float = 8.0) -> float:
    """Exponential backoff with full jitter.

    Jitter is not decoration: without it, several traces throttled at the same
    moment retry in lockstep and reproduce the burst that caused the 429.
    """
    return random.uniform(0, min(cap, base * (2**attempt)))


def _cache_key(url: str, params: dict | None) -> str:
    """Key on the full request, with secrets stripped.

    API keys travel in the query string for these providers. They must not be
    written into a cache key, and two callers using different keys should still
    share a cached response for the same address.
    """
    secret_names = {"apikey", "key", "api_key"}
    safe = {k: v for k, v in sorted((params or {}).items()) if k.lower() not in secret_names}
    digest = hashlib.sha256(f"{url}|{json.dumps(safe, default=str)}".encode()).hexdigest()
    return f"{CACHE_PREFIX}{digest[:32]}"


def _cache_get(key: str) -> dict | None:
    if settings.connector_cache_ttl_seconds <= 0:
        return None
    try:
        from app.db import redis_client

        raw = redis_client.get_client().get(key)
        return json.loads(raw) if raw else None
    except Exception as exc:  # noqa: BLE001 - a cache miss and a cache outage are the same thing
        logger.debug("indexer cache read skipped: %s", exc)
        return None


def _cache_put(key: str, payload: dict) -> None:
    if settings.connector_cache_ttl_seconds <= 0:
        return
    try:
        from app.db import redis_client

        redis_client.get_client().setex(
            key, settings.connector_cache_ttl_seconds, json.dumps(payload)
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("indexer cache write skipped: %s", exc)


def get_json(
    client: httpx.Client,
    url: str,
    *,
    params: dict | None = None,
    source: str,
    use_cache: bool = True,
) -> dict | list:
    """GET a JSON document, retrying transient failures.

    Raises ConnectorError on a non-retryable status, on invalid JSON, or once
    the retry budget is spent. The message always names the provider, because
    "request failed" in a log tells an operator nothing about which of three
    indexers is having a bad day.
    """
    key = _cache_key(url, params) if use_cache else None
    if key:
        cached = _cache_get(key)
        if cached is not None:
            logger.debug("%s: cache hit for %s", source, url)
            return cached["payload"]

    attempts = max(1, settings.connector_max_attempts)
    last_error = "no attempt was made"

    for attempt in range(attempts):
        try:
            response = client.get(url, params=params)
        except httpx.HTTPError as exc:
            # Timeouts and connection resets: transient by nature.
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt + 1 >= attempts:
                break
            delay = _backoff(attempt)
            logger.warning(
                "%s: %s (attempt %d/%d), retrying in %.1fs",
                source, last_error, attempt + 1, attempts, delay,
            )
            time.sleep(delay)
            continue

        if response.status_code in RETRY_STATUS:
            last_error = f"HTTP {response.status_code}"
            if attempt + 1 >= attempts:
                break
            delay = _retry_after_seconds(response)
            if delay is not None and delay > MAX_RETRY_AFTER_SECONDS:
                raise ConnectorError(
                    f"{source} asked us to wait {delay:.0f}s (Retry-After), which is longer "
                    f"than this request can reasonably block. Try again later."
                )
            if delay is None:
                delay = _backoff(attempt)
            logger.warning(
                "%s: %s (attempt %d/%d), retrying in %.1fs",
                source, last_error, attempt + 1, attempts, delay,
            )
            time.sleep(delay)
            continue

        if response.status_code >= 400:
            # Not transient. Retrying a 400 or a 404 only burns rate limit.
            raise ConnectorError(
                f"{source} returned HTTP {response.status_code}: {response.text[:200]}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise ConnectorError(
                f"{source} returned a non-JSON response: {response.text[:200]}"
            ) from exc

        if key:
            _cache_put(key, {"payload": payload, "fetched_at": datetime.now(UTC).isoformat()})
        return payload

    raise ConnectorError(
        f"{source} request failed after {attempts} attempt(s): {last_error}"
    )
