import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import CHAR, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class SovereigntyMode(str, enum.Enum):
    standard = "standard"
    strict = "strict"


class DecisionVerdict(str, enum.Enum):
    approve = "Approve"
    reject = "Reject"
    needs_review = "NeedsReview"


class ApprovalStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    canceled = "canceled"


class ComplianceTrigger(str, enum.Enum):
    schedule = "schedule"
    event = "event"
    threshold = "threshold"


class ComplianceAction(str, enum.Enum):
    notify = "notify"
    block = "block"
    require_approval = "require_approval"
    tag = "tag"


class ComplianceSource(str, enum.Enum):
    user_setup = "user_setup"
    ai_inferred = "ai_inferred"
    external_context = "external_context"


class Tenant(Base):
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(96), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(256), nullable=False)
    postgres_schema: Mapped[str] = mapped_column(String(128), nullable=False)
    falkordb_subgraph: Mapped[str] = mapped_column(String(128), nullable=False)
    garage_bucket: Mapped[str] = mapped_column(String(128), nullable=False)
    hipaa_protected: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    sovereignty_mode: Mapped[SovereigntyMode] = mapped_column(
        Enum(SovereigntyMode, name="sovereignty_mode"),
        nullable=False,
        server_default=SovereigntyMode.standard.value,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


class TenantScopedMixin:
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )


class EventLog(TenantScopedMixin, Base):
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_type: Mapped[str] = mapped_column(String(96), nullable=False)
    event_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    predecessor_event_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("event_log.id"))
    actor_uc_uid: Mapped[str | None] = mapped_column(String(128))
    actor_agent_id: Mapped[str | None] = mapped_column(String(256))
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    previous_hash: Mapped[str | None] = mapped_column(String(64))
    merkle_root: Mapped[str | None] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


class Vendor(TenantScopedMixin, Base):
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    external_refs: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


class Counterparty(TenantScopedMixin, Base):
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    display_name: Mapped[str] = mapped_column(String(256), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("vendor.id"))
    external_refs: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


class Receipt(TenantScopedMixin, Base):
    __table_args__ = (CheckConstraint("currency_code = upper(currency_code)", name="ck_receipt_currency_upper"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    garage_bucket: Mapped[str] = mapped_column(String(128), nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    currency_code: Mapped[str | None] = mapped_column(CHAR(3))
    usd_equivalent_at_event_time: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    fx_source: Mapped[str | None] = mapped_column(String(64))
    extracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


class JournalEntry(TenantScopedMixin, Base):
    __table_args__ = (CheckConstraint("currency_code = upper(currency_code)", name="ck_journal_entry_currency_upper"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("event_log.id"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    currency_code: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    usd_equivalent_at_event_time: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    fx_source: Mapped[str | None] = mapped_column(String(64))
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    category_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("receipt.id"))
    source_document_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    counterparty_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("counterparty.id"), nullable=False)
    reasoning_trace: Mapped[str] = mapped_column(Text, nullable=False)
    rule_version_applied: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    model_version: Mapped[str] = mapped_column(String(128), nullable=False)
    decision_verdict: Mapped[DecisionVerdict] = mapped_column(
        Enum(DecisionVerdict, name="decision_verdict"), nullable=False
    )
    identity: Mapped[str] = mapped_column(String(256), nullable=False)
    approving_human_uc_uid: Mapped[str | None] = mapped_column(String(128))
    reverses_entry_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("journal_entry.id"))


class ComplianceProfile(TenantScopedMixin, Base):
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    rules: Mapped[list["ComplianceRule"]] = relationship(back_populates="profile")


class ComplianceRule(TenantScopedMixin, Base):
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("compliance_profile.id"))
    kind: Mapped[str] = mapped_column(String(96), nullable=False)
    params: Mapped[dict] = mapped_column(JSONB, nullable=False)
    trigger: Mapped[ComplianceTrigger] = mapped_column(Enum(ComplianceTrigger, name="compliance_trigger"), nullable=False)
    action: Mapped[ComplianceAction] = mapped_column(Enum(ComplianceAction, name="compliance_action"), nullable=False)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[ComplianceSource] = mapped_column(Enum(ComplianceSource, name="compliance_source"), nullable=False)
    reasoning: Mapped[str] = mapped_column(Text, nullable=False)
    profile: Mapped[ComplianceProfile] = relationship(back_populates="rules")


class ApprovalQueue(TenantScopedMixin, Base):
    __table_args__ = (CheckConstraint("currency_code = upper(currency_code)", name="ck_approval_queue_currency_upper"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    subject_type: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    currency_code: Mapped[str | None] = mapped_column(CHAR(3))
    usd_equivalent_at_event_time: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    fx_source: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[ApprovalStatus] = mapped_column(
        Enum(ApprovalStatus, name="approval_status"), nullable=False, server_default=ApprovalStatus.pending.value
    )
    reasoning: Mapped[str] = mapped_column(Text, nullable=False)
    requested_by_identity: Mapped[str] = mapped_column(String(256), nullable=False)
    decided_by_uc_uid: Mapped[str | None] = mapped_column(String(128))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


class ConnectorState(TenantScopedMixin, Base):
    __table_args__ = (UniqueConstraint("tenant_id", "connector_name", name="uq_connector_state_tenant_connector"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connector_name: Mapped[str] = mapped_column(String(96), nullable=False)
    state: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentSession(TenantScopedMixin, Base):
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[str] = mapped_column(String(256), nullable=False)
    user_uc_uid: Mapped[str | None] = mapped_column(String(128))
    scope: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DecisionAudit(TenantScopedMixin, Base):
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("agent_session.id"))
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    request_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    response_payload: Mapped[dict | None] = mapped_column(JSONB)
    reasoning_trace: Mapped[str] = mapped_column(Text, nullable=False)
    rule_version_applied: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("receipt.id"))
    model_version: Mapped[str | None] = mapped_column(String(128))
    outcome: Mapped[str] = mapped_column(String(64), nullable=False)
    latency_ms: Mapped[int | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


class FxRate(TenantScopedMixin, Base):
    __table_args__ = (
        CheckConstraint("base_currency = upper(base_currency)", name="ck_fx_rate_base_currency_upper"),
        CheckConstraint("quote_currency = upper(quote_currency)", name="ck_fx_rate_quote_currency_upper"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    base_currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    quote_currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ReconLink(TenantScopedMixin, Base):
    __table_args__ = (CheckConstraint("currency_code = upper(currency_code)", name="ck_recon_link_currency_upper"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    journal_entry_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("journal_entry.id"))
    receipt_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("receipt.id"))
    external_transaction_ref: Mapped[str | None] = mapped_column(String(256))
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    currency_code: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    usd_equivalent_at_event_time: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    fx_source: Mapped[str | None] = mapped_column(String(64))
    matched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


for table in (
    EventLog,
    Vendor,
    Counterparty,
    Receipt,
    JournalEntry,
    ComplianceProfile,
    ComplianceRule,
    ApprovalQueue,
    ConnectorState,
    AgentSession,
    DecisionAudit,
    FxRate,
    ReconLink,
):
    Index(f"ix_{table.__tablename__}_tenant_id", table.tenant_id)

Index("ix_journal_entry_tenant_posted_at", JournalEntry.tenant_id, JournalEntry.posted_at.desc())
Index("ix_journal_entry_tenant_counterparty", JournalEntry.tenant_id, JournalEntry.counterparty_id)
Index("ix_counterparty_tenant_counterparty", Counterparty.tenant_id, Counterparty.id)
Index("ix_receipt_content_hash", Receipt.content_hash, unique=True)
