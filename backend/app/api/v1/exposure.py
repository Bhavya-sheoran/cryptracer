"""Service exposure endpoint.

`GET /api/v1/exposure?address=...` answers the question an investigator asks
first: where did this money end up, and how confident are we.

Query parameter rather than a path segment, deliberately. Addresses are
user-supplied strings of varying alphabets, and putting one in a path invites
encoding problems the moment someone pastes a URI-prefixed or whitespace-padded
value. It also keeps the shape consistent with the existing
`GET /api/v1/wallet?address=`.

Authenticated: a result names services, transaction hashes and rupee amounts
tied to an investigation subject. That is not public information.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.postgres import get_db
from app.deps import get_current_user
from app.models import Case, CaseWallet, ExposurePathRecord, ServiceExposureRecord, User, Wallet
from app.services import exposure as exposure_svc
from app.services.chain_detect import detect

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(tags=["exposure"])


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _persist(
    db: Session,
    result: dict,
    chain: str,
    address_norm: str,
    user: User,
) -> ServiceExposureRecord:
    """Store the finding and every candidate behind it.

    Stored rather than recomputed on demand because an exposure can end up cited
    in a freeze request. Re-running it later against a changed graph, changed
    weights or a changed price would quietly produce a different answer with no
    record that the first one ever existed.
    """
    case_id = db.execute(
        select(Case.id)
        .join(CaseWallet, CaseWallet.case_id == Case.id)
        .join(Wallet, Wallet.id == CaseWallet.wallet_id)
        .where(Wallet.chain == chain, Wallet.address_norm == address_norm)
        .order_by(Case.reported_at.desc())
        .limit(1)
    ).scalar_one_or_none()

    top = result.get("top") or {}
    top_features = top.get("features") or {}

    record = ServiceExposureRecord(
        chain=chain,
        address_norm=address_norm,
        case_id=case_id,
        kind=result["kind"],
        searched_to_hop=result["searched_to_hop"],
        top_service=top.get("service") or top_features.get("service"),
        top_service_type=top.get("service_type") or top_features.get("service_type"),
        top_hop=top.get("hop") or top_features.get("hop"),
        top_score=top.get("score"),
        top_volume_inr=top_features.get("total_volume_inr"),
        scoring_version=result["scoring_version"],
        weights=result.get("weights") or {},
        explanation=result.get("explanation"),
        computed_by=user.id,
    )
    db.add(record)
    db.flush()

    for index, candidate in enumerate(result.get("candidates") or [], start=1):
        features = candidate.get("features") or {}
        evidence = candidate.get("evidence") or {}
        # A direct exposure carries one label; an indirect candidate carries the
        # set gathered across its paths.
        label = candidate.get("label") or {}
        label_sources = features.get("label_sources") or (
            [label["source"]] if label.get("source") else None
        )
        db.add(
            ExposurePathRecord(
                exposure_id=record.id,
                rank=candidate.get("rank", index),
                service=candidate.get("service") or features.get("service") or "unknown",
                service_type=candidate.get("service_type") or features.get("service_type"),
                service_address=candidate.get("address"),
                hop=candidate.get("hop") or features.get("hop") or 0,
                score=candidate.get("score"),
                total_volume=features.get("total_volume"),
                total_volume_inr=features.get("total_volume_inr"),
                volume_basis=features.get("volume_basis"),
                transfer_count=features.get("transfer_count"),
                unique_counterparties=features.get("unique_counterparties"),
                continuity_ok=features.get("continuity_ok"),
                last_seen=_parse_dt(features.get("last_seen")),
                label_confidence=(
                    features.get("label_confidence") or label.get("confidence")
                ),
                label_sources=label_sources,
                price_sources=features.get("price_sources"),
                path=candidate.get("path") or features.get("shortest_path"),
                features=features,
                explanation=candidate.get("explanation") or [],
                evidence_txid=evidence.get("txid"),
                evidence_amount=evidence.get("amount_attributed") or evidence.get("amount"),
                evidence_asset=evidence.get("asset"),
                evidence_timestamp=_parse_dt(evidence.get("timestamp")),
            )
        )

    db.commit()
    return record


@router.get("/exposure")
def get_exposure(
    address: str = Query(..., description="Suspect wallet address"),
    max_hops: int = Query(5, ge=1, le=8, description="How far to search"),
    persist: bool = Query(True, description="Store the finding for later reference"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Which service did this wallet's money reach, and why is that the answer.

    Direct exposure short-circuits: a one-hop hit needs no ranking and carries
    the strongest evidence. Otherwise candidates are ranked on volume, recency,
    frequency, proximity, path continuity and label quality, each contribution
    reported so the ordering can be argued with rather than trusted.
    """
    info = detect(address)
    if not info.valid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=info.reason or "invalid address",
        )

    result = exposure_svc.analyse_exposure(info.chain, info.address_norm, max_hops=max_hops)

    record_id = None
    if persist:
        try:
            record_id = str(_persist(db, result, info.chain, info.address_norm, user).id)
        except Exception as exc:  # noqa: BLE001
            # A storage failure must not deny the investigator the answer.
            db.rollback()
            logger.warning("exposure persistence failed: %s", exc)

    return {
        **result,
        "exposure_id": record_id,
        "address_input": info.address,
        "data_provenance": "synthetic" if settings.demo_mode else "live_indexer_apis",
        "notice": (
            "Recommendation only. Service attribution rests on public labels and an "
            "explainable score, not on exchange KYC. Any freeze or disclosure request "
            "requires explicit approval by an authorised officer."
        ),
    }


@router.get("/exposure/history")
def exposure_history(
    address: str = Query(..., description="Suspect wallet address"),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Previous findings for this address, newest first.

    Lets an investigator see that an answer changed - and when, and under which
    scoring version - rather than only ever seeing the current one.
    """
    info = detect(address)
    if not info.valid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=info.reason or "invalid address",
        )

    rows = (
        db.execute(
            select(ServiceExposureRecord)
            .where(
                ServiceExposureRecord.chain == info.chain,
                ServiceExposureRecord.address_norm == info.address_norm,
            )
            .order_by(ServiceExposureRecord.computed_at.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )

    return {
        "address": info.address_norm,
        "chain": info.chain,
        "count": len(rows),
        "findings": [
            {
                "exposure_id": str(r.id),
                "kind": r.kind,
                "top_service": r.top_service,
                "top_service_type": r.top_service_type,
                "top_hop": r.top_hop,
                "top_score": float(r.top_score) if r.top_score is not None else None,
                "top_volume_inr": (
                    float(r.top_volume_inr) if r.top_volume_inr is not None else None
                ),
                "scoring_version": r.scoring_version,
                "explanation": r.explanation,
                "computed_at": r.computed_at.isoformat() if r.computed_at else None,
            }
            for r in rows
        ],
    }
