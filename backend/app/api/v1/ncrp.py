"""MOCK NCRP / 1930 intake endpoint.

THIS IS A MOCK. There is no connection to the National Cybercrime Reporting
Portal or the 1930 helpline, and no such access was available for this project.
What this models is the *API contract* such an integration would need: a
complaint arrives from an upstream portal, is acknowledged, and enters the same
pipeline a manually filed complaint does.

Every complaint arriving here is recorded with `source = "ncrp_mock"`, so a
mock submission can never be mistaken for a real one in the case record.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.postgres import get_db
from app.services.ingest import IntakeError, intake_wallet

router = APIRouter(prefix="/ncrp", tags=["ncrp (mock)"])

MOCK_NOTICE = (
    "MOCK ENDPOINT. This models the NCRP/1930 intake contract for demonstration. "
    "It is not connected to the National Cybercrime Reporting Portal and processes "
    "no real complaint data."
)


class NcrpComplaint(BaseModel):
    """The shape an NCRP-style upstream would plausibly send."""

    ncrp_ack_no: str = Field(
        ..., min_length=4, max_length=64, description="Upstream acknowledgement number"
    )
    suspect_wallet: str = Field(..., description="Reported suspect wallet address")
    amount_inr: float | None = Field(None, ge=0)
    incident_datetime: datetime | None = None
    complainant_ref: str | None = Field(
        None,
        max_length=64,
        description="Pseudonymous reference ONLY. Do not send names or contact details.",
    )
    category: str | None = Field(None, max_length=64, description="e.g. investment_fraud")
    narrative: str | None = Field(None, max_length=4000)


class NcrpAck(BaseModel):
    accepted: bool
    ncrp_ack_no: str
    internal_case_number: str
    internal_case_id: uuid.UUID
    chain: str
    duplicate_of: list[str] = Field(default_factory=list)
    trace_status: str
    received_at: datetime
    notice: str


@router.post("/intake", response_model=NcrpAck, status_code=status.HTTP_202_ACCEPTED)
def ncrp_intake(payload: NcrpComplaint, db: Session = Depends(get_db)):
    """Accept a complaint from the mock NCRP feed and run the standard pipeline."""
    try:
        result = intake_wallet(
            db,
            address=payload.suspect_wallet,
            victim_ref=payload.complainant_ref,
            amount_inr=payload.amount_inr,
            narrative=payload.narrative,
            ncrp_ref=payload.ncrp_ack_no,
            source="ncrp_mock",
        )
    except IntakeError as exc:
        # Reject at the boundary: an upstream portal needs to know its address was
        # malformed now, not discover an untraceable case later.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    return NcrpAck(
        accepted=True,
        ncrp_ack_no=payload.ncrp_ack_no,
        internal_case_number=result["case_number"],
        internal_case_id=result["case_id"],
        chain=result["chain"],
        duplicate_of=result["duplicate"]["prior_case_numbers"],
        trace_status=result["trace"]["status"],
        received_at=datetime.now(UTC),
        notice=MOCK_NOTICE,
    )


@router.get("/contract")
def contract():
    """Document the mock contract so an integrator can see what is expected."""
    return {
        "notice": MOCK_NOTICE,
        "endpoint": "POST /api/v1/ncrp/intake",
        "request_example": {
            "ncrp_ack_no": "NCRP2026DEMO000123",
            "suspect_wallet": "BTC / 0x / T-prefixed address",
            "amount_inr": 250000,
            "incident_datetime": "2026-08-30T10:15:00Z",
            "complainant_ref": "PSEUDO-REF-014",
            "category": "investment_fraud",
            "narrative": "Free-text description of the incident.",
        },
        "pii_policy": (
            "complainant_ref must be a pseudonymous reference. The schema has no field "
            "for a name, phone number or address, deliberately."
        ),
        "responses": {
            "202": "Accepted; complaint entered the tracing pipeline.",
            "422": "Rejected; the wallet address failed checksum validation.",
        },
    }
