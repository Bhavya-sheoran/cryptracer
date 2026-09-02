"""Phase 4: case management, reports, NCRP mock, STR, freeze workflow, alerts.

The freeze tests are the important ones. The problem statement requires that no
freeze or disclosure fires without an explicit human approval, so these assert
every way that rule could be broken: approving a draft, approving without the
role, approving your own request, and approving with no credentials at all.
"""

from __future__ import annotations

import hashlib
import io

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


@pytest.fixture(scope="module")
def stack_up() -> bool:
    resp = client.get("/api/v1/health/ready")
    return resp.status_code == 200 and resp.json()["status"] == "ready"


@pytest.fixture(scope="module")
def tokens(stack_up):
    if not stack_up:
        pytest.skip("compose stack not fully up")
    client.post("/api/v1/auth/seed-demo-users")

    def login(username, password):
        resp = client.post(
            "/api/v1/auth/login", data={"username": username, "password": password}
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["access_token"]

    return {
        "investigator": login("investigator", "investigator123"),
        "supervisor": login("supervisor", "supervisor123"),
    }


def auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def case_id(tokens):
    """A case with a traced wallet, filed fresh so the graph is populated."""
    from app.services.connectors.synthetic import get_complaints

    complaints = get_complaints()
    if not complaints:
        pytest.skip("synthetic dataset not generated")
    address = next(c["address"] for c in complaints if c["chain"] == "TRON")
    resp = client.post("/api/v1/wallets", json={"address": address, "source": "synthetic"})
    assert resp.status_code == 201, resp.text
    return resp.json()["case_id"]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def test_login_returns_role_claim(tokens):
    resp = client.get("/api/v1/auth/me", headers=auth(tokens["supervisor"]))
    assert resp.status_code == 200
    body = resp.json()
    assert body["role"] == "supervisor"
    assert body["can_approve"] is True


def test_investigator_cannot_approve(tokens):
    body = client.get("/api/v1/auth/me", headers=auth(tokens["investigator"])).json()
    assert body["role"] == "investigator"
    assert body["can_approve"] is False


def test_bad_password_rejected(stack_up):
    if not stack_up:
        pytest.skip("stack not up")
    resp = client.post(
        "/api/v1/auth/login", data={"username": "investigator", "password": "nope"}
    )
    assert resp.status_code == 401


def test_unknown_user_and_bad_password_give_the_same_message(stack_up):
    """Must not reveal whether the username exists."""
    if not stack_up:
        pytest.skip("stack not up")
    a = client.post("/api/v1/auth/login", data={"username": "nobody", "password": "x"})
    b = client.post("/api/v1/auth/login", data={"username": "investigator", "password": "x"})
    assert a.status_code == b.status_code == 401
    assert a.json()["detail"] == b.json()["detail"]


def test_protected_endpoint_requires_a_token(stack_up):
    if not stack_up:
        pytest.skip("stack not up")
    assert client.get("/api/v1/cases").status_code == 401


def test_password_over_72_bytes_is_rejected_not_truncated():
    """bcrypt ignores bytes past 72; truncating would let two passwords collide."""
    from app.services.auth import AuthError, hash_password

    with pytest.raises(AuthError):
        hash_password("a" * 73)


# ---------------------------------------------------------------------------
# Case notes and evidence
# ---------------------------------------------------------------------------
def test_add_note_to_case(tokens, case_id):
    resp = client.post(
        f"/api/v1/cases/{case_id}/notes",
        json={"body": "Traced to exchange deposit address."},
        headers=auth(tokens["investigator"]),
    )
    assert resp.status_code == 201
    assert resp.json()["author"] == "investigator"


def test_evidence_hash_matches_uploaded_bytes(tokens, case_id):
    """The stored digest must attest to the bytes that actually landed."""
    payload = b"Exhibit A: synthetic demo content.\n"
    expected = hashlib.sha256(payload).hexdigest()

    resp = client.post(
        f"/api/v1/cases/{case_id}/evidence",
        files={"file": ("exhibit.txt", io.BytesIO(payload), "text/plain")},
        headers=auth(tokens["investigator"]),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["sha256"] == expected

    evidence_id = resp.json()["id"]
    verify = client.get(
        f"/api/v1/cases/{case_id}/evidence/{evidence_id}/verify",
        headers=auth(tokens["investigator"]),
    ).json()
    assert verify["verified"] is True
    assert verify["recomputed_sha256"] == expected


def test_empty_evidence_upload_is_rejected(tokens, case_id):
    resp = client.post(
        f"/api/v1/cases/{case_id}/evidence",
        files={"file": ("empty.txt", io.BytesIO(b""), "text/plain")},
        headers=auth(tokens["investigator"]),
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Forensic report
# ---------------------------------------------------------------------------
def test_report_is_a_pdf_whose_hash_verifies(tokens, case_id):
    resp = client.post(f"/api/v1/cases/{case_id}/report", headers=auth(tokens["investigator"]))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    report_id, digest = body["report_id"], body["sha256"]
    assert len(digest) == 64

    download = client.get(
        f"/api/v1/cases/{case_id}/report/{report_id}/download",
        headers=auth(tokens["investigator"]),
    )
    assert download.status_code == 200
    assert download.content[:5] == b"%PDF-"
    # Independent recomputation - this is the chain-of-custody claim.
    assert hashlib.sha256(download.content).hexdigest() == digest

    verify = client.get(
        f"/api/v1/cases/{case_id}/report/{report_id}/verify",
        headers=auth(tokens["investigator"]),
    ).json()
    assert verify["verified"] is True


def test_report_requires_authentication(case_id):
    assert client.post(f"/api/v1/cases/{case_id}/report").status_code == 401


# ---------------------------------------------------------------------------
# Freeze workflow - the human-in-the-loop guarantees
# ---------------------------------------------------------------------------
def _new_freeze(tokens, case_id, as_role="investigator"):
    return client.post(
        "/api/v1/freeze-requests",
        json={
            "case_id": case_id,
            "target_address": "TRyRpB9pg4aegknQoXD4HpBJZN9xY4zqub",
            "justification": "Traced funds terminate at this exchange deposit address.",
        },
        headers=auth(tokens[as_role]),
    )


def test_freeze_request_is_created_as_draft(tokens, case_id):
    """Creation must never yield an approved request."""
    resp = _new_freeze(tokens, case_id)
    assert resp.status_code == 201
    assert resp.json()["status"] == "draft"
    assert resp.json()["approved_by"] is None


def test_draft_cannot_be_approved_without_submission(tokens, case_id):
    fr = _new_freeze(tokens, case_id).json()["id"]
    resp = client.post(
        f"/api/v1/freeze-requests/{fr}/approve", headers=auth(tokens["supervisor"])
    )
    assert resp.status_code == 409


def test_investigator_cannot_approve_a_freeze(tokens, case_id):
    fr = _new_freeze(tokens, case_id).json()["id"]
    client.post(f"/api/v1/freeze-requests/{fr}/submit", headers=auth(tokens["investigator"]))
    resp = client.post(
        f"/api/v1/freeze-requests/{fr}/approve", headers=auth(tokens["investigator"])
    )
    assert resp.status_code == 403
    assert "cannot approve" in resp.json()["detail"]


def test_unauthenticated_cannot_approve_a_freeze(tokens, case_id):
    fr = _new_freeze(tokens, case_id).json()["id"]
    client.post(f"/api/v1/freeze-requests/{fr}/submit", headers=auth(tokens["investigator"]))
    assert client.post(f"/api/v1/freeze-requests/{fr}/approve").status_code == 401


def test_requester_cannot_approve_their_own_request(tokens, case_id):
    """Separation of duties, even when the requester holds the approver role."""
    fr = _new_freeze(tokens, case_id, as_role="supervisor").json()["id"]
    client.post(f"/api/v1/freeze-requests/{fr}/submit", headers=auth(tokens["supervisor"]))
    resp = client.post(
        f"/api/v1/freeze-requests/{fr}/approve", headers=auth(tokens["supervisor"])
    )
    assert resp.status_code == 403
    assert "cannot approve it" in resp.json()["detail"]


def test_supervisor_approval_succeeds_and_is_attributed(tokens, case_id):
    fr = _new_freeze(tokens, case_id).json()["id"]
    client.post(f"/api/v1/freeze-requests/{fr}/submit", headers=auth(tokens["investigator"]))
    resp = client.post(
        f"/api/v1/freeze-requests/{fr}/approve", headers=auth(tokens["supervisor"])
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "approved"
    assert body["approved_by"] == "supervisor"
    assert body["requested_by"] == "investigator"
    assert body["approved_at"] is not None


def test_only_an_approved_request_can_be_dispatched(tokens, case_id):
    fr = _new_freeze(tokens, case_id).json()["id"]
    client.post(f"/api/v1/freeze-requests/{fr}/submit", headers=auth(tokens["investigator"]))
    # Not approved yet.
    assert (
        client.post(
            f"/api/v1/freeze-requests/{fr}/dispatch", headers=auth(tokens["supervisor"])
        ).status_code
        == 409
    )
    client.post(f"/api/v1/freeze-requests/{fr}/approve", headers=auth(tokens["supervisor"]))
    resp = client.post(
        f"/api/v1/freeze-requests/{fr}/dispatch", headers=auth(tokens["supervisor"])
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "dispatched"


def test_no_analysis_call_ever_creates_an_approved_freeze(tokens, case_id):
    """Running the pipeline must not manufacture an approval anywhere."""
    from app.services.connectors.synthetic import get_complaints

    address = next(c["address"] for c in get_complaints() if c["chain"] == "TRON")
    client.get(
        "/api/v1/wallet", params={"address": address}, headers=auth(tokens["investigator"])
    )

    listing = client.get(
        "/api/v1/freeze-requests", params={"case_id": case_id}, headers=auth(tokens["investigator"])
    ).json()["requests"]
    auto_approved = [
        r for r in listing if r["status"] in ("approved", "dispatched") and r["approved_by"] is None
    ]
    assert auto_approved == [], "a freeze reached approved with no approver recorded"


# ---------------------------------------------------------------------------
# Mock NCRP intake
# ---------------------------------------------------------------------------
def test_ncrp_intake_accepts_and_marks_source(tokens):
    from app.services.connectors.synthetic import get_complaints

    address = next(c["address"] for c in get_complaints() if c["chain"] == "BTC")
    resp = client.post(
        "/api/v1/ncrp/intake",
        json={
            "ncrp_ack_no": "NCRP2026TEST0001",
            "suspect_wallet": address,
            "amount_inr": 125000,
            "complainant_ref": "PSEUDO-TEST-1",
        },
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["accepted"] is True
    assert body["internal_case_number"].startswith("SIH183-")
    assert "MOCK" in body["notice"]


def test_ncrp_intake_rejects_a_bad_checksum():
    resp = client.post(
        "/api/v1/ncrp/intake",
        json={
            "ncrp_ack_no": "NCRP2026TEST0002",
            "suspect_wallet": "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAeD",
        },
    )
    assert resp.status_code == 422


def test_ncrp_contract_is_documented_as_mock():
    body = client.get("/api/v1/ncrp/contract").json()
    assert "MOCK" in body["notice"]
    assert "pseudonymous" in body["pii_policy"]


# ---------------------------------------------------------------------------
# STR drafts
# ---------------------------------------------------------------------------
def test_str_draft_states_it_is_not_filed(tokens, case_id):
    resp = client.post(
        "/api/v1/str-drafts", json={"case_id": case_id}, headers=auth(tokens["investigator"])
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "draft"
    assert "NOT FILED" in body["body"]
    assert "FIU-IND" in body["notice"]


def test_str_draft_declares_its_limitations(tokens, case_id):
    """A narrative that hides its evidential gaps is worse than one that names them."""
    body = client.post(
        "/api/v1/str-drafts", json={"case_id": case_id}, headers=auth(tokens["investigator"])
    ).json()["body"]
    assert "No KYC or account-holder information" in body
    assert "LIMITATIONS" in body
    assert "OFFICER ACTION REQUIRED" in body


def test_str_creator_cannot_approve_their_own_draft(tokens, case_id):
    draft = client.post(
        "/api/v1/str-drafts", json={"case_id": case_id}, headers=auth(tokens["investigator"])
    ).json()["id"]
    resp = client.post(
        f"/api/v1/str-drafts/{draft}/approve", headers=auth(tokens["investigator"])
    )
    assert resp.status_code == 403


def test_supervisor_can_approve_an_str_draft(tokens, case_id):
    draft = client.post(
        "/api/v1/str-drafts", json={"case_id": case_id}, headers=auth(tokens["investigator"])
    ).json()["id"]
    resp = client.post(f"/api/v1/str-drafts/{draft}/approve", headers=auth(tokens["supervisor"]))
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"
    assert "files nothing" in resp.json()["notice"]


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------
def test_low_risk_does_not_alert():
    from app.services import alerts as alerts_svc

    assert alerts_svc.should_alert("low") is False
    assert alerts_svc.should_alert("medium") is True
    assert alerts_svc.should_alert("high") is True


def test_analysis_publishes_an_alert_for_medium_or_high(stack_up, tokens):
    if not stack_up:
        pytest.skip("stack not up")
    from app.db.redis_client import get_client
    from app.services import alerts as alerts_svc
    from app.services.connectors.synthetic import get_complaints

    before = len(alerts_svc.read_alerts("0-0", count=500))
    address = next(c["address"] for c in get_complaints() if c["chain"] == "TRON")
    body = client.get(
        "/api/v1/wallet", params={"address": address}, headers=auth(tokens["investigator"])
    ).json()

    after = len(alerts_svc.read_alerts("0-0", count=500))
    if body["risk_label"] in ("medium", "high"):
        assert after > before, "a medium/high resolution must raise an alert"
        assert get_client().xlen(alerts_svc.STREAM_KEY) > 0


def test_websocket_delivers_backlog_and_declares_provenance(stack_up, tokens):
    """The socket must announce whether alerts describe synthetic data."""
    if not stack_up:
        pytest.skip("stack not up")
    from app.services import alerts as alerts_svc

    alerts_svc.publish_alert(
        None,
        severity="high",
        message="Test alert for the websocket backlog.",
        case_number="SIH183-TEST",
        entity_name="Test Exchange",
        risk_score=91.0,
        persist=False,
    )

    token = tokens["investigator"]
    with client.websocket_connect(f"/api/v1/ws/alerts?backlog=50&token={token}") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert hello["data_source"] in ("synthetic", "live_indexer_apis")

        # The alert just published must arrive in the replayed backlog.
        seen = []
        for _ in range(50):
            msg = ws.receive_json()
            if msg.get("type") == "alert":
                seen.append(msg)
                if any("websocket backlog" in m.get("message", "") for m in seen):
                    break
        assert any("websocket backlog" in m.get("message", "") for m in seen)
        assert all(m.get("is_synthetic") == "true" for m in seen)


def test_alerts_rest_fallback_lists_recent(stack_up, tokens):
    if not stack_up:
        pytest.skip("stack not up")
    body = client.get(
        "/api/v1/alerts/recent", params={"limit": 5}, headers=auth(tokens["investigator"])
    ).json()
    assert body["transport"] == "redis-streams"
    assert isinstance(body["alerts"], list)


def test_alert_socket_refuses_an_unauthenticated_client(stack_up):
    """Alert text names cases and destination exchanges - not public."""
    if not stack_up:
        pytest.skip("stack not up")
    from starlette.websockets import WebSocketDisconnect as WSDisconnect

    with pytest.raises((WSDisconnect, Exception)):
        with client.websocket_connect("/api/v1/ws/alerts?backlog=1") as ws:
            ws.receive_json()


def test_alert_socket_refuses_a_forged_token(stack_up):
    if not stack_up:
        pytest.skip("stack not up")
    from starlette.websockets import WebSocketDisconnect as WSDisconnect

    with pytest.raises((WSDisconnect, Exception)):
        with client.websocket_connect("/api/v1/ws/alerts?token=not-a-jwt") as ws:
            ws.receive_json()


def test_recent_alerts_returns_the_newest_not_the_oldest(stack_up, tokens):
    """Regression: XRANGE returns the OLDEST entries.

    `read_alerts` originally used xrange(count=N), which on a stream past N
    entries returns the first N alerts ever recorded - a list that never changes
    and never contains anything new. Both the dashboard backlog and the polling
    fallback silently showed ancient alerts.
    """
    if not stack_up:
        pytest.skip("stack not up")
    from app.services import alerts as alerts_svc

    marker = f"newest-check-{hashlib.sha256(str(id(object())).encode()).hexdigest()[:10]}"
    alerts_svc.publish_alert(
        None, severity="high", message=marker, case_number="SIH183-TEST",
        risk_score=99.0, persist=False,
    )

    recent = alerts_svc.read_alerts("0-0", count=5)
    assert recent, "stream returned nothing"
    assert any(a.get("message") == marker for a in recent), (
        "a just-published alert must appear in the recent list"
    )
    assert recent[0].get("message") == marker, "recent alerts must be newest-first"
