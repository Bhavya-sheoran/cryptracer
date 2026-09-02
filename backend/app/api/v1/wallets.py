"""Wallet intake and lookup endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.postgres import get_db
from app.deps import get_current_user
from app.models import Case, CaseWallet, User, Wallet
from app.schemas.wallet import (
    AddressValidationRequest,
    AddressValidationResponse,
    CaseBrief,
    ClusterSummary,
    WalletDetailResponse,
    WalletIntakeRequest,
    WalletIntakeResponse,
)
from app.services import clustering
from app.services.chain_detect import detect
from app.services.ingest import IntakeError, intake_wallet

router = APIRouter(prefix="/wallets", tags=["wallets"])


@router.post("/validate", response_model=AddressValidationResponse)
def validate_address(payload: AddressValidationRequest) -> AddressValidationResponse:
    """Detect the chain and verify the checksum without creating a case.

    Lets the intake form give immediate feedback on a mistyped address instead
    of opening a case that can never be traced.
    """
    return AddressValidationResponse(**detect(payload.address).as_dict())


@router.post("", response_model=WalletIntakeResponse, status_code=status.HTTP_201_CREATED)
def create_intake(payload: WalletIntakeRequest, db: Session = Depends(get_db)):
    """Report a suspect wallet: opens a case, builds the graph, clusters it."""
    try:
        result = intake_wallet(
            db,
            address=payload.address,
            victim_ref=payload.victim_ref,
            amount_inr=payload.amount_inr,
            narrative=payload.narrative,
            ncrp_ref=payload.ncrp_ref,
            source=payload.source,
            trace_depth=payload.trace_depth,
        )
    except IntakeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return WalletIntakeResponse(**result)


# NOTE: the literal-path routes below must be declared BEFORE the
# /{chain}/{address} catch-all, or FastAPI matches e.g. /wallets/id/<uuid>
# against it and treats "id" as a chain name.
@router.get("/multi-reported", response_model=list[dict])
def multi_reported(
    db: Session = Depends(get_db), _user: User = Depends(get_current_user)
):
    """Wallets named in more than one case - candidate fraud rings.

    Authenticated: reveals which addresses recur across complaints.

    Reads v_multi_reported_wallets, the cross-case view from the Phase 0 schema.
    """
    from sqlalchemy import text

    rows = db.execute(
        text(
            "SELECT wallet_id::text, address, chain::text, case_count, "
            "first_reported_at, last_reported_at "
            "FROM v_multi_reported_wallets ORDER BY case_count DESC, last_reported_at DESC"
        )
    ).mappings()
    return [dict(r) for r in rows]


@router.get("/id/{wallet_id}", response_model=WalletDetailResponse)
def get_wallet_by_id(wallet_id: uuid.UUID, db: Session = Depends(get_db)):
    wallet = db.get(Wallet, wallet_id)
    if wallet is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="wallet not found")
    return get_wallet(wallet.chain, wallet.address, db)


@router.get("/{chain}/{address}", response_model=WalletDetailResponse)
def get_wallet(chain: str, address: str, db: Session = Depends(get_db)):
    """Look up a wallet, the cases naming it, and its cluster."""
    info = detect(address)
    if not info.valid or info.chain != chain.upper():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=info.reason or f"address is not a valid {chain.upper()} address",
        )

    wallet = db.execute(
        select(Wallet).where(
            Wallet.chain == info.chain, Wallet.address_norm == info.address_norm
        )
    ).scalar_one_or_none()
    if wallet is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="wallet not found")

    cases = (
        db.execute(
            select(Case)
            .join(CaseWallet, CaseWallet.case_id == Case.id)
            .where(CaseWallet.wallet_id == wallet.id)
            .order_by(Case.reported_at.desc())
        )
        .scalars()
        .all()
    )

    cluster = clustering.get_cluster_for_address(info.chain, info.address_norm) or {}

    return WalletDetailResponse(
        wallet_id=wallet.id,
        address=wallet.address,
        chain=wallet.chain,
        address_kind=wallet.address_kind,
        first_seen_at=wallet.first_seen_at,
        last_traced_at=wallet.last_traced_at,
        report_count=wallet.report_count,
        cases=[
            CaseBrief(
                case_id=c.id,
                case_number=c.case_number,
                reported_at=c.reported_at,
                status=c.status,
                amount_inr=c.amount_inr,
                victim_ref=c.victim_ref,
            )
            for c in cases
        ],
        cluster=ClusterSummary(
            cluster_key=cluster.get("cluster_key"),
            heuristic=cluster.get("heuristic"),
            size=cluster.get("size", 0),
            members=cluster.get("members", []),
        ),
    )
