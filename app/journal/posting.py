"""Atomic posting of a transaction to the journal.

  resolve_accounts_for_txn(txn, mapping, category) → (debit_account, credit_account)
  post_journal_entry(...) → JournalEntry|PostingRefusal

Idempotency: (entity_id, source, source_external_id) UNIQUE in the DB. If
the same SimpleFIN/eBay txn arrives twice, the second post returns the
first's id with outcome='blocked_idempotent' rather than raising.

Hash chain: each post fetches the entity's last this_hash, computes the
new this_hash, inserts with both. The unique constraint on
(entity_id, this_hash) prevents concurrent posts from forking the chain —
the second one fails and retries with the fresh prev_hash.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.journal.hash_chain import GENESIS_HASH, compute_hash, get_last_chain_hash
from app.journal.materiality import MaterialityVerdict, evaluate, materiality_tier


# ---------- Account resolution -----------------------------------------

@dataclass(frozen=True)
class AccountPair:
    debit_account: str   # canonical_form
    credit_account: str  # canonical_form


# Maps human-readable category → COA account number whose balance changes
# when this category is posted. The OTHER side of the entry is always the
# cash/credit-card account the txn came from.
CATEGORY_TO_ACCOUNT: dict[str, str] = {
    "eBay Resale · Intercompany Due to MU": "2600",  # intercompany payable (MU Inc fronted contractor+shipping)
    # --- Listing-Ops division (Magic Unicorn Inc — Listing-Ops eBay resale).
    #     Additive keys → this entity's purpose-built eBay-resale COA
    #     (4150/5020/5030/5630). Distinct names → no collision with the
    #     generic revenue/contractor keys below. Consumed by the Listing-Ops
    #     AO-sync emitter (source="listing-ops").
    "eBay Resale · Revenue": "4150",
    "eBay Resale · Marketplace Fees": "5020",
    "eBay Resale · Shipping": "5030",
    "eBay Resale · Contractor 1099-NEC": "5630",
    # Revenue
    "Revenue · Marketplace": "4010",
    "Revenue · Music": "4020",
    # Expenses
    "SaaS · AI": "6210",
    "SaaS · Email": "6230",
    "SaaS · Productivity": "6200",
    "Infrastructure · VPS": "6110",
    "Infrastructure · CDN": "6100",
    "Infrastructure · Network": "6120",
    "Banking · Fees": "6700",
    "Banking · Interest": "7010",  # routes to interest income; sign disambiguates
    "Inter-entity transfer": "6900",
    # Equity — owner moving their own money in/out of the business. Sign-based
    # posting handles direction: a +inflow CREDITs the equity account (owner puts
    # money in), a -outflow DEBITs it (owner takes money out / personal spend).
    # These map to the DEFAULT_COA equity accounts every entity is seeded with
    # (3010/3020), so no per-entity backfill is needed. Covers the personal
    # Zelle/Venmo/Cash-App traffic that has no business income/expense bucket.
    "Owner's Contribution": "3010",  # money IN from the owner (e.g. self-funding)
    "Owner's Draw": "3020",          # money OUT to the owner / personal spend
    "Travel · Lodging": "6500",
    "Travel · Transport": "6510",
    "Meals": "6520",
    "Office · Supplies": "6400",
    # --- Catch-up reconciliation (example entity): equity, COGS, contractor,
    #     services revenue, related-party loan. All target accounts already exist
    #     in the COA; these keys just wire them into the posting map (the missing
    #     keys are why these categories blocked with category_not_in_CATEGORY_TO_ACCOUNT).
    "Revenue · Services": "4030",
    "Owner Contribution": "3010",
    "Owner Distribution": "3020",
    "Contractor · 1099-NEC": "6300",
    "COGS · Inventory": "5010",
    "Loan from Nate Ellison": "2400",  # related-party note payable (generic)
    "Vehicle · Repairs": "6620",  # business-use vehicle (see COA note re: accountable plan + substantiation)
    # Depreciation expense (COA 8030). Routes to Form 1120 line 20 via
    # CATEGORY_TO_1120 — WITHOUT this key an 8030 posting fell through the
    # unmapped-account catch-all onto line 26 "Other deductions". The figure that
    # belongs on line 20 is the TAX deduction from Form 4562, not book
    # depreciation; see the Depreciation note in app/journal/tax_mapping_1120.py.
    "Depreciation": "8030",
    # Common operating expenses. These COA accounts ship in the default
    # template (app/journal/accounts.py); without a category key an agent
    # cannot post rent/utilities/insurance/payroll at all. Keys use the COA
    # canonical "Type · Subtype" form; each also has a CATEGORY_TO_1120 line.
    "Rent · Facilities": "6600",
    "Utilities": "6610",
    "Insurance": "6800",  # account existed in the template; key was missing
    "Payroll · Wages": "6350",
}


async def resolve_accounts_for_txn(
    session: AsyncSession,
    *,
    entity_id: str,
    category: str,
    amount_cents: int,
    external_account_id: str,
    source: str,
) -> AccountPair | None:
    """Given a categorized txn, return the (debit, credit) account pair.

    Returns None if either side can't be resolved (caller blocks the post).
    """
    # 1. Find the cash/credit-card side: the COA account linked to the
    #    external bank account this txn came from. Match by external_account_id
    #    ALONE (not linked_external_source) — external_account_id is uniquely
    #    identifying, and the source label is for data lineage (stored on
    #    journal_entry.source) NOT for COA resolution. This lets the SAME COA
    #    account legitimately serve multiple ingest sources (e.g. the live
    #    'simplefin' feed + a 'bofa-statement' historical-statement backfill,
    #    or a future 'plaid' source). The `source` param is preserved in this
    #    function's signature for callers and audit logging.
    cash_row = (
        await session.execute(
            text(
                """
                SELECT canonical_form, account_type
                FROM chart_of_account
                WHERE entity_id = :eid
                  AND linked_external_account_id = :ext_id
                  AND is_active = true
                LIMIT 1
                """
            ),
            {"eid": entity_id, "ext_id": external_account_id},
        )
    ).mappings().first()
    if not cash_row:
        return None
    cash_canonical = cash_row["canonical_form"]

    # 2. Find the category side: the COA account whose number matches
    #    CATEGORY_TO_ACCOUNT[category].
    cat_number = CATEGORY_TO_ACCOUNT.get(category)
    if not cat_number:
        return None
    cat_row = (
        await session.execute(
            text(
                """
                SELECT canonical_form
                FROM chart_of_account
                WHERE entity_id = :eid AND account_number = :num AND is_active = true
                LIMIT 1
                """
            ),
            {"eid": entity_id, "num": cat_number},
        )
    ).mappings().first()
    if not cat_row:
        return None
    cat_canonical = cat_row["canonical_form"]

    # 3. Direction depends on sign:
    #    - Outflow (amount<0):  DR category (expense),  CR cash
    #    - Inflow  (amount>0):  DR cash,                CR category (revenue)
    if amount_cents < 0:
        return AccountPair(debit_account=cat_canonical, credit_account=cash_canonical)
    return AccountPair(debit_account=cash_canonical, credit_account=cat_canonical)


async def _diagnose_resolution_failure(
    session: AsyncSession,
    *,
    entity_id: str,
    category: str,
    external_account_id: str,
) -> tuple[str, "PostingResult"]:
    """Explain WHY ``resolve_accounts_for_txn`` returned None.

    The single "not in CATEGORY_TO_ACCOUNT or COA missing entry" message
    conflated three distinct failures (unlinked cash account, unknown category,
    missing category account) and cost callers several blind probe rounds. Runs
    only on the refusal path. Returns ``(reason, outcome)``.
    """
    cash = (
        await session.execute(
            text(
                "SELECT 1 FROM chart_of_account WHERE entity_id = :eid "
                "AND linked_external_account_id = :ext AND is_active = true LIMIT 1"
            ),
            {"eid": entity_id, "ext": external_account_id},
        )
    ).first()
    if not cash:
        return (
            f"No active chart-of-accounts cash/card account is linked to "
            f"external_account_id '{external_account_id}' for this entity — link a "
            f"COA account to it (connector setup) before posting from this account.",
            "blocked_unmapped",
        )
    num = CATEGORY_TO_ACCOUNT.get(category)
    if not num:
        return (
            f"Category '{category}' has no chart-of-accounts mapping "
            f"(not in CATEGORY_TO_ACCOUNT).",
            "blocked_unknown_category",
        )
    cat = (
        await session.execute(
            text(
                "SELECT 1 FROM chart_of_account WHERE entity_id = :eid "
                "AND account_number = :num AND is_active = true LIMIT 1"
            ),
            {"eid": entity_id, "num": num},
        )
    ).first()
    if not cat:
        return (
            f"Category '{category}' maps to account {num}, but this entity has no "
            f"active chart-of-accounts row for {num} — create the account first.",
            "blocked_unknown_category",
        )
    return (
        f"Could not resolve accounts for category '{category}' "
        f"(external_account_id '{external_account_id}').",
        "blocked_unknown_category",
    )


# ---------- Posting ----------------------------------------------------

PostingResult = Literal[
    "posted", "queued_for_confirm", "blocked_materiality",
    "blocked_unmapped", "blocked_unknown_category", "blocked_idempotent",
    "blocked_period_closed",
]


@dataclass
class PostingOutcome:
    """Successful post — the journal entry id + the materiality verdict."""

    journal_entry_id: UUID
    materiality: MaterialityVerdict


@dataclass
class PostingRefusal:
    """Refused post — outcome string + reason. No DB write happened."""

    outcome: PostingResult
    reason: str
    existing_entry_id: UUID | None = None


# Audit 2026-05-31 Bug 9: chain-race retry bound.
# A concurrent post advancing the per-entity hash chain MUST cause this writer
# to re-fetch get_last_chain_hash and recompute this_hash, NOT mis-classify as
# idempotent. Bounded at 3 to avoid livelock if write pressure is pathological;
# in that case the call returns blocked_idempotent and the caller can retry.
_MAX_CHAIN_RETRIES = 3


@dataclass
class _ChainInsertIdempotent:
    """The (entity_id, source, source_external_id) uniqueness was hit during
    insert — a TRUE duplicate raced our pre-check. Carries the existing entry
    id so the caller can return it on the PostingRefusal."""

    existing_entry_id: UUID | None


@dataclass
class _ChainInsertExhausted:
    """All _MAX_CHAIN_RETRIES attempts hit a non-idempotency IntegrityError —
    pathological chain contention. Treat as blocked_idempotent so the caller
    can retry, mirroring pre-Bug-9 behavior for the unrecoverable case."""


async def _attempt_chain_insert(
    session: AsyncSession,
    *,
    je_id: UUID,
    workspace_id: str,
    entity_id: str,
    amount_cents: int,  # absolute value
    effective_date,
    debit_account: str,
    credit_account: str,
    category: str,
    tier: str,
    source: str,
    source_external_id: str | None,
    description: str,
    payee: str | None,
    memo: str | None,
    reasoning_trace: str,
    model_version: str,
    identity: str,
    approving_uid: str | None,
    receipt_link_id: UUID | None,
) -> None | _ChainInsertIdempotent | _ChainInsertExhausted:
    """Attempt to advance the per-entity chain + insert, with bounded retry.

    Audit 2026-05-31 Bug 9. Returns ``None`` on a clean insert; an
    ``_ChainInsertIdempotent`` when the OTHER writer won the idempotency race
    (true duplicate); an ``_ChainInsertExhausted`` when all retries hit
    non-idempotency IntegrityErrors.

    On each attempt:
      1. Re-fetch ``get_last_chain_hash`` so prev_hash reflects the LATEST
         committed entry for this entity (a concurrent post may have advanced
         it since the previous attempt).
      2. Compute this_hash + a FRESH posted_at (so collisions on posted_at are
         vanishingly unlikely between retries).
      3. INSERT inside a savepoint so a duplicate-key error doesn't poison the
         outer transaction.

    IntegrityError disambiguation: PostgreSQL/asyncpg raises ``UniqueViolation``
    with a ``constraint_name`` attribute set on the wrapped ``orig`` exception.
    We treat conflicts on ``uq_je_entity_source_external`` as idempotency
    (terminal) and anything else as a chain-advance race (retryable).
    """
    last_exc: IntegrityError | None = None
    for attempt in range(_MAX_CHAIN_RETRIES):
        prev_hash = await get_last_chain_hash(session, entity_id)
        posted_at = datetime.now(tz=timezone.utc)
        this_hash = compute_hash(
            prev_hash,
            effective_date=effective_date,
            amount_cents=amount_cents,
            debit_account=debit_account,
            credit_account=credit_account,
            source=source,
            source_external_id=source_external_id,
            payee=payee,
            description=description,
            posted_at=posted_at,
        )
        try:
            async with session.begin_nested():
                await _insert_journal_entry(
                    session,
                    je_id=je_id,
                    workspace_id=workspace_id,
                    entity_id=entity_id,
                    amount_cents=amount_cents,
                    effective_date=effective_date,
                    posted_at=posted_at,
                    debit_account=debit_account,
                    credit_account=credit_account,
                    category=category,
                    tier=tier,
                    source=source,
                    source_external_id=source_external_id,
                    description=description,
                    payee=payee,
                    memo=memo,
                    prev_hash=prev_hash,
                    this_hash=this_hash,
                    reasoning_trace=reasoning_trace,
                    model_version=model_version,
                    identity=identity,
                    approving_uid=approving_uid,
                    receipt_link_id=receipt_link_id,
                )
            return None
        except IntegrityError as exc:
            last_exc = exc
            constraint = _integrity_constraint_name(exc)
            if constraint == "uq_je_entity_source_external":
                # True idempotency: the other writer's row is already in.
                existing_id = await _lookup_existing_entry_id(
                    session,
                    entity_id=entity_id,
                    source=source,
                    source_external_id=source_external_id,
                )
                return _ChainInsertIdempotent(existing_entry_id=existing_id)
            if constraint == "uq_je_entity_this_hash":
                # Defense-in-depth (migration 0018, audit 2026-05-31 Issue 2):
                # the (entity_id, this_hash) DB unique constraint fired. This
                # can only happen if two writers hashed IDENTICAL inputs
                # (same prev_hash + same effective_date + same accounts + same
                # source + same source_external_id + same payee + same
                # description + same posted_at micro). That's effectively
                # idempotent — the existing row IS the same logical post — so
                # we treat it as such. In normal operation Bug 9's prev_hash
                # retry would have caught the race upstream of THIS constraint;
                # if this branch fires the writers contested the chain AND
                # raced into the same micro-timestamp. Surface the existing id
                # when we can resolve it via (source, source_external_id);
                # otherwise None (callers handle None as best-effort).
                existing_id = await _lookup_existing_entry_id(
                    session,
                    entity_id=entity_id,
                    source=source,
                    source_external_id=source_external_id,
                )
                return _ChainInsertIdempotent(existing_entry_id=existing_id)
            # Anything else: assume chain-race / transient — retry with a
            # fresh prev_hash + this_hash + posted_at on next loop iteration.
            continue
    # Exhausted retries on non-idempotency conflicts.
    return _ChainInsertExhausted()


def _integrity_constraint_name(exc: IntegrityError) -> str | None:
    """Best-effort name extraction from an asyncpg/SQLAlchemy IntegrityError.

    asyncpg wraps the underlying ``UniqueViolationError`` (which carries
    ``constraint_name``) on the SQLAlchemy exception's ``orig`` attribute.
    Returns the constraint name when available, else None.
    """
    orig = getattr(exc, "orig", None)
    if orig is None:
        return None
    name = getattr(orig, "constraint_name", None)
    if isinstance(name, str) and name:
        return name
    # Fallback: scrape the message. Postgres formats unique-violation messages
    # as: 'duplicate key value violates unique constraint "<name>"'.
    msg = str(orig)
    if 'unique constraint "' in msg:
        try:
            return msg.split('unique constraint "', 1)[1].split('"', 1)[0]
        except (IndexError, ValueError):
            pass
    return None


async def _lookup_existing_entry_id(
    session: AsyncSession,
    *,
    entity_id: str,
    source: str,
    source_external_id: str | None,
) -> UUID | None:
    """Read the id of the already-posted row for (entity, source, ext_id).

    Used after a uq_je_entity_source_external violation to report the
    existing_entry_id back on the PostingRefusal.
    """
    if not source_external_id:
        return None
    row = (
        await session.execute(
            text(
                """
                SELECT id::text FROM journal_entry
                WHERE entity_id = :eid AND source = :src AND source_external_id = :ext
                LIMIT 1
                """
            ),
            {"eid": entity_id, "src": source, "ext": source_external_id},
        )
    ).first()
    if row is None:
        return None
    try:
        return UUID(row.id)
    except (ValueError, AttributeError):
        return None


async def post_journal_entry(
    session: AsyncSession,
    *,
    workspace_id: str,
    entity_id: str,
    source: str,
    source_external_id: str | None,
    effective_date,  # date
    amount_cents: int,
    description: str,
    payee: str | None,
    memo: str | None,
    category: str,
    category_status: str,
    entity_mapped: bool,
    sign_matched: bool,
    external_account_id: str | None,
    has_receipt_link: bool,
    receipt_link_id: UUID | None,
    posted_by_uc_uid: str,
    posted_by_agent_id: str | None = None,
    user_confirmed: bool = False,
    approving_human_uc_uid: str | None = None,
    reasoning_trace: str = "",
    rule_version_applied: UUID | None = None,
    model_version: str = "",
    counterparty_id: UUID | None = None,
    event_id: UUID | None = None,
) -> PostingOutcome | PostingRefusal:
    """Atomic post with materiality gate + hash chain advance.

    Materiality gate is consulted FIRST. If it refuses, we log to
    posting_log and return a PostingRefusal — no journal_entry row created.

    TRUST CONTRACT: ``user_confirmed=True`` asserts that a HUMAN attested
    this post. Only callers that verified the attestation themselves may set
    it: the HTTP route (the authenticated human IS the caller), the approval
    decide path (an admin decision row exists), or the MCP tool layer after
    it has matched an APPROVED approval_queue row. It must never be plumbed
    through from an agent-supplied argument. ``approving_human_uc_uid``, when
    given, stamps that human as the approver instead of the posting identity.
    """
    # ---- 1. Materiality gate ----
    verdict = evaluate(
        amount_cents=amount_cents,
        category=category,
        category_status=category_status,
        entity_mapped=entity_mapped,
        sign_matched=sign_matched,
        has_receipt_link=has_receipt_link,
        user_confirmed=user_confirmed,
    )
    if verdict.outcome != "auto_post":
        outcome_map = {
            "queue_for_confirm": "queued_for_confirm",
            "block_high_value": "blocked_materiality",
            "block_274d": "blocked_materiality",
        }
        outcome: PostingResult = outcome_map.get(verdict.outcome, "blocked_materiality")  # type: ignore[assignment]
        await _log_post_attempt(
            session,
            workspace_id=workspace_id,
            entity_id=entity_id,
            source=source,
            source_external_id=source_external_id,
            outcome=outcome,
            journal_entry_id=None,
            reason=verdict.reason,
            actor_uc_uid=posted_by_uc_uid,
            actor_agent_id=posted_by_agent_id,
            amount_cents=amount_cents,
            category=category,
        )
        return PostingRefusal(outcome=outcome, reason=verdict.reason)

    # ---- 2. Resolve double-entry accounts ----
    if not external_account_id:
        await _log_post_attempt(
            session, workspace_id=workspace_id, entity_id=entity_id, source=source,
            source_external_id=source_external_id, outcome="blocked_unmapped",
            journal_entry_id=None, reason="No external_account_id provided",
            actor_uc_uid=posted_by_uc_uid, actor_agent_id=posted_by_agent_id,
            amount_cents=amount_cents, category=category,
        )
        return PostingRefusal(outcome="blocked_unmapped", reason="No external account id")

    pair = await resolve_accounts_for_txn(
        session,
        entity_id=entity_id,
        category=category,
        amount_cents=amount_cents,
        external_account_id=external_account_id,
        source=source,
    )
    if pair is None:
        reason, refusal_outcome = await _diagnose_resolution_failure(
            session,
            entity_id=entity_id,
            category=category,
            external_account_id=external_account_id,
        )
        await _log_post_attempt(
            session, workspace_id=workspace_id, entity_id=entity_id, source=source,
            source_external_id=source_external_id, outcome=refusal_outcome,
            journal_entry_id=None,
            reason=reason,
            actor_uc_uid=posted_by_uc_uid, actor_agent_id=posted_by_agent_id,
            amount_cents=amount_cents, category=category,
        )
        return PostingRefusal(outcome=refusal_outcome, reason=reason)

    # ---- 3. Idempotency check (before chain compute) ----
    if source_external_id:
        existing = (
            await session.execute(
                text(
                    """
                    SELECT id::text FROM journal_entry
                    WHERE entity_id = :eid AND source = :src AND source_external_id = :ext
                    LIMIT 1
                    """
                ),
                {"eid": entity_id, "src": source, "ext": source_external_id},
            )
        ).first()
        if existing:
            await _log_post_attempt(
                session, workspace_id=workspace_id, entity_id=entity_id, source=source,
                source_external_id=source_external_id, outcome="blocked_idempotent",
                journal_entry_id=UUID(existing.id),
                reason="Already posted",
                actor_uc_uid=posted_by_uc_uid, actor_agent_id=posted_by_agent_id,
                amount_cents=amount_cents, category=category,
            )
            return PostingRefusal(
                outcome="blocked_idempotent",
                reason="Already posted",
                existing_entry_id=UUID(existing.id),
            )

    # ---- 3.5. Period-close guard ----
    # Refuse the post if the entity has a closed_period row whose period_end
    # is >= effective_date (i.e. the effective date falls inside or before the
    # closed window).  This runs after the idempotency check so a re-delivered
    # already-posted entry still returns blocked_idempotent (which is harmless),
    # not blocked_period_closed (which would be confusing for a no-op).
    # Audit 2026-05-31 Bug 4: extracted to ``assert_period_open`` and reused by
    # ``_apply_recategorization`` so a recat can't bypass the close gate.
    period_end = await assert_period_open(
        session, entity_id=entity_id, effective_date=effective_date
    )
    if period_end is not None:
        await _log_post_attempt(
            session,
            workspace_id=workspace_id,
            entity_id=entity_id,
            source=source,
            source_external_id=source_external_id,
            outcome="blocked_period_closed",
            journal_entry_id=None,
            reason=f"Period closed at {period_end}; cannot post {effective_date}.",
            actor_uc_uid=posted_by_uc_uid,
            actor_agent_id=posted_by_agent_id,
            amount_cents=amount_cents,
            category=category,
        )
        return PostingRefusal(
            outcome="blocked_period_closed",
            reason=f"Period closed at {period_end}; cannot post {effective_date}.",
        )

    # ---- 4 + 5. Hash chain advance + insert (with bounded chain-race retry) ----
    # Audit 2026-05-31 Bug 9: a concurrent post advancing the chain past us
    # MUST NOT manifest as a false ``blocked_idempotent``. The chain-race
    # IntegrityError is distinct from the (entity_id, source, source_external_id)
    # idempotency violation — only the latter is a "true" duplicate. For a
    # chain-race conflict we re-read get_last_chain_hash and recompute this_hash
    # up to ``_MAX_CHAIN_RETRIES`` times so legitimate concurrent imports both
    # land instead of one being silently dropped.
    abs_amount_cents = abs(amount_cents)
    je_id = uuid4()
    insert_outcome = await _attempt_chain_insert(
        session,
        je_id=je_id,
        workspace_id=workspace_id,
        entity_id=entity_id,
        amount_cents=abs_amount_cents,
        effective_date=effective_date,
        debit_account=pair.debit_account,
        credit_account=pair.credit_account,
        category=category,
        tier=verdict.tier,
        source=source,
        source_external_id=source_external_id,
        description=description,
        payee=payee,
        memo=memo,
        reasoning_trace=reasoning_trace,
        model_version=model_version,
        identity=posted_by_uc_uid,
        approving_uid=(
            approving_human_uc_uid
            or (posted_by_uc_uid if user_confirmed else None)
        ),
        receipt_link_id=receipt_link_id,
    )
    if isinstance(insert_outcome, _ChainInsertIdempotent):
        # True (entity_id, source, source_external_id) duplicate — the OTHER
        # writer won the idempotency race after our pre-check at step 3.
        await _log_post_attempt(
            session, workspace_id=workspace_id, entity_id=entity_id, source=source,
            source_external_id=source_external_id, outcome="blocked_idempotent",
            journal_entry_id=insert_outcome.existing_entry_id,
            reason="Concurrent writer posted the same (source, source_external_id)",
            actor_uc_uid=posted_by_uc_uid, actor_agent_id=posted_by_agent_id,
            amount_cents=amount_cents, category=category,
        )
        return PostingRefusal(
            outcome="blocked_idempotent",
            reason="Already posted",
            existing_entry_id=insert_outcome.existing_entry_id,
        )
    if isinstance(insert_outcome, _ChainInsertExhausted):
        # _MAX_CHAIN_RETRIES rounds of chain-race conflicts. Surface as
        # blocked_idempotent so the caller can retry the whole post, matching
        # pre-Bug-9 behavior on the truly pathological case — but log loudly so
        # operators can see the hot entity.
        await _log_post_attempt(
            session, workspace_id=workspace_id, entity_id=entity_id, source=source,
            source_external_id=source_external_id, outcome="blocked_idempotent",
            journal_entry_id=None,
            reason=(
                f"Chain advance retried {_MAX_CHAIN_RETRIES} times; "
                "concurrent posts contesting this entity's chain"
            ),
            actor_uc_uid=posted_by_uc_uid, actor_agent_id=posted_by_agent_id,
            amount_cents=amount_cents, category=category,
        )
        return PostingRefusal(
            outcome="blocked_idempotent",
            reason=(
                f"Chain race exhausted {_MAX_CHAIN_RETRIES} retries — caller should retry"
            ),
        )

    await _log_post_attempt(
        session,
        workspace_id=workspace_id,
        entity_id=entity_id,
        source=source,
        source_external_id=source_external_id,
        outcome="posted",
        journal_entry_id=je_id,
        reason=verdict.reason,
        actor_uc_uid=posted_by_uc_uid,
        actor_agent_id=posted_by_agent_id,
        amount_cents=amount_cents,
        category=category,
    )

    return PostingOutcome(journal_entry_id=je_id, materiality=verdict)


async def _insert_journal_entry(
    session: AsyncSession,
    *,
    je_id: UUID,
    workspace_id: str,
    entity_id: str,
    amount_cents: int,  # absolute value; stored as NUMERIC(20,4)
    effective_date,
    posted_at: datetime,
    debit_account: str,
    credit_account: str,
    category: str,
    tier: str,
    source: str,
    source_external_id: str | None,
    description: str,
    payee: str | None,
    memo: str | None,
    prev_hash: bytes,
    this_hash: bytes,
    reasoning_trace: str,
    model_version: str,
    identity: str,
    approving_uid: str | None,
    receipt_link_id: UUID | None,
    reverses_entry_id: UUID | None = None,
) -> None:
    """Single INSERT into journal_entry. The ONE place the column list +
    schema defaults live so post_journal_entry and post_correction can never
    drift. Caller owns the savepoint + chain-hash computation.

    The existing 0001 schema requires event_id, counterparty_id,
    source_document_hash, reasoning_trace, rule_version_applied, model_version,
    decision_verdict, identity. We supply sane defaults for the fields the v2
    augmentation didn't redefine (event_id/counterparty_id NULL since those
    subsystems aren't built; source_document_hash carries the chain hash hex).
    """
    amount_decimal = Decimal(amount_cents) / Decimal(100)
    await session.execute(
        text(
            """
            INSERT INTO journal_entry (
                id, workspace_id, entity_id, event_id,
                amount, currency_code, usd_equivalent_at_event_time, fx_source,
                posted_at, effective_date,
                source_document_id, source_document_hash,
                counterparty_id,
                reasoning_trace, rule_version_applied, model_version,
                decision_verdict, identity, approving_human_uc_uid,
                debit_account, credit_account, category, materiality_tier,
                source, source_external_id,
                description, payee, memo,
                prev_hash, this_hash, status, receipt_link_id,
                reverses_entry_id
            ) VALUES (
                :id, :workspace_id, :entity_id, NULL,
                :amount, 'USD', :amount, NULL,
                :posted_at, :effective_date,
                NULL, :hash_hex,
                NULL,
                :reasoning, NULL, :model_version,
                'Approve', :identity, :approving_uid,
                :debit, :credit, :category, :tier,
                :source, :source_external_id,
                :description, :payee, :memo,
                :prev_hash, :this_hash, 'posted', :receipt_link_id,
                :reverses_entry_id
            )
            """
        ),
        {
            "id": str(je_id),
            "workspace_id": workspace_id,
            "entity_id": entity_id,
            "amount": amount_decimal,
            "posted_at": posted_at,
            "effective_date": effective_date,
            "hash_hex": this_hash.hex(),
            "reasoning": reasoning_trace,
            "model_version": model_version,
            "identity": identity,
            "approving_uid": approving_uid,
            "debit": debit_account,
            "credit": credit_account,
            "category": category,
            "tier": tier,
            "source": source,
            "source_external_id": source_external_id,
            "description": description,
            "payee": payee,
            "memo": memo,
            "prev_hash": prev_hash,
            "this_hash": this_hash,
            "receipt_link_id": str(receipt_link_id) if receipt_link_id else None,
            "reverses_entry_id": str(reverses_entry_id) if reverses_entry_id else None,
        },
    )


@dataclass
class CorrectionOutcome:
    """A category correction that was applied to the journal.

    The original entry is left in place (append-only); a reversing entry plus a
    re-stated entry with the new category are appended. ``net_unchanged`` is the
    accounting invariant: reversal + restatement leave total debits/credits on
    the *cash* side unchanged — only the category (expense/revenue) account moves.
    """

    reversal_entry_id: UUID
    restated_entry_id: UUID
    materiality_tier: str
    from_category: str
    to_category: str


@dataclass
class CorrectionQueued:
    """A material correction was NOT posted; it queued for human attestation.

    Mirrors post_journal_entry's queue/block behavior: a recategorization of an
    M/H-tier entry is not auto-applied. An approval_queue row is created and the
    ledger is left untouched until a human approves.
    """

    approval_id: UUID
    materiality_tier: str
    reason: str
    from_category: str
    to_category: str


async def _resolve_category_account(
    session: AsyncSession, *, entity_id: str, category: str
) -> str | None:
    """canonical_form of the COA account a category posts to, or None.

    Same resolution post path uses (CATEGORY_TO_ACCOUNT -> chart_of_account),
    so a correction lands on exactly the account a fresh post of that category
    would have used."""
    cat_number = CATEGORY_TO_ACCOUNT.get(category)
    if not cat_number:
        return None
    row = (
        await session.execute(
            text(
                """
                SELECT canonical_form
                FROM chart_of_account
                WHERE entity_id = :eid AND account_number = :num AND is_active = true
                LIMIT 1
                """
            ),
            {"eid": entity_id, "num": cat_number},
        )
    ).mappings().first()
    return row["canonical_form"] if row else None


async def _is_cash_side(
    session: AsyncSession, *, entity_id: str, canonical_form: str
) -> bool:
    """True if the COA account is asset/liability (the cash/credit-card side of
    a simple two-line entry), False if it's the income-statement (category) side.
    """
    row = (
        await session.execute(
            text(
                """
                SELECT account_type FROM chart_of_account
                WHERE entity_id = :eid AND canonical_form = :cf
                LIMIT 1
                """
            ),
            {"eid": entity_id, "cf": canonical_form},
        )
    ).mappings().first()
    return bool(row) and row["account_type"] in ("asset", "liability")


async def assert_period_open(
    session: AsyncSession, *, entity_id: str, effective_date
):
    """Return the closed_period.period_end date IF the effective_date falls
    inside (or before) a closed window for this entity, otherwise return None.

    The post path checks this and refuses with PostingRefusal('blocked_period_closed').
    The recategorization path (``_apply_recategorization``) MUST run this BEFORE
    appending the reversal+restatement pair so a held material recat approved
    after a period close can't silently change closed books — Bug 4 in
    audits/code-audit-2026-05-31.md.

    A returned date means "blocked"; ``None`` means "open" (caller proceeds).
    Kept as a thin SELECT so both call sites stay cheap on the hot path; we do
    not raise here so each caller can decide between a PostingRefusal (the
    posting path) and a clean rollback (the recategorization path).
    """
    closed_row = (
        await session.execute(
            text(
                """
                SELECT period_end
                FROM closed_period
                WHERE entity_id = :eid
                  AND period_end >= :effective_date
                ORDER BY period_end ASC
                LIMIT 1
                """
            ),
            {"eid": entity_id, "effective_date": effective_date},
        )
    ).first()
    return closed_row[0] if closed_row is not None else None


async def _apply_recategorization(
    session: AsyncSession,
    *,
    workspace_id: str,
    entity_id: str,
    orig_id: UUID,
    from_category: str,
    debit_account: str,
    credit_account: str,
    debit_is_cash: bool,
    amount_cents: int,
    effective_date,
    payee: str | None,
    description: str,
    memo: str | None,
    receipt_link_id: UUID | None,
    new_category: str,
    new_cat_canonical: str,
    tier: str,
    reasoning: str,
    actor_uc_uid: str,
    actor_agent_id: str | None = None,
    approving_human_uc_uid: str | None = None,
) -> CorrectionOutcome | PostingRefusal:
    """THE one place the reversal + restatement of a recat is posted.

    Both the auto-apply path (``post_correction`` tier L) and the approve-apply
    path (a human approving a held material recat) call this — so the ledger
    effect of "apply a recategorization" is defined exactly once and can never
    drift between the two surfaces. The caller has already:
      * resolved ``new_cat_canonical`` (the new category's COA account),
      * decided ``debit_is_cash`` (which side is the cash/credit-card side),
      * computed the materiality ``tier``.

    We append a reversing entry (debit/credit flipped, reverses_entry_id ->
    original) then a re-stated entry that keeps the cash side and swaps the
    income-statement side to the new category account. Both advance the
    per-entity hash chain via compute_hash so verify_chain stays green.

    ``approving_human_uc_uid``: when a human attested this recat (approve path),
    it is stamped onto BOTH appended entries' approving_human_uc_uid so the
    ledger itself records who authorized the material change. The auto-apply
    (tier L) path passes ``actor_uc_uid`` (mirrors the prior behavior, where the
    appended entries recorded the actor as the approver).

    Returns CorrectionOutcome on success, or PostingRefusal('blocked_idempotent')
    if a chain race occurs or this entry was already recategorized (the
    'reversal:<orig_id>' source_external_id already exists), or
    PostingRefusal('blocked_period_closed') if the original entry's effective_date
    falls inside a closed window — audit 2026-05-31 Bug 4. Returning a
    PostingRefusal here means the approve-apply caller can abort/roll back the
    approval decision atomically rather than half-applying a recat that mutates
    closed books.
    """
    # ---- Period-close guard (audit 2026-05-31 Bug 4) ----
    # post_journal_entry checks this before appending; the recat path must do
    # the same or a held material recat approved after a period close would
    # silently change closed books (the reversal+restatement use the ORIGINAL
    # effective_date, which is inside the now-closed window). Return a
    # PostingRefusal so the outer decision (e.g. decide_approval) rolls back.
    closed_period_end = await assert_period_open(
        session, entity_id=entity_id, effective_date=effective_date
    )
    if closed_period_end is not None:
        await _log_post_attempt(
            session,
            workspace_id=workspace_id,
            entity_id=entity_id,
            source="recategorization",
            source_external_id=f"recat:{orig_id}",
            outcome="blocked_period_closed",
            journal_entry_id=orig_id,
            reason=(
                f"Period closed at {closed_period_end}; cannot recategorize "
                f"entry effective {effective_date}."
            ),
            actor_uc_uid=actor_uc_uid,
            actor_agent_id=actor_agent_id,
            amount_cents=amount_cents,
            category=new_category,
        )
        return PostingRefusal(
            outcome="blocked_period_closed",
            reason=(
                f"Period closed at {closed_period_end}; cannot recategorize "
                f"entry effective {effective_date}."
            ),
        )

    # Restatement keeps the cash side and swaps the income-statement side.
    if debit_is_cash:
        restated_debit, restated_credit = debit_account, new_cat_canonical
    else:
        restated_debit, restated_credit = new_cat_canonical, credit_account

    reversal_id = uuid4()
    restated_id = uuid4()
    abs_cents = abs(amount_cents)
    # The approving human stamped on the appended entries: the attesting human
    # on the approve path; the actor on the tier-L auto-apply path.
    approver = approving_human_uc_uid or actor_uc_uid
    # The reversal carries its own description; verify_chain rehashes from the
    # STORED description, so we must hash the exact same string we store.
    reversal_description = f"Reversal of {orig_id} for recategorization"
    rev_src_ext = f"reversal:{orig_id}"
    restated_src_ext = f"recat:{orig_id}"

    # Audit 2026-05-31 Bug 9: bounded chain-race retry. A concurrent post on
    # the same entity advancing the chain between get_last_chain_hash and
    # insert MUST cause this recat to recompute on the latest prev_hash, NOT
    # be silently treated as idempotent. The reversal+restatement pair is
    # appended atomically inside the savepoint; on a chain-race conflict the
    # whole pair is recomputed against a fresh prev_hash.
    last_recat_failure: IntegrityError | None = None
    for attempt in range(_MAX_CHAIN_RETRIES):
        try:
            async with session.begin_nested():
                # Two appended entries must verify in chain order. verify_chain walks
                # by (posted_at ASC, id ASC); guarantee posted_at(reversal) <
                # posted_at(restatement) by construction so the walk order always
                # matches the link order (reversal -> restatement) regardless of the
                # random ids — otherwise a same-microsecond tie could invert them and
                # break the prev_hash check.
                posted_at = datetime.now(tz=timezone.utc)
                posted_at2 = posted_at + timedelta(microseconds=1)
                # 1. Reversal: flip the original's debit/credit, point back at it.
                prev_hash = await get_last_chain_hash(session, entity_id)
                rev_hash = compute_hash(
                    prev_hash,
                    effective_date=effective_date,
                    amount_cents=abs_cents,
                    debit_account=credit_account,
                    credit_account=debit_account,
                    source="reversal",
                    source_external_id=rev_src_ext,
                    payee=payee,
                    description=reversal_description,
                    posted_at=posted_at,
                )
                await _insert_journal_entry(
                    session,
                    je_id=reversal_id,
                    workspace_id=workspace_id,
                    entity_id=entity_id,
                    amount_cents=abs_cents,
                    effective_date=effective_date,
                    posted_at=posted_at,
                    debit_account=credit_account,
                    credit_account=debit_account,
                    category=from_category,
                    tier=tier,
                    source="reversal",
                    source_external_id=rev_src_ext,
                    description=reversal_description,
                    payee=payee,
                    memo=memo,
                    prev_hash=prev_hash,
                    this_hash=rev_hash,
                    reasoning_trace=reasoning,
                    model_version="",
                    identity=actor_uc_uid,
                    approving_uid=approver,
                    receipt_link_id=receipt_link_id,
                    reverses_entry_id=orig_id,
                )
                # 2. Restatement: cash side unchanged, category side -> new account.
                restated_hash = compute_hash(
                    rev_hash,
                    effective_date=effective_date,
                    amount_cents=abs_cents,
                    debit_account=restated_debit,
                    credit_account=restated_credit,
                    source="recategorization",
                    source_external_id=restated_src_ext,
                    payee=payee,
                    description=description,
                    posted_at=posted_at2,
                )
                await _insert_journal_entry(
                    session,
                    je_id=restated_id,
                    workspace_id=workspace_id,
                    entity_id=entity_id,
                    amount_cents=abs_cents,
                    effective_date=effective_date,
                    posted_at=posted_at2,
                    debit_account=restated_debit,
                    credit_account=restated_credit,
                    category=new_category,
                    tier=tier,
                    source="recategorization",
                    source_external_id=restated_src_ext,
                    description=description,
                    payee=payee,
                    memo=memo,
                    prev_hash=rev_hash,
                    this_hash=restated_hash,
                    reasoning_trace=reasoning,
                    model_version="",
                    identity=actor_uc_uid,
                    approving_uid=approver,
                    receipt_link_id=receipt_link_id,
                    reverses_entry_id=None,
                )
            # Success — break out of the retry loop.
            break
        except IntegrityError as exc:
            last_recat_failure = exc
            constraint = _integrity_constraint_name(exc)
            if constraint == "uq_je_entity_source_external":
                # The 'reversal:<orig_id>' or 'recat:<orig_id>' source_external_id
                # already exists for this entity — TRUE duplicate. Not a chain
                # race, not retryable.
                return PostingRefusal(
                    outcome="blocked_idempotent",
                    reason=(
                        f"Entry {orig_id} was already recategorized "
                        f"(source_external_id '{rev_src_ext}' exists)."
                    ),
                )
            if constraint == "uq_je_entity_this_hash":
                # Defense-in-depth (migration 0018, audit 2026-05-31 Issue 2):
                # the (entity_id, this_hash) constraint fired on the recat
                # pair. Same logic as the single-post path — if two writers
                # produced identical hashes the row IS already in. Surface as
                # idempotent rather than retrying because a retry against an
                # advanced chain would either succeed cleanly (different
                # prev_hash → different this_hash) or hit the source_external_id
                # branch above (a duplicate of THIS specific recat).
                return PostingRefusal(
                    outcome="blocked_idempotent",
                    reason=(
                        f"Recategorization of {orig_id} would duplicate an "
                        "existing chain entry (entity_id, this_hash). "
                        "Likely a concurrent recat won the race."
                    ),
                )
            # Otherwise assume a chain race; loop will recompute prev_hash.
            continue
    else:
        # Exhausted retries on non-idempotency conflicts.
        return PostingRefusal(
            outcome="blocked_idempotent",
            reason=(
                f"Recategorization chain race exhausted {_MAX_CHAIN_RETRIES} "
                "retries — concurrent posts contesting this entity's chain"
            ),
        )

    await _log_post_attempt(
        session, workspace_id=workspace_id, entity_id=entity_id,
        source="recategorization", source_external_id=f"recat:{orig_id}",
        outcome="posted", journal_entry_id=restated_id,
        reason=f"Recategorized {orig_id}: {from_category} -> {new_category}",
        actor_uc_uid=actor_uc_uid, actor_agent_id=actor_agent_id,
        amount_cents=amount_cents, category=new_category,
    )
    return CorrectionOutcome(
        reversal_entry_id=reversal_id,
        restated_entry_id=restated_id,
        materiality_tier=tier,
        from_category=from_category,
        to_category=new_category,
    )


async def post_correction(
    session: AsyncSession,
    *,
    workspace_id: str,
    entity_id: str,
    original: dict,
    new_category: str,
    reasoning: str,
    actor_uc_uid: str,
    actor_agent_id: str | None = None,
) -> CorrectionOutcome | CorrectionQueued | PostingRefusal:
    """Re-categorize a POSTED journal entry, append-only.

    journal_entry is immutable (DB trigger + role grant forbid UPDATE/DELETE),
    so a category change is expressed as accounting corrections, NOT a field
    edit — exactly the documented pattern ("Reversals are NEW entries that point
    back via reverses_entry_id"). We:

      1. resolve the new category's COA account (same path a fresh post uses);
      2. consult the SAME materiality gate as post_journal_entry. A material
         (M/H) correction is NOT auto-applied — it queues an approval_queue row
         and leaves the ledger untouched (parity with post's queue/block);
      3. for an auto-applicable (tier L) correction, append a reversing entry
         (debit/credit flipped, reverses_entry_id -> original) then a re-stated
         entry that keeps the cash side and swaps the category side. Both
         advance the per-entity hash chain via compute_hash, so verify_chain
         stays green.

    ``original`` is the posted row: id, amount, debit_account, credit_account,
    category, effective_date, payee, description, memo (+ optional receipt_link_id).
    Returns CorrectionOutcome (applied), CorrectionQueued (material -> queued),
    or PostingRefusal (new category not resolvable / original already reversed).
    """
    orig_id = UUID(str(original["id"]))
    from_category = original.get("category") or "Uncategorized"
    debit_account = original["debit_account"]
    credit_account = original["credit_account"]
    # amount is NUMERIC(20,4) absolute value; the gate + chain work in cents.
    amount_cents = int((Decimal(str(original["amount"])) * 100).to_integral_value())
    effective_date = original["effective_date"]
    payee = original.get("payee")
    description = original.get("description") or ""
    memo = original.get("memo")
    receipt_link_id = original.get("receipt_link_id")
    if receipt_link_id is not None:
        receipt_link_id = UUID(str(receipt_link_id))

    # Resolve the NEW category's account. If the entry can't be expressed as
    # the standard two-line (cash + category) shape, refuse rather than guess.
    new_cat_canonical = await _resolve_category_account(
        session, entity_id=entity_id, category=new_category
    )
    if new_cat_canonical is None:
        return PostingRefusal(
            outcome="blocked_unknown_category",
            reason=(
                f"Category '{new_category}' is not in CATEGORY_TO_ACCOUNT or the "
                "entity has no matching active COA account."
            ),
        )
    debit_is_cash = await _is_cash_side(
        session, entity_id=entity_id, canonical_form=debit_account
    )
    credit_is_cash = await _is_cash_side(
        session, entity_id=entity_id, canonical_form=credit_account
    )
    if debit_is_cash == credit_is_cash:
        # Both or neither side is cash: not a simple expense/revenue entry
        # (e.g. a transfer or an opening balance). Recategorization via this
        # tool isn't defined for those — refuse cleanly.
        return PostingRefusal(
            outcome="blocked_unknown_category",
            reason=(
                "Entry is not a simple cash+category posting (could not identify "
                "exactly one income-statement side to recategorize)."
            ),
        )

    # ---- Materiality gate (same evaluate() the posting path uses) ----
    # has_receipt_link reflects the original; sign already matched at post time;
    # category_status='auto' because this is an explicit, deterministic recat.
    verdict = evaluate(
        amount_cents=amount_cents,
        category=new_category,
        category_status="auto",
        entity_mapped=True,
        sign_matched=True,
        has_receipt_link=receipt_link_id is not None,
        user_confirmed=False,
    )
    if verdict.outcome != "auto_post":
        # Material (or 274(d)-blocked) correction: queue, do NOT touch the ledger.
        # The structured action is stored in approval_queue.payload so the
        # approve-apply path has a clean, machine-readable thing to act on
        # (subject_id still points at the ORIGINAL entry; the target category +
        # the resolved shape live in payload). The approve path re-resolves the
        # COA + cash-side from the live ledger at decision time — it does NOT
        # trust the stored canonical/side blindly — so a COA change between
        # propose and approve can't post a stale account; payload carries intent.
        import json

        approval_id = uuid4()
        payload = {
            "action": "recategorize",
            "original_entry_id": str(orig_id),
            "from_category": from_category,
            "new_category": new_category,
            "amount_cents": amount_cents,
            "agent_reasoning": reasoning,
            "materiality_tier": verdict.tier,
        }
        await session.execute(
            text(
                """
                INSERT INTO approval_queue (
                    id, workspace_id, entity_id, subject_type, subject_id,
                    amount, currency_code, usd_equivalent_at_event_time,
                    status, reasoning, requested_by_identity, payload
                ) VALUES (
                    :id, :wid, :eid, 'journal_recategorization', :subject,
                    :amount, 'USD', :amount,
                    'pending', :reasoning, :identity, CAST(:payload AS jsonb)
                )
                """
            ),
            {
                "id": str(approval_id),
                "wid": workspace_id,
                "eid": entity_id,
                "subject": str(orig_id),
                "amount": Decimal(abs(amount_cents)) / Decimal(100),
                "reasoning": (
                    f"Re-categorize entry {orig_id} from '{from_category}' to "
                    f"'{new_category}'. {verdict.reason} Agent reasoning: {reasoning}"
                ),
                "identity": actor_uc_uid,
                "payload": json.dumps(payload),
            },
        )
        await _log_post_attempt(
            session, workspace_id=workspace_id, entity_id=entity_id,
            source="recategorization",
            source_external_id=f"recat:{orig_id}",
            outcome=(
                "blocked_materiality"
                if verdict.outcome != "queue_for_confirm"
                else "queued_for_confirm"
            ),
            journal_entry_id=orig_id, reason=verdict.reason,
            actor_uc_uid=actor_uc_uid, actor_agent_id=actor_agent_id,
            amount_cents=amount_cents, category=new_category,
        )
        return CorrectionQueued(
            approval_id=approval_id,
            materiality_tier=verdict.tier,
            reason=verdict.reason,
            from_category=from_category,
            to_category=new_category,
        )

    # ---- Auto-applicable (tier L): append reversal + restatement ----
    # Delegates to the SHARED apply-primitive — the SAME function the
    # approve-apply path calls — so the ledger effect of "apply a recat" is
    # defined exactly once. Tier L stamps the actor as the approver (no separate
    # human attestation occurred); the approve path passes the attesting human.
    return await _apply_recategorization(
        session,
        workspace_id=workspace_id,
        entity_id=entity_id,
        orig_id=orig_id,
        from_category=from_category,
        debit_account=debit_account,
        credit_account=credit_account,
        debit_is_cash=debit_is_cash,
        amount_cents=amount_cents,
        effective_date=effective_date,
        payee=payee,
        description=description,
        memo=memo,
        receipt_link_id=receipt_link_id,
        new_category=new_category,
        new_cat_canonical=new_cat_canonical,
        tier=verdict.tier,
        reasoning=reasoning,
        actor_uc_uid=actor_uc_uid,
        actor_agent_id=actor_agent_id,
        approving_human_uc_uid=None,
    )


async def apply_held_recategorization(
    session: AsyncSession,
    *,
    workspace_id: str,
    entity_id: str,
    original: dict,
    new_category: str,
    reasoning: str,
    approving_human_uc_uid: str,
    actor_agent_id: str | None = None,
) -> CorrectionOutcome | PostingRefusal:
    """Apply a recategorization that was HELD in approval_queue, now that a
    human has attested it.

    This is the approve-apply counterpart to ``post_correction`` and is what the
    approve endpoint calls when a 'journal_recategorization' approval is APPROVED.
    It re-derives everything from the LIVE original entry (passed re-fetched at
    decision time) and re-resolves the new category's COA account + the cash side
    from the current ledger — it does NOT trust any value snapshotted at propose
    time. The only thing taken from the queued action is the target
    ``new_category`` (carried in approval_queue.payload).

    The materiality gate is intentionally NOT re-consulted here: the gate's
    purpose is to decide whether human attestation is required, and that
    attestation has now happened (``approving_human_uc_uid``). We still re-run
    the structural guards (category resolvable, exactly one income-statement
    side) so a stale/edited target can't post a malformed entry.

    Posts via the SAME ``_apply_recategorization`` primitive the auto-apply path
    uses, stamping the attesting human onto the appended reversal + restatement
    (approving_human_uc_uid). Append-only + the per-entity hash chain are
    preserved by construction. Returns CorrectionOutcome on success or
    PostingRefusal (unresolvable category / not a simple cash+category entry /
    chain race / already recategorized).
    """
    orig_id = UUID(str(original["id"]))
    from_category = original.get("category") or "Uncategorized"
    debit_account = original["debit_account"]
    credit_account = original["credit_account"]
    amount_cents = int((Decimal(str(original["amount"])) * 100).to_integral_value())
    effective_date = original["effective_date"]
    payee = original.get("payee")
    description = original.get("description") or ""
    memo = original.get("memo")
    receipt_link_id = original.get("receipt_link_id")
    if receipt_link_id is not None:
        receipt_link_id = UUID(str(receipt_link_id))

    # Re-resolve the NEW category's account against the LIVE COA (defense against
    # a COA change between propose and approve).
    new_cat_canonical = await _resolve_category_account(
        session, entity_id=entity_id, category=new_category
    )
    if new_cat_canonical is None:
        return PostingRefusal(
            outcome="blocked_unknown_category",
            reason=(
                f"Category '{new_category}' is not in CATEGORY_TO_ACCOUNT or the "
                "entity has no matching active COA account."
            ),
        )
    debit_is_cash = await _is_cash_side(
        session, entity_id=entity_id, canonical_form=debit_account
    )
    credit_is_cash = await _is_cash_side(
        session, entity_id=entity_id, canonical_form=credit_account
    )
    if debit_is_cash == credit_is_cash:
        return PostingRefusal(
            outcome="blocked_unknown_category",
            reason=(
                "Entry is not a simple cash+category posting (could not identify "
                "exactly one income-statement side to recategorize)."
            ),
        )

    return await _apply_recategorization(
        session,
        workspace_id=workspace_id,
        entity_id=entity_id,
        orig_id=orig_id,
        from_category=from_category,
        debit_account=debit_account,
        credit_account=credit_account,
        debit_is_cash=debit_is_cash,
        amount_cents=amount_cents,
        effective_date=effective_date,
        payee=payee,
        description=description,
        memo=memo,
        receipt_link_id=receipt_link_id,
        new_category=new_category,
        new_cat_canonical=new_cat_canonical,
        tier=materiality_tier(amount_cents),
        reasoning=reasoning,
        actor_uc_uid=approving_human_uc_uid,
        actor_agent_id=actor_agent_id,
        approving_human_uc_uid=approving_human_uc_uid,
    )


async def _log_post_attempt(
    session: AsyncSession,
    *,
    workspace_id: str,
    entity_id: str,
    source: str,
    source_external_id: str | None,
    outcome: str,
    journal_entry_id: UUID | None,
    reason: str,
    actor_uc_uid: str,
    actor_agent_id: str | None,
    amount_cents: int | None,
    category: str | None,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO posting_log (
                workspace_id, entity_id, source, source_external_id,
                outcome, journal_entry_id, reason, actor_uc_uid,
                actor_agent_id, amount_cents, category
            ) VALUES (
                :workspace_id, :entity_id, :source, :source_external_id,
                :outcome, :journal_entry_id, :reason, :actor_uc_uid,
                :actor_agent_id, :amount_cents, :category
            )
            """
        ),
        {
            "workspace_id": workspace_id,
            "entity_id": entity_id,
            "source": source,
            "source_external_id": source_external_id,
            "outcome": outcome,
            "journal_entry_id": str(journal_entry_id) if journal_entry_id else None,
            "reason": reason,
            "actor_uc_uid": actor_uc_uid,
            "actor_agent_id": actor_agent_id,
            "amount_cents": amount_cents,
            "category": category,
        },
    )
