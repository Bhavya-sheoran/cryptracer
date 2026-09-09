"""Structured logging with request correlation.

Two problems this solves.

The first is machine-readability. Plain-text logs are fine to read one at a
time and useless in aggregate: "which addresses failed to trace yesterday" is a
grep-and-hope question against text and a filter against JSON.

The second, and the reason this exists at all, is correlation. An officer says
"the trace I ran this morning failed". Without a request id threaded through
every line, finding the twenty log lines belonging to that one request among
thousands means guessing from timestamps. With one, it is a single filter - and
the id is returned in the response header and in error bodies, so the officer
can quote it.

Format is configurable because the two audiences differ: a developer reading a
terminal wants aligned columns, a log aggregator wants JSON. Defaulting to
JSON in a dev terminal makes people turn logging down, which is worse than
either.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import UTC, datetime

#: Set per-request by RequestContextMiddleware; read by the filter below.
#: A ContextVar rather than a thread-local because the app is async - several
#: requests share a thread, and a thread-local would leak one request's id into
#: another's log lines.
request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)

#: Attributes LogRecord always carries. Anything else a caller attached via
#: `extra=` is application context worth emitting.
_STANDARD_RECORD_FIELDS = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "module", "msecs",
        "message", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName", "taskName",
        "request_id",
    }
)


class RequestIdFilter(logging.Filter):
    """Attach the current request id to every record.

    A filter rather than an adapter so it applies to third-party loggers too -
    an error raised inside sqlalchemy or neo4j during a request is exactly the
    line you most want correlated, and those libraries know nothing about us.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        request_id = getattr(record, "request_id", None)
        if request_id:
            payload["request_id"] = request_id

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        # Anything passed as `extra=` - the structured half of structured
        # logging. Values are coerced to str when not JSON-serialisable rather
        # than dropping the line: a log record that fails to emit because of a
        # stray object is a log record lost exactly when it mattered.
        for key, value in record.__dict__.items():
            if key in _STANDARD_RECORD_FIELDS or key.startswith("_"):
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = str(value)

        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    """Human-readable, with the request id appended when there is one."""

    DEFAULT_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

    def __init__(self) -> None:
        super().__init__(fmt=self.DEFAULT_FORMAT, datefmt="%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        request_id = getattr(record, "request_id", None)
        return f"{line}  [req {request_id}]" if request_id else line


def configure_logging(log_format: str = "console", level: str = "INFO") -> None:
    """Install the root handler. Safe to call more than once.

    Existing handlers are removed rather than added to: uvicorn installs its
    own, and leaving them in place produces every line twice - which looks like
    a duplicated request and wastes real time during an incident.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if log_format == "json" else ConsoleFormatter())
    handler.addFilter(RequestIdFilter())

    root.addHandler(handler)
    root.setLevel(level.upper())

    # uvicorn's access log duplicates what RequestContextMiddleware records,
    # minus the request id and the duration. Silenced rather than left to
    # double every line.
    logging.getLogger("uvicorn.access").handlers = []
    logging.getLogger("uvicorn.access").propagate = False

    for noisy in ("uvicorn.error", "neo4j", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # Alembic logs a dozen "setup plugin" lines every time migrations are
    # checked, which is on every worker start. The migration result is logged
    # by app.main with the detail that actually matters (stamped / from / to),
    # so the library's own chatter is suppressed and its warnings kept.
    for chatty in ("alembic.runtime.plugins", "alembic.runtime.migration"):
        logging.getLogger(chatty).setLevel(logging.WARNING)
