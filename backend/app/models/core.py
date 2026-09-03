"""SQLAlchemy ORM models.

These map onto the tables created by infra/postgres/init.sql, which remains the
authoritative DDL - the models do not create or migrate schema. Postgres ENUM
types are referenced with create_type=False for the same reason.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _pg_enum(*values: str, name: str) -> Enum:
    """Reference an existing Postgres ENUM without trying to create it."""
    return Enum(*values, name=name, create_type=False, native_enum=True)


CHAIN_ENUM = _pg_enum("BTC", "ETH", "TRON", name="chain_t")
USER_ROLE_ENUM = _pg_enum("investigator", "supervisor", "admin", name="user_role_t")
CASE_STATUS_ENUM = _pg_enum(
    "open", "tracing", "analysed", "escalated", "closed", name="case_status_t"
)
WALLET_ROLE_ENUM = _pg_enum(
    "reported_suspect", "intermediate", "terminal", name="wallet_role_t"
)
ENTITY_TYPE_ENUM = _pg_enum(
    "exchange",
    "mixer",
    "gambling",
    "darknet",
    "sanctioned",
    "payment_processor",
    "unknown",
    name="entity_type_t",
)
RISK_LABEL_ENUM = _pg_enum("low", "medium", "high", name="risk_label_t")
TRACE_STATUS_ENUM = _pg_enum("queued", "running", "complete", "failed", name="trace_status_t")
ATTRIBUTION_ENUM = _pg_enum(
    "tagged_db", "classifier", "manual", "none", name="attribution_m_t"
)
APPROVAL_ENUM = _pg_enum(
    "draft", "pending_approval", "approved", "rejected", "dispatched", name="approval_t"
)


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


# ---------------------------------------------------------------------------
# Identity & audit
# ---------------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    username: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str | None] = mapped_column(Text, unique=True)
    hashed_password: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(USER_ROLE_ENUM, nullable=False, default="investigator")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    ip_address: Mapped[str | None] = mapped_column(INET)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------------------
# Wallets & cases
# ---------------------------------------------------------------------------
class Wallet(Base):
    __tablename__ = "wallets"
    __table_args__ = (UniqueConstraint("chain", "address_norm", name="uq_wallet_chain_addr"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    address: Mapped[str] = mapped_column(Text, nullable=False)
    address_norm: Mapped[str] = mapped_column(Text, nullable=False)
    chain: Mapped[str] = mapped_column(CHAIN_ENUM, nullable=False)
    address_kind: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_traced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    report_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    case_links: Mapped[list[CaseWallet]] = relationship(back_populates="wallet")


class Case(Base):
    __tablename__ = "cases"

    id: Mapped[uuid.UUID] = _uuid_pk()
    case_number: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    ncrp_ref: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, nullable=False, default="manual")
    status: Mapped[str] = mapped_column(CASE_STATUS_ENUM, nullable=False, default="open")
    victim_ref: Mapped[str | None] = mapped_column(Text)
    amount_inr: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    reported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    narrative: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    wallet_links: Mapped[list[CaseWallet]] = relationship(
        back_populates="case", cascade="all, delete-orphan"
    )


class CaseWallet(Base):
    __tablename__ = "case_wallets"

    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cases.id", ondelete="CASCADE"), primary_key=True
    )
    wallet_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("wallets.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(
        WALLET_ROLE_ENUM, primary_key=True, default="reported_suspect"
    )
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    case: Mapped[Case] = relationship(back_populates="wallet_links")
    wallet: Mapped[Wallet] = relationship(back_populates="case_links")


# ---------------------------------------------------------------------------
# VASP / entity intelligence
# ---------------------------------------------------------------------------
class Entity(Base):
    __tablename__ = "entities"
    __table_args__ = (UniqueConstraint("name", "entity_type", name="uq_entity_name_type"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str] = mapped_column(ENTITY_TYPE_ENUM, nullable=False, default="unknown")
    jurisdiction: Mapped[str | None] = mapped_column(Text)
    website: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TaggedAddress(Base):
    __tablename__ = "tagged_addresses"
    __table_args__ = (
        UniqueConstraint("chain", "address_norm", "source", name="uq_tag_chain_addr"),
        CheckConstraint("confidence BETWEEN 0 AND 1", name="ck_tag_confidence"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    address_norm: Mapped[str] = mapped_column(Text, nullable=False)
    chain: Mapped[str] = mapped_column(CHAIN_ENUM, nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    entity: Mapped[Entity] = relationship()


class Cluster(Base):
    __tablename__ = "clusters"

    id: Mapped[uuid.UUID] = _uuid_pk()
    cluster_key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    chain: Mapped[str] = mapped_column(CHAIN_ENUM, nullable=False)
    heuristic: Mapped[str] = mapped_column(Text, nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entities.id")
    )
    attribution_method: Mapped[str] = mapped_column(
        ATTRIBUTION_ENUM, nullable=False, default="none"
    )
    attribution_confidence: Mapped[float | None] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    entity: Mapped[Entity | None] = relationship()


# ---------------------------------------------------------------------------
# Tracing
# ---------------------------------------------------------------------------
class TraceRun(Base):
    __tablename__ = "trace_runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    case_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cases.id", ondelete="CASCADE")
    )
    root_wallet_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("wallets.id"), nullable=False
    )
    max_depth: Mapped[int] = mapped_column(Integer, nullable=False, default=6)
    status: Mapped[str] = mapped_column(TRACE_STATUS_ENUM, nullable=False, default="queued")
    hops_discovered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    addresses_touched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mixer_interaction: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    terminal_cluster_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clusters.id")
    )
    data_source: Mapped[str] = mapped_column(Text, nullable=False, default="synthetic")
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------------------
# Fraud-linkage risk scoring
# ---------------------------------------------------------------------------
class RiskScore(Base):
    __tablename__ = "risk_scores"

    id: Mapped[uuid.UUID] = _uuid_pk()
    entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entities.id", ondelete="CASCADE")
    )
    cluster_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clusters.id", ondelete="CASCADE")
    )
    score: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    label: Mapped[str] = mapped_column(RISK_LABEL_ENUM, nullable=False)
    half_life_days: Mapped[int] = mapped_column(Integer, nullable=False)
    model_version: Mapped[str] = mapped_column(Text, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    contributions: Mapped[list[RiskContribution]] = relationship(
        back_populates="risk_score", cascade="all, delete-orphan"
    )


class RiskContribution(Base):
    """One case's share of a risk score - the explainability record."""

    __tablename__ = "risk_contributions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    risk_score_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("risk_scores.id", ondelete="CASCADE"), nullable=False
    )
    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cases.id", ondelete="CASCADE"), nullable=False
    )
    terminal_address: Mapped[str | None] = mapped_column(Text)
    age_days: Mapped[int] = mapped_column(Integer, nullable=False)
    decay_weight: Mapped[float] = mapped_column(Float, nullable=False)
    points: Mapped[Decimal] = mapped_column(Numeric(6, 3), nullable=False)

    risk_score: Mapped[RiskScore] = relationship(back_populates="contributions")


# ---------------------------------------------------------------------------
# Case work product (Phase 4)
# ---------------------------------------------------------------------------
class CaseNote(Base):
    __tablename__ = "case_notes"

    id: Mapped[uuid.UUID] = _uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cases.id", ondelete="CASCADE"), nullable=False
    )
    author_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Evidence(Base):
    """An uploaded exhibit. `sha256` is the chain-of-custody digest."""

    __tablename__ = "evidence"

    id: Mapped[uuid.UUID] = _uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cases.id", ondelete="CASCADE"), nullable=False
    )
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id")
    )
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[uuid.UUID] = _uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cases.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    generated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id")
    )
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class FreezeRequest(Base):
    """Human-in-the-loop by construction.

    Nothing in this codebase sets `status` to 'approved' outside the explicit
    approval endpoint, which requires a supervisor distinct from the requester.
    The DB additionally refuses an approved row without an approver (see
    ck_freeze_approved_needs_actor in infra/postgres/init.sql).
    """

    __tablename__ = "freeze_requests"

    id: Mapped[uuid.UUID] = _uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cases.id", ondelete="CASCADE"), nullable=False
    )
    entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entities.id")
    )
    target_address: Mapped[str] = mapped_column(Text, nullable=False)
    amount_inr: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    justification: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(APPROVAL_ENUM, nullable=False, default="draft")
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id")
    )
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id")
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StrDraft(Base):
    """FIU-IND-style Suspicious Transaction Report draft. Draft only."""

    __tablename__ = "str_drafts"

    id: Mapped[uuid.UUID] = _uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cases.id", ondelete="CASCADE"), nullable=False
    )
    entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entities.id")
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(APPROVAL_ENUM, nullable=False, default="draft")
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id")
    )
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id")
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[uuid.UUID] = _uuid_pk()
    case_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cases.id", ondelete="CASCADE")
    )
    entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entities.id")
    )
    severity: Mapped[str] = mapped_column(RISK_LABEL_ENUM, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    stream_msg_id: Mapped[str | None] = mapped_column(Text)
    acknowledged_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id")
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------------------
# Service exposure results
# ---------------------------------------------------------------------------
class ServiceExposureRecord(Base):
    """A stored exposure finding.

    Persisted because it is something an officer may act on: a freeze request
    citing "78% of traced funds reached Meridian Exchange" has to be
    reconstructable later, including the weights and price basis behind it.
    """

    __tablename__ = "service_exposures"

    id: Mapped[uuid.UUID] = _uuid_pk()
    chain: Mapped[str] = mapped_column(CHAIN_ENUM, nullable=False)
    address_norm: Mapped[str] = mapped_column(Text, nullable=False)
    case_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cases.id", ondelete="SET NULL")
    )

    kind: Mapped[str] = mapped_column(Text, nullable=False)
    searched_to_hop: Mapped[int] = mapped_column(Integer, nullable=False)

    top_service: Mapped[str | None] = mapped_column(Text)
    top_service_type: Mapped[str | None] = mapped_column(Text)
    top_hop: Mapped[int | None] = mapped_column(Integer)
    top_score: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    top_volume_inr: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))

    scoring_version: Mapped[str] = mapped_column(Text, nullable=False)
    weights: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    explanation: Mapped[str | None] = mapped_column(Text)

    computed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id")
    )
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    paths: Mapped[list[ExposurePathRecord]] = relationship(
        back_populates="exposure", cascade="all, delete-orphan"
    )


class ExposurePathRecord(Base):
    """One ranked candidate, with the arithmetic that produced its rank.

    The explanation is stored rather than recomputed - recomputing it later
    against different weights would silently rewrite history.
    """

    __tablename__ = "exposure_paths"

    id: Mapped[uuid.UUID] = _uuid_pk()
    exposure_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("service_exposures.id", ondelete="CASCADE"),
        nullable=False,
    )

    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    service: Mapped[str] = mapped_column(Text, nullable=False)
    service_type: Mapped[str | None] = mapped_column(Text)
    service_address: Mapped[str | None] = mapped_column(Text)
    hop: Mapped[int] = mapped_column(Integer, nullable=False)
    score: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))

    total_volume: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    total_volume_inr: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    volume_basis: Mapped[str | None] = mapped_column(Text)
    transfer_count: Mapped[int | None] = mapped_column(Integer)
    unique_counterparties: Mapped[int | None] = mapped_column(Integer)
    continuity_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    label_confidence: Mapped[float | None] = mapped_column(Float)
    label_sources: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    price_sources: Mapped[list[str] | None] = mapped_column(ARRAY(Text))

    path: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    features: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    explanation: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    evidence_txid: Mapped[str | None] = mapped_column(Text)
    evidence_amount: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    evidence_asset: Mapped[str | None] = mapped_column(Text)
    evidence_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    exposure: Mapped[ServiceExposureRecord] = relationship(back_populates="paths")


__all__ = [
    "Base",
    "User",
    "AuditLog",
    "Wallet",
    "Case",
    "CaseWallet",
    "Entity",
    "TaggedAddress",
    "Cluster",
    "TraceRun",
    "RiskScore",
    "RiskContribution",
    "CaseNote",
    "Evidence",
    "Report",
    "FreezeRequest",
    "StrDraft",
    "Alert",
]
