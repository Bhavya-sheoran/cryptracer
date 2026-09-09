"""Structured logging, request correlation and metrics.

The two properties that carry weight here are both about not trusting input and
not growing without bound: a client-supplied request id ends up in log files, and
a path segment ends up as a metric label. Both are places where accepting what
you were given causes a problem much later and somewhere else.
"""

from __future__ import annotations

import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import metrics
from app.logging_config import ConsoleFormatter, JsonFormatter, request_id_var
from app.middleware import RequestContextMiddleware


def build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/api/v1/thing")
    def thing():
        logging.getLogger("app.test").info("handling")
        return {"ok": True}

    @app.get("/api/v1/boom")
    def boom():
        raise RuntimeError("deliberate")

    app.add_middleware(RequestContextMiddleware)
    return app


@pytest.fixture
def client():
    return TestClient(build_app(), raise_server_exceptions=False)


# --- request correlation -------------------------------------------------


def test_every_response_carries_a_request_id(client):
    response = client.get("/api/v1/thing")
    assert response.headers.get("X-Request-ID")


def test_ids_differ_between_requests(client):
    first = client.get("/api/v1/thing").headers["X-Request-ID"]
    second = client.get("/api/v1/thing").headers["X-Request-ID"]
    assert first != second


def test_inbound_id_is_honoured(client):
    """So a proxy or calling service can correlate across hops."""
    response = client.get("/api/v1/thing", headers={"X-Request-ID": "upstream-abc123"})
    assert response.headers["X-Request-ID"] == "upstream-abc123"


def test_inbound_id_is_stripped_of_anything_unusual(client):
    """The id reaches log files. Unbounded client text there is log injection.

    A newline in particular lets an attacker forge whole log lines - inventing
    an entry that says an approval happened, for instance.
    """
    hostile = 'evil"\r\ninjected: line'
    response = client.get("/api/v1/thing", headers={"X-Request-ID": hostile})
    returned = response.headers["X-Request-ID"]

    assert "\n" not in returned
    assert "\r" not in returned
    assert '"' not in returned
    assert returned == "evilinjectedline"


def test_inbound_id_is_length_capped(client):
    response = client.get("/api/v1/thing", headers={"X-Request-ID": "A" * 500})
    assert len(response.headers["X-Request-ID"]) == RequestContextMiddleware.MAX_ID_LENGTH


def test_all_punctuation_id_falls_back_to_a_generated_one(client):
    """Sanitising to nothing must not produce an empty id."""
    response = client.get("/api/v1/thing", headers={"X-Request-ID": "!!!///"})
    assert response.headers["X-Request-ID"]


def test_context_does_not_leak_between_requests(client):
    client.get("/api/v1/thing", headers={"X-Request-ID": "first-request"})
    assert request_id_var.get() is None, "the id outlived its request"


def test_a_failing_request_still_resets_context(client):
    client.get("/api/v1/boom")
    assert request_id_var.get() is None


# --- formatters ----------------------------------------------------------


def record(**kwargs) -> logging.LogRecord:
    rec = logging.LogRecord(
        name="app.test", level=logging.INFO, pathname="x.py", lineno=1,
        msg="traced %s", args=("wallet",), exc_info=None,
    )
    for key, value in kwargs.items():
        setattr(rec, key, value)
    return rec


def test_json_formatter_emits_one_parseable_object():
    payload = json.loads(JsonFormatter().format(record(request_id="abc")))

    assert payload["message"] == "traced wallet"
    assert payload["level"] == "INFO"
    assert payload["request_id"] == "abc"
    assert "timestamp" in payload


def test_json_formatter_includes_structured_extras():
    payload = json.loads(JsonFormatter().format(record(chain="BTC", hops=7)))
    assert payload["chain"] == "BTC"
    assert payload["hops"] == 7


def test_json_formatter_does_not_drop_a_line_over_an_odd_value():
    """A record that fails to serialise is a record lost when it mattered."""

    class Unserialisable:
        def __repr__(self) -> str:
            return "<obj>"

    payload = json.loads(JsonFormatter().format(record(thing=Unserialisable())))
    assert payload["thing"] == "<obj>"
    assert payload["message"] == "traced wallet"


def test_json_formatter_omits_request_id_when_there_is_none():
    payload = json.loads(JsonFormatter().format(record(request_id=None)))
    assert "request_id" not in payload


def test_console_formatter_appends_the_id():
    assert "[req xyz]" in ConsoleFormatter().format(record(request_id="xyz"))


def test_console_formatter_stays_clean_without_one():
    assert "[req" not in ConsoleFormatter().format(record(request_id=None))


# --- metrics -------------------------------------------------------------


def test_request_metrics_are_recorded(client):
    before = metrics.http_requests_total.labels(
        method="GET", endpoint="/api/v1/thing", status="200"
    )._value.get()

    client.get("/api/v1/thing")

    after = metrics.http_requests_total.labels(
        method="GET", endpoint="/api/v1/thing", status="200"
    )._value.get()
    assert after == before + 1


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/api/v1/cases/3f2b9c1e-1111-2222-3333-444455556666", "/api/v1/cases/{id}"),
        ("/api/v1/wallet/0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045", "/api/v1/wallet/{address}"),
        ("/api/v1/health/ready", "/api/v1/health/ready"),
        ("/api/v1/exchanges/ranked", "/api/v1/exchanges/ranked"),
    ],
)
def test_identifiers_are_collapsed_out_of_metric_labels(path, expected):
    """Unbounded label cardinality is how a metrics backend gets taken down.

    One time series per case id grows without limit as the system is used -
    the monitoring fails precisely when there is most to monitor.
    """
    assert metrics.normalise_endpoint(path) == expected


def test_two_different_ids_produce_one_series():
    a = metrics.normalise_endpoint("/api/v1/cases/aaaaaaaa-1111-2222-3333-444444444444")
    b = metrics.normalise_endpoint("/api/v1/cases/bbbbbbbb-5555-6666-7777-888888888888")
    assert a == b


def test_metrics_use_a_dedicated_registry():
    """Keeps our deliberate metrics apart from whatever a library registers."""
    names = {m.name for m in metrics.REGISTRY.collect()}
    assert any(n.startswith("chaintrace_") for n in names)
    assert not any(n.startswith("python_gc") for n in names)
