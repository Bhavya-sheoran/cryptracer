"""Request/response models for wallet intake."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class AddressValidationRequest(BaseModel):
    address: str = Field(..., description="Suspect wallet address as reported by the victim")


class AddressValidationResponse(BaseModel):
    address: str
    valid: bool
    chain: str | None = None
    address_norm: str | None = None
    address_kind: str | None = None
    reason: str | None = None
    warnings: list[str] = Field(default_factory=list)


class WalletIntakeRequest(BaseModel):
    """A victim-reported suspect wallet.

    `victim_ref` is a pseudonymous handle. Do not send names, phone numbers or
    any other personal data - the schema has nowhere to put it on purpose.
    """

    address: str = Field(..., description="Suspect wallet address")
    victim_ref: str | None = Field(None, max_length=64, description="Pseudonymous handle only")
    amount_inr: Decimal | None = Field(None, ge=0)
    narrative: str | None = Field(None, max_length=4000)
    ncrp_ref: str | None = Field(None, max_length=64, description="Mock NCRP acknowledgement id")
    source: str = Field("manual", description="manual | ncrp_mock | synthetic")
    trace_depth: int | None = Field(None, ge=1, le=8)


class DuplicateInfo(BaseModel):
    """Multiple victims reporting one wallet is itself a fraud signal."""

    is_duplicate: bool
    prior_case_count: int
    prior_case_numbers: list[str] = Field(default_factory=list)
    note: str | None = None


class TraceSummary(BaseModel):
    trace_run_id: uuid.UUID
    status: str
    data_source: str
    max_depth: int
    hops_discovered: int
    addresses_touched: int
    transactions_ingested: int
    mixer_interaction: bool

    # How much of the reachable graph was actually walked. `status` says the
    # run did not error; these say whether it ran out of budget first. An
    # investigator reading "no exchange found" needs to distinguish "none
    # within 8 hops" from "we stopped looking".
    upstream_calls: int = 0
    complete: bool = True
    budget_exhausted: bool = False
    frontier_truncated: bool = False
    coverage_note: str | None = None


class ClusterSummary(BaseModel):
    cluster_key: str | None = None
    heuristic: str | None = None
    size: int = 0
    members: list[str] = Field(default_factory=list)


class WalletIntakeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    case_id: uuid.UUID
    case_number: str
    wallet_id: uuid.UUID
    address: str
    chain: str
    address_kind: str | None
    reported_at: datetime
    duplicate: DuplicateInfo
    trace: TraceSummary
    cluster: ClusterSummary
    warnings: list[str] = Field(default_factory=list)
    data_provenance: str = Field(
        ...,
        description="'synthetic' or the live indexer used. Never presented as real NCRP/KYC data.",
    )


class CaseBrief(BaseModel):
    case_id: uuid.UUID
    case_number: str
    reported_at: datetime
    status: str
    amount_inr: Decimal | None = None
    victim_ref: str | None = None


class WalletDetailResponse(BaseModel):
    wallet_id: uuid.UUID
    address: str
    chain: str
    address_kind: str | None
    first_seen_at: datetime
    last_traced_at: datetime | None
    report_count: int
    cases: list[CaseBrief] = Field(default_factory=list)
    cluster: ClusterSummary
