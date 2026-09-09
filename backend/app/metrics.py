"""Prometheus metrics.

Chosen for what would actually wake someone up, not for what is easy to count.

The one that matters most here is `upstream_calls_total`. Every traced address
spends calls against a rate-limited third-party quota, and the person who
exhausts a day's allowance does it with a single click having been given no
indication the click was expensive. A counter on that is the difference between
noticing at 40% and discovering at 100%, when every trace has already started
failing.

`indexer_errors_total` is second, because an indexer failing does not break the
system visibly - it silently degrades every trace into a shorter one, and the
answer still looks like an answer.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

#: Own registry rather than the global default. The default collects process
#: and GC metrics from any library that happens to import prometheus_client,
#: which makes what we deliberately expose hard to find among what we did not.
REGISTRY = CollectorRegistry()

http_requests_total = Counter(
    "chaintrace_http_requests_total",
    "HTTP requests handled.",
    ["method", "endpoint", "status"],
    registry=REGISTRY,
)

http_request_duration_seconds = Histogram(
    "chaintrace_http_request_duration_seconds",
    "HTTP request duration.",
    ["method", "endpoint"],
    # Tuned to this application rather than left at the library defaults. A
    # trace legitimately takes seconds, so the default top bucket of 10s would
    # put the interesting tail in +Inf where it cannot be distinguished.
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
    registry=REGISTRY,
)

upstream_calls_total = Counter(
    "chaintrace_upstream_calls_total",
    "Calls made to external blockchain indexers. This is the rate-limited quota.",
    ["source"],
    registry=REGISTRY,
)

indexer_errors_total = Counter(
    "chaintrace_indexer_errors_total",
    "Failed calls to external indexers. Degrades traces silently, so alert on it.",
    ["source"],
    registry=REGISTRY,
)

trace_duration_seconds = Histogram(
    "chaintrace_trace_duration_seconds",
    "End-to-end wallet trace duration.",
    ["chain"],
    buckets=(0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0),
    registry=REGISTRY,
)

traces_total = Counter(
    "chaintrace_traces_total",
    "Wallet traces run, by chain and completeness.",
    ["chain", "complete"],
    registry=REGISTRY,
)

alerts_published_total = Counter(
    "chaintrace_alerts_published_total",
    "Alerts pushed onto the Redis stream.",
    ["severity"],
    registry=REGISTRY,
)

datastore_up = Gauge(
    "chaintrace_datastore_up",
    "1 when the datastore answered its last health check, 0 when it did not.",
    ["datastore"],
    registry=REGISTRY,
)

model_available = Gauge(
    "chaintrace_illicit_model_available",
    "1 when the illicit-transaction classifier is loaded and serving.",
    registry=REGISTRY,
)


def normalise_endpoint(path: str) -> str:
    """Collapse identifiers out of a path so the label set stays bounded.

    `/api/v1/cases/<uuid>` must become `/api/v1/cases/{id}`. Without this every
    case id becomes its own time series - cardinality that grows with usage is
    how a metrics backend is brought down by the thing meant to monitor it.
    """
    parts = []
    for segment in path.split("/"):
        if not segment:
            continue

        # Address shapes are tested BEFORE the generic id shape. An Ethereum
        # address is long and contains digits, so the generic rule matches it
        # too - and whichever runs first wins the label. Either collapses the
        # cardinality correctly; only this order labels it truthfully.
        if segment.startswith("0x") and len(segment) >= 20:
            parts.append("{address}")
        elif len(segment) >= 26 and segment.isalnum() and not segment.isdigit():
            # Base58 - Bitcoin and Tron. Excludes all-digit segments, which are
            # far more likely to be a page number or a limit than an address.
            parts.append("{address}")
        elif len(segment) >= 16 and any(c.isdigit() for c in segment):
            parts.append("{id}")
        else:
            parts.append(segment)
    return "/" + "/".join(parts)
