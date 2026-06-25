"""SQLAlchemy models for Accounting-Ops.

Mirrors the migration in migrations/tenant/versions/0001_tenant_initial.py.
Tables here are defined WITHOUT explicit `schema=` (per the schema-per-workspace
research synthesis) — they get created in whatever schema the search_path is
pointed at, set by app/workspace_session.py per request.

The workspace_id column on every row is denormalized (the schema IS the
workspace boundary). We keep it because the consolidation overlay agent
runs cross-workspace UNION queries against schema-qualified Tables.

Entity-scoped tables get an entity_id column with FK to the entity table.
Workspace-shared tables (vendor, counterparty, fx_rate) have no entity_id.
"""
from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CHAR,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


# ---------------------------------------------------------------------------
# Enums (per workspace schema)
# ---------------------------------------------------------------------------


class DecisionVerdict(str, enum.Enum):
    approve = "Approve"
    reject = "Reject"
    needs_review = "NeedsReview"


class ApprovalStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    canceled = "canceled"
    # 'skipped' added 2026-05-31 (migration 0015) for the "decide later" queue.
    # Skipped rows do NOT post to the ledger; they remain pending in spirit
    # and can be flipped back to 'pending' via the un-skip endpoint.
    skipped = "skipped"


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


class ActivityVerdict(str, enum.Enum):
    approve = "APPROVE"
    reject = "REJECT"
    needs_review = "NEEDS_REVIEW"
    blocked = "BLOCKED"
    thinking = "THINKING"
    canceled = "CANCELED"


class ActivityMateriality(str, enum.Enum):
    low = "L"
    medium = "M"
    high = "H"


class EntityType(str, enum.Enum):
    c_corp = "c_corp"
    s_corp = "s_corp"
    llc = "llc"
    partnership = "partnership"
    sole_proprietorship = "sole_proprietorship"
    personal = "personal"
    trust = "trust"
    non_profit = "non_profit"
    estate = "estate"
    other = "other"


class EntityStatus(str, enum.Enum):
    active = "active"
    dormant = "dormant"
    dissolved = "dissolved"


class ReviewMode(str, enum.Enum):
    """How much professional review a workspace's work products require.

    The product is an autonomous professional-substitute: when no CPA / EA / tax
    attorney is available, the user can still complete a finished work product.

      ``pro_required``         classic hard gate — a licensed pro must review
                               before a work product leaves DRAFT. SAFE DEFAULT,
                               so existing workspaces are unchanged.
      ``self_attest_allowed``  a user with no available pro may self-certify and
                               finish the work product themselves.
      ``autonomous_off``       agent-initiated preparation is disabled entirely.
    """

    PRO_REQUIRED = "pro_required"
    SELF_ATTEST_ALLOWED = "self_attest_allowed"
    AUTONOMOUS_OFF = "autonomous_off"


class WorkProductStatus(str, enum.Enum):
    """Lifecycle of a generated finished work product (filled form / letter).

    ``status`` drives the rendered watermark (see exports.form_prefill):
    draft -> DRAFT/"do not file"; self_approved -> softer self-certification
    banner; pro_reviewed / finalized -> clean.
    """

    DRAFT = "draft"
    SELF_APPROVED = "self_approved"
    PRO_REVIEWED = "pro_reviewed"
    FINALIZED = "finalized"


class AttestationKind(str, enum.Enum):
    SELF = "self"
    LICENSED_PRO = "licensed_pro"


# ---------------------------------------------------------------------------
# Mixin
# ---------------------------------------------------------------------------


class WorkspaceScopedMixin:
    """All tables in the workspace schema carry workspace_id for cross-schema queries."""

    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)


class EntityScopedMixin(WorkspaceScopedMixin):
    """Tables scoped to a specific legal entity within the workspace."""

    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("entity.id", ondelete="RESTRICT"),
        nullable=False,
    )


# ---------------------------------------------------------------------------
# Entity (workspace's legal entities — one row per book)
# ---------------------------------------------------------------------------


class Entity(WorkspaceScopedMixin, Base):
    __table_args__ = (
        CheckConstraint(
            "country_code = upper(country_code)", name="ck_entity_country_upper"
        ),
        CheckConstraint(
            "functional_currency = upper(functional_currency)",
            name="ck_entity_currency_upper",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    slug: Mapped[str] = mapped_column(String(96), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(256), nullable=False)
    legal_name: Mapped[str | None] = mapped_column(String(512))
    kind: Mapped[EntityType] = mapped_column(
        Enum(EntityType, name="entity_type"), nullable=False
    )
    tax_id_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    country_code: Mapped[str] = mapped_column(
        CHAR(2), nullable=False, server_default="US"
    )
    functional_currency: Mapped[str] = mapped_column(
        CHAR(3), nullable=False, server_default="USD"
    )
    fiscal_year_start_month: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="1"
    )
    hipaa_protected: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    status: Mapped[EntityStatus] = mapped_column(
        Enum(EntityStatus, name="entity_status"),
        nullable=False,
        server_default=EntityStatus.active.value,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


# ---------------------------------------------------------------------------
# Event log (entity-scoped, append-only)
# ---------------------------------------------------------------------------


class EventLog(EntityScopedMixin, Base):
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    event_type: Mapped[str] = mapped_column(String(96), nullable=False)
    event_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    predecessor_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("event_log.id")
    )
    actor_uc_uid: Mapped[str | None] = mapped_column(String(128))
    actor_agent_id: Mapped[str | None] = mapped_column(String(256))
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    previous_hash: Mapped[str | None] = mapped_column(String(64))
    merkle_root: Mapped[str | None] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


# ---------------------------------------------------------------------------
# Vendor + Counterparty (workspace-shared, NO entity_id)
# ---------------------------------------------------------------------------


class Vendor(WorkspaceScopedMixin, Base):
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    external_refs: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class Counterparty(WorkspaceScopedMixin, Base):
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    display_name: Mapped[str] = mapped_column(String(256), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vendor.id")
    )
    external_refs: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


# ---------------------------------------------------------------------------
# Entity-scoped accounting tables
# ---------------------------------------------------------------------------


class Receipt(EntityScopedMixin, Base):
    __table_args__ = (
        CheckConstraint(
            "currency_code = upper(currency_code)", name="ck_receipt_currency_upper"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    garage_bucket: Mapped[str] = mapped_column(String(128), nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    currency_code: Mapped[str | None] = mapped_column(CHAR(3))
    usd_equivalent_at_event_time: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    fx_source: Mapped[str | None] = mapped_column(String(64))
    extracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class DocumentExtraction(EntityScopedMixin, Base):
    """Structured result of the tax-document vision pipeline for one uploaded
    document (one row per source ``receipt``).

    AO owns the OCR/extraction; this table is the durable, queryable home for
    what the local vision VLM read off a tax form (1099-R, W-2, SSA-1099, K-1,
    1098, IRS notices, …). Tax-Planning-Ops consumes these rows over the
    existing federation (by entity + tax_year) instead of re-running OCR.

    Written best-effort by a background task after upload (see
    app/api/documents.py). A failed extraction STILL records a row
    (status='failed', error set) so an unreadable doc is visible, never silent.
    NEVER computes or sums — ``fields`` holds verbatim transcribed box values
    (e.g. "box 2a taxable amount": "UNKNOWN").
    """

    __table_args__ = (
        UniqueConstraint("receipt_id", name="uq_document_extraction_receipt"),
        Index("ix_document_extraction_workspace", "workspace_id"),
        Index(
            "ix_document_extraction_entity_doctype",
            "entity_id",
            "doc_type",
            "tax_year",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # The source document this extraction describes. CASCADE is belt-and-
    # suspenders: receipts are append-only (no DELETE grant on the runtime role).
    receipt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("receipt.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Classified form type (one of tax_doc_vlm.TAX_DOC_TYPES); 'other' = not a
    # recognized tax document.
    doc_type: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'other'")
    )
    tax_year: Mapped[int | None] = mapped_column(Integer)
    payer_name: Mapped[str | None] = mapped_column(String(300))
    recipient_name: Mapped[str | None] = mapped_column(String(300))
    # Verbatim transcribed labeled values, e.g.
    # {"box 1 gross distribution": "39612.00", "box 2a taxable amount": "UNKNOWN"}.
    fields: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    # 'extracted' = the VLM returned a result; 'failed' = render/VLM error (see
    # ``error``). Both states are stored so a doc is never silently un-processed.
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'extracted'")
    )
    error: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(128))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    extracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class WorkProduct(EntityScopedMixin, Base):
    """A finished, submission-ready tax work product (filled W-4P / ST-3 / 1040 /
    CP504 response, …) prepared for an entity.

    This is the artifact the platform produces when it takes a user ALL THE WAY:
    not advice, but a real document. The PDF is re-rendered on demand from
    ``inputs`` (each a {value, source_ref, origin} record) so there is never a
    stale snapshot and the watermark always reflects the current ``status``.
    NEVER fabricates a figure: a non-blank line whose input lacks a ``source_ref``
    is rejected at render time, and missing data is left blank (conservative
    blank-fill), never guessed.
    """

    __table_args__ = (
        Index("ix_work_product_workspace", "workspace_id"),
        Index("ix_work_product_entity_kind", "entity_id", "kind", "tax_year"),
        CheckConstraint(
            "status IN ('draft','self_approved','pro_reviewed','finalized')",
            name="ck_work_product_status",
        ),
        CheckConstraint(
            "attestation_kind IS NULL OR attestation_kind IN ('self','licensed_pro')",
            name="ck_work_product_attestation_kind",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # e.g. 'w-4p', 'st-3', 'cp504', '1040' — matches form_prefill.FormName values
    # where a fillable form exists.
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    tax_year: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str | None] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=text("'draft'")
    )
    # Provenance-bearing field inputs:
    #   {field_key: {"value": "...",
    #                "source_ref": "<document_extraction.id|worksheet|user_entered>",
    #                "origin": "extracted|computed|user_entered"}}.
    inputs: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # Extra provenance: source doc ids, worksheet assumptions, engine versions.
    provenance: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # The originating tax-document extraction (e.g. the 1099-R behind a W-4P),
    # the anchor source_ref. SET NULL so deleting a source never silently orphans.
    source_extraction_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("document_extraction.id", ondelete="SET NULL"),
    )
    attestation_kind: Mapped[str | None] = mapped_column(String(16))
    attested_by_uc_uid: Mapped[str | None] = mapped_column(String(128))
    attested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Optional immutable snapshot key (a finalized artifact persisted to object
    # storage). Null = re-render on demand from ``inputs``.
    garage_key: Mapped[str | None] = mapped_column(String(512))
    created_by_uc_uid: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class EngagementPolicy(WorkspaceScopedMixin, Base):
    """Per-workspace "is a professional available?" policy — the switch that makes
    the CPA-review gate OPTIONAL instead of a hard wall.

    One row per workspace, lazily created with safe defaults (``pro_required`` so
    existing workspaces are unchanged). ``review_mode`` governs whether a user may
    self-certify a work product to completion when no pro is available.
    ``prefer_local_inference`` flips chat/reasoning to the sovereign local qwen
    tier. ``submission_channels`` + ``rail_kill_switch`` govern the dormant
    SubmissionRail (every transmission channel default-OFF).
    """

    __table_args__ = (
        UniqueConstraint("workspace_id", name="uq_engagement_policy_workspace"),
        Index("ix_engagement_policy_workspace", "workspace_id"),
        CheckConstraint(
            "review_mode IN ('pro_required','self_attest_allowed','autonomous_off')",
            name="ck_engagement_policy_review_mode",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    review_mode: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'pro_required'")
    )
    has_licensed_pro: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    prefer_local_inference: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # Per-channel enable flags for the dormant submission rail, e.g.
    # {"efile_irs": false, "state_portal": false, "eftps": false, "esign": false}.
    # Empty = every channel disabled (PaperPrint is the always-on default).
    submission_channels: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # Global kill-switch for the submission rail (true = even enabled channels
    # are blocked).
    rail_kill_switch: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class JournalEntry(EntityScopedMixin, Base):
    __table_args__ = (
        CheckConstraint(
            "currency_code = upper(currency_code)",
            name="ck_journal_entry_currency_upper",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("event_log.id"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    currency_code: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    usd_equivalent_at_event_time: Mapped[Decimal] = mapped_column(
        Numeric(20, 4), nullable=False
    )
    fx_source: Mapped[str | None] = mapped_column(String(64))
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    category_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("receipt.id")
    )
    source_document_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    counterparty_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("counterparty.id"), nullable=False
    )
    reasoning_trace: Mapped[str] = mapped_column(Text, nullable=False)
    rule_version_applied: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    model_version: Mapped[str] = mapped_column(String(128), nullable=False)
    decision_verdict: Mapped[DecisionVerdict] = mapped_column(
        Enum(DecisionVerdict, name="decision_verdict"), nullable=False
    )
    identity: Mapped[str] = mapped_column(String(256), nullable=False)
    approving_human_uc_uid: Mapped[str | None] = mapped_column(String(128))
    reverses_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("journal_entry.id")
    )


class ComplianceProfile(EntityScopedMixin, Base):
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    rules: Mapped[list["ComplianceRule"]] = relationship(back_populates="profile")


class ComplianceRule(EntityScopedMixin, Base):
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("compliance_profile.id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(96), nullable=False)
    params: Mapped[dict] = mapped_column(JSONB, nullable=False)
    trigger: Mapped[ComplianceTrigger] = mapped_column(
        Enum(ComplianceTrigger, name="compliance_trigger"), nullable=False
    )
    action: Mapped[ComplianceAction] = mapped_column(
        Enum(ComplianceAction, name="compliance_action"), nullable=False
    )
    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[ComplianceSource] = mapped_column(
        Enum(ComplianceSource, name="compliance_source"), nullable=False
    )
    reasoning: Mapped[str] = mapped_column(Text, nullable=False)
    profile: Mapped[ComplianceProfile] = relationship(back_populates="rules")


class ApprovalQueue(EntityScopedMixin, Base):
    __table_args__ = (
        CheckConstraint(
            "currency_code = upper(currency_code)",
            name="ck_approval_queue_currency_upper",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subject_type: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    currency_code: Mapped[str | None] = mapped_column(CHAR(3))
    usd_equivalent_at_event_time: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    fx_source: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[ApprovalStatus] = mapped_column(
        Enum(ApprovalStatus, name="approval_status"),
        nullable=False,
        server_default=ApprovalStatus.pending.value,
    )
    reasoning: Mapped[str] = mapped_column(Text, nullable=False)
    requested_by_identity: Mapped[str] = mapped_column(String(256), nullable=False)
    decided_by_uc_uid: Mapped[str | None] = mapped_column(String(128))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class ConnectorState(EntityScopedMixin, Base):
    __table_args__ = (
        UniqueConstraint(
            "entity_id",
            "connector_name",
            "connector_instance_name",
            name="uq_connector_state_entity",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    connector_name: Mapped[str] = mapped_column(String(96), nullable=False)
    connector_instance_name: Mapped[str] = mapped_column(String(256), nullable=False)
    state: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    credentials_secret_ref: Mapped[str | None] = mapped_column(String(256))
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class AgentSession(EntityScopedMixin, Base):
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    agent_id: Mapped[str] = mapped_column(String(256), nullable=False)
    user_uc_uid: Mapped[str | None] = mapped_column(String(128))
    scope: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DecisionAudit(EntityScopedMixin, Base):
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    agent_session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_session.id")
    )
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    request_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    response_payload: Mapped[dict | None] = mapped_column(JSONB)
    reasoning_trace: Mapped[str] = mapped_column(Text, nullable=False)
    rule_version_applied: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("receipt.id")
    )
    model_version: Mapped[str | None] = mapped_column(String(128))
    outcome: Mapped[str] = mapped_column(String(64), nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class ActivityEvent(EntityScopedMixin, Base):
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    emitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    agent_id: Mapped[str] = mapped_column(String(128), nullable=False)
    agent_version: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(200), nullable=False)
    materiality: Mapped[ActivityMateriality | None] = mapped_column(
        Enum(ActivityMateriality, name="activity_materiality")
    )
    amount_cents: Mapped[int | None] = mapped_column(BigInteger)
    verdict: Mapped[ActivityVerdict] = mapped_column(
        Enum(ActivityVerdict, name="activity_verdict"), nullable=False
    )
    decision_basis: Mapped[dict | None] = mapped_column(JSONB)
    transparency: Mapped[dict | None] = mapped_column(JSONB)
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    elapsed_ms: Mapped[int | None] = mapped_column(Integer)
    needs_human: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    resolved_by: Mapped[str | None] = mapped_column(String(256))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# Workspace-shared (no entity_id) -----------------------------------------


class FxRate(WorkspaceScopedMixin, Base):
    __table_args__ = (
        CheckConstraint(
            "base_currency = upper(base_currency)",
            name="ck_fx_rate_base_currency_upper",
        ),
        CheckConstraint(
            "quote_currency = upper(quote_currency)",
            name="ck_fx_rate_quote_currency_upper",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    base_currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    quote_currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ReconLink(EntityScopedMixin, Base):
    __table_args__ = (
        CheckConstraint(
            "currency_code = upper(currency_code)",
            name="ck_recon_link_currency_upper",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    journal_entry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("journal_entry.id"), nullable=False
    )
    receipt_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("receipt.id")
    )
    external_transaction_ref: Mapped[str | None] = mapped_column(String(256))
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    currency_code: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    usd_equivalent_at_event_time: Mapped[Decimal] = mapped_column(
        Numeric(20, 4), nullable=False
    )
    fx_source: Mapped[str | None] = mapped_column(String(64))
    matched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


# Convenience indexes (re-declared here for ORM-driven introspection) ---------

for table in (
    Entity,
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
    ActivityEvent,
    FxRate,
    ReconLink,
):
    Index(f"ix_{table.__tablename__}_workspace_id", table.workspace_id)

for table in (
    EventLog,
    Receipt,
    JournalEntry,
    ComplianceProfile,
    ComplianceRule,
    ApprovalQueue,
    ConnectorState,
    AgentSession,
    DecisionAudit,
    ActivityEvent,
    ReconLink,
):
    Index(f"ix_{table.__tablename__}_entity_id", table.entity_id)
