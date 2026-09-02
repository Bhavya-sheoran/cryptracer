"""The unified analysis endpoint: GET /api/v1/wallet.

Returns everything an investigator needs about one reported address in a single
response: the traced flow, what the terminal cluster attributes to, the
exchange's fraud-linkage score, and the case IDs that produced that score.

The contributing case IDs are not decoration - they are the audit trail. A
"High" rating that cannot name the complaints behind it is not actionable, and
this endpoint is built so that never happens.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.postgres import get_db
from app.deps import get_current_user
from app.models import Case, CaseWallet, User, Wallet
from app.schemas.analysis import WalletAnalysisResponse
from app.services import alerts as alerts_svc
from app.services import attribution as attribution_svc
from app.services import risk as risk_svc
from app.services import tracing
from app.services.chain_detect import detect

router = APIRouter(tags=["analysis"])
settings = get_settings()


@router.get("/wallet", response_model=WalletAnalysisResponse)
def analyse_wallet(
    address: str = Query(..., description="Suspect wallet address"),
    depth: int | None = Query(None, ge=1, le=8, description="Trace depth override"),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Trace, attribute and score a reported wallet in one call.

    Requires a signed-in officer: the response names contributing case numbers,
    case ids and reported timestamps. That is the investigation picture - which
    exchanges are under scrutiny and how many complaints sit behind each - and
    it must not be readable anonymously.
    """
    info = detect(address)
    if not info.valid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=info.reason or "invalid address",
        )

    chain, address_norm = info.chain, info.address_norm
    trace_depth = depth or settings.trace_max_depth

    # --- flow ------------------------------------------------------------
    path = tracing.trace_path(chain, address_norm, depth=trace_depth)
    terminals = tracing.terminal_attributions(chain, address_norm, depth=trace_depth)

    if path["node_count"] <= 1 and not terminals:
        # Nothing in the graph yet: the wallet has not been through intake.
        wallet = db.execute(
            select(Wallet).where(Wallet.chain == chain, Wallet.address_norm == address_norm)
        ).scalar_one_or_none()
        if wallet is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    "No graph data for this address. Submit it via POST /api/v1/wallets "
                    "first so the flow can be traced."
                ),
            )

    # --- attribution -----------------------------------------------------
    # Attribute the nearest tagged terminal if the trace reached one; otherwise
    # fall back to attributing the reported address's own cluster.
    attribution_target = terminals[0]["address"] if terminals else address_norm
    attribution = attribution_svc.attribute(chain, attribution_target)

    # --- mixer flag ------------------------------------------------------
    mixer_hit = any(
        n.get("entity_type") == "mixer" for n in path["nodes"] if isinstance(n, dict)
    )

    # --- risk ------------------------------------------------------------
    risk = risk_svc.score_entity(
        db,
        chain=chain,
        cluster_key=attribution.cluster_key,
        entity_name=attribution.entity_name,
        entity_type=attribution.entity_type,
        mixer_interaction=mixer_hit,
        persist=True,
    )

    # --- cases naming this wallet ---------------------------------------
    reported_in = [
        {
            "case_id": str(c.id),
            "case_number": c.case_number,
            "reported_at": c.reported_at.isoformat() if c.reported_at else None,
            "status": c.status,
        }
        for c in db.execute(
            select(Case)
            .join(CaseWallet, CaseWallet.case_id == Case.id)
            .join(Wallet, Wallet.id == CaseWallet.wallet_id)
            .where(Wallet.chain == chain, Wallet.address_norm == address_norm)
            .order_by(Case.reported_at.desc())
        )
        .scalars()
        .all()
    ]

    # --- real-time alert -------------------------------------------------
    # A Medium/High resolution is pushed to the dashboard over Redis Streams ->
    # WebSocket rather than written silently to a table. Low is deliberately not
    # alerted: alerting on everything trains investigators to ignore the feed.
    if alerts_svc.should_alert(risk.label):
        case_number = reported_in[0]["case_number"] if reported_in else "an unfiled query"
        case_id = reported_in[0]["case_id"] if reported_in else None
        alerts_svc.publish_alert(
            db,
            severity=risk.label,
            message=alerts_svc.build_message(
                attribution.entity_name, risk.label, float(risk.score), case_number
            ),
            case_id=case_id,
            case_number=case_number,
            entity_name=attribution.entity_name,
            risk_score=float(risk.score),
            address=address_norm,
        )

    return WalletAnalysisResponse(
        address=info.address,
        chain=chain,
        address_norm=address_norm,
        address_kind=info.address_kind,
        trace_path=path,
        terminal_attributions=terminals,
        attribution=attribution.as_dict(),
        risk_label=risk.label,
        risk_score=risk.score,
        risk_explanation=risk.explanation,
        risk_factors=risk.factors,
        contributing_case_ids=risk.contributing_case_ids,
        contributions=risk.contributions,
        mixer_interaction=mixer_hit,
        reported_in_cases=reported_in,
        data_provenance="synthetic" if settings.demo_mode else "live_indexer_apis",
        notice=(
            "Recommendation only. Any freeze or disclosure request requires explicit "
            "approval by an authorised officer. Demonstration system built on synthetic "
            "complaints and public datasets."
        ),
    )


@router.get("/exchanges/ranked")
def ranked_exchanges(
    chain: str | None = Query(None, description="Filter to one chain"),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Exchanges ranked by fraud linkage across all cases.

    Authenticated: each row lists the case numbers behind the ranking.
    """
    return {
        "chain": chain,
        "entities": risk_svc.rank_entities(db, chain=chain, limit=limit),
        "scoring": {
            "half_life_days": settings.risk_decay_half_life_days,
            "medium_threshold": settings.risk_medium_threshold,
            "high_threshold": settings.risk_high_threshold,
            "model_version": risk_svc.MODEL_VERSION,
        },
        "notice": "Scores are explainable aggregates over reported cases, not a black box.",
    }
