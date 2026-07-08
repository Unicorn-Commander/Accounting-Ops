"""Map Stripe balance_transactions to double-entry journal entries.

The Stripe /v1/balance_transactions endpoint emits one row per fund movement
on the merchant's account. The ``type`` field discriminates what happened:

  * charge                — a customer payment landed (net of fee)
  * payment               — same as charge for older API versions
  * refund                — a previous charge was refunded
  * payment_refund        — same as refund (per-payment-intent shape)
  * stripe_fee            — Stripe billing the merchant (e.g. monthly fees)
  * payout                — funds swept to the merchant's bank account
  * payout_failure        — payout bounced; reverses payout
  * adjustment            — Stripe-initiated balance correction
  * application_fee*      — Connect-platform-fee variants (flag for CPA review)
  * other                 — flag for CPA review

Account routing (uses the entity's COA; per-entity revenue account configurable):

  Type              Debit                                  Credit
  ----              -----                                  ------
  charge            1210 Stripe Receivable                 4xxx Revenue (default 4200)
  payment           1210 Stripe Receivable                 4xxx Revenue (default 4200)
  refund            4xxx Revenue                           1210 Stripe Receivable
  payment_refund    4xxx Revenue                           1210 Stripe Receivable
  stripe_fee        5520 Payment Processor Fees            1210 Stripe Receivable
  payout            1010 Cash · BoA Primary                1210 Stripe Receivable
  payout_failure    1210 Stripe Receivable                 1010 Cash · BoA Primary
  adjustment        flag for CPA review (sign carries the side)
  application_fee   flag for CPA review

Per balance_transaction we ALSO emit a separate stripe_fee entry when the row
has a non-zero ``fee`` field AND is a charge/refund type. This way the gross
revenue (4xxx) is the headline number on the P&L and processor fees
accumulate cleanly in 5520 — matching how Stripe presents data in its own
dashboard's "Revenue minus fees = net" view.

Idempotency: source = 'stripe_api' + source_external_id = '<bt id>' (e.g.
'txn_3Abc...') matches the existing UNIQUE constraint on journal_entry.
For the separate fee entry we suffix '#fee' so it gets its own row.

First-30-rows guard: by default the first 30 mapped entries per (workspace,
entity) go into approval_queue regardless of materiality. After that,
normal materiality-gate rules apply.

PII: stripe charge.metadata may carry customer email or last4. The mapper
NEVER lands raw metadata in the journal; payee/description are constructed
from txn id + opaque type only.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, date, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.journal.hash_chain import compute_hash, get_last_chain_hash
from app.journal.materiality import evaluate
from app.journal.posting import _insert_journal_entry, _log_post_attempt

_log = logging.getLogger("acctops.stripe_mapper")

# ----------------------------------------------------------------------------
# Account number constants — these must exist in chart_of_account for the
# entity we're posting to. The c_corp template seeds 1210 / 5520 / 1010 by
# default; the revenue account (4xxx) is configurable per-credential.
# ----------------------------------------------------------------------------
ACCT_STRIPE_RECEIVABLE = "1210"  # Stripe Receivable
ACCT_PROCESSOR_FEES = "5520"  # Payment Processor Fees
ACCT_CASH_PRIMARY_DEFAULT = "1010"  # Cash · Primary Checking (BoA via SimpleFIN)
DEFAULT_REVENUE_ACCOUNT = "4200"  # AI Infrastructure Revenue (configurable per entity)

# Discriminator union — keep loosely typed since Stripe adds new types over time.
StripeTxnType = Literal[
    "charge", "payment",
    "refund", "payment_refund",
    "stripe_fee", "payout", "payout_failure",
    "adjustment", "application_fee", "application_fee_refund",
    "transfer", "other",
]

# Safety: first N mapped journal entries per (workspace, entity) go to
# approval queue regardless of materiality — the operator reviews them by hand
# before we trust the mapping for auto-posting.
FIRST_RUN_REVIEW_LIMIT = 30

STRIPE_SOURCE = "stripe_api"


@dataclass
class MapResult:
    """Outcome of attempting to map+post one Stripe balance_transaction."""

    outcome: Literal[
        "posted", "queued_for_confirm", "queued_first_run",
        "blocked_unknown_type", "blocked_unknown_account",
        "blocked_idempotent", "blocked_period_closed",
        "blocked_materiality", "skipped_zero_amount",
        "flagged_for_cpa",
    ]
    stripe_balance_txn_id: str
    stripe_type: str
    journal_entry_id: UUID | None = None
    fee_journal_entry_id: UUID | None = None  # if a stripe-fee child entry was also posted
    approval_id: UUID | None = None
    reason: str = ""


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _amount_to_cents(d: int | float | str | Decimal | None) -> int:
    """Stripe amounts are already integer cents. Coerce defensively."""
    if d is None:
        return 0
    try:
        return int(Decimal(str(d)))
    except (InvalidOperation, ValueError):
        return 0


def _parse_stripe_ts(value: int | None) -> date | None:
    """Stripe ``created`` is a unix epoch int."""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).date()
    except (ValueError, OverflowError, OSError):
        return None


def _opaque_payee(txn: dict[str, Any]) -> str:
    """Build a PII-free payee string for the journal entry.

    Stripe txn.source may carry customer email / card last4. We use the
    txn id and its type. NEVER pull anything from metadata or customer.
    """
    txn_type = (txn.get("type") or "?").lower()
    txn_id = (txn.get("id") or "").strip()
    if txn_id:
        short = txn_id[-8:]
        return f"Stripe {txn_type} · #{short}"
    return f"Stripe {txn_type}"


def _opaque_description(txn: dict[str, Any]) -> str:
    """Description: Stripe-provided category + short id. PII-free."""
    txn_type = (txn.get("type") or "?").lower()
    category = (txn.get("reporting_category") or txn.get("description") or "").strip()
    bits = [f"Stripe {txn_type}"]
    # ``reporting_category`` is a safe Stripe-controlled enum
    # (charge, refund, fee, payout, dispute, etc.).
    if category and category != txn_type:
        # If description is too long or could carry merchant statement text,
        # cap to a short safe value. Reporting categories from Stripe are
        # short keywords.
        if txn.get("reporting_category"):
            bits.append(f"category={txn['reporting_category']}")
        # Don't include free-text ``description`` — could carry customer name.
    return " · ".join(bits)


def _payout_description(txn: dict[str, Any]) -> str:
    txn_id = (txn.get("id") or "").strip()
    short = txn_id[-8:] if txn_id else "?"
    status = (txn.get("status") or "succeeded").lower()
    return f"Stripe payout #{short} · status {status}"


# ----------------------------------------------------------------------------
# COA account resolution
# ----------------------------------------------------------------------------


async def _resolve_canonical(
    session: AsyncSession, *, entity_id: str, account_number: str
) -> str | None:
    """canonical_form for a (entity_id, account_number). None if missing/inactive."""
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
            {"eid": entity_id, "num": account_number},
        )
    ).first()
    return row.canonical_form if row else None


async def _resolve_cash_for_payout(
    session: AsyncSession,
    *,
    entity_id: str,
    external_account_id: str | None,
) -> tuple[str, str] | None:
    """Pick (account_number, canonical_form) of the cash account a payout lands in.

    Prefers a COA account that is linked to ``external_account_id`` (i.e. the
    Stripe payout destination's bank-account id mapped to the operator's bank via
    SimpleFIN). Falls back to the default 1010 entry if no linked account.
    """
    if external_account_id:
        row = (
            await session.execute(
                text(
                    """
                    SELECT account_number, canonical_form
                    FROM chart_of_account
                    WHERE entity_id = :eid
                      AND linked_external_account_id = :ext
                      AND is_active = true
                    LIMIT 1
                    """
                ),
                {"eid": entity_id, "ext": external_account_id},
            )
        ).first()
        if row:
            return row.account_number, row.canonical_form
    canonical = await _resolve_canonical(
        session, entity_id=entity_id, account_number=ACCT_CASH_PRIMARY_DEFAULT
    )
    return (ACCT_CASH_PRIMARY_DEFAULT, canonical) if canonical else None


# ----------------------------------------------------------------------------
# First-run review counter
# ----------------------------------------------------------------------------


async def _count_existing_stripe_posts(
    session: AsyncSession, *, entity_id: str
) -> int:
    """How many stripe_api entries already exist for this entity?"""
    r = (
        await session.execute(
            text(
                """
                SELECT count(*) AS c FROM journal_entry
                WHERE entity_id = :eid AND source = :src
                """
            ),
            {"eid": entity_id, "src": STRIPE_SOURCE},
        )
    ).first()
    return int(r.c or 0) if r else 0


async def _enqueue_for_review(
    session: AsyncSession,
    *,
    workspace_id: str,
    entity_id: str,
    stripe_balance_txn_id: str,
    stripe_type: str,
    amount_cents: int,
    debit_account: str,
    credit_account: str,
    effective_date: date,
    payee: str,
    description: str,
    raw_txn: dict[str, Any],
    actor_uc_uid: str,
    reason: str,
) -> UUID:
    """Insert a row in approval_queue for human review.

    The payload carries the structured action so the approve-apply path can
    post the entry without re-mapping. subject_id is a fresh UUID because the
    Stripe txn isn't yet in the journal.

    PII guard: we never persist customer email / card last4 / metadata; only
    the Stripe-controlled keyset (sans 'source', 'customer', 'metadata').
    """
    approval_id = uuid4()
    subject_id = uuid4()
    safe_keys = sorted([
        k for k in raw_txn.keys()
        if k not in ("source", "customer", "metadata", "billing_details", "shipping")
    ])
    payload = {
        "action": "stripe_post_balance_transaction",
        "stripe_balance_txn_id": stripe_balance_txn_id,
        "stripe_type": stripe_type,
        "amount_cents": amount_cents,
        "debit_account": debit_account,
        "credit_account": credit_account,
        "effective_date": effective_date.isoformat(),
        "payee": payee,
        "description": description,
        "raw_txn_keys": safe_keys,
    }
    await session.execute(
        text(
            """
            INSERT INTO approval_queue (
                id, workspace_id, entity_id, subject_type, subject_id,
                amount, currency_code, usd_equivalent_at_event_time,
                status, reasoning, requested_by_identity, payload
            ) VALUES (
                :id, :wid, :eid, 'stripe_post_balance_transaction', :subject,
                :amount, 'USD', :amount,
                'pending', :reasoning, :identity, CAST(:payload AS jsonb)
            )
            """
        ),
        {
            "id": str(approval_id),
            "wid": workspace_id,
            "eid": entity_id,
            "subject": str(subject_id),
            "amount": Decimal(abs(amount_cents)) / Decimal(100),
            "reasoning": reason,
            "identity": actor_uc_uid,
            "payload": json.dumps(payload),
        },
    )
    return approval_id


# ----------------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------------


async def map_and_post_balance_transaction(
    session: AsyncSession,
    *,
    workspace_id: str,
    entity_id: str,
    txn: dict[str, Any],
    actor_uc_uid: str,
    actor_agent_id: str | None = None,
    revenue_account: str = DEFAULT_REVENUE_ACCOUNT,
) -> MapResult:
    """Map ONE Stripe balance_transaction to a journal entry and post it.

    `txn` is the raw Stripe balance_transaction object (the dict returned by
    GET /v1/balance_transactions/{id} or one entry of /v1/balance_transactions).
    The caller has already loaded credentials + paginated the API; we only
    classify + post.

    Returns a MapResult; never raises on routine refusals (unknown type,
    materiality block, idempotency conflict). Raises only on schema/DB errors.

    For charges/refunds with a non-zero ``fee`` field, this also posts a
    separate fee journal entry so the P&L breaks out gross revenue from
    processor fees cleanly. The fee entry's external id is suffixed with
    '#fee'.
    """
    bt_id = (txn.get("id") or "").strip()
    stripe_type = (txn.get("type") or "").strip().lower()
    if not bt_id or not stripe_type:
        return MapResult(
            outcome="blocked_unknown_type",
            stripe_balance_txn_id=bt_id or "?",
            stripe_type=stripe_type or "?",
            reason="Missing balance_transaction id or type",
        )

    # Stripe amounts are signed integer cents already.
    amount_cents = _amount_to_cents(txn.get("amount"))
    fee_cents = _amount_to_cents(txn.get("fee"))
    if amount_cents == 0 and stripe_type != "payout":
        # A zero-amount non-payout has nothing to post.
        return MapResult(
            outcome="skipped_zero_amount",
            stripe_balance_txn_id=bt_id,
            stripe_type=stripe_type,
            reason="Amount is zero",
        )

    abs_cents = abs(amount_cents)

    # ---- 1. Idempotency pre-check ----
    existing = (
        await session.execute(
            text(
                """
                SELECT id::text FROM journal_entry
                WHERE entity_id = :eid AND source = :src AND source_external_id = :ext
                LIMIT 1
                """
            ),
            {"eid": entity_id, "src": STRIPE_SOURCE, "ext": bt_id},
        )
    ).first()
    if existing:
        return MapResult(
            outcome="blocked_idempotent",
            stripe_balance_txn_id=bt_id,
            stripe_type=stripe_type,
            journal_entry_id=UUID(existing.id),
            reason="Already posted",
        )

    # ---- 2. Resolve accounts per type ----
    stripe_receivable_canonical = await _resolve_canonical(
        session, entity_id=entity_id, account_number=ACCT_STRIPE_RECEIVABLE
    )
    fees_canonical = await _resolve_canonical(
        session, entity_id=entity_id, account_number=ACCT_PROCESSOR_FEES
    )
    revenue_canonical = await _resolve_canonical(
        session, entity_id=entity_id, account_number=revenue_account
    )

    # The amount sign in Stripe's balance_transaction is:
    #   * +ve for credits TO the merchant balance (charge, payout reversal, etc.)
    #   * -ve for debits FROM the merchant balance (refund, fee, payout, dispute)
    # We use the absolute value and pick debit/credit accounts by type.
    if stripe_type in ("charge", "payment"):
        if not (stripe_receivable_canonical and revenue_canonical):
            return MapResult(
                outcome="blocked_unknown_account",
                stripe_balance_txn_id=bt_id,
                stripe_type=stripe_type,
                reason=(
                    f"Missing COA accounts: need {ACCT_STRIPE_RECEIVABLE} "
                    f"(Stripe Receivable) and {revenue_account} (revenue). "
                    "Re-run scripts/extend_coa_mu_inc.sql or the COA seeder."
                ),
            )
        debit_canonical = stripe_receivable_canonical
        credit_canonical = revenue_canonical
        category = "Revenue · Stripe"
        # Charge amount is the GROSS (incl. fee). The fee is broken out in
        # txn.fee — we'll post a separate fee entry below.
    elif stripe_type in ("refund", "payment_refund"):
        if not (stripe_receivable_canonical and revenue_canonical):
            return MapResult(
                outcome="blocked_unknown_account",
                stripe_balance_txn_id=bt_id,
                stripe_type=stripe_type,
                reason=f"Missing COA accounts for {stripe_type}",
            )
        debit_canonical = revenue_canonical
        credit_canonical = stripe_receivable_canonical
        category = "Revenue · Stripe (REFUND)"
    elif stripe_type == "stripe_fee":
        if not (fees_canonical and stripe_receivable_canonical):
            return MapResult(
                outcome="blocked_unknown_account",
                stripe_balance_txn_id=bt_id,
                stripe_type=stripe_type,
                reason=(
                    f"Missing COA accounts: need {ACCT_PROCESSOR_FEES} and "
                    f"{ACCT_STRIPE_RECEIVABLE}"
                ),
            )
        debit_canonical = fees_canonical
        credit_canonical = stripe_receivable_canonical
        category = "Banking · Stripe Fees"
    elif stripe_type == "payout":
        # PAYOUT settles Stripe-receivable back to BoA cash. The destination
        # bank account id is in txn.source.destination on the source object;
        # but balance_transactions don't include that — we look at the parent
        # payout if Stripe inlined it, else fall back to the default 1010.
        ext_id = None
        source = txn.get("source")
        if isinstance(source, dict):
            ext_id = source.get("destination") or source.get("bank_account") or None
        cash = await _resolve_cash_for_payout(
            session, entity_id=entity_id, external_account_id=ext_id
        )
        if not cash or not stripe_receivable_canonical:
            return MapResult(
                outcome="blocked_unknown_account",
                stripe_balance_txn_id=bt_id,
                stripe_type=stripe_type,
                reason=(
                    f"Missing COA accounts: need a cash account "
                    f"(default {ACCT_CASH_PRIMARY_DEFAULT}) and "
                    f"{ACCT_STRIPE_RECEIVABLE}"
                ),
            )
        _, cash_canonical = cash
        debit_canonical = cash_canonical
        credit_canonical = stripe_receivable_canonical
        category = "Stripe Payout Settlement"
    elif stripe_type == "payout_failure":
        # Reverses a payout — bank rejected the deposit; funds return to Stripe.
        ext_id = None
        source = txn.get("source")
        if isinstance(source, dict):
            ext_id = source.get("destination") or source.get("bank_account") or None
        cash = await _resolve_cash_for_payout(
            session, entity_id=entity_id, external_account_id=ext_id
        )
        if not cash or not stripe_receivable_canonical:
            return MapResult(
                outcome="blocked_unknown_account",
                stripe_balance_txn_id=bt_id,
                stripe_type=stripe_type,
                reason="Missing COA accounts for payout_failure",
            )
        _, cash_canonical = cash
        debit_canonical = stripe_receivable_canonical
        credit_canonical = cash_canonical
        category = "Stripe Payout Failure"
    elif stripe_type in (
        "application_fee", "application_fee_refund", "adjustment", "transfer",
    ):
        # Connect-platform constructs or balance corrections — these need CPA
        # eyes. We DO NOT auto-post; we enqueue with a clear reason.
        approval_id = await _enqueue_for_review(
            session,
            workspace_id=workspace_id,
            entity_id=entity_id,
            stripe_balance_txn_id=bt_id,
            stripe_type=stripe_type,
            amount_cents=amount_cents,
            debit_account="(undecided)",
            credit_account="(undecided)",
            effective_date=_parse_stripe_ts(txn.get("created")) or date.today(),
            payee=_opaque_payee(txn),
            description=_opaque_description(txn),
            raw_txn=txn,
            actor_uc_uid=actor_uc_uid,
            reason=(
                f"Stripe type '{stripe_type}' requires CPA review. "
                "Adjustment / Connect platform fee / transfer — no default mapping."
            ),
        )
        await _log_post_attempt(
            session, workspace_id=workspace_id, entity_id=entity_id,
            source=STRIPE_SOURCE, source_external_id=bt_id,
            outcome="queued_for_confirm", journal_entry_id=None,
            reason=f"Stripe type '{stripe_type}' flagged for CPA",
            actor_uc_uid=actor_uc_uid, actor_agent_id=actor_agent_id,
            amount_cents=amount_cents, category="Stripe · CPA Review",
        )
        return MapResult(
            outcome="flagged_for_cpa",
            stripe_balance_txn_id=bt_id,
            stripe_type=stripe_type,
            approval_id=approval_id,
            reason=f"Stripe type '{stripe_type}' needs CPA decision",
        )
    else:
        # Unknown type — refuse to post; let a human classify.
        return MapResult(
            outcome="blocked_unknown_type",
            stripe_balance_txn_id=bt_id,
            stripe_type=stripe_type,
            reason=f"Unhandled Stripe balance_transaction type: {stripe_type!r}",
        )

    # ---- 3. Effective date ----
    eff_date = _parse_stripe_ts(txn.get("available_on") or txn.get("created"))
    if eff_date is None:
        eff_date = date.today()

    if stripe_type == "payout":
        payee = "Stripe payout"
        description = _payout_description(txn)
        memo = f"payout_id={(txn.get('source') or {}).get('id') if isinstance(txn.get('source'), dict) else bt_id}"
    elif stripe_type == "payout_failure":
        payee = "Stripe payout failure"
        description = _payout_description(txn)
        memo = None
    else:
        payee = _opaque_payee(txn)
        description = _opaque_description(txn)
        # source id is a stripe-controlled string (ch_..., re_..., etc.) — safe.
        src = txn.get("source")
        if isinstance(src, dict):
            src = src.get("id")
        if isinstance(src, str) and src:
            memo = f"source={src}"
        else:
            memo = None

    # ---- 4. Period-close guard ----
    closed = (
        await session.execute(
            text(
                """
                SELECT period_end FROM closed_period
                WHERE entity_id = :eid AND period_end >= :ed
                ORDER BY period_end ASC LIMIT 1
                """
            ),
            {"eid": entity_id, "ed": eff_date},
        )
    ).first()
    if closed is not None:
        reason = f"Effective date {eff_date} ≤ closed_period {closed.period_end}"
        await _log_post_attempt(
            session, workspace_id=workspace_id, entity_id=entity_id,
            source=STRIPE_SOURCE, source_external_id=bt_id,
            outcome="blocked_period_closed", journal_entry_id=None,
            reason=reason, actor_uc_uid=actor_uc_uid, actor_agent_id=actor_agent_id,
            amount_cents=amount_cents, category=category,
        )
        return MapResult(
            outcome="blocked_period_closed",
            stripe_balance_txn_id=bt_id,
            stripe_type=stripe_type,
            reason=reason,
        )

    # ---- 5. First-run review guard ----
    existing_stripe_count = await _count_existing_stripe_posts(
        session, entity_id=entity_id
    )
    if existing_stripe_count < FIRST_RUN_REVIEW_LIMIT:
        approval_id = await _enqueue_for_review(
            session,
            workspace_id=workspace_id,
            entity_id=entity_id,
            stripe_balance_txn_id=bt_id,
            stripe_type=stripe_type,
            amount_cents=amount_cents,
            debit_account=debit_canonical,
            credit_account=credit_canonical,
            effective_date=eff_date,
            payee=payee,
            description=description,
            raw_txn=txn,
            actor_uc_uid=actor_uc_uid,
            reason=(
                f"First-{FIRST_RUN_REVIEW_LIMIT}-rows review guard: stripe txn "
                f"#{existing_stripe_count + 1} for this entity needs human eyes "
                "before auto-posting trust is established."
            ),
        )
        await _log_post_attempt(
            session, workspace_id=workspace_id, entity_id=entity_id,
            source=STRIPE_SOURCE, source_external_id=bt_id,
            outcome="queued_for_confirm", journal_entry_id=None,
            reason=f"First-{FIRST_RUN_REVIEW_LIMIT} review guard",
            actor_uc_uid=actor_uc_uid, actor_agent_id=actor_agent_id,
            amount_cents=amount_cents, category=category,
        )
        return MapResult(
            outcome="queued_first_run",
            stripe_balance_txn_id=bt_id,
            stripe_type=stripe_type,
            approval_id=approval_id,
            reason="First-run review guard",
        )

    # ---- 6. Materiality gate ----
    verdict = evaluate(
        amount_cents=amount_cents,
        category=category,
        category_status="auto",
        entity_mapped=True,
        sign_matched=True,
        has_receipt_link=False,
        user_confirmed=False,
    )
    if verdict.outcome != "auto_post":
        approval_id = await _enqueue_for_review(
            session,
            workspace_id=workspace_id, entity_id=entity_id,
            stripe_balance_txn_id=bt_id, stripe_type=stripe_type,
            amount_cents=amount_cents,
            debit_account=debit_canonical, credit_account=credit_canonical,
            effective_date=eff_date, payee=payee, description=description,
            raw_txn=txn, actor_uc_uid=actor_uc_uid,
            reason=f"Materiality tier {verdict.tier}: {verdict.reason}",
        )
        outcome_map = {
            "queue_for_confirm": "queued_for_confirm",
            "block_high_value": "blocked_materiality",
            "block_274d": "blocked_materiality",
        }
        await _log_post_attempt(
            session, workspace_id=workspace_id, entity_id=entity_id,
            source=STRIPE_SOURCE, source_external_id=bt_id,
            outcome=outcome_map.get(verdict.outcome, "queued_for_confirm"),
            journal_entry_id=None, reason=verdict.reason,
            actor_uc_uid=actor_uc_uid, actor_agent_id=actor_agent_id,
            amount_cents=amount_cents, category=category,
        )
        return MapResult(
            outcome=outcome_map.get(verdict.outcome, "queued_for_confirm"),  # type: ignore[arg-type]
            stripe_balance_txn_id=bt_id,
            stripe_type=stripe_type,
            approval_id=approval_id,
            reason=verdict.reason,
        )

    # ---- 7. Hash chain advance + insert ----
    posted_at = datetime.now(tz=timezone.utc)
    prev_hash = await get_last_chain_hash(session, entity_id)
    this_hash = compute_hash(
        prev_hash,
        effective_date=eff_date,
        amount_cents=abs_cents,
        debit_account=debit_canonical,
        credit_account=credit_canonical,
        source=STRIPE_SOURCE,
        source_external_id=bt_id,
        payee=payee,
        description=description,
        posted_at=posted_at,
    )
    je_id = uuid4()
    try:
        async with session.begin_nested():
            await _insert_journal_entry(
                session,
                je_id=je_id,
                workspace_id=workspace_id,
                entity_id=entity_id,
                amount_cents=abs_cents,
                effective_date=eff_date,
                posted_at=posted_at,
                debit_account=debit_canonical,
                credit_account=credit_canonical,
                category=category,
                tier=verdict.tier,
                source=STRIPE_SOURCE,
                source_external_id=bt_id,
                description=description,
                payee=payee,
                memo=memo,
                prev_hash=prev_hash,
                this_hash=this_hash,
                reasoning_trace=(
                    f"stripe_api mapper: type={stripe_type} "
                    f"amount_cents={amount_cents} fee_cents={fee_cents} "
                    f"category={category}"
                ),
                model_version="stripe_mapper:v1",
                identity=actor_uc_uid,
                approving_uid=None,
                receipt_link_id=None,
            )
    except IntegrityError as exc:
        await _log_post_attempt(
            session, workspace_id=workspace_id, entity_id=entity_id,
            source=STRIPE_SOURCE, source_external_id=bt_id,
            outcome="blocked_idempotent", journal_entry_id=None,
            reason=f"chain race / duplicate: {exc.__class__.__name__}",
            actor_uc_uid=actor_uc_uid, actor_agent_id=actor_agent_id,
            amount_cents=amount_cents, category=category,
        )
        return MapResult(
            outcome="blocked_idempotent",
            stripe_balance_txn_id=bt_id,
            stripe_type=stripe_type,
            reason="Concurrent post / duplicate",
        )

    await _log_post_attempt(
        session, workspace_id=workspace_id, entity_id=entity_id,
        source=STRIPE_SOURCE, source_external_id=bt_id,
        outcome="posted", journal_entry_id=je_id, reason=verdict.reason,
        actor_uc_uid=actor_uc_uid, actor_agent_id=actor_agent_id,
        amount_cents=amount_cents, category=category,
    )

    # ---- 8. Sidecar fee entry for charges/refunds with txn.fee != 0 ----
    # Charges have ``fee`` set to Stripe's cut. We post a separate fee entry
    # so the P&L breaks out gross revenue vs processor fees cleanly. Refunds
    # may also carry a fee reversal (Stripe returns the original processing
    # fee, so fee is negative).
    fee_je_id: UUID | None = None
    if stripe_type in ("charge", "payment", "refund", "payment_refund") and fee_cents != 0 and fees_canonical and stripe_receivable_canonical:
        fee_external_id = f"{bt_id}#fee"
        # Idempotency for the fee child entry too.
        fee_existing = (
            await session.execute(
                text(
                    """
                    SELECT id::text FROM journal_entry
                    WHERE entity_id = :eid AND source = :src AND source_external_id = :ext
                    LIMIT 1
                    """
                ),
                {"eid": entity_id, "src": STRIPE_SOURCE, "ext": fee_external_id},
            )
        ).first()
        if not fee_existing:
            # For a charge: fee is positive (Stripe took it from your credit).
            # For a refund: fee is negative (Stripe refunded the processing fee).
            # The journal flips accordingly.
            if fee_cents > 0:
                fee_debit = fees_canonical
                fee_credit = stripe_receivable_canonical
            else:
                fee_debit = stripe_receivable_canonical
                fee_credit = fees_canonical
            fee_abs = abs(fee_cents)
            fee_posted_at = datetime.now(tz=timezone.utc)
            fee_prev_hash = await get_last_chain_hash(session, entity_id)
            fee_payee = f"Stripe fee · #{bt_id[-8:]}"
            fee_description = f"Stripe processing fee on {stripe_type} {bt_id[-8:]}"
            fee_hash = compute_hash(
                fee_prev_hash,
                effective_date=eff_date,
                amount_cents=fee_abs,
                debit_account=fee_debit,
                credit_account=fee_credit,
                source=STRIPE_SOURCE,
                source_external_id=fee_external_id,
                payee=fee_payee,
                description=fee_description,
                posted_at=fee_posted_at,
            )
            fee_je_id = uuid4()
            try:
                async with session.begin_nested():
                    await _insert_journal_entry(
                        session,
                        je_id=fee_je_id,
                        workspace_id=workspace_id,
                        entity_id=entity_id,
                        amount_cents=fee_abs,
                        effective_date=eff_date,
                        posted_at=fee_posted_at,
                        debit_account=fee_debit,
                        credit_account=fee_credit,
                        category="Banking · Stripe Fees",
                        tier="L",
                        source=STRIPE_SOURCE,
                        source_external_id=fee_external_id,
                        description=fee_description,
                        payee=fee_payee,
                        memo=f"parent={bt_id}",
                        prev_hash=fee_prev_hash,
                        this_hash=fee_hash,
                        reasoning_trace=(
                            f"stripe_api mapper sidecar fee: parent={bt_id} "
                            f"fee_cents={fee_cents}"
                        ),
                        model_version="stripe_mapper:v1",
                        identity=actor_uc_uid,
                        approving_uid=None,
                        receipt_link_id=None,
                    )
                await _log_post_attempt(
                    session, workspace_id=workspace_id, entity_id=entity_id,
                    source=STRIPE_SOURCE, source_external_id=fee_external_id,
                    outcome="posted", journal_entry_id=fee_je_id,
                    reason="Stripe fee sidecar on charge/refund",
                    actor_uc_uid=actor_uc_uid, actor_agent_id=actor_agent_id,
                    amount_cents=fee_cents, category="Banking · Stripe Fees",
                )
            except IntegrityError:
                # Fee sidecar duplicate; not fatal. Parent post succeeded.
                fee_je_id = None

    return MapResult(
        outcome="posted",
        stripe_balance_txn_id=bt_id,
        stripe_type=stripe_type,
        journal_entry_id=je_id,
        fee_journal_entry_id=fee_je_id,
        reason=f"posted as {category}",
    )


__all__ = (
    "MapResult",
    "STRIPE_SOURCE",
    "FIRST_RUN_REVIEW_LIMIT",
    "ACCT_STRIPE_RECEIVABLE",
    "ACCT_PROCESSOR_FEES",
    "ACCT_CASH_PRIMARY_DEFAULT",
    "DEFAULT_REVENUE_ACCOUNT",
    "map_and_post_balance_transaction",
)
