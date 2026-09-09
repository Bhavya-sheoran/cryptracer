"""Cross-cutting HTTP middleware: security headers and rate limiting.

Both were flagged by `scripts/security_probe.py`. Neither is exotic; both are
the kind of control whose absence is noticed in a security review long before
anything clever is.

Rate limiting counts *failed* logins rather than all of them, so an officer
signing in repeatedly during a demonstration is never locked out while a
password-guessing loop is stopped after a handful of attempts.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app import metrics
from app.logging_config import request_id_var

logger = logging.getLogger(__name__)

# Swagger UI pulls its bundle from jsdelivr, so the docs pages need a policy
# that permits it. Everything else is an API response that should execute
# nothing at all if a browser is ever tricked into rendering it.
_DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")

_API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
_DOCS_CSP = (
    "default-src 'self'; "
    "script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
    "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
    "img-src 'self' https://fastapi.tiangolo.com data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'"
)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Give every request an id, log its outcome, and return the id.

    The id is echoed in `X-Request-ID` so an officer reporting "my trace
    failed" can quote something that finds the exact log lines, rather than
    describing what they were doing and hoping the timestamps line up.

    An inbound `X-Request-ID` is honoured so a reverse proxy or a calling
    service can correlate across hops - but it is length-capped and stripped of
    anything unusual first. It ends up in log output, and an unbounded
    client-controlled string in a log file is how log injection works.
    """

    MAX_ID_LENGTH = 64
    #: Anything outside this is dropped rather than escaped. Request ids are
    #: opaque identifiers; there is no legitimate reason for one to contain a
    #: newline, a quote, or a control character.
    _SAFE_ID = re.compile(r"[^A-Za-z0-9._-]")

    #: Health probes run on a timer and would otherwise dominate the log.
    QUIET_PATHS = ("/api/v1/health",)

    def _incoming_id(self, request: Request) -> str | None:
        raw = request.headers.get("X-Request-ID", "").strip()
        if not raw:
            return None
        cleaned = self._SAFE_ID.sub("", raw)[: self.MAX_ID_LENGTH]
        return cleaned or None

    async def dispatch(self, request: Request, call_next):
        request_id = self._incoming_id(request) or uuid.uuid4().hex[:16]
        token = request_id_var.set(request_id)
        started = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            # Logged here because the exception handler that turns this into a
            # 500 runs outside the request context, where the id is already
            # gone - so this is the last place the failure and its id coexist.
            logger.exception(
                "request failed",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                },
            )
            request_id_var.reset(token)
            raise

        elapsed = time.perf_counter() - started
        duration_ms = round(elapsed * 1000, 1)
        response.headers["X-Request-ID"] = request_id

        # Recorded for health paths too. A readiness probe that starts taking
        # two seconds is an early symptom of a datastore in trouble, and
        # excluding it from metrics the way it is excluded from logs would
        # discard exactly that signal.
        endpoint = metrics.normalise_endpoint(request.url.path)
        metrics.http_requests_total.labels(
            method=request.method, endpoint=endpoint, status=str(response.status_code)
        ).inc()
        metrics.http_request_duration_seconds.labels(
            method=request.method, endpoint=endpoint
        ).observe(elapsed)

        if not request.url.path.startswith(self.QUIET_PATHS):
            logger.log(
                logging.WARNING if response.status_code >= 500 else logging.INFO,
                "%s %s -> %s",
                request.method,
                request.url.path,
                response.status_code,
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": duration_ms,
                },
            )

        request_id_var.reset(token)
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Standard hardening headers on every response."""

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        headers = response.headers

        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "no-referrer")
        headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")

        is_docs = request.url.path.rstrip("/") in _DOCS_PATHS
        headers.setdefault("Content-Security-Policy", _DOCS_CSP if is_docs else _API_CSP)

        # Only meaningful over TLS, and actively unhelpful on plain HTTP during
        # local development, where it would pin localhost to https.
        if request.url.scheme == "https":
            headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )

        # Nothing this API returns belongs in a shared cache: every
        # authenticated response is specific to one officer.
        if request.url.path.startswith("/api/"):
            headers.setdefault("Cache-Control", "no-store")

        return response


class _Counter:
    """Fixed-window counter, Redis-backed with an in-process fallback.

    Redis keeps the window shared across workers, which is what matters when
    the API is scaled out. If Redis is unreachable the limiter degrades to
    per-process counting rather than failing open completely - a partial limit
    is still a limit, and a rate limiter that takes the API down with it when
    the cache blips is the worse failure.
    """

    PREFIX = "sih183:ratelimit:"

    def __init__(self) -> None:
        self._local: dict[str, tuple[int, float]] = {}

    def _local_hit(self, key: str, window: int, *, increment: bool) -> int:
        now = time.time()
        count, expires = self._local.get(key, (0, 0.0))
        if now >= expires:
            count, expires = 0, now + window
        if increment:
            count += 1
        self._local[key] = (count, expires)

        if len(self._local) > 10_000:  # bound the dict; windows are short
            self._local = {k: v for k, v in self._local.items() if v[1] > now}
        return count

    def hit(self, key: str, window: int, *, increment: bool = True) -> int:
        """Return the count in the current window, optionally counting this call."""
        from app.db import redis_client

        try:
            client = redis_client.get_client()
            full = f"{self.PREFIX}{key}"
            if not increment:
                return int(client.get(full) or 0)
            pipe = client.pipeline()
            pipe.incr(full)
            pipe.expire(full, window, nx=True)  # only on the first hit of a window
            return int(pipe.execute()[0])
        except Exception as exc:  # noqa: BLE001 - any Redis failure degrades the same way
            logger.debug("rate limit falling back to in-process counting: %s", exc)
            return self._local_hit(key, window, increment=increment)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Two limits: a broad per-IP ceiling and a tight one on login failures.

    Health probes are exempt - the container healthcheck polls them on a timer
    and must never be throttled into reporting the service as down.
    """

    EXEMPT_PREFIXES = ("/api/v1/health", "/docs", "/redoc", "/openapi.json")

    def __init__(self, app, *, general_per_minute: int, auth_failures: int, auth_window: int):
        super().__init__(app)
        self.general_per_minute = general_per_minute
        self.auth_failures = auth_failures
        self.auth_window = auth_window
        self.counter = _Counter()

    @staticmethod
    def _client_key(request: Request) -> str:
        """Identify the caller.

        X-Forwarded-For is honoured only when it parses as an IP, and only the
        first entry. It is client-controlled, so this is a convenience for a
        trusted reverse proxy, never an authorisation input.
        """
        forwarded = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        if forwarded:
            try:
                return str(ipaddress.ip_address(forwarded))
            except ValueError:
                pass
        return request.client.host if request.client else "unknown"

    def _too_many(self, retry_after: int, detail: str) -> JSONResponse:
        return JSONResponse(
            status_code=429,
            content={"detail": detail},
            headers={"Retry-After": str(retry_after)},
        )

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path.startswith(self.EXEMPT_PREFIXES):
            return await call_next(request)

        client = self._client_key(request)
        is_login = path.endswith("/auth/login") and request.method == "POST"

        # Checked before the request runs, so a locked-out client never reaches
        # the password comparison at all.
        if is_login:
            failures = self.counter.hit(f"authfail:{client}", self.auth_window, increment=False)
            if failures >= self.auth_failures:
                logger.warning("rate limit: login blocked for %s (%d failures)", client, failures)
                return self._too_many(
                    self.auth_window,
                    "Too many failed sign-in attempts. Try again in "
                    f"{max(1, self.auth_window // 60)} minutes.",
                )

        count = self.counter.hit(f"general:{client}", 60)
        if count > self.general_per_minute:
            return self._too_many(60, "Rate limit exceeded. Slow down and retry shortly.")

        response = await call_next(request)

        # Count the failure only after seeing the outcome: successful sign-ins
        # cost nothing, so ordinary use can never trip the lockout.
        if is_login and response.status_code == 401:
            self.counter.hit(f"authfail:{client}", self.auth_window)

        return response
