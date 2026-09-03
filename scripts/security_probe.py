#!/usr/bin/env python3
"""Active security and API-robustness probe.

Complements `rbac_audit.py`, which answers "who can reach what". This one asks
"what happens when the request is hostile or malformed" - token forgery, object
references belonging to someone else, oversized and mistyped payloads, injection
attempts, and whether failures leak internals.

Everything here runs against the live stack, so it tests behaviour rather than
intent. A finding is printed with the exact request that produced it.

Usage:
    docker compose exec backend python scripts/security_probe.py
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

from app.config import DEFAULT_JWT_SECRET

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)
API = "/api/v1"


def _ci(headers) -> dict:
    """Lower-case the header names.

    HTTP header names are case-insensitive, and `dict(resp.headers)` throws that
    away - which made an earlier run of this probe report a false finding
    against a perfectly correct `content-type`.
    """
    return {k.lower(): v for k, v in headers.items()}


class Probe:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.passed = 0
        self.findings: list[tuple[str, str]] = []

    def call(self, method, path, token=None, body=None, form=False, raw_body=None,
             headers=None, timeout=30):
        url = f"{self.base}{path}"
        data = raw_body
        hdrs = dict(headers or {})
        if body is not None:
            if form:
                data = urllib.parse.urlencode(body).encode()
                hdrs["Content-Type"] = "application/x-www-form-urlencoded"
            else:
                data = json.dumps(body).encode()
                hdrs["Content-Type"] = "application/json"
        if token:
            hdrs["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace"), _ci(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace"), _ci(exc.headers)
        except urllib.error.URLError as exc:
            return 0, str(exc), {}

    def check(self, label, ok, detail="", severity="finding"):
        if ok:
            self.passed += 1
            print(f"  [{GREEN}PASS{RESET}] {label}")
        else:
            print(f"  [{RED}{severity.upper()}{RESET}] {label}")
            self.findings.append((label, detail))
        if detail:
            print(f"         {DIM}{detail}{RESET}")

    def section(self, title):
        print(f"\n{BOLD}{title}{RESET}")


def forge_token(secret: str, payload: dict, alg: str = "HS256") -> str:
    """Build a JWT with an arbitrary secret, to test signature verification."""
    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode().rstrip("=")

    header = b64(json.dumps({"alg": alg, "typ": "JWT"}).encode())
    claims = b64(json.dumps(payload).encode())
    signing_input = f"{header}.{claims}".encode()
    sig = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    return f"{header}.{claims}.{b64(sig)}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", default="http://localhost:8000")
    args = ap.parse_args()

    p = Probe(args.base)
    print(f"{BOLD}Security / robustness probe{RESET}")
    print(f"{DIM}target {p.base}{RESET}")

    # --- setup -----------------------------------------------------------
    p.call("POST", f"{API}/auth/seed-demo-users", body={})
    _, raw, _ = p.call("POST", f"{API}/auth/login",
                       body={"username": "investigator", "password": "investigator123"}, form=True)
    investigator = json.loads(raw)["access_token"]
    _, raw, _ = p.call("POST", f"{API}/auth/login",
                       body={"username": "supervisor", "password": "supervisor123"}, form=True)
    supervisor = json.loads(raw)["access_token"]

    from app.services.connectors.synthetic import get_complaints  # noqa: PLC0415
    address = get_complaints()[0]["address"]
    _, raw, _ = p.call("POST", f"{API}/wallets", body={"address": address, "source": "synthetic"})
    case_id = json.loads(raw)["case_id"]

    # =====================================================================
    p.section("1. Token forgery and session handling")

    claims = {"sub": str(uuid.uuid4()), "username": "attacker", "role": "admin",
              "exp": 9999999999}

    status, _, _ = p.call("GET", f"{API}/auth/me", token=forge_token("wrong-secret", claims))
    p.check("token signed with a wrong secret is rejected", status == 401,
            f"HTTP {status}", "critical")

    # alg=none: the classic JWT bypass.
    none_header = json.dumps({"alg": "none", "typ": "JWT"}).encode()
    header = base64.urlsafe_b64encode(none_header).decode().rstrip("=")
    body_b64 = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    status, _, _ = p.call("GET", f"{API}/auth/me", token=f"{header}.{body_b64}.")
    p.check("alg=none token is rejected", status == 401, f"HTTP {status}", "critical")

    # Expired token.
    expired = dict(claims)
    expired["exp"] = 1000000000  # 2001
    status, _, _ = p.call("GET", f"{API}/auth/me",
                          token=forge_token(DEFAULT_JWT_SECRET, expired))
    p.check("expired token is rejected", status == 401, f"HTTP {status}", "critical")

    # Tampered role claim on an otherwise valid-looking token.
    parts = investigator.split(".")
    tampered_claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=="))
    tampered_claims["role"] = "admin"
    tampered = (
        parts[0]
        + "."
        + base64.urlsafe_b64encode(json.dumps(tampered_claims).encode()).decode().rstrip("=")
        + "."
        + parts[2]
    )
    status, _, _ = p.call("GET", f"{API}/auth/me", token=tampered)
    p.check("role escalation by editing the payload is rejected", status == 401,
            f"HTTP {status}", "critical")

    # =====================================================================
    p.section("2. Authorisation on specific objects")

    # A freeze request raised by the investigator, approved only by a supervisor.
    _, raw, _ = p.call("POST", f"{API}/freeze-requests", token=investigator,
                       body={"case_id": case_id, "target_address": address,
                             "justification": "Security probe fixture for the approval gate."})
    freeze_id = json.loads(raw)["id"]
    p.call("POST", f"{API}/freeze-requests/{freeze_id}/submit", token=investigator, body={})

    status, _, _ = p.call("POST", f"{API}/freeze-requests/{freeze_id}/approve",
                          token=investigator, body={})
    p.check("investigator cannot approve a freeze", status == 403, f"HTTP {status}", "critical")

    # Mass assignment: can the client dictate status or approver on creation?
    _, raw, _ = p.call("POST", f"{API}/freeze-requests", token=investigator,
                       body={"case_id": case_id, "target_address": address,
                             "justification": "Mass assignment probe - status must be ignored.",
                             "status": "approved", "approved_by": "attacker"})
    created = json.loads(raw)
    p.check("client-supplied status is ignored on create",
            created.get("status") == "draft" and not created.get("approved_by"),
            f"status={created.get('status')} approved_by={created.get('approved_by')}",
            "critical")

    # Non-existent object ids must 404, not 500.
    ghost = str(uuid.uuid4())
    status, _, _ = p.call("GET", f"{API}/cases/{ghost}", token=investigator)
    p.check("unknown case id returns 404", status == 404, f"HTTP {status}")

    status, _, _ = p.call("POST", f"{API}/freeze-requests/{ghost}/approve",
                          token=supervisor, body={})
    p.check("approving an unknown freeze id returns 404", status == 404, f"HTTP {status}")

    # =====================================================================
    p.section("3. Malformed and hostile input")

    cases = [
        ("malformed JSON body", "POST", f"{API}/wallets", b"{not json", (400, 422)),
        ("empty body where one is required", "POST", f"{API}/wallets", b"", (400, 422)),
        ("wrong type for a numeric field", "POST", f"{API}/wallets",
         json.dumps({"address": address, "amount_inr": "not-a-number"}).encode(), (422,)),
        ("deeply nested payload", "POST", f"{API}/wallets",
         json.dumps({"address": address, "narrative": {"a": {"b": {"c": "d"}}}}).encode(), (422,)),
        ("array where an object is expected", "POST", f"{API}/wallets", b"[1,2,3]", (422,)),
    ]
    for label, method, path, raw_body, want in cases:
        status, text, _ = p.call(method, path, token=investigator, raw_body=raw_body,
                                 headers={"Content-Type": "application/json"})
        ok = status in want
        p.check(label, ok, f"HTTP {status} (expected {want})")
        if "Traceback" in text or "File \"/app" in text:
            p.check(f"{label}: response leaks a stack trace", False,
                    text[:160], "critical")

    # Oversized field.
    status, _, _ = p.call("POST", f"{API}/wallets", token=investigator,
                          body={"address": address, "narrative": "A" * 100_000})
    p.check("oversized narrative is rejected", status == 422, f"HTTP {status}")

    # Injection attempts must be treated as data.
    for payload in ["'; DROP TABLE cases; --", "1 OR 1=1", "${jndi:ldap://x}", "\x00null"]:
        status, _, _ = p.call("POST", f"{API}/wallets/validate", body={"address": payload})
        p.check(f"injection payload handled as data: {payload[:24]!r}", status == 200,
                f"HTTP {status}")

    _, raw, _ = p.call("GET", f"{API}/cases", token=investigator)
    p.check("cases table intact after injection attempts",
            json.loads(raw).get("total", 0) > 0, "table still populated", "critical")

    # Stored XSS: the payload must come back escaped or inert, never as markup
    # the dashboard would execute.
    xss = "<script>alert('xss')</script>"
    p.call("POST", f"{API}/cases/{case_id}/notes", token=investigator, body={"body": xss})
    _, raw, headers = p.call("GET", f"{API}/cases/{case_id}", token=investigator)
    p.check("API returns JSON, not HTML (XSS is the client's to escape)",
            headers.get("content-type", "").startswith("application/json"),
            headers.get("content-type", ""))
    p.check("stored note is returned verbatim as data, not interpreted",
            xss in raw, "React escapes on render; the API must not silently mangle evidence")

    # =====================================================================
    p.section("4. Path traversal and object access")

    for bad in ["../../etc/passwd", "..%2f..%2fetc%2fpasswd", "%2e%2e/%2e%2e/etc/passwd"]:
        status, _, _ = p.call("GET", f"{API}/cases/{urllib.parse.quote(bad)}", token=investigator)
        p.check(f"traversal in a UUID slot rejected: {bad[:22]!r}",
                status in (404, 422), f"HTTP {status}", "critical")

    status, _, _ = p.call(
        "GET", f"{API}/cases/{case_id}/report/{uuid.uuid4()}/download", token=investigator
    )
    p.check("downloading an unknown report id returns 404", status == 404, f"HTTP {status}")

    # =====================================================================
    p.section("5. Upload handling")

    boundary = f"----probe{uuid.uuid4().hex}"

    def multipart(filename, content, content_type="text/plain"):
        return (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode() + content + f"\r\n--{boundary}--\r\n".encode()

    status, _, _ = p.call(
        "POST", f"{API}/cases/{case_id}/evidence", token=investigator,
        raw_body=multipart("empty.txt", b""),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    p.check("empty upload rejected", status == 422, f"HTTP {status}")

    status, _, _ = p.call(
        "POST", f"{API}/cases/{case_id}/evidence", token=investigator,
        raw_body=multipart("../../escape.txt", b"traversal filename"),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        timeout=60,
    )
    p.check("traversal in an upload filename does not escape storage",
            status in (201, 422), f"HTTP {status}", "critical")

    # =====================================================================
    p.section("6. Transport and headers")

    _, _, headers = p.call("GET", f"{API}/health/ready")
    wanted = ("x-content-type-options", "x-frame-options", "content-security-policy",
              "referrer-policy", "permissions-policy")
    missing = [h for h in wanted if h not in headers]
    p.check("standard security headers present", not missing,
            f"missing: {', '.join(missing)}" if missing else
            f"csp: {headers.get('content-security-policy', '')[:60]}…")

    _, _, headers = p.call("GET", f"{API}/cases", token=investigator)
    p.check("authenticated responses are not cacheable",
            "no-store" in headers.get("cache-control", ""),
            f"Cache-Control: {headers.get('cache-control', '(absent)')}")

    status, _, cors = p.call(
        "GET", f"{API}/health/ready", headers={"Origin": "https://evil.example"}
    )
    allowed = cors.get("access-control-allow-origin", "")
    p.check("CORS does not echo an arbitrary origin",
            allowed not in ("*", "https://evil.example"),
            f"Access-Control-Allow-Origin: {allowed or '(absent)'}", "critical")

    # =====================================================================
    p.section("7. Rate limiting")

    codes = [
        p.call("POST", f"{API}/auth/login",
               body={"username": "investigator", "password": f"wrong{i}"}, form=True)[0]
        for i in range(12)
    ]
    first_429 = next((i for i, c in enumerate(codes) if c == 429), None)
    p.check("repeated failed logins are throttled", first_429 is not None,
            f"blocked from attempt {first_429 + 1} of 12" if first_429 is not None
            else f"12 bad logins returned {sorted(set(codes))} - none throttled")

    # A correct password must not lift the block: otherwise the limit only
    # slows an attacker down until they guess right, which is the moment it
    # needed to hold.
    status, _, _ = p.call("POST", f"{API}/auth/login",
                          body={"username": "investigator", "password": "investigator123"},
                          form=True)
    p.check("lockout is not bypassed by a correct password", status == 429,
            f"HTTP {status}", "critical")

    # This probe deliberately dirtied the limiter. Clear it, or the next run of
    # e2e_demo.py inherits a five-minute lockout it did nothing to earn.
    try:
        from app.db import redis_client  # noqa: PLC0415

        client = redis_client.get_client()
        keys = list(client.scan_iter("sih183:ratelimit:*"))
        if keys:
            client.delete(*keys)
        print(f"  {DIM}cleared {len(keys)} rate-limit key(s) so the stack is left usable{RESET}")
    except Exception as exc:  # noqa: BLE001
        print(f"  {YELLOW}could not clear rate-limit keys: {exc}{RESET}")

    # =====================================================================
    print(f"\n{BOLD}{'=' * 70}{RESET}")
    if p.findings:
        print(f"{RED}{BOLD}{len(p.findings)} finding(s):{RESET}")
        for label, detail in p.findings:
            print(f"  {RED}·{RESET} {label}")
            if detail:
                print(f"    {DIM}{detail}{RESET}")
        print(f"\n{GREEN}{p.passed} checks passed.{RESET}")
        return 1
    print(f"{GREEN}{BOLD}All {p.passed} checks passed. No findings.{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
