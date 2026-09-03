#!/usr/bin/env python3
"""RBAC and access-control audit.

Probes every documented endpoint three times - unauthenticated, as an
investigator, and as a supervisor - and reports the status each returns. This
tests behaviour rather than declarations: a `Depends(get_current_user)` that was
forgotten shows up here as a 200 where a 401 was expected, which static
inspection of the router tree would miss.

Endpoints are classified by the access they should require, and the audit fails
if any of them is reachable more freely than its class allows.

  PUBLIC    - intentionally open (health, address validation, the mock NCRP feed)
  AUTH      - any signed-in officer
  APPROVER  - supervisor/admin only (freeze and STR approval)

Usage:
    docker compose exec backend python scripts/rbac_audit.py
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)

API = "/api/v1"
PUBLIC, AUTH, APPROVER = "PUBLIC", "AUTH", "APPROVER"

# Endpoints deliberately left open, each with the reason it is safe to be.
PUBLIC_REASONS = {
    f"{API}/health/live": "liveness probe for the container runtime",
    f"{API}/health/ready": "readiness probe; reports dependency state only",
    f"{API}/wallets/validate": "checksum check on a supplied string; touches no stored data",
    f"{API}/wallets": "victim-facing intake; a complaint must be fileable without an account",
    f"{API}/ncrp/intake": "machine-to-machine feed; authenticated by deployment, not by officer",
    f"{API}/ncrp/contract": "self-documentation for integrators",
    f"{API}/auth/login": "credential exchange",
    f"{API}/auth/seed-demo-users": "demo bootstrap; must be removed before any real deployment",
    "/": "service banner",
}

# Closed during the Phase 5 security pass. Each of these returns case numbers,
# case ids or the exchange a case resolved to - that is the investigation
# picture, and an earlier revision served all of it anonymously.
CLOSED_IN_PHASE5 = {
    f"{API}/wallet": "returns contributing case numbers, ids and reported timestamps",
    f"{API}/exchanges/ranked": "each row lists the case numbers behind the score",
    f"{API}/alerts/recent": "alert text names the case and destination exchange",
    f"{API}/wallets/multi-reported": "reveals which addresses recur across complaints",
    "WS " + f"{API}/ws/alerts": "same payload as the alert feed, over a socket",
}


def request(base, method, path, token=None, body=None, form=False):
    url = f"{base}{path}"
    data, headers = None, {}
    if body is not None:
        if form:
            data = urllib.parse.urlencode(body).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        return 0, str(exc).encode()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", default="http://localhost:8000")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    print(f"{BOLD}RBAC / access-control audit{RESET}")
    print(f"{DIM}target {base}{RESET}\n")

    request(base, "POST", f"{API}/auth/seed-demo-users", body={})
    _, raw = request(base, "POST", f"{API}/auth/login",
                     body={"username": "investigator", "password": "investigator123"}, form=True)
    investigator = json.loads(raw)["access_token"]
    _, raw = request(base, "POST", f"{API}/auth/login",
                     body={"username": "supervisor", "password": "supervisor123"}, form=True)
    supervisor = json.loads(raw)["access_token"]

    # Fixtures so the probes hit real objects rather than 404 paths.
    from app.services.connectors.synthetic import get_complaints  # noqa: PLC0415
    address = next(c["address"] for c in get_complaints() if c["chain"] == "TRON")
    _, raw = request(base, "POST", f"{API}/wallets",
                     body={"address": address, "source": "synthetic"})
    case_id = json.loads(raw)["case_id"]
    _, raw = request(base, "POST", f"{API}/freeze-requests", token=investigator,
                     body={"case_id": case_id, "target_address": address,
                           "justification": "RBAC audit fixture for the approval gate."})
    freeze_id = json.loads(raw)["id"]
    request(base, "POST", f"{API}/freeze-requests/{freeze_id}/submit", token=investigator, body={})
    _, raw = request(base, "POST", f"{API}/str-drafts", token=investigator,
                     body={"case_id": case_id})
    draft_id = json.loads(raw)["id"]

    probes = [
        (PUBLIC,   "GET",  f"{API}/health/live", None),
        (PUBLIC,   "GET",  f"{API}/health/ready", None),
        (PUBLIC,   "POST", f"{API}/wallets/validate", {"address": address}),
        (PUBLIC,   "GET",  f"{API}/ncrp/contract", None),

        (AUTH,     "GET",  f"{API}/wallet?address={urllib.parse.quote(address)}", None),
        (AUTH,     "GET",  f"{API}/exchanges/ranked", None),
        (AUTH,     "GET",  f"{API}/alerts/recent", None),
        (AUTH,     "GET",  f"{API}/wallets/multi-reported", None),

        (AUTH,     "GET",  f"{API}/auth/me", None),
        (AUTH,     "GET",  f"{API}/cases", None),
        (AUTH,     "GET",  f"{API}/cases/{case_id}", None),
        (AUTH,     "POST", f"{API}/cases/{case_id}/notes", {"body": "rbac audit note"}),
        (AUTH,     "POST", f"{API}/cases/{case_id}/report", {}),
        (AUTH,     "GET",  f"{API}/freeze-requests", None),
        (AUTH,     "POST", f"{API}/freeze-requests",
         {"case_id": case_id, "target_address": address,
          "justification": "RBAC audit probe for the create endpoint."}),
        (AUTH,     "GET",  f"{API}/str-drafts", None),
        (AUTH,     "POST", f"{API}/str-drafts", {"case_id": case_id}),

        (APPROVER, "POST", f"{API}/freeze-requests/{freeze_id}/approve", {}),
        (APPROVER, "POST", f"{API}/str-drafts/{draft_id}/approve", {}),
    ]

    failures = []
    print(f"{BOLD}{'class':9s} {'method':6s} {'endpoint':52s} "
          f"{'anon':>5s} {'inv':>5s} {'sup':>5s}{RESET}")
    print(DIM + "-" * 92 + RESET)

    for expected, method, path, body in probes:
        anon, _ = request(base, method, path, token=None, body=body)
        inv, _ = request(base, method, path, token=investigator, body=body)
        sup, _ = request(base, method, path, token=supervisor, body=body)

        problems = []
        if expected == PUBLIC:
            if anon in (401, 403):
                problems.append(f"documented public but anon got {anon}")
        else:
            if anon not in (401, 403):
                problems.append(f"reachable unauthenticated (anon={anon})")
        if expected == APPROVER and inv not in (401, 403):
            problems.append(f"investigator reached an approver-only route (inv={inv})")

        ok = not problems
        colour = GREEN if ok else RED
        short = path if len(path) <= 52 else path[:49] + "..."
        print(f"{colour}{expected:9s}{RESET} {method:6s} {short:52s} "
              f"{anon:>5d} {inv:>5d} {sup:>5d}")
        for p in problems:
            print(f"          {RED}{p}{RESET}")
            failures.append((path, p))

    # --- separation of duties -------------------------------------------
    print(f"\n{BOLD}Separation of duties{RESET}")
    _, raw = request(base, "POST", f"{API}/freeze-requests", token=supervisor,
                     body={"case_id": case_id, "target_address": address,
                           "justification": "Self-approval probe for the audit."})
    own = json.loads(raw)["id"]
    request(base, "POST", f"{API}/freeze-requests/{own}/submit", token=supervisor, body={})
    status, _ = request(base, "POST", f"{API}/freeze-requests/{own}/approve",
                        token=supervisor, body={})
    ok = status == 403
    print(f"  [{GREEN + 'PASS' + RESET if ok else RED + 'FAIL' + RESET}] "
          f"a supervisor cannot approve their own freeze request (HTTP {status})")
    if not ok:
        failures.append(("freeze self-approval", f"expected 403, got {status}"))

    # --- token handling --------------------------------------------------
    print(f"\n{BOLD}Token handling{RESET}")
    for label, tok, want in [
        ("garbage token rejected", "not-a-jwt", (401,)),
        ("empty token rejected", "", (401,)),
        ("token signed with the wrong key rejected",
         "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
         "eyJzdWIiOiJhdHRhY2tlciIsInJvbGUiOiJhZG1pbiJ9.bm90LWEtcmVhbC1zaWduYXR1cmU", (401,)),
    ]:
        status, _ = request(base, "GET", f"{API}/auth/me", token=tok)
        ok = status in want
        mark = GREEN + "PASS" + RESET if ok else RED + "FAIL" + RESET
        print(f"  [{mark}] {label} (HTTP {status})")
        if not ok:
            failures.append((label, f"expected {want}, got {status}"))

    # --- input hardening --------------------------------------------------
    print(f"\n{BOLD}Input handling{RESET}")
    checks = [
        ("oversized address rejected", "POST", f"{API}/wallets",
         {"address": "1" * 5000, "source": "manual"}, (422,)),
        ("SQL-ish string is treated as data, not code", "POST", f"{API}/wallets/validate",
         {"address": "'; DROP TABLE cases; --"}, (200,)),
        ("path traversal in a UUID slot is rejected", "GET",
         f"{API}/cases/..%2f..%2fetc%2fpasswd", None, (401, 404, 422)),
    ]
    for label, method, path, body, want in checks:
        status, _ = request(base, method, path, token=investigator, body=body)
        ok = status in want
        mark = GREEN + "PASS" + RESET if ok else RED + "FAIL" + RESET
        print(f"  [{mark}] {label} (HTTP {status})")
        if not ok:
            failures.append((label, f"expected one of {want}, got {status}"))

    # SQL injection must not have destroyed anything.
    status, raw = request(base, "GET", f"{API}/cases", token=investigator)
    if status == 200:
        total = json.loads(raw)["total"]
        ok = total > 0
        print(f"  [{GREEN + 'PASS' + RESET if ok else RED + 'FAIL' + RESET}] "
              f"cases table intact after injection probe ({total} rows)")
        if not ok:
            failures.append(("injection", "cases table appears empty"))

    # --- public-by-design register ---------------------------------------
    print(f"\n{BOLD}Endpoints intentionally public, and why{RESET}")
    for path, reason in PUBLIC_REASONS.items():
        print(f"  {DIM}{path:34s}{RESET} {reason}")

    print(f"\n{BOLD}{'=' * 92}{RESET}")
    if failures:
        print(f"{RED}{BOLD}{len(failures)} finding(s):{RESET}")
        for path, msg in failures:
            print(f"  {RED}·{RESET} {path}: {msg}")
        return 1
    print(f"{GREEN}{BOLD}No access-control findings.{RESET}")
    print(f"{YELLOW}Prototype scope: demo credentials are published, /auth/seed-demo-users is "
          f"open, and JWT replaces Keycloak. Address all three before any real deployment.{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
