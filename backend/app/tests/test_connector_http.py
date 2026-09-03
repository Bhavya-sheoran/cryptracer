"""Retry, backoff and caching for the live indexer connectors.

Live mode is off by default and is the least-exercised path in the system, so
this is the only place its failure handling is proved. Every request here is
served by an in-process transport - no test in this file touches the network.
"""

from __future__ import annotations

import httpx
import pytest

from app.services.connectors import http as conn_http
from app.services.connectors.base import ConnectorError


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch):
    """Record backoff delays instead of waiting them out."""
    slept: list[float] = []
    monkeypatch.setattr(conn_http.time, "sleep", slept.append)
    return slept


@pytest.fixture(autouse=True)
def no_cache(monkeypatch):
    """Default to caching off, so retry behaviour is measured in isolation.

    Redis is shared with the running stack; a test that cached a response would
    leak into the next test and into the live app.
    """
    monkeypatch.setattr(conn_http.settings, "connector_cache_ttl_seconds", 0)


def transport(*responses):
    """Serve the given responses in order; the last one repeats."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        i = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        result = responses[i]
        if isinstance(result, Exception):
            raise result
        return result

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return client, calls


def ok(payload=None):
    return httpx.Response(200, json=payload if payload is not None else {"result": "ok"})


# --- retry ---------------------------------------------------------------


def test_a_throttled_request_is_retried_and_succeeds(no_sleeping):
    client, calls = transport(httpx.Response(429), httpx.Response(429), ok({"data": 1}))

    result = conn_http.get_json(client, "https://x.test/a", source="etherscan")

    assert result == {"data": 1}
    assert calls["n"] == 3
    assert len(no_sleeping) == 2, "should have backed off before each retry"


def test_server_errors_are_retried():
    client, calls = transport(httpx.Response(503), ok())
    conn_http.get_json(client, "https://x.test/a", source="blockchair")
    assert calls["n"] == 2


def test_transport_failures_are_retried():
    client, calls = transport(httpx.ConnectTimeout("timed out"), ok())
    conn_http.get_json(client, "https://x.test/a", source="trongrid")
    assert calls["n"] == 2


def test_client_errors_are_not_retried():
    """A malformed address does not become well-formed on the second attempt."""
    client, calls = transport(httpx.Response(400, text="bad address"))

    with pytest.raises(ConnectorError) as exc:
        conn_http.get_json(client, "https://x.test/a", source="etherscan")

    assert calls["n"] == 1, "retrying a 400 only burns rate limit"
    assert "HTTP 400" in str(exc.value)
    assert "etherscan" in str(exc.value), "the message must name the provider"


def test_retry_budget_is_finite(monkeypatch):
    monkeypatch.setattr(conn_http.settings, "connector_max_attempts", 3)
    client, calls = transport(httpx.Response(429))

    with pytest.raises(ConnectorError) as exc:
        conn_http.get_json(client, "https://x.test/a", source="etherscan")

    assert calls["n"] == 3
    assert "after 3 attempt(s)" in str(exc.value)


def test_non_json_body_is_reported_not_swallowed():
    client, _ = transport(httpx.Response(200, text="<html>maintenance</html>"))

    with pytest.raises(ConnectorError) as exc:
        conn_http.get_json(client, "https://x.test/a", source="blockchair")

    assert "non-JSON" in str(exc.value)


# --- Retry-After ---------------------------------------------------------


def test_retry_after_header_is_honoured(no_sleeping):
    client, _ = transport(httpx.Response(429, headers={"Retry-After": "2"}), ok())

    conn_http.get_json(client, "https://x.test/a", source="etherscan")

    assert no_sleeping == [2.0], "a server-stated delay beats our own backoff"


def test_retry_after_as_http_date(no_sleeping):
    client, _ = transport(
        httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2099 07:28:00 GMT"}), ok()
    )
    # Far-future date: longer than we will block for, so this refuses rather
    # than sleeping for decades.
    with pytest.raises(ConnectorError) as exc:
        conn_http.get_json(client, "https://x.test/a", source="etherscan")

    assert "Retry-After" in str(exc.value)
    assert no_sleeping == []


def test_an_absurd_retry_after_is_refused_rather_than_waited_out(no_sleeping):
    client, _ = transport(httpx.Response(429, headers={"Retry-After": "3600"}), ok())

    with pytest.raises(ConnectorError) as exc:
        conn_http.get_json(client, "https://x.test/a", source="trongrid")

    assert "3600s" in str(exc.value)
    assert no_sleeping == [], "must not block the request for an hour"


# --- caching -------------------------------------------------------------


def test_cache_key_excludes_the_api_key():
    """Two officers with different keys must share a cached response.

    And the key itself must never be written into Redis as part of a key name.
    """
    with_key = conn_http._cache_key("https://x.test/a", {"address": "0xabc", "apikey": "SECRET"})
    other_key = conn_http._cache_key("https://x.test/a", {"address": "0xabc", "apikey": "OTHER"})
    no_key = conn_http._cache_key("https://x.test/a", {"address": "0xabc"})

    assert with_key == other_key == no_key
    assert "SECRET" not in with_key


def test_cache_key_separates_different_addresses():
    a = conn_http._cache_key("https://x.test/a", {"address": "0xaaa"})
    b = conn_http._cache_key("https://x.test/a", {"address": "0xbbb"})
    assert a != b


def test_a_repeated_fetch_hits_the_cache(monkeypatch):
    """The point of the cache: a trace revisiting an address costs one call."""
    store: dict[str, str] = {}

    class FakeRedis:
        def get(self, k):
            return store.get(k)

        def setex(self, k, _ttl, v):
            store[k] = v

    monkeypatch.setattr(conn_http.settings, "connector_cache_ttl_seconds", 300)

    import app.db.redis_client as redis_client

    monkeypatch.setattr(redis_client, "get_client", lambda: FakeRedis())

    client, calls = transport(ok({"data": "first"}))
    params = {"address": "0xabc"}

    first = conn_http.get_json(client, "https://x.test/a", params=params, source="etherscan")
    second = conn_http.get_json(client, "https://x.test/a", params=params, source="etherscan")

    assert first == second == {"data": "first"}
    assert calls["n"] == 1, "the second call should have been served from cache"


def test_a_redis_outage_does_not_fail_the_request(monkeypatch):
    """A cache that breaks must degrade to no cache, not to no answer."""
    monkeypatch.setattr(conn_http.settings, "connector_cache_ttl_seconds", 300)

    import app.db.redis_client as redis_client

    def boom():
        raise ConnectionError("redis is down")

    monkeypatch.setattr(redis_client, "get_client", boom)

    client, calls = transport(ok({"data": 1}))
    assert conn_http.get_json(client, "https://x.test/a", source="etherscan") == {"data": 1}
    assert calls["n"] == 1


def test_failures_are_never_cached(monkeypatch):
    """Caching a 429 would turn a two-second blip into a five-minute outage."""
    store: dict[str, str] = {}

    class FakeRedis:
        def get(self, k):
            return store.get(k)

        def setex(self, k, _ttl, v):
            store[k] = v

    monkeypatch.setattr(conn_http.settings, "connector_cache_ttl_seconds", 300)
    monkeypatch.setattr(conn_http.settings, "connector_max_attempts", 1)

    import app.db.redis_client as redis_client

    monkeypatch.setattr(redis_client, "get_client", lambda: FakeRedis())

    client, _ = transport(httpx.Response(429))
    with pytest.raises(ConnectorError):
        conn_http.get_json(client, "https://x.test/a", source="etherscan")

    assert store == {}


# --- backoff shape -------------------------------------------------------


def test_backoff_grows_and_is_capped():
    assert conn_http._backoff(0, base=0.5, cap=8.0) <= 0.5
    assert conn_http._backoff(3, base=0.5, cap=8.0) <= 4.0
    assert conn_http._backoff(20, base=0.5, cap=8.0) <= 8.0


def test_backoff_is_jittered():
    """Lockstep retries recreate the burst that caused the throttling."""
    samples = {conn_http._backoff(5) for _ in range(40)}
    assert len(samples) > 1
