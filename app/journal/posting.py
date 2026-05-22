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
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.journal.hash_chain import GENESIS_HASH, compute_hash, get_last_chain_hash
from app.journal.materiality import MaterialityVerdict, evaluate


# ---------- Account resolution -----------------------------------------

@dataclass(frozen=True)
class AccountPair:
    debit_account: str   # canonical_form
    credit_account: str  # canonical_form


# Maps human-readable category → COA account number whose balance changes
# when this category is posted. The OTHER side of the entry is always the
# cash/credit-card account the txn came from.
CATEGORY_TO_ACCOUNT: dict[str, str] = {
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
    "Travel · Lodging": "6500",
    "Travel · Transport": "6510",
    "Meals": "6520",
    "Office · Supplies": "6400",
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
    #    external bank account this txn came from.
    cash_row = (
        await session.execute(
            text(
                """
                SELECT canonical_form, account_type
                FROM chart_of_account
                WHERE entity_id = :eid
                  AND linked_external_source = :source
                  AND linked_external_account_id = :ext_id
                  AND is_active = true
                LIMIT 1
                """
            ),
            {"eid": entity_id, "source": source, "ext_id": external_account_id},
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


# ---------- Posting ----------------------------------------------------

PostingResult = Literal[
    "posted", "queued_for_confirm", "blocked_materiality",
    "blocked_unmapped", "blocked_unknown_category", "blocked_idempotent",
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
    reasoning_trace: str = "",
    rule_version_applied: UUID | None = None,
    model_version: str = "",
    counterparty_id: UUID | None = None,
    event_id: UUID | None = None,
) -> PostingOutcome | PostingRefusal:
    """Atomic post with materiality gate + hash chain advance.

    Materiality gate is consulted FIRST. If it refuses, we log to
    posting_log and return a PostingRefusal — no journal_entry row created.
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
        await _log_post_attempt(
            session, workspace_id=workspace_id, entity_id=entity_id, source=source,
            source_external_id=source_external_id, outcome="blocked_unknown_category",
            journal_entry_id=None,
            reason=f"Could not resolve COA accounts for category '{category}'",
            actor_uc_uid=posted_by_uc_uid, actor_agent_id=posted_by_agent_id,
            amount_cents=amount_cents, category=category,
        )
        return PostingRefusal(
            outcome="blocked_unknown_category",
            reason=f"Category '{category}' not in CATEGORY_TO_ACCOUNT or COA missing entry",
        )

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

    # ---- 4. Hash chain advance ----
    prev_hash = await get_last_chain_hash(session, entity_id)
    posted_at = datetime.now(tz=timezone.utc)
    abs_amount_cents = abs(amount_cents)
    this_hash = compute_hash(
        prev_hash,
        effective_date=effective_date,
        amount_cents=abs_amount_cents,
        debit_account=pair.debit_account,
        credit_account=pair.credit_account,
        source=source,
        source_external_id=source_external_id,
        payee=payee,
        description=description,
        posted_at=posted_at,
    )

    # ---- 5. Insert into journal_entry ----
    je_id = uuid4()
    # The existing 0001 schema requires: event_id, counterparty_id,
    # amount, currency_code, usd_equivalent_at_event_time, posted_at,
    # source_document_hash, reasoning_trace, rule_version_applied,
    # model_version, decision_verdict, identity. We provide sane defaults
    # for fields the v2 augmentation didn't redefine.
    amount_decimal = Decimal(abs_amount_cents) / Decimal(100)
    # Use a savepoint so a duplicate-key error on (entity_id, source_external_id)
    # or (entity_id, this_hash) doesn't poison the outer transaction.
    try:
        async with session.begin_nested():
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
                        prev_hash, this_hash, status, receipt_link_id
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
                        :prev_hash, :this_hash, 'posted', :receipt_link_id
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
                    "identity": posted_by_uc_uid,
                    "approving_uid": posted_by_uc_uid if user_confirmed else None,
                    "debit": pair.debit_account,
                    "credit": pair.credit_account,
                    "category": category,
                    "tier": verdict.tier,
                    "source": source,
                    "source_external_id": source_external_id,
                    "description": description,
                    "payee": payee,
                    "memo": memo,
                    "prev_hash": prev_hash,
                    "this_hash": this_hash,
                    "receipt_link_id": str(receipt_link_id) if receipt_link_id else None,
                },
            )
    except IntegrityError as exc:
        # Concurrent post advanced the chain past us, OR an idempotency
        # race. Either way the caller should retry.
        await _log_post_attempt(
            session, workspace_id=workspace_id, entity_id=entity_id, source=source,
            source_external_id=source_external_id, outcome="blocked_idempotent",
            journal_entry_id=None, reason=f"chain race: {exc.__class__.__name__}",
            actor_uc_uid=posted_by_uc_uid, actor_agent_id=posted_by_agent_id,
            amount_cents=amount_cents, category=category,
        )
        return PostingRefusal(
            outcome="blocked_idempotent",
            reason="Chain race detected (concurrent post) — retry",
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
