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
        description="Cases that produced this score. The audit trail behind the number. "
        "Truncated to `listing_limit`; `contributing_case_count` is the true total.",
    )
    contributions: list[dict] = Field(
        default_factory=list,
        description="Per-case breakdown: age, decay weight, points. Highest-scoring "
        "first, truncated to `listing_limit`.",
    )
    contributing_case_count: int = Field(
        0,
        description="How many cases contributed to the score in total. The score is "
        "computed over all of them, not just the ones listed.",
    )

    reported_in_cases: list[dict] = Field(default_factory=list)
    reported_in_cases_total: int = Field(
        0, description="Total cases naming this wallet; `reported_in_cases` is capped."
    )
    listing_limit: int = Field(
        0, description="The cap applied to the case lists in this response."
    )
    data_provenance: str
    notice: str
