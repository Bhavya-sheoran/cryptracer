"""Case management: listing, notes, evidence, and the hashed forensic report."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.postgres import get_db
from app.deps import get_current_user, record_audit
from app.models import Case, CaseNote, CaseWallet, Evidence, Report, TraceRun, User, Wallet
from app.services import reports as reports_svc

router = APIRouter(prefix="/cases", tags=["cases"])

EVIDENCE_DIR = Path("/app/storage/evidence")
MAX_EVIDENCE_BYTES = 25 * 1024 * 1024  # 25 MB


class NoteIn(BaseModel):
    body: str = Field(..., min_length=1, max_length=8000)


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------
@router.get("")
def list_cases(
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    total = db.execute(select(func.count()).select_from(Case)).scalar_one()
    rows = (
        db.execute(select(Case).order_by(Case.reported_at.desc()).limit(limit).offset(offset))
        .scalars()
        .all()
    )
    return {
        "total": total,
        "cases": [
            {
                "case_id": str(c.id),
                "case_number": c.case_number,
                "status": c.status,
                "source": c.source,
                "ncrp_ref": c.ncrp_ref,
                "victim_ref": c.victim_ref,
                "amount_inr": float(c.amount_inr) if c.amount_inr is not None else None,
                "reported_at": c.reported_at.isoformat() if c.reported_at else None,
            }
            for c in rows
        ],
    }


def _get_case(db: Session, case_id: uuid.UUID) -> Case:
    case = db.get(Case, case_id)
    if case is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="case not found")
    return case


@router.get("/{case_id}")
def get_case(
    case_id: uuid.UUID, db: Session = Depends(get_db), _user: User = Depends(get_current_user)
):
    case = _get_case(db, case_id)
    wallets = db.execute(
        select(Wallet, CaseWallet.role)
        .join(CaseWallet, CaseWallet.wallet_id == Wallet.id)
        .where(CaseWallet.case_id == case.id)
    ).all()
    traces = (
        db.execute(select(TraceRun).where(TraceRun.case_id == case.id)).scalars().all()
    )
    notes = (
        db.execute(
            select(CaseNote)
            .where(CaseNote.case_id == case.id)
            .order_by(CaseNote.created_at)
        )
        .scalars()
        .all()
    )
    exhibits = (
        db.execute(select(Evidence).where(Evidence.case_id == case.id)).scalars().all()
    )
    docs = (
        db.execute(
            select(Report)
            .where(Report.case_id == case.id)
            .order_by(Report.generated_at.desc())
        )
        .scalars()
        .all()
    )

    return {
        "case_id": str(case.id),
        "case_number": case.case_number,
        "status": case.status,
        "source": case.source,
        "ncrp_ref": case.ncrp_ref,
        "victim_ref": case.victim_ref,
        "amount_inr": float(case.amount_inr) if case.amount_inr is not None else None,
        "narrative": case.narrative,
        "reported_at": case.reported_at.isoformat() if case.reported_at else None,
        "wallets": [
            {"address": w.address, "chain": w.chain, "role": role} for w, role in wallets
        ],
        "traces": [
            {
                "trace_run_id": str(t.id),
                "status": t.status,
                "max_depth": t.max_depth,
                "hops_discovered": t.hops_discovered,
                "addresses_touched": t.addresses_touched,
                "mixer_interaction": t.mixer_interaction,
                "data_source": t.data_source,
            }
            for t in traces
        ],
        "notes": [
            {
                "id": str(n.id),
                "body": n.body,
                "created_at": n.created_at.isoformat() if n.created_at else None,
            }
            for n in notes
        ],
        "evidence": [
            {
                "id": str(e.id),
                "filename": e.filename,
                "content_type": e.content_type,
                "size_bytes": e.size_bytes,
                "sha256": e.sha256,
                "uploaded_at": e.uploaded_at.isoformat() if e.uploaded_at else None,
            }
            for e in exhibits
        ],
        "reports": [
            {
                "id": str(r.id),
                "kind": r.kind,
                "sha256": r.sha256,
                "generated_at": r.generated_at.isoformat() if r.generated_at else None,
            }
            for r in docs
        ],
    }


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------
@router.post("/{case_id}/notes", status_code=status.HTTP_201_CREATED)
def add_note(
    case_id: uuid.UUID,
    payload: NoteIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    case = _get_case(db, case_id)
    note = CaseNote(case_id=case.id, author_id=user.id, body=payload.body)
    db.add(note)
    record_audit(db, user, "case.note.add", "case", str(case.id))
    db.commit()
    db.refresh(note)
    return {
        "id": str(note.id),
        "body": note.body,
        "author": user.username,
        "created_at": note.created_at.isoformat() if note.created_at else None,
    }


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------
@router.post("/{case_id}/evidence", status_code=status.HTTP_201_CREATED)
async def upload_evidence(
    case_id: uuid.UUID,
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Attach an exhibit. The SHA-256 is computed on the received bytes.

    The digest is taken from what actually landed on disk, so it attests to the
    stored artifact rather than to whatever the client claimed it sent.
    """
    case = _get_case(db, case_id)

    payload = await file.read()
    if len(payload) == 0:
        raise HTTPException(status_code=422, detail="uploaded file is empty")
    if len(payload) > MAX_EVIDENCE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"file exceeds the {MAX_EVIDENCE_BYTES // (1024 * 1024)} MB limit",
        )

    digest = hashlib.sha256(payload).hexdigest()

    # Store under a generated name: the uploaded filename is attacker-controlled
    # and must never determine a path on disk.
    case_dir = EVIDENCE_DIR / str(case.id)
    case_dir.mkdir(parents=True, exist_ok=True)
    stored = case_dir / f"{digest[:16]}_{uuid.uuid4().hex[:8]}"
    stored.write_bytes(payload)

    evidence = Evidence(
        case_id=case.id,
        filename=Path(file.filename or "exhibit").name,
        content_type=file.content_type,
        size_bytes=len(payload),
        sha256=digest,
        storage_path=str(stored),
        uploaded_by=user.id,
    )
    db.add(evidence)
    record_audit(
        db, user, "case.evidence.upload", "case", str(case.id),
        {"filename": evidence.filename, "sha256": digest}, request,
    )
    db.commit()
    db.refresh(evidence)

    return {
        "id": str(evidence.id),
        "filename": evidence.filename,
        "size_bytes": evidence.size_bytes,
        "sha256": evidence.sha256,
        "uploaded_at": evidence.uploaded_at.isoformat() if evidence.uploaded_at else None,
    }


@router.get("/{case_id}/evidence/{evidence_id}/verify")
def verify_evidence(
    case_id: uuid.UUID,
    evidence_id: uuid.UUID,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Recompute the stored exhibit's digest and compare with the record."""
    evidence = db.get(Evidence, evidence_id)
    if evidence is None or evidence.case_id != case_id:
        raise HTTPException(status_code=404, detail="evidence not found")

    path = Path(evidence.storage_path)
    if not path.exists():
        return {"verified": False, "reason": "stored file is missing"}
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "verified": actual == evidence.sha256,
        "stored_sha256": evidence.sha256,
        "recomputed_sha256": actual,
        "filename": evidence.filename,
    }


# ---------------------------------------------------------------------------
# Forensic report
# ---------------------------------------------------------------------------
@router.post("/{case_id}/report", status_code=status.HTTP_201_CREATED)
def generate_report(
    case_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Render the case to a hashed PDF."""
    case = _get_case(db, case_id)

    analysis = None
    wallet_row = db.execute(
        select(Wallet)
        .join(CaseWallet, CaseWallet.wallet_id == Wallet.id)
        .where(CaseWallet.case_id == case.id)
        .limit(1)
    ).scalar_one_or_none()

    if wallet_row is not None:
        # Reuse the same analysis the dashboard shows, so the report and the
        # screen can never disagree about the score.
        try:
            from app.services import attribution as attribution_svc
            from app.services import risk as risk_svc
            from app.services import tracing

            terminals = tracing.terminal_attributions(wallet_row.chain, wallet_row.address_norm)
            target = terminals[0]["address"] if terminals else wallet_row.address_norm
            attrib = attribution_svc.attribute(wallet_row.chain, target)
            risk = risk_svc.score_entity(
                db,
                chain=wallet_row.chain,
                cluster_key=attrib.cluster_key,
                entity_name=attrib.entity_name,
                entity_type=attrib.entity_type,
                persist=False,
            )
            analysis = {
                "attribution": attrib.as_dict(),
                "risk_label": risk.label,
                "risk_score": risk.score,
                "contributions": risk.contributions,
            }
        except Exception:  # noqa: BLE001 - a report without analysis is still useful
            analysis = None

    report = reports_svc.generate_case_report(db, case, generated_by=user, analysis=analysis)
    record_audit(
        db, user, "case.report.generate", "report", str(report.id),
        {"case_number": case.case_number, "sha256": report.sha256}, request,
    )
    db.commit()

    return {
        "report_id": str(report.id),
        "case_number": case.case_number,
        "kind": report.kind,
        "sha256": report.sha256,
        "generated_at": report.generated_at.isoformat() if report.generated_at else None,
        "download_url": f"/api/v1/cases/{case.id}/report/{report.id}/download",
        "verify_command": f"sha256sum <downloaded.pdf>  # expect {report.sha256}",
    }


@router.get("/{case_id}/report/{report_id}/download")
def download_report(
    case_id: uuid.UUID,
    report_id: uuid.UUID,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    report = db.get(Report, report_id)
    if report is None or report.case_id != case_id:
        raise HTTPException(status_code=404, detail="report not found")
    path = Path(report.storage_path)
    if not path.exists():
        raise HTTPException(status_code=410, detail="report file is no longer in storage")
    return FileResponse(path, media_type="application/pdf", filename=path.name)


@router.get("/{case_id}/report/{report_id}/verify")
def verify_report(
    case_id: uuid.UUID,
    report_id: uuid.UUID,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    report = db.get(Report, report_id)
    if report is None or report.case_id != case_id:
        raise HTTPException(status_code=404, detail="report not found")
    result = reports_svc.verify_report(report)
    result["generated_at"] = report.generated_at.isoformat() if report.generated_at else None
    result["checked_at"] = datetime.now(UTC).isoformat()
    return result
