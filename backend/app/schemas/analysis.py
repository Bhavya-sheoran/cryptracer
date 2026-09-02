"""Response model for the unified /api/v1/wallet analysis endpoint."""

from __future__ import annotations

from pydantic import BaseModel, Field


class WalletAnalysisResponse(BaseModel):
    address: str
    chain: str
    address_norm: str
    address_kind: str | None = None

    # --- flow ---------------------------------------------------------------
    trace_path: dict = Field(..., description="Hops plus Sankey-ready nodes and links")
    terminal_attributions: list[dict] = Field(default_factory=list)
    mixer_interaction: bool = False

    # --- attribution --------------------------------------------------------
    attribution: dict = Field(
        ...,
        description="Includes `method`: tagged_db | classifier | none, so the "
        "investigator always knows whether this is a curated fact or a guess.",
    )

    # --- risk ---------------------------------------------------------------
    risk_label: str
    risk_score: float
    risk_explanation: str
    risk_factors: list[str] = Field(default_factory=list)
    contributing_case_ids: list[str] = Field(
        default_factory=list,
        description="Cases that produced this score. The audit trail behind the number.",
    )
    contributions: list[dict] = Field(
        default_factory=list,
        description="Per-case breakdown: age, decay weight, points.",
    )

    reported_in_cases: list[dict] = Field(default_factory=list)
    data_provenance: str
    notice: str
