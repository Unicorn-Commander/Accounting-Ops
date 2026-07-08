"""Map eBay Sell-Finances transactions to double-entry journal entries.

The eBay Finances API returns five primary transaction types:

  * SALE                  — order paid by buyer
  * REFUND                — buyer refunded
  * FEE                   — eBay's selling fee on a sale
  * NON_SALE_CHARGE       — store-subscription fees, ad fees, payment-dispute
                            holds, etc. (anything that's not directly tied to
                            a sale order)
  * PAYOUT                — eBay sweeps the available balance to the seller's
                            bank account (this is the bank deposit the operator sees)

Account routing (uses MU Inc's extended COA, see scripts/extend_coa_mu_inc.sql
and the c_corp template):

  Type              Debit                                  Credit
  ----              -----                                  ------
  SALE              1220 eBay Payouts Receivable           4150 eBay Resale Revenue
  REFUND            4150 eBay Resale Revenue               1220 eBay Payouts Receivable
  FEE               5520 Payment Processor Fees            1220 eBay Payouts Receivable
  NON_SALE_CHARGE   5520 Payment Processor Fees (default)  1220 eBay Payouts Receivable
  PAYOUT            1010 Cash · BoA Primary                1220 eBay Payouts Receivable

Idempotency: `source = 'ebay_finances'` + `source_external_id = '<ebay txn id>'`
matches the existing UNIQUE constraint on journal_entry.

First-30-rows guard: by default the first 30 mapped entries per (workspace,
entity) go into approval_queue instead of posting directly, regardless of
materiality. After that, normal materiality-gate rules apply.
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

_log = logging.getLogger("acctops.ebay_mapper")

# ----------------------------------------------------------------------------
# Account number constants — these must exist in chart_of_account for the
# entity we're posting to. The COA-extension SQL (extend_coa_mu_inc.sql) seeds
# them; the c_corp template also includes 1220 / 4150 / 5520 by default.
# ----------------------------------------------------------------------------
ACCT_PAYOUTS_RECEIVABLE = "1220"  # eBay Payouts Receivable
ACCT_EBAY_REVENUE = "4150"  # eBay Resale Revenue
ACCT_PROCESSOR_FEES = "5520"  # Payment Processor Fees
ACCT_CASH_PRIMARY_DEFAULT = "1010"  # Cash · Primary Checking (BoA via SimpleFIN)

EbayTxnType = Literal["SALE", "REFUND", "FEE", "NON_SALE_CHARGE", "PAYOUT", "CREDIT", "DISPUTE", "SHIPPING_LABEL", "TRANSFER"]

# Safety: first N mapped journal entries per (workspace, entity) go to
# approval queue regardless of materiality — the operator reviews them by hand
# before we trust the mapping for auto-posting.
FIRST_RUN_REVIEW_LIMIT = 30

EBAY_SOURCE = "ebay_finances"


@dataclass
class MapResult:
    """Outcome of attempting to map+post one eBay transaction."""

    outcome: Literal[
        "posted", "queued_for_confirm", "queued_first_run",
        "blocked_unknown_type", "blocked_unknown_account",
        "blocked_idempotent", "blocked_period_closed",
        "blocked_materiality", "skipped_zero_amount",
    ]
    ebay_transaction_id: str
    ebay_type: str
    journal_entry_id: UUID | None = None
    approval_id: UUID | None = None
    reason: str = ""


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _decimal_to_cents(d: str | int | float | Decimal | None) -> int:
    """Convert a Decimal-string amount to integer cents.

    eBay returns amounts as { "value": "12.34", "currency": "USD" }. We accept
    string OR numeric. Returns 0 on None / unparseable.
    """
    if d is None:
        return 0
    try:
        dec = Decimal(str(d))
    except (InvalidOperation, ValueError):
        return 0
    return int((dec * 100).quantize(Decimal("1")))


def _txn_amount_cents(txn: dict[str, Any]) -> int:
    """Pull the cent-value of an eBay transaction.

    Shape: txn["amount"] = {"value": "12.34", "currency": "USD"}. eBay sometimes
    nests in totalAmount; we try both.
    """
    amount = txn.get("amount") or txn.get("totalAmount") or {}
    if isinstance(amount, dict):
        return _decimal_to_cents(amount.get("value"))
    return _decimal_to_cents(amount)


def _payout_amount_cents(payout: dict[str, Any]) -> int:
    """Pull the cent-value of an eBay payout. amount → value."""
    amount = payout.get("amount") or {}
    if isinstance(amount, dict):
        return _decimal_to_cents(amount.get("value"))
    return _decimal_to_cents(amount)


def _parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None


def _opaque_payee(txn: dict[str, Any]) -> str:
    """Build a PII-free payee string for the journal entry.

    eBay returns buyer info in buyer.{username} on SALEs. Per safety rule
    "no PII in eBay txn -> journal mapper": we use the order id or the last
    portion of the transaction id and prefix the txn type.
    """
    ebay_type = txn.get("transactionType") or "SALE"
    order_id = (txn.get("orderId") or "").strip()
    txn_id = (txn.get("transactionId") or "").strip()
    if order_id:
        short = order_id[-8:]
        return f"eBay {ebay_type} · order #{short}"
    if txn_id:
        short = txn_id[-8:]
        return f"eBay {ebay_type} · txn #{short}"
    return f"eBay {ebay_type}"


def _opaque_description(txn: dict[str, Any]) -> str:
    """Description carries the eBay-provided memo / fee type code if any."""
    ebay_type = txn.get("transactionType") or "SALE"
    fee_type = (txn.get("feeType") or "").strip()
    detail = (txn.get("transactionMemo") or "").strip()
    bits = [f"eBay {ebay_type}"]
    if fee_type:
        bits.append(f"feeType={fee_type}")
    if detail:
        # Cap to 96 chars — descriptions land in journal_entry.description which
        # is narrow and the receipt-side already has structured data.
        bits.append(detail[:96])
    return " · ".join(bits)


def _payout_description(payout: dict[str, Any]) -> str:
    pid = (payout.get("payoutId") or "").strip()
    short = pid[-8:] if pid else "?"
    status = payout.get("payoutStatus") or "SUCCEEDED"
    return f"eBay PAYOUT #{short} · status {status}"


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
    session: AsyncSession, *, entity_id: str, external_account_id: str | None
) -> tuple[str, str] | None:
    """Pick (account_number, canonical_form) of the cash account a PAYOUT lands in.

    Prefers a COA account that is linked to ``external_account_id`` (i.e.
    the operator's bank via SimpleFIN). Falls back to the default 1010 entry if no
    linked account exists.
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


async def _count_existing_ebay_posts(
    session: AsyncSession, *, entity_id: str
) -> int:
    """How many ebay_finances entries already exist for this entity?"""
    r = (
        await session.execute(
            text(
                """
                SELECT count(*) AS c FROM journal_entry
                WHERE entity_id = :eid AND source = :src
                """
            ),
            {"eid": entity_id, "src": EBAY_SOURCE},
        )
    ).first()
    return int(r.c or 0) if r else 0


async def _enqueue_for_review(
    session: AsyncSession,
    *,
    workspace_id: str,
    entity_id: str,
    ebay_transaction_id: str,
    ebay_type: str,
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
    eBay txn isn't yet in the journal.
    """
    approval_id = uuid4()
    subject_id = uuid4()
    payload = {
        "action": "ebay_post_transaction",
        "ebay_transaction_id": ebay_transaction_id,
        "ebay_type": ebay_type,
        "amount_cents": amount_cents,
        "debit_account": debit_account,
        "credit_account": credit_account,
        "effective_date": effective_date.isoformat(),
        "payee": payee,
        "description": description,
        "raw_txn_keys": sorted([k for k in raw_txn.keys() if k != "buyer"]),
    }
    await session.execute(
        text(
            """
            INSERT INTO approval_queue (
                id, workspace_id, entity_id, subject_type, subject_id,
                amount, currency_code, usd_equivalent_at_event_time,
                status, reasoning, requested_by_identity, payload
            ) VALUES (
                :id, :wid, :eid, 'ebay_post_transaction', :subject,
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


async def map_and_post_transaction(
    session: AsyncSession,
    *,
    workspace_id: str,
    entity_id: str,
    txn: dict[str, Any],
    actor_uc_uid: str,
    actor_agent_id: str | None = None,
) -> MapResult:
    """Map ONE eBay transaction to a journal entry and post it.

    `txn` is the eBay /transaction response item (or /payout item promoted
    to type='PAYOUT'). The caller has already loaded credentials + paginated
    the API; we do nothing but classify + post.

    Returns a MapResult; never raises on routine refusals (unknown type,
    materiality block, idempotency conflict). Raises only on schema/DB errors.
    """
    ebay_id = (txn.get("transactionId") or txn.get("payoutId") or "").strip()
    ebay_type = (txn.get("transactionType") or txn.get("_type") or "").strip().upper()
    if not ebay_id or not ebay_type:
        return MapResult(
            outcome="blocked_unknown_type",
            ebay_transaction_id=ebay_id or "?",
            ebay_type=ebay_type or "?",
            reason="Missing transactionId or transactionType",
        )

    amount_cents = _txn_amount_cents(txn)
    if ebay_type == "PAYOUT":
        amount_cents = _payout_amount_cents(txn)
    if amount_cents == 0:
        return MapResult(
            outcome="skipped_zero_amount",
            ebay_transaction_id=ebay_id,
            ebay_type=ebay_type,
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
            {"eid": entity_id, "src": EBAY_SOURCE, "ext": ebay_id},
        )
    ).first()
    if existing:
        return MapResult(
            outcome="blocked_idempotent",
            ebay_transaction_id=ebay_id,
            ebay_type=ebay_type,
            journal_entry_id=UUID(existing.id),
            reason="Already posted",
        )

    # ---- 2. Resolve accounts per type ----
    # Cache: receivable used for every SALE/REFUND/FEE side.
    payouts_receivable_canonical = await _resolve_canonical(
        session, entity_id=entity_id, account_number=ACCT_PAYOUTS_RECEIVABLE
    )
    revenue_canonical = await _resolve_canonical(
        session, entity_id=entity_id, account_number=ACCT_EBAY_REVENUE
    )
    fees_canonical = await _resolve_canonical(
        session, entity_id=entity_id, account_number=ACCT_PROCESSOR_FEES
    )

    if ebay_type == "SALE":
        if not (payouts_receivable_canonical and revenue_canonical):
            return MapResult(
                outcome="blocked_unknown_account",
                ebay_transaction_id=ebay_id,
                ebay_type=ebay_type,
                reason=(
                    f"Missing COA accounts: need {ACCT_PAYOUTS_RECEIVABLE} "
                    f"(eBay Payouts Receivable) and {ACCT_EBAY_REVENUE} "
                    "(eBay Resale Revenue). Re-run "
                    "scripts/extend_coa_mu_inc.sql or the COA seeder."
                ),
            )
        debit_canonical = payouts_receivable_canonical
        credit_canonical = revenue_canonical
        category = "Revenue · eBay Resale"
    elif ebay_type == "REFUND":
        if not (payouts_receivable_canonical and revenue_canonical):
            return MapResult(
                outcome="blocked_unknown_account",
                ebay_transaction_id=ebay_id,
                ebay_type=ebay_type,
                reason="Missing COA accounts (1220 / 4150) for REFUND",
            )
        debit_canonical = revenue_canonical
        credit_canonical = payouts_receivable_canonical
        category = "Revenue · eBay Resale (REFUND)"
    elif ebay_type in ("FEE", "NON_SALE_CHARGE", "DISPUTE", "SHIPPING_LABEL"):
        if not (fees_canonical and payouts_receivable_canonical):
            return MapResult(
                outcome="blocked_unknown_account",
                ebay_transaction_id=ebay_id,
                ebay_type=ebay_type,
                reason=(
                    f"Missing COA accounts: need {ACCT_PROCESSOR_FEES} "
                    f"and {ACCT_PAYOUTS_RECEIVABLE} for {ebay_type}"
                ),
            )
        debit_canonical = fees_canonical
        credit_canonical = payouts_receivable_canonical
        category = "Banking · Fees"
    elif ebay_type == "CREDIT":
        # eBay credit BACK to the seller. Reverse of a fee.
        if not (fees_canonical and payouts_receivable_canonical):
            return MapResult(
                outcome="blocked_unknown_account",
                ebay_transaction_id=ebay_id,
                ebay_type=ebay_type,
                reason="Missing COA accounts for CREDIT",
            )
        debit_canonical = payouts_receivable_canonical
        credit_canonical = fees_canonical
        category = "Banking · Fees (CREDIT)"
    elif ebay_type == "PAYOUT":
        # PAYOUT is special — debit cash (BoA), credit 1220 receivable.
        # The cash account is whichever 1010-class account is mapped to the
        # eBay payoutInstrument's external_account_id. Falls back to 1010 default.
        ext_id = None
        payout_instrument = txn.get("payoutInstrument") or {}
        if isinstance(payout_instrument, dict):
            ext_id = payout_instrument.get("accountLastFourDigits") or payout_instrument.get("instrumentId")
        cash = await _resolve_cash_for_payout(
            session, entity_id=entity_id, external_account_id=ext_id
        )
        if not cash or not payouts_receivable_canonical:
            return MapResult(
                outcome="blocked_unknown_account",
                ebay_transaction_id=ebay_id,
                ebay_type=ebay_type,
                reason=(
                    f"Missing COA accounts: need a cash account "
                    f"(default {ACCT_CASH_PRIMARY_DEFAULT}) and "
                    f"{ACCT_PAYOUTS_RECEIVABLE}"
                ),
            )
        cash_number, cash_canonical = cash
        debit_canonical = cash_canonical
        credit_canonical = payouts_receivable_canonical
        category = "eBay Payout Settlement"
    else:
        # TRANSFER and anything unknown — flag for review, do NOT auto-post.
        return MapResult(
            outcome="blocked_unknown_type",
            ebay_transaction_id=ebay_id,
            ebay_type=ebay_type,
            reason=f"Unhandled eBay transaction type: {ebay_type!r}",
        )

    # ---- 3. Effective date ----
    eff_date_str = (
        txn.get("transactionDate")
        or txn.get("payoutDate")
        or txn.get("transactionCreationDate")
    )
    eff_date = _parse_iso_date(eff_date_str) or date.today()

    payee = _opaque_payee(txn) if ebay_type != "PAYOUT" else "eBay payout"
    description = (
        _payout_description(txn) if ebay_type == "PAYOUT" else _opaque_description(txn)
    )
    memo = None
    if ebay_type == "PAYOUT":
        memo = f"payoutId={txn.get('payoutId')}"
    else:
        order_id = txn.get("orderId") or ""
        if order_id:
            memo = f"orderId={order_id}"

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
            source=EBAY_SOURCE, source_external_id=ebay_id,
            outcome="blocked_period_closed", journal_entry_id=None,
            reason=reason, actor_uc_uid=actor_uc_uid, actor_agent_id=actor_agent_id,
            amount_cents=amount_cents, category=category,
        )
        return MapResult(
            outcome="blocked_period_closed",
            ebay_transaction_id=ebay_id,
            ebay_type=ebay_type,
            reason=reason,
        )

    # ---- 5. First-run review guard ----
    # The first 30 eBay transactions for this entity ALWAYS queue regardless
    # of materiality. The operator eyeballs them and approves; after that we trust
    # the mapper.
    existing_ebay_count = await _count_existing_ebay_posts(session, entity_id=entity_id)
    if existing_ebay_count < FIRST_RUN_REVIEW_LIMIT:
        approval_id = await _enqueue_for_review(
            session,
            workspace_id=workspace_id,
            entity_id=entity_id,
            ebay_transaction_id=ebay_id,
            ebay_type=ebay_type,
            amount_cents=amount_cents,
            debit_account=debit_canonical,
            credit_account=credit_canonical,
            effective_date=eff_date,
            payee=payee,
            description=description,
            raw_txn=txn,
            actor_uc_uid=actor_uc_uid,
            reason=(
                f"First-{FIRST_RUN_REVIEW_LIMIT}-rows review guard: ebay txn "
                f"#{existing_ebay_count + 1} for this entity needs human eyes "
                "before auto-posting trust is established."
            ),
        )
        await _log_post_attempt(
            session, workspace_id=workspace_id, entity_id=entity_id,
            source=EBAY_SOURCE, source_external_id=ebay_id,
            outcome="queued_for_confirm", journal_entry_id=None,
            reason=f"First-{FIRST_RUN_REVIEW_LIMIT} review guard",
            actor_uc_uid=actor_uc_uid, actor_agent_id=actor_agent_id,
            amount_cents=amount_cents, category=category,
        )
        return MapResult(
            outcome="queued_first_run",
            ebay_transaction_id=ebay_id,
            ebay_type=ebay_type,
            approval_id=approval_id,
            reason="First-run review guard",
        )

    # ---- 6. Materiality gate ----
    # eBay is "trusted by definition" (it's a 1st-party API, double-entry by
    # construction). We pass category_status='auto', entity_mapped=True,
    # sign_matched=True so the gate's only real concern is the tier threshold
    # (and §274(d) — which eBay categories never hit).
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
        # Queue (M tier) or block (H tier). For eBay specifically we treat
        # M/H as approval queue items too.
        approval_id = await _enqueue_for_review(
            session,
            workspace_id=workspace_id, entity_id=entity_id,
            ebay_transaction_id=ebay_id, ebay_type=ebay_type,
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
            source=EBAY_SOURCE, source_external_id=ebay_id,
            outcome=outcome_map.get(verdict.outcome, "queued_for_confirm"),
            journal_entry_id=None, reason=verdict.reason,
            actor_uc_uid=actor_uc_uid, actor_agent_id=actor_agent_id,
            amount_cents=amount_cents, category=category,
        )
        return MapResult(
            outcome=outcome_map.get(verdict.outcome, "queued_for_confirm"),  # type: ignore[arg-type]
            ebay_transaction_id=ebay_id,
            ebay_type=ebay_type,
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
        source=EBAY_SOURCE,
        source_external_id=ebay_id,
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
                source=EBAY_SOURCE,
                source_external_id=ebay_id,
                description=description,
                payee=payee,
                memo=memo,
                prev_hash=prev_hash,
                this_hash=this_hash,
                reasoning_trace=(
                    f"ebay_finances mapper: type={ebay_type} "
                    f"amount_cents={amount_cents} "
                    f"category={category}"
                ),
                model_version="ebay_mapper:v1",
                identity=actor_uc_uid,
                approving_uid=None,
                receipt_link_id=None,
            )
    except IntegrityError as exc:
        # Either chain race OR duplicate (entity, source, source_external_id).
        # Treat as idempotent — a second posting attempt of the same txn id.
        await _log_post_attempt(
            session, workspace_id=workspace_id, entity_id=entity_id,
            source=EBAY_SOURCE, source_external_id=ebay_id,
            outcome="blocked_idempotent", journal_entry_id=None,
            reason=f"chain race / duplicate: {exc.__class__.__name__}",
            actor_uc_uid=actor_uc_uid, actor_agent_id=actor_agent_id,
            amount_cents=amount_cents, category=category,
        )
        return MapResult(
            outcome="blocked_idempotent",
            ebay_transaction_id=ebay_id,
            ebay_type=ebay_type,
            reason="Concurrent post / duplicate",
        )

    await _log_post_attempt(
        session, workspace_id=workspace_id, entity_id=entity_id,
        source=EBAY_SOURCE, source_external_id=ebay_id,
        outcome="posted", journal_entry_id=je_id, reason=verdict.reason,
        actor_uc_uid=actor_uc_uid, actor_agent_id=actor_agent_id,
        amount_cents=amount_cents, category=category,
    )
    return MapResult(
        outcome="posted",
        ebay_transaction_id=ebay_id,
        ebay_type=ebay_type,
        journal_entry_id=je_id,
        reason=f"posted as {category}",
    )


def normalize_payout_as_txn(payout: dict[str, Any]) -> dict[str, Any]:
    """Turn an eBay /payout item into the shape map_and_post_transaction expects.

    Adds transactionId (= payoutId), transactionType='PAYOUT', and copies
    amount/payoutInstrument across.
    """
    return {
        "transactionId": payout.get("payoutId"),
        "transactionType": "PAYOUT",
        "amount": payout.get("amount"),
        "payoutDate": payout.get("payoutDate"),
        "payoutStatus": payout.get("payoutStatus"),
        "payoutInstrument": payout.get("payoutInstrument"),
        "_raw_payout": True,
        "payoutId": payout.get("payoutId"),
    }


__all__ = (
    "MapResult",
    "EBAY_SOURCE",
    "FIRST_RUN_REVIEW_LIMIT",
    "map_and_post_transaction",
    "normalize_payout_as_txn",
)
