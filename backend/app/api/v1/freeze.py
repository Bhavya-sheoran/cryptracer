"""Exchange freeze-request workflow. Human approval is mandatory.

Lifecycle:

    draft  ->  pending_approval  ->  approved   ->  dispatched
                               \\->  rejected

Rules enforced here, and asserted by tests:

  1. Creating a request NEVER produces an approved one. A new request is always
     `draft`, whatever the caller sends.
  2. Only a supervisor or admin can approve (`require_approver`).
  3. The approver must be a **different person** from the requester. One
     account cannot draft and then wave through its own freeze.
  4. Only `approved` requests can be dispatched, and dispatch is itself a
     separate explicit call.
  5. Every transition is written to the audit log with the actor.

There is deliberately no code path anywhere in this system - not in intake, not
in scoring, not in alerting - that calls the approval function. It is reachable
only from an authenticated request made by a human.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.postgres import get_db
from app.deps import get_current_user, record_audit, require_approver
from app.models import Case, Entity, FreezeRequest, User

router = APIRouter(prefix="/freeze-requests", tags=["freeze"])

HUMAN_IN_THE_LOOP_NOTICE = (
    "This system recommends; it does not act. A freeze request is transmitted to an "
    "exchange only after an authorised officer approves it, and this prototype does "
    "not transmit anything to any real exchange."
)


class FreezeRequestIn(BaseModel):
    case_id: uuid.UUID
    target_address: str = Field(..., min_length=6, max_length=128)
    entity_name: str | None = None
    amount_inr: float | None = Field(None, ge=0)
    justification: str = Field(..., min_length=20, max_length=4000)


class RejectIn(BaseModel):
    reason: str = Field(..., min_length=5, max_length=1000)


def _serialise(fr: FreezeRequest, db: Session) -> dict:
    def username(user_id):
        if not user_id:
            return None
        u = db.get(User, user_id)
        return u.username if u else None

    return {
        "id": str(fr.id),
        "case_id": str(fr.case_id),
        "target_address": fr.target_address,
        "amount_inr": float(fr.amount_inr) if fr.amount_inr is not None else None,
        "justification": fr.justification,
        "status": fr.status,
        "requested_by": username(fr.requested_by),
        "approved_by": username(fr.approved_by),
        "approved_at": fr.approved_at.isoformat() if fr.approved_at else None,
        "rejection_reason": fr.rejection_reason,
        "created_at": fr.created_at.isoformat() if fr.created_at else None,
        "notice": HUMAN_IN_THE_LOOP_NOTICE,
    }


@router.get("")
def list_requests(
    case_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    stmt = select(FreezeRequest).order_by(FreezeRequest.created_at.desc())
    if case_id:
        stmt = stmt.where(FreezeRequest.case_id == case_id)
    return {
        "requests": [_serialise(fr, db) for fr in db.execute(stmt).scalars().all()],
        "notice": HUMAN_IN_THE_LOOP_NOTICE,
    }


@router.post("", status_code=status.HTTP_201_CREATED)
def create_request(
    payload: FreezeRequestIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Draft a freeze request. Always created in `draft` - never approved."""
    case = db.get(Case, payload.case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="case not found")

    entity_id = None
    if payload.entity_name:
        entity = db.execute(
            select(Entity).where(Entity.name == payload.entity_name)
        ).scalars().first()
        entity_id = entity.id if entity else None

    fr = FreezeRequest(
        case_id=case.id,
        entity_id=entity_id,
        target_address=payload.target_address,
        amount_inr=payload.amount_inr,
        justification=payload.justification,
        status="draft",          # not settable by the caller, on purpose
        requested_by=user.id,
    )
    db.add(fr)
    record_audit(db, user, "freeze.create", "freeze_request", None,
                 {"case": case.case_number, "target": payload.target_address}, request)
    db.commit()
    db.refresh(fr)
    return _serialise(fr, db)


@router.post("/{request_id}/submit")
def submit_for_approval(
    request_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Move a draft to `pending_approval`. Still not approved."""
    fr = db.get(FreezeRequest, request_id)
    if fr is None:
        raise HTTPException(status_code=404, detail="freeze request not found")
    if fr.status != "draft":
        raise HTTPException(status_code=409, detail=f"cannot submit a request in '{fr.status}'")

    fr.status = "pending_approval"
    record_audit(db, user, "freeze.submit", "freeze_request", str(fr.id), {}, request)
    db.commit()
    db.refresh(fr)
    return _serialise(fr, db)


@router.post("/{request_id}/approve")
def approve_request(
    request_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    approver: User = Depends(require_approver),
):
    """Approve a pending freeze request.

    The ONLY path to `approved` in this codebase. Requires a supervisor/admin
    who is not the requester.
    """
    fr = db.get(FreezeRequest, request_id)
    if fr is None:
        raise HTTPException(status_code=404, detail="freeze request not found")
    if fr.status != "pending_approval":
        raise HTTPException(
            status_code=409,
            detail=(
                "only a request in 'pending_approval' can be approved "
                f"(currently '{fr.status}')"
            ),
        )
    if fr.requested_by is not None and fr.requested_by == approver.id:
        raise HTTPException(
            status_code=403,
            detail=(
                "The officer who raised a freeze request cannot approve it. "
                "Separation of duties requires a different authorised officer."
            ),
        )

    fr.status = "approved"
    fr.approved_by = approver.id
    fr.approved_at = datetime.now(UTC)
    record_audit(
        db, approver, "freeze.approve", "freeze_request", str(fr.id),
        {"target": fr.target_address}, request,
    )
    db.commit()
    db.refresh(fr)
    return _serialise(fr, db)


@router.post("/{request_id}/reject")
def reject_request(
    request_id: uuid.UUID,
    payload: RejectIn,
    request: Request,
    db: Session = Depends(get_db),
    approver: User = Depends(require_approver),
):
    fr = db.get(FreezeRequest, request_id)
    if fr is None:
        raise HTTPException(status_code=404, detail="freeze request not found")
    if fr.status not in ("pending_approval", "draft"):
        raise HTTPException(status_code=409, detail=f"cannot reject a request in '{fr.status}'")

    fr.status = "rejected"
    fr.rejection_reason = payload.reason
    fr.approved_by = approver.id
    fr.approved_at = datetime.now(UTC)
    record_audit(db, approver, "freeze.reject", "freeze_request", str(fr.id),
                 {"reason": payload.reason}, request)
    db.commit()
    db.refresh(fr)
    return _serialise(fr, db)


@router.post("/{request_id}/dispatch")
def dispatch_request(
    request_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    approver: User = Depends(require_approver),
):
    """Mark an approved request as transmitted.

    This prototype has no exchange integration and transmits nothing. The
    endpoint records that a human dispatched it through their own channel.
    """
    fr = db.get(FreezeRequest, request_id)
    if fr is None:
        raise HTTPException(status_code=404, detail="freeze request not found")
    if fr.status != "approved":
        raise HTTPException(
            status_code=409,
            detail=f"only an approved request can be dispatched (currently '{fr.status}')",
        )

    fr.status = "dispatched"
    record_audit(db, approver, "freeze.dispatch", "freeze_request", str(fr.id), {}, request)
    db.commit()
    db.refresh(fr)
    result = _serialise(fr, db)
    result["notice"] = (
        "Recorded as dispatched. This prototype does not transmit to any real exchange; "
        "the officer is responsible for sending the request through the lawful channel."
    )
    return result
