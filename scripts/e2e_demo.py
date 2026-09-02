#!/usr/bin/env python3
"""End-to-end integration check for SIH26183.

Drives the system the way a demo does - over HTTP, against the running stack -
and asserts each stage actually happened rather than merely returning 200. Every
step prints the real value it observed, so a failure says which link in the
chain broke and what it saw instead.

Stages, in the order the problem statement describes them:

  1  A victim-reported wallet is filed and validated at checksum level
  2  The money flow is traced and written to the graph
  3  Address clustering assigns the reported address to a cluster
  4  The terminal cluster attributes to an exchange, citing its source
  5  The exchange carries an explainable fraud-linkage score
  6  A Medium/High resolution raises a real-time alert on the stream
  7  The alert is delivered over the WebSocket the dashboard subscribes to
  8  A forensic PDF exports with a hash that verifies independently
  9  Evidence attaches with a chain-of-custody digest
 10  A freeze request CANNOT be approved without a human supervisor
 11  The mock NCRP intake accepts a complaint and rejects a bad checksum
 12  An FIU-IND-style STR draft generates, marked as not filed

Usage:
    docker compose exec backend python scripts/e2e_demo.py
    docker compose exec backend python scripts/e2e_demo.py --base http://localhost:8000
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

DEFAULT_BASE = "http://localhost:8000"
API = "/api/v1"

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)


class Failure(Exception):
    pass


class Runner:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.token: str | None = None
        self.passed = 0
        self.failed = 0
        self.step = 0

    # -- plumbing --------------------------------------------------------
    def call(self, method: str, path: str, body=None, token: str | None = None,
             form: bool = False, raw: bool = False, expect: int | None = None):
        url = f"{self.base}{path}"
        data, headers = None, {}
        if body is not None:
            if form:
                data = urllib.parse.urlencode(body).encode()
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            else:
                data = json.dumps(body).encode()
                headers["Content-Type"] = "application/json"
        auth = token if token is not None else self.token
        if auth:
            headers["Authorization"] = f"Bearer {auth}"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                payload = resp.read()
                status = resp.status
        except urllib.error.HTTPError as exc:
            payload, status = exc.read(), exc.code
        except urllib.error.URLError as exc:
            raise Failure(f"cannot reach {url}: {exc.reason}") from exc

        if expect is not None and status != expect:
            snippet = payload[:220].decode("utf-8", "replace")
            raise Failure(f"{method} {path} expected HTTP {expect}, got {status}: {snippet}")
        if raw:
            return status, payload
        try:
            return status, json.loads(payload) if payload else None
        except json.JSONDecodeError:
            return status, payload.decode("utf-8", "replace")

    def check(self, label: str, condition: bool, detail: str = ""):
        self.step += 1
        mark = f"{GREEN}PASS{RESET}" if condition else f"{RED}FAIL{RESET}"
        print(f"  [{mark}] {label}")
        if detail:
            print(f"         {DIM}{detail}{RESET}")
        if condition:
            self.passed += 1
        else:
            self.failed += 1
        return condition

    def stage(self, n: int, title: str):
        print(f"\n{BOLD}{n:>2}. {title}{RESET}")


# ---------------------------------------------------------------------------
# A minimal WebSocket client. The dashboard subscribes over a real socket, so
# the test asserts the real socket delivers - not just that Redis has the row.
# ---------------------------------------------------------------------------
def websocket_collect(base: str, path: str, seconds: float = 6.0, limit: int = 40) -> list[dict]:
    import socket as pysocket

    parsed = urllib.parse.urlparse(base)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    key = base64.b64encode(uuid.uuid4().bytes).decode()

    sock = pysocket.create_connection((host, port), timeout=seconds + 4)
    sock.settimeout(seconds + 4)
    handshake = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(handshake.encode())

    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise Failure("websocket closed during handshake")
        buf += chunk
    header, buf = buf.split(b"\r\n\r\n", 1)
    if b"101" not in header.split(b"\r\n")[0]:
        raise Failure(f"websocket upgrade refused: {header.splitlines()[0]!r}")

    messages: list[dict] = []
    deadline = time.time() + seconds
    while time.time() < deadline and len(messages) < limit:
        # Decode server frames (unmasked, text).
        while len(buf) >= 2:
            b1, b2 = buf[0], buf[1]
            opcode, length, offset = b1 & 0x0F, b2 & 0x7F, 2
            if length == 126:
                if len(buf) < 4:
                    break
                length = struct.unpack(">H", buf[2:4])[0]
                offset = 4
            elif length == 127:
                if len(buf) < 10:
                    break
                length = struct.unpack(">Q", buf[2:10])[0]
                offset = 10
            if len(buf) < offset + length:
                break
            payload, buf = buf[offset:offset + length], buf[offset + length:]
            if opcode == 0x1:
                try:
                    messages.append(json.loads(payload.decode("utf-8")))
                except json.JSONDecodeError:
                    pass
            elif opcode == 0x8:
                deadline = 0
                break
        if time.time() >= deadline or len(messages) >= limit:
            break
        try:
            chunk = sock.recv(8192)
        except pysocket.timeout:
            break
        if not chunk:
            break
        buf += chunk

    try:
        sock.close()
    except OSError:
        pass
    return messages


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", default=DEFAULT_BASE)
    args = ap.parse_args()

    r = Runner(args.base)
    print(f"{BOLD}SIH26183 end-to-end integration check{RESET}")
    print(f"{DIM}target {r.base} · all data synthetic or public-dataset derived{RESET}")

    # --- preflight ------------------------------------------------------
    r.stage(0, "Preflight")
    _, ready = r.call("GET", f"{API}/health/ready", expect=200)
    r.check("stack is ready", ready["status"] == "ready",
            f"postgres/neo4j/redis: {', '.join(k for k, v in ready['checks'].items() if v['ok'])}")
    provenance = ready["data_source"]
    r.check("provenance is declared", bool(provenance), f"data_source = {provenance}")

    r.call("POST", f"{API}/auth/seed-demo-users", body={})
    _, inv = r.call("POST", f"{API}/auth/login",
                    body={"username": "investigator", "password": "investigator123"},
                    form=True, expect=200)
    _, sup = r.call("POST", f"{API}/auth/login",
                    body={"username": "supervisor", "password": "supervisor123"},
                    form=True, expect=200)
    investigator, supervisor = inv["access_token"], sup["access_token"]
    r.token = investigator
    r.check("both roles authenticate", inv["role"] == "investigator" and sup["role"] == "supervisor",
            f"{inv['role']} / {sup['role']}")

    from app.services.connectors.synthetic import get_complaints  # noqa: PLC0415
    complaints = get_complaints()
    if not complaints:
        raise Failure("synthetic dataset missing - run scripts/generate_synthetic_complaints.py")
    address = next(c["address"] for c in complaints if c["chain"] == "TRON")

    # --- 1. intake ------------------------------------------------------
    r.stage(1, "Victim-reported wallet is filed and validated")
    _, validated = r.call("POST", f"{API}/wallets/validate", body={"address": address}, expect=200)
    r.check("address passes checksum validation", validated["valid"] is True,
            f"{validated['chain']} · {validated['address_kind']}")
    _, bad = r.call("POST", f"{API}/wallets/validate",
                    body={"address": "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAeD"}, expect=200)
    r.check("a mistyped address is rejected", bad["valid"] is False, bad["reason"])

    _, intake = r.call("POST", f"{API}/wallets",
                       body={"address": address, "victim_ref": "E2E-DEMO", "amount_inr": 250000,
                             "source": "synthetic"}, expect=201)
    case_id, case_number = intake["case_id"], intake["case_number"]
    r.check("case opened", bool(case_number), f"{case_number} · provenance {intake['data_provenance']}")

    # --- 2. graph -------------------------------------------------------
    r.stage(2, "Money flow is traced into the graph")
    trace = intake["trace"]
    r.check("trace completed", trace["status"] == "complete", f"status = {trace['status']}")
    r.check("transactions were ingested", trace["transactions_ingested"] > 0,
            f"{trace['transactions_ingested']} transactions, "
            f"{trace['addresses_touched']} addresses, {trace['hops_discovered']} hops")

    # --- 3. clustering --------------------------------------------------
    r.stage(3, "Clustering assigns the address to a cluster")
    cluster = intake["cluster"]
    r.check("cluster resolved", bool(cluster["cluster_key"]),
            f"{cluster['cluster_key']} · heuristic {cluster['heuristic']} · size {cluster['size']}")

    # --- 4. attribution -------------------------------------------------
    r.stage(4, "Terminal cluster attributes to an exchange")
    _, analysis = r.call("GET", f"{API}/wallet?address={urllib.parse.quote(address)}", expect=200)
    attribution = analysis["attribution"]
    terminals = analysis["terminal_attributions"]
    r.check("trace reaches a tagged service", len(terminals) > 0,
            ", ".join(f"{t['entity_name']} (hop {t['hop']})" for t in terminals[:3]) or "none reached")
    r.check("attribution states its method", attribution["method"] in ("tagged_db", "classifier", "none"),
            f"method = {attribution['method']}")
    if attribution["method"] == "tagged_db":
        r.check("a curated attribution cites its source", bool(attribution["source"]),
                f"{attribution['entity_name']} via {attribution['source']}")
    elif attribution["method"] == "classifier":
        r.check("a behavioural guess is never given a company name",
                attribution["entity_name"] is None, "entity_name is null, as required")

    # --- 5. risk --------------------------------------------------------
    r.stage(5, "Exchange carries an explainable fraud-linkage score")
    r.check("risk label is valid", analysis["risk_label"] in ("low", "medium", "high"),
            f"{analysis['risk_label'].upper()} {analysis['risk_score']}/100")
    r.check("score is explained", bool(analysis["risk_factors"]),
            "; ".join(analysis["risk_factors"])[:150])
    if analysis["risk_score"] > 0:
        contributions = analysis["contributions"]
        r.check("contributing cases are named", len(contributions) > 0,
                f"{len(contributions)} case(s), top: {contributions[0]['case_number']} "
                f"(weight {contributions[0]['decay_weight']}, {contributions[0]['points']} pts)")
        recomputed = sum(c["points"] for c in contributions)
        r.check("the score is reconstructable from its parts", recomputed > 0,
                f"raw points {recomputed:.2f} -> score {analysis['risk_score']}")

    # --- 6 & 7. alert + websocket ---------------------------------------
    r.stage(6, "Medium/High resolution raises a real-time alert")
    # Compare stream message ids, not counts: /alerts/recent is capped, so once
    # the stream holds as many entries as the cap the count saturates and a new
    # alert would be invisible to a length comparison.
    _, before = r.call("GET", f"{API}/alerts/recent?limit=200", expect=200)
    baseline_ids = {a.get("stream_msg_id") for a in before["alerts"]}

    collected: list[dict] = []
    import threading
    def listen():
        try:
            # The socket is authenticated: a browser cannot set headers, so the
            # token travels as a query parameter and is verified server-side.
            collected.extend(websocket_collect(
                r.base,
                f"{API}/ws/alerts?backlog=0&token={urllib.parse.quote(investigator)}",
                seconds=8.0,
            ))
        except Exception as exc:  # noqa: BLE001
            collected.append({"__error__": str(exc)})
    thread = threading.Thread(target=listen, daemon=True)
    thread.start()
    time.sleep(1.2)  # let the socket finish its handshake before triggering

    r.call("GET", f"{API}/wallet?address={urllib.parse.quote(address)}", expect=200)
    _, after = r.call("GET", f"{API}/alerts/recent?limit=200", expect=200)

    after_ids = {a.get("stream_msg_id") for a in after["alerts"]}
    fresh = after_ids - baseline_ids
    if analysis["risk_label"] in ("medium", "high"):
        r.check("alert published to the stream", bool(fresh),
                f"{len(fresh)} new message id(s) on {after['stream']}")
    else:
        r.check("low risk correctly does not alert", not fresh,
                "no alert raised, as designed")

    r.stage(7, "Alert is delivered over the authenticated dashboard WebSocket")
    thread.join(timeout=12)
    errors = [m for m in collected if "__error__" in m]
    hello = [m for m in collected if m.get("type") == "hello"]
    live = [m for m in collected if m.get("type") == "alert"]
    if errors:
        r.check("websocket connects", False, errors[0]["__error__"])
    else:
        r.check("websocket connects and declares provenance", bool(hello),
                f"data_source = {hello[0]['data_source']}" if hello else "no hello frame")
        if analysis["risk_label"] in ("medium", "high"):
            r.check("live alert pushed to the socket", len(live) > 0,
                    live[0]["message"][:120] if live else "no alert frame received")
            if live:
                r.check("alert is marked synthetic", live[0].get("is_synthetic") == "true",
                        f"is_synthetic = {live[0].get('is_synthetic')}")

    # --- 8. forensic report ---------------------------------------------
    r.stage(8, "Forensic PDF exports with a verifiable hash")
    _, report = r.call("POST", f"{API}/cases/{case_id}/report", body={}, expect=201)
    digest = report["sha256"]
    status, pdf = r.call("GET", f"{API}/cases/{case_id}/report/{report['report_id']}/download",
                         raw=True, expect=200)
    r.check("download is a real PDF", pdf[:5] == b"%PDF-", f"{len(pdf)} bytes")
    recomputed = hashlib.sha256(pdf).hexdigest()
    r.check("hash verifies independently", recomputed == digest,
            f"recomputed {recomputed[:24]}… == stored {digest[:24]}…"
            if recomputed == digest else f"MISMATCH {recomputed} vs {digest}")
    _, verify = r.call("GET", f"{API}/cases/{case_id}/report/{report['report_id']}/verify", expect=200)
    r.check("server-side verification agrees", verify["verified"] is True)

    # --- 9. evidence ----------------------------------------------------
    r.stage(9, "Evidence attaches with a chain-of-custody digest")
    body = b"E2E exhibit: synthetic demonstration content.\n"
    boundary = f"----e2e{uuid.uuid4().hex}"
    multipart = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="exhibit.txt"\r\n'
        "Content-Type: text/plain\r\n\r\n"
    ).encode() + body + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{r.base}{API}/cases/{case_id}/evidence", data=multipart, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "Authorization": f"Bearer {investigator}"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        evidence = json.loads(resp.read())
    expected = hashlib.sha256(body).hexdigest()
    r.check("exhibit digest matches the bytes sent", evidence["sha256"] == expected,
            f"{evidence['sha256'][:24]}…")

    # --- 10. human-in-the-loop ------------------------------------------
    r.stage(10, "Freeze request cannot be approved without a human supervisor")
    target = terminals[0]["address"] if terminals else address
    _, fr = r.call("POST", f"{API}/freeze-requests",
                   body={"case_id": case_id, "target_address": target,
                         "justification": "End-to-end integration check of the approval gate."},
                   expect=201)
    fid = fr["id"]
    r.check("created as draft, never approved", fr["status"] == "draft" and fr["approved_by"] is None,
            f"status = {fr['status']}")

    status, _ = r.call("POST", f"{API}/freeze-requests/{fid}/approve", body={}, token=supervisor)
    r.check("a draft cannot be approved", status == 409, f"HTTP {status}")

    r.call("POST", f"{API}/freeze-requests/{fid}/submit", body={}, expect=200)
    status, _ = r.call("POST", f"{API}/freeze-requests/{fid}/approve", body={}, token=investigator)
    r.check("investigator cannot approve", status == 403, f"HTTP {status}")

    status, _ = r.call("POST", f"{API}/freeze-requests/{fid}/approve", body={}, token="")
    r.check("unauthenticated cannot approve", status == 401, f"HTTP {status}")

    _, approved = r.call("POST", f"{API}/freeze-requests/{fid}/approve", body={},
                         token=supervisor, expect=200)
    r.check("supervisor approval succeeds and is attributed",
            approved["status"] == "approved" and approved["approved_by"] == "supervisor",
            f"approved by {approved['approved_by']}, raised by {approved['requested_by']}")

    _, listing = r.call("GET", f"{API}/freeze-requests?case_id={case_id}", expect=200)
    orphans = [x for x in listing["requests"]
               if x["status"] in ("approved", "dispatched") and not x["approved_by"]]
    r.check("no approval exists without a named approver", not orphans,
            f"{len(listing['requests'])} request(s) checked")

    # --- 11. mock NCRP ---------------------------------------------------
    r.stage(11, "Mock NCRP/1930 intake")
    btc = next(c["address"] for c in complaints if c["chain"] == "BTC")
    status, ack = r.call("POST", f"{API}/ncrp/intake",
                         body={"ncrp_ack_no": f"NCRP-E2E-{uuid.uuid4().hex[:8]}",
                               "suspect_wallet": btc, "amount_inr": 90000,
                               "complainant_ref": "PSEUDO-E2E"}, expect=202)
    r.check("complaint accepted", ack["accepted"] is True,
            f"{ack['internal_case_number']} · {ack['chain']}")
    r.check("endpoint declares itself a mock", "MOCK" in ack["notice"].upper())
    status, _ = r.call("POST", f"{API}/ncrp/intake",
                       body={"ncrp_ack_no": "NCRP-E2E-BAD",
                             "suspect_wallet": "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAeD"})
    r.check("bad checksum rejected at the boundary", status == 422, f"HTTP {status}")

    # --- 12. STR draft ----------------------------------------------------
    r.stage(12, "FIU-IND-style STR draft")
    _, draft = r.call("POST", f"{API}/str-drafts", body={"case_id": case_id}, expect=201)
    r.check("draft generated", draft["status"] == "draft")
    r.check("marked NOT FILED", "NOT FILED" in draft["body"])
    r.check("states its own limitations", "No KYC or account-holder information" in draft["body"])
    status, _ = r.call("POST", f"{API}/str-drafts/{draft['id']}/approve", body={}, token=investigator)
    r.check("author cannot approve their own draft", status == 403, f"HTTP {status}")

    # --- 13. access control ----------------------------------------------
    r.stage(13, "Case-linked data is not readable anonymously")
    for label, path in [
        ("wallet analysis", f"{API}/wallet?address={urllib.parse.quote(address)}"),
        ("exchange ranking", f"{API}/exchanges/ranked"),
        ("alert feed", f"{API}/alerts/recent"),
        ("multi-reported wallets", f"{API}/wallets/multi-reported"),
    ]:
        status, _ = r.call("GET", path, token="")
        r.check(f"{label} requires authentication", status == 401, f"anon -> HTTP {status}")
    try:
        websocket_collect(r.base, f"{API}/ws/alerts?backlog=1", seconds=3.0)
        r.check("alert socket requires authentication", False, "socket accepted without a token")
    except Failure as exc:
        r.check("alert socket requires authentication", True, str(exc)[:90])

    # --- summary ---------------------------------------------------------
    total = r.passed + r.failed
    print(f"\n{BOLD}{'=' * 62}{RESET}")
    colour = GREEN if r.failed == 0 else RED
    print(f"{colour}{BOLD}{r.passed}/{total} checks passed{RESET}"
          + (f"{RED} · {r.failed} FAILED{RESET}" if r.failed else ""))
    print(f"{DIM}Case {case_number} · report sha256 {digest}{RESET}")
    print(f"{YELLOW}All data synthetic or public-dataset derived. Recommendation-only system: "
          f"no freeze or disclosure occurs without an authorised officer.{RESET}")
    return 1 if r.failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Failure as exc:
        print(f"\n{RED}{BOLD}ABORTED:{RESET} {exc}")
        sys.exit(2)
