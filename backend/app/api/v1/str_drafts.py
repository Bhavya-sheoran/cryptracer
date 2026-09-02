"""FIU-IND-style STR draft endpoints. Drafts only - nothing is filed."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.postgres import get_db
from app.deps import get_current_user, record_audit, require_approver
from app.models import Case, CaseWallet, StrDraft, User, Wallet
from app.services import attribution as attribution_svc
from app.services import risk as risk_svc
from app.services import tracing
from app.services.str_draft import create_str_draft

router = APIRouter(prefix="/str-drafts", tags=["str"])

NOT_FILED = (
    "DRAFT ONLY. This system does not file anything with FIU-IND and has no FinNet "
    "connection. A designated officer must review, complete and file any actual STR."
)


class StrDraftIn(BaseModel):
    case_id: uuid.UUID


def _serialise(d: StrDraft) -> dict:
    return {
        "id": str(d.id),
        "case_id": str(d.case_id),
        "status": d.status,
        "body": d.body,
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "approved_at": d.approved_at.isoformat() if d.approved_at else None,
        "notice": NOT_FILED,
    }


@router.post("", status_code=status.HTTP_201_CREATED)
def generate(
    payload: StrDraftIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    case = db.get(Case, payload.case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="case not found")

    wallet = db.execute(
        select(Wallet)
        .join(CaseWallet, CaseWallet.wallet_id == Wallet.id)
        .where(CaseWallet.case_id == case.id)
        .limit(1)
    ).scalar_one_or_none()
    if wallet is None:
        raise HTTPException(status_code=409, detail="case has no reported wallet to report on")

    terminals = tracing.terminal_attributions(wallet.chain, wallet.address_norm)
    target = terminals[0]["address"] if terminals else wallet.address_norm
    attrib = attribution_svc.attribute(wallet.chain, target)
    risk = risk_svc.score_entity(
        db,
        chain=wallet.chain,
        cluster_key=attrib.cluster_key,
        entity_name=attrib.entity_name,
        entity_type=attrib.entity_type,
        persist=False,
    )

    draft = create_str_draft(
        db,
        case,
        created_by=user,
        address=wallet.address,
        chain=wallet.chain,
        entity_name=attrib.entity_name,
        entity_type=attrib.entity_type,
        attribution_method=attrib.method,
        attribution_source=attrib.source,
        risk_label=risk.label,
        risk_score=float(risk.score),
        contributing_cases=[c["case_number"] for c in risk.contributions],
        mixer_interaction=any(t.get("entity_type") == "mixer" for t in terminals),
        hops=terminals[0]["hop"] if terminals else None,
    )
    record_audit(
        db, user, "str.draft.create", "str_draft", str(draft.id),
        {"case": case.case_number}, request,
    )
    db.commit()
    return _serialise(draft)


@router.get("")
def list_drafts(
    case_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    stmt = select(StrDraft).order_by(StrDraft.created_at.desc())
    if case_id:
        stmt = stmt.where(StrDraft.case_id == case_id)
    return {
        "drafts": [_serialise(d) for d in db.execute(stmt).scalars().all()],
        "notice": NOT_FILED,
    }


@router.get("/{draft_id}/text", response_class=PlainTextResponse)
def draft_text(
    draft_id: uuid.UUID, db: Session = Depends(get_db), _user: User = Depends(get_current_user)
):
    draft = db.get(StrDraft, draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="draft not found")
    return draft.body


@router.post("/{draft_id}/approve")
def approve(
    draft_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    approver: User = Depends(require_approver),
):
    """Record an officer's review and approval of a draft.

    Approving here records a decision. It does not transmit the STR anywhere.
    """
    draft = db.get(StrDraft, draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="draft not found")
    if draft.status != "draft":
        raise HTTPException(status_code=409, detail=f"draft is already {draft.status}")
    if draft.created_by is not None and draft.created_by == approver.id:
        raise HTTPException(
            status_code=403,
            detail="The officer who generated the draft cannot approve it.",
        )

    draft.status = "approved"
    draft.approved_by = approver.id
    draft.approved_at = datetime.now(UTC)
    record_audit(db, approver, "str.draft.approve", "str_draft", str(draft.id), {}, request)
    db.commit()
    db.refresh(draft)
    result = _serialise(draft)
    result["notice"] = (
        "Approved for filing by an officer. This system still files nothing; "
        "transmission to FIU-IND remains a manual, human step."
    )
    return result
