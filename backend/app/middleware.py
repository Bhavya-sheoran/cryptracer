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
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

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
