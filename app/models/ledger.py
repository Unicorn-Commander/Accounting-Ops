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
# Entity ownership (shareholders / partners) — drives Schedule K-1
# Mirrors migrations/tenant/versions/0027_entity_owner.py
# ---------------------------------------------------------------------------


class OwnerType(str, enum.Enum):
    individual = "individual"
    entity = "entity"
    trust = "trust"
    estate = "estate"


class AllocationBasis(str, enum.Enum):
    """How an entity's items are allocated to its owners.

    ``per_share_per_day`` is the S-corp statutory method (§1377(a)(1)) and is
    computed automatically. The two ``partnership_agreement_*`` values are
    ATTESTATIONS about what the partnership agreement says — a partnership may
    allocate disproportionately under §704(a), so a NULL basis is treated as a
    refusal to allocate, never as pro rata.
    """

    PER_SHARE_PER_DAY = "per_share_per_day"
    PARTNERSHIP_AGREEMENT_PRO_RATA = "partnership_agreement_pro_rata"
    PARTNERSHIP_AGREEMENT_SPECIAL = "partnership_agreement_special"


class EntityOwner(EntityScopedMixin, Base):
    """A shareholder (1120-S) or partner/member (1065), for a period.

    Multiple rows per owner are expected: ownership is time-sliced by
    ``effective_from``/``effective_to`` so a mid-year transfer is representable,
    which is exactly what the per-share-per-day allocation consumes.

    ``shares`` is the S-corp input and the percentages are the partnership
    inputs — they are not redundant, because a percentage cannot express a
    mid-year transfer and a partnership's profit/loss/capital percentages
    routinely differ from one another.

    Stored as a plain ``String`` rather than a PG enum to match 0027 (and to
    avoid a schema-scoped enum type per workspace); the CHECK constraint in the
    migration is the enforcement point.
    """

    __table_args__ = (
        CheckConstraint(
            "owner_type IN ('individual','entity','trust','estate')",
            name="ck_entity_owner_type",
        ),
        CheckConstraint(
            "allocation_basis IS NULL OR allocation_basis IN ("
            "'per_share_per_day',"
            "'partnership_agreement_pro_rata',"
            "'partnership_agreement_special')",
            name="ck_entity_owner_allocation_basis",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_entity_owner_period",
        ),
        CheckConstraint(
            "shares IS NULL OR shares >= 0", name="ck_entity_owner_shares_nonneg"
        ),
        CheckConstraint(
            "(profit_pct IS NULL OR (profit_pct >= 0 AND profit_pct <= 100)) "
            "AND (loss_pct IS NULL OR (loss_pct >= 0 AND loss_pct <= 100)) "
            "AND (capital_pct IS NULL OR "
            "     (capital_pct >= 0 AND capital_pct <= 100))",
            name="ck_entity_owner_pct_range",
        ),
        Index("ix_entity_owner_entity_period", "entity_id", "effective_from"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_name: Mapped[str] = mapped_column(String(300), nullable=False)
    owner_type: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=OwnerType.individual.value
    )
    #: Last 4 of SSN/EIN only — the full identifier belongs in the encrypted
    #: document path, not in a table a reporting sidecar reads.
    tax_id_last4: Mapped[str | None] = mapped_column(String(4))

    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)

    #: S corporation — share count held during this period (§1377(a)(1)).
    shares: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))

    #: Partnership / LLC — percentages 0..100. May legitimately differ.
    profit_pct: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    loss_pct: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    capital_pct: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))

    is_general_partner: Mapped[bool | None] = mapped_column(Boolean)
    is_officer: Mapped[bool | None] = mapped_column(Boolean)

    allocation_basis: Mapped[str | None] = mapped_column(String(48))
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
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


class FixedAsset(EntityScopedMixin, Base):
    """One depreciable fixed asset on an entity's books.

    WHY THIS IS A TABLE AND NOT A CALLER-SUPPLIED LIST
    -------------------------------------------------
    Depreciation is inherently multi-year and stateful: an asset placed in
    service in 2023 must still produce the right 2026 deduction, using the
    convention and the §168(k) percentage that were fixed AT PLACEMENT — not the
    2026 ones. If the asset list were supplied per-call, the 2026 return would
    silently depend on the caller correctly re-typing 2023 facts, which is
    exactly the fabrication risk the platform exists to eliminate (a mistyped
    prior-year §179 or bonus figure produces a wrong basis with nothing to catch
    it). Persisting the asset makes the basis history auditable, lets the
    §179 carryforward chain year to year, and matches the product's thesis of
    deriving the return FROM THE BOOKS.

    The row is the ``source_ref`` anchor for every Form 4562 figure derived from
    it, so a reviewer can trace a line on the form back to an asset record.
    """

    __table_args__ = (
        Index("ix_fixed_asset_workspace", "workspace_id"),
        Index("ix_fixed_asset_entity_placed", "entity_id", "placed_in_service"),
        CheckConstraint("cost_cents >= 0", name="ck_fixed_asset_cost_nonneg"),
        CheckConstraint(
            "business_use_pct >= 0 AND business_use_pct <= 100",
            name="ck_fixed_asset_business_use_pct",
        ),
        CheckConstraint(
            "recovery_period_years > 0", name="ck_fixed_asset_recovery_positive"
        ),
        CheckConstraint(
            "depreciation_system IN ('GDS','ADS')",
            name="ck_fixed_asset_system",
        ),
        CheckConstraint(
            "method IS NULL OR method IN ('200DB','150DB','S/L')",
            name="ck_fixed_asset_method",
        ),
        CheckConstraint(
            "convention IS NULL OR convention IN ('HY','MQ','MM')",
            name="ck_fixed_asset_convention",
        ),
        CheckConstraint(
            "listed_property_type IS NULL OR listed_property_type IN "
            "('passenger_auto','heavy_vehicle','other_listed')",
            name="ck_fixed_asset_listed_type",
        ),
        # A listed asset MUST declare its type — the §280F(a) caps depend on it.
        CheckConstraint(
            "NOT is_listed_property OR listed_property_type IS NOT NULL",
            name="ck_fixed_asset_listed_type_required",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    # Optional client-facing asset tag / serial.
    asset_tag: Mapped[str | None] = mapped_column(String(64))
    placed_in_service: Mapped[date] = mapped_column(Date, nullable=False)
    # When the property was ACQUIRED (binding contract / purchase date). This is
    # NOT redundant with placed_in_service: from TY2025 the §168(k) percentage
    # turns on whether acquisition was after the OBBBA cutoff (2025-01-19), so a
    # 2025 asset acquired in 2024 gets 40% while one acquired in March 2025 gets
    # 100%. The engine REFUSES to guess when this is null.
    acquired_on: Mapped[date | None] = mapped_column(Date)
    cost_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # GDS/ADS recovery period in years (3, 5, 7, 10, 15, 20, 25, 27.5, 39, …).
    recovery_period_years: Mapped[Decimal] = mapped_column(
        Numeric(4, 1), nullable=False
    )
    depreciation_system: Mapped[str] = mapped_column(
        String(3), nullable=False, server_default=text("'GDS'")
    )
    # NULL = derive from §168(b)/(d) (the normal case). Set only to record a
    # taxpayer election (e.g. §168(b)(5) straight-line election) or the
    # convention that actually applied in a prior placement year.
    method: Mapped[str | None] = mapped_column(String(8))
    convention: Mapped[str | None] = mapped_column(String(2))
    # §280F qualified business/investment use percentage.
    business_use_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, server_default=text("100")
    )
    is_listed_property: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # REQUIRED whenever is_listed_property: 'passenger_auto' (§280F(d)(5), GVW
    # <= 6,000 lbs — subject to the §280F(a) dollar caps), 'heavy_vehicle'
    # (>6,000 lbs, listed but uncapped), or 'other_listed'. The engine REFUSES to
    # infer this, because defaulting it the wrong way over-deducts a vehicle.
    listed_property_type: Mapped[str | None] = mapped_column(String(16))
    # §179 elected in the placement year (0 = none elected).
    section_179_elected_cents: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    # §168(k)(7) irrevocable election out of bonus.
    bonus_elected_out: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # For assets placed in service BEFORE the engine's supported config window:
    # the §179 / §168(k) amounts actually taken that year. The engine refuses to
    # guess these, so they must be recorded when migrating historical books.
    prior_section_179_cents: Mapped[int | None] = mapped_column(BigInteger)
    prior_bonus_cents: Mapped[int | None] = mapped_column(BigInteger)
    # Disposal (stops the schedule; gain/loss handled on Form 4797, not here).
    disposed_on: Mapped[date | None] = mapped_column(Date)
    disposal_proceeds_cents: Mapped[int | None] = mapped_column(BigInteger)
    # Where this asset came from (an invoice, a journal entry, an intake) — the
    # provenance anchor carried onto every Form 4562 figure derived from it.
    source_ref: Mapped[str | None] = mapped_column(String(300))
    notes: Mapped[str | None] = mapped_column(Text)
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


class SignatureRequest(EntityScopedMixin, Base):
    """A request for a client's e-signature on a finished work product.

    REAL, not dormant: this is the taxpayer's OWN signature on the taxpayer's
    OWN document (a filled W-4P / 1040 / engagement letter, …) — there is no
    unauthorized-practice-of-law or IRS-transmission concern, so unlike the
    dormant e-FILE / EFTPS rail this is genuinely usable. Creation is still
    gated behind a per-workspace feature flag (``submission_channels.esign``).

    The work-product PDF is re-rendered on demand (anti-fabrication provenance
    enforced at render time) and handed to a provider — a self-hostable Docuseal
    instance when configured, else the always-available Manual provider
    (download → wet-sign → re-upload). The completed/signed PDF is stored to
    Garage (``signed_garage_key``) or the local filesystem (``signed_path``).
    Entity-scoped + workspace-isolated by RLS like the other tenant tables.
    """

    __table_args__ = (
        Index("ix_signature_request_work_product", "work_product_id"),
        CheckConstraint(
            "status IN ('created','sent','out_for_signature','signed',"
            "'declined','canceled')",
            name="ck_signature_request_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # The finished work product being signed. CASCADE: a signature request has no
    # meaning once its work product is gone (work products are append-only at the
    # row level, so this is belt-and-suspenders).
    work_product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("work_product.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 'docuseal' | 'manual' — which backend owns external_id / signing_url.
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    # The provider's id for this request (e.g. a Docuseal submission id). Null for
    # the Manual provider (no external system).
    external_id: Mapped[str | None] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=text("'created'")
    )
    signer_name: Mapped[str | None] = mapped_column(String(300))
    signer_email: Mapped[str | None] = mapped_column(String(320))
    # The signer-facing URL for a remote provider (Null for Manual / wet-sign).
    signing_url: Mapped[str | None] = mapped_column(Text)
    # Where the completed/signed PDF lives. Exactly one is set once signed:
    # signed_garage_key (object store) OR signed_path (local filesystem).
    signed_garage_key: Mapped[str | None] = mapped_column(String(512))
    signed_path: Mapped[str | None] = mapped_column(Text)
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
    SignatureRequest,
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
    SignatureRequest,
):
    Index(f"ix_{table.__tablename__}_entity_id", table.entity_id)
