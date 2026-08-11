"""Bank-Statement Reconstruction — BofA statement ingest script.

PLATFORM FEATURE (not a bespoke one-off script)
================================================
This script is the *first instance* of a reusable "Bank-Statement Reconstruction"
platform feature. A future customer can take their bank statements (any bank,
parsed to the same JSON shape), set their entity IDs, point at their own LLM
endpoint, and reconstruct their books exactly the same way. Pass your own
workspace/entity IDs and account last-4 via the CLI flags below.

To adapt for a different customer:
  --workspace-id    their workspace UUID
  --entity-id       their entity UUID
  --source-account-last4  their account's last-4 suffix
  --input-dir       directory of bofa_<last4>_<period_end>.json files

What this script does
---------------------
For each transaction in each statement (in chronological order):

  1.  CATEGORIZE — Tier 1 vendor rules (deterministic); falls back to
      Tier 2 LLM (async, pooled to --max-llm-concurrency) when Tier 1 returns
      Uncategorized and --use-llm-fallback is on.

  2.  MATERIALITY gate via app.journal.materiality.evaluate():
        L < $75    → potential auto_post
        M $75-$1k  → queue_for_confirm
        H > $1k    → block_high_value (needs explicit confirm)

  3.  DECIDE + ACT (live mode only):
        auto_post             → post_journal_entry()
        queue_for_confirm     → INSERT into approval_queue
        block_high_value      → INSERT into approval_queue (human reviews)
        block_274d            → INSERT into approval_queue (needs receipt)
        blocked_unknown/other → log + skip

  4.  AUDIT — write_decision_audit() row per transaction decision.

  5.  RECONCILE — after each statement, compare the per-period journal cash
      delta against the statement's (ending_balance − beginning_balance),
      accounting for queued/blocked transactions that did NOT post.

      Design choice (a) — per-period delta comparison:
        journal_delta = sum(signed amount_cents) of all posted entries in
                        the period's date range for the cash account (1010).
        queued_drift  = sum(signed amount_cents) for plans with
                        proposed_action != 'would_post' in this period.
        expected_stmt_delta = (ending_balance − beginning_balance) in cents.
        Pass iff journal_delta == expected_stmt_delta − queued_drift
              (i.e., journal posted exactly the transactions that cleared
              materiality; the rest sit in approval_queue awaiting humans).
        Halt only on a *real* drift (journal_delta ≠ expected).

      This is cleaner than the alternative (require 100% queue-clearance before
      advancing to the next year and re-anchoring reconciliation from zero),
      because it doesn't force artificial sequencing between years and handles
      multi-year runs naturally as long as each period's queued set is tracked.

Idempotency
-----------
source_external_id is deterministic:
    bofa-<last4>:<period_end>:<idx:03d>:<date>:<amount_cents:+d>:<rb_cents>

The platform's (entity_id, source, source_external_id) UNIQUE constraint
catches any duplicate on re-run → script logs and continues.

Approval-queue idempotency: before inserting an approval_queue row the script
pre-checks for an existing row with the same (entity_id, source_external_id) in
the payload (SELECT … WHERE payload->>'source_external_id' = …).  If one exists,
the row is skipped.  A future migration should add a UNIQUE constraint on
(entity_id, payload->>'source_external_id') via a partial functional index to
enforce this at the DB level (tracked as follow-up: approval_queue dedup index).

Approval-queue payload schema
------------------------------
Rows inserted for queue/block cases carry:

    {
      "action": "bank_transaction_import",
      "source_external_id": "bofa-1792:...",
      "effective_date": "YYYY-MM-DD",
      "amount_cents": -4999,
      "category": "Uncategorized",
      "category_status": "unknown",
      "categorization_source": "default",
      "debit_account_hint": null,
      "credit_account_hint": null,
      "materiality_tier": "L",
      "block_reason": "uncategorized",
      "llm_suggested": false
    }

The approve→apply workflow picks up "bank_transaction_import" entries and,
on APPROVE, calls post_journal_entry() directly. DO NOT add a parallel
"approve" path here — reuse the existing endpoint.

Usage
-----
Dry-run (safe, default):
    python3 scripts/ingest_bofa_live.py --year 2022

Live run (lead only — reads the header carefully first):
    python3 scripts/ingest_bofa_live.py --year 2022 --live

See --help for all flags.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, asdict
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import UUID, uuid4

# ---------------------------------------------------------------------------
# Make the worktree app/ importable when invoked as a top-level script.
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
WORKTREE_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(WORKTREE_ROOT))

# ---------------------------------------------------------------------------
# Platform modules (pure / no DB for categorization+materiality)
# ---------------------------------------------------------------------------
from app.categorization.engine import categorize_transaction_async  # noqa: E402
from app.categorization.llm import LLMCategorizer  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.journal.materiality import evaluate  # noqa: E402
from app.journal.posting import (  # noqa: E402
    CATEGORY_TO_ACCOUNT,
    post_journal_entry,
    PostingOutcome,
    PostingRefusal,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("ingest_bofa")

# ---------------------------------------------------------------------------
# Constants — placeholder defaults (override via CLI flags or env vars)
# ---------------------------------------------------------------------------

DEFAULT_WORKSPACE_ID = os.getenv("AO_WORKSPACE_ID", "00000000-0000-0000-0000-000000000000")
DEFAULT_ENTITY_ID = os.getenv("AO_ENTITY_ID", "00000000-0000-0000-0000-000000000000")
DEFAULT_LAST4 = os.getenv("AO_ACCOUNT_LAST4", "0000")
DEFAULT_INPUT_DIR = os.getenv("AO_INPUT_DIR", "./out")

# COA cash account for the primary checking account being reconstructed.
COA_CASH_CANONICAL = "1010·Cash · Primary Checking"
COA_CASH_LINKED_SOURCE = "simplefin"
COA_CASH_LINKED_EXT_ID = "ACT-7066851a-cf7a-41d5-aac2-a9d435c19c47"

SOURCE_LABEL = "bofa-statement"

# Agent identity recorded in journal rows / approval_queue rows during bulk ingest.
INGEST_AGENT_ID = "ingest_bofa_live.py"
INGEST_UC_UID = "system:ingest_bofa"   # no real Keycloak uid for a batch job


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_cents(amount_str: str | None) -> int:
    if amount_str is None or amount_str == "":
        return 0
    try:
        d = Decimal(str(amount_str))
    except (InvalidOperation, ValueError):
        return 0
    return int((d * 100).to_integral_value())


def _fmt_cents(c: int) -> str:
    return f"${abs(c) / 100:,.2f}"


def _build_sxid(*, last4: str, period_end: str, idx: int, txn_date: str,
                 amount_cents: int, rb_cents: int) -> str:
    """Deterministic source_external_id — mirrors the dry-run agent's pattern."""
    return (
        f"bofa-{last4}:{period_end}:{idx:03d}"
        f":{txn_date}:{amount_cents:+d}:{rb_cents}"
    )


def _load_statements(input_dir: Path, last4: str, year: int | None) -> list[dict]:
    """Load and sort all bofa_<last4>_*.json files from input_dir.

    Returns list of statement dicts, sorted by statement_period_end.
    If year is given, only statements fully within that calendar year are
    included (period_end year == year).
    """
    pattern = f"bofa_{last4}_*.json"
    paths = sorted(input_dir.glob(pattern))
    if not paths:
        raise SystemExit(f"No {pattern} files found under {input_dir}")

    stmts = []
    for p in paths:
        with p.open() as f:
            s = json.load(f)
        if year is not None:
            period_end = s.get("statement_period_end", "")
            try:
                pe_year = int(period_end[:4])
            except (ValueError, TypeError):
                pe_year = 0
            if pe_year != year:
                continue
        stmts.append(s)

    stmts.sort(key=lambda s: s["statement_period_end"])
    return stmts


# ---------------------------------------------------------------------------
# Per-transaction processing (pure / no DB)
# ---------------------------------------------------------------------------


@dataclass
class TxnPlan:
    """Planning output for one transaction — what the live runner will do."""
    source_external_id: str
    effective_date: str
    description: str
    amount_cents: int          # signed
    amount_cents_abs: int
    category: str
    category_status: str       # auto | suggest | unknown
    categorization_source: str
    categorization_reason: str
    sign_matched: bool
    materiality_tier: str
    materiality_outcome: str
    materiality_reason: str
    proposed_action: str       # would_post | would_queue | would_block
    block_reason: str | None
    llm_suggested: bool
    llm_duration_ms: int | None
    # approval_queue payload (None if proposed_action == would_post)
    approval_payload: dict | None


async def plan_txn(
    *,
    txn: dict,
    stmt_period_end: str,
    idx: int,
    last4: str,
    llm: LLMCategorizer | None,
    llm_semaphore: asyncio.Semaphore | None,
) -> TxnPlan:
    """Build the plan for one transaction (async so LLM call can be awaited)."""
    txn_date = txn["date"]
    amount_str = txn["amount"]
    description = txn.get("description", "") or ""
    rb_str = txn.get("running_balance", "0")

    amount_cents = _to_cents(amount_str)
    rb_cents = _to_cents(rb_str)
    amount_float = float(Decimal(amount_str))

    sxid = _build_sxid(
        last4=last4,
        period_end=stmt_period_end,
        idx=idx,
        txn_date=txn_date,
        amount_cents=amount_cents,
        rb_cents=rb_cents,
    )

    # ---- Tier-1 + optional Tier-2 categorization ----
    llm_suggested = False
    llm_duration_ms = None

    if llm is not None and llm_semaphore is not None:
        # Rate-limit concurrent LLM calls via the semaphore.
        async with llm_semaphore:
            t0 = time.monotonic()
            suggestion = await categorize_transaction_async(
                payee=None,
                description=description,
                memo=None,
                amount=amount_float,
                overrides=None,
                llm_fallback=llm,
                allowed_categories=list(CATEGORY_TO_ACCOUNT.keys()),
            )
            elapsed = time.monotonic() - t0
        if suggestion.source.startswith("llm:"):
            llm_suggested = True
            llm_duration_ms = int(elapsed * 1000)
    else:
        suggestion = await categorize_transaction_async(
            payee=None,
            description=description,
            memo=None,
            amount=amount_float,
            overrides=None,
            llm_fallback=None,
        )

    # ---- Materiality gate ----
    verdict = evaluate(
        amount_cents=amount_cents,
        category=suggestion.category,
        category_status=suggestion.status,
        entity_mapped=True,
        sign_matched=suggestion.sign_matched,
        has_receipt_link=False,
        user_confirmed=False,
    )

    # ---- Determine proposed action ----
    category_known = suggestion.category in CATEGORY_TO_ACCOUNT
    if not category_known:
        proposed_action = "would_block"
        block_reason = "category_not_in_CATEGORY_TO_ACCOUNT"
    elif verdict.outcome == "auto_post":
        proposed_action = "would_post"
        block_reason = None
    else:
        proposed_action = "would_queue"
        block_reason = verdict.outcome

    # ---- Build approval_queue payload for queued/blocked cases ----
    approval_payload: dict | None = None
    if proposed_action != "would_post":
        # Compute hint debit/credit based on sign (same logic as posting.py).
        cat_num = CATEGORY_TO_ACCOUNT.get(suggestion.category)
        if cat_num and amount_cents < 0:
            debit_hint = f"{cat_num}·<resolve-at-approve>"
            credit_hint = COA_CASH_CANONICAL
        elif cat_num and amount_cents > 0:
            debit_hint = COA_CASH_CANONICAL
            credit_hint = f"{cat_num}·<resolve-at-approve>"
        else:
            debit_hint = None
            credit_hint = None

        approval_payload = {
            "action": "bank_statement_import",
            "source_external_id": sxid,
            "effective_date": txn_date,
            "amount_cents": amount_cents,
            # Carry the full surface so the approve handler has what it needs
            # without falling back to a COA lookup, AND so the human approver
            # in /approvals can see what they're actually approving.
            "description": description,
            "payee": None,            # BofA eStatements don't break out a payee
            "memo": None,             # BofA eStatements don't carry a memo
            "external_account_id": COA_CASH_LINKED_EXT_ID,
            "category": suggestion.category,
            "category_status": suggestion.status,
            "categorization_source": suggestion.source,
            "sign_matched": suggestion.sign_matched,
            "debit_account_hint": debit_hint,
            "credit_account_hint": credit_hint,
            "materiality_tier": verdict.tier,
            "materiality_outcome": verdict.outcome,
            "block_reason": block_reason,
            "llm_suggested": llm_suggested,
        }

    return TxnPlan(
        source_external_id=sxid,
        effective_date=txn_date,
        description=description,
        amount_cents=amount_cents,
        amount_cents_abs=abs(amount_cents),
        category=suggestion.category,
        category_status=suggestion.status,
        categorization_source=suggestion.source,
        categorization_reason=suggestion.reason,
        sign_matched=suggestion.sign_matched,
        materiality_tier=verdict.tier,
        materiality_outcome=verdict.outcome,
        materiality_reason=verdict.reason,
        proposed_action=proposed_action,
        block_reason=block_reason,
        llm_suggested=llm_suggested,
        llm_duration_ms=llm_duration_ms,
        approval_payload=approval_payload,
    )


# ---------------------------------------------------------------------------
# Per-statement planning (async fan-out)
# ---------------------------------------------------------------------------


async def plan_statement(
    stmt: dict,
    *,
    last4: str,
    llm: LLMCategorizer | None,
    llm_semaphore: asyncio.Semaphore | None,
) -> list[TxnPlan]:
    """Plan all transactions in one statement concurrently (LLM calls share semaphore)."""
    period_end = stmt["statement_period_end"]
    txns = stmt.get("transactions", [])
    tasks = [
        plan_txn(
            txn=txn,
            stmt_period_end=period_end,
            idx=idx,
            last4=last4,
            llm=llm,
            llm_semaphore=llm_semaphore,
        )
        for idx, txn in enumerate(txns)
    ]
    return list(await asyncio.gather(*tasks))


# ---------------------------------------------------------------------------
# Live DB actions (require a real async session + write access)
# ---------------------------------------------------------------------------

async def _get_db_engine(workspace_id: str):
    """Create the async engine for the live run.

    Returns (engine,).  The session is managed per-transaction via
    workspace_session() so the GUC name (app.current_workspace) and the schema
    format (accounting_ops_<uuid_underscored>) are always the platform's own
    logic — this script can't drift from it.

    Imported lazily so --dry-run mode works without a live Postgres connection.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False)
    return engine


async def _insert_approval_queue_row(
    session,
    *,
    workspace_id: str,
    entity_id: str,
    plan: TxnPlan,
    amount_cents: int,
) -> UUID | None:
    """Insert one approval_queue row for a queued/blocked transaction.

    Returns the new approval_queue UUID, or None if a row for this
    source_external_id already exists (idempotent skip — no DB write).

    subject_id is a freshly generated UUID per row (the column is sa.UUID() in
    the schema).  The deterministic source_external_id lives in
    payload->>'source_external_id' so the approve handler can locate the
    proposed entry.

    Pre-check idempotency via SELECT before INSERT (no migration needed).
    Follow-up: add a UNIQUE index on (entity_id, (payload->>'source_external_id'))
    to enforce this at the DB level.
    """
    from sqlalchemy import text

    # Idempotency pre-check: skip if a row for this source_external_id already exists.
    existing = (
        await session.execute(
            text(
                """
                SELECT id FROM approval_queue
                WHERE entity_id = :eid
                  AND payload->>'source_external_id' = :sxid
                LIMIT 1
                """
            ),
            {"eid": entity_id, "sxid": plan.source_external_id},
        )
    ).first()
    if existing is not None:
        log.debug(
            "Approval_queue row already exists for %s — skipping",
            plan.source_external_id,
        )
        return None

    # subject_id is a generated UUID (the column is UUID type); the deterministic
    # source_external_id is stored in payload->>'source_external_id'.
    approval_id = uuid4()
    amount_decimal = Decimal(abs(amount_cents)) / Decimal(100)
    await session.execute(
        text(
            """
            INSERT INTO approval_queue (
                id, workspace_id, entity_id, subject_type, subject_id,
                amount, currency_code, usd_equivalent_at_event_time,
                status, reasoning, requested_by_identity, payload
            ) VALUES (
                :id, :wid, :eid, 'bank_transaction_import', :subject,
                :amount, 'USD', :amount,
                'pending', :reasoning, :identity, CAST(:payload AS jsonb)
            )
            """
        ),
        {
            "id": str(approval_id),
            "wid": workspace_id,
            "eid": entity_id,
            "subject": str(uuid4()),  # UUID per row; source_external_id lives in payload
            "amount": amount_decimal,
            "reasoning": (
                f"BofA statement import: {plan.effective_date} "
                f"category={plan.category!r} status={plan.category_status} "
                f"materiality={plan.materiality_tier} "
                f"reason={plan.materiality_reason}"
            ),
            "identity": INGEST_AGENT_ID,
            "payload": json.dumps(plan.approval_payload),
        },
    )
    return approval_id


async def _write_audit(
    session,
    *,
    workspace_id: str,
    entity_id: str,
    plan: TxnPlan,
    outcome: str,
    response_extra: dict | None = None,
) -> None:
    """Write one decision_audit row via the platform's write_decision_audit."""
    from sqlalchemy import text
    from uuid import uuid4

    audit_id = uuid4()
    request_payload = {
        "source_external_id": plan.source_external_id,
        "effective_date": plan.effective_date,
        "amount_cents": plan.amount_cents,
        "category": plan.category,
        "category_status": plan.category_status,
        "categorization_source": plan.categorization_source,
        "materiality_tier": plan.materiality_tier,
    }
    response_payload = {
        "proposed_action": plan.proposed_action,
        "block_reason": plan.block_reason,
        "llm_suggested": plan.llm_suggested,
        **(response_extra or {}),
    }
    await session.execute(
        text(
            """
            INSERT INTO decision_audit (
                id, workspace_id, entity_id, agent_session_id,
                tool_name, request_payload, response_payload,
                reasoning_trace, source_document_id, model_version, outcome
            ) VALUES (
                :id, :wid, :eid, NULL,
                'ingest_bofa_live', CAST(:req AS jsonb), CAST(:resp AS jsonb),
                :reasoning, NULL, :model_version, :outcome
            )
            """
        ),
        {
            "id": str(audit_id),
            "wid": workspace_id,
            "eid": entity_id,
            "req": json.dumps(request_payload),
            "resp": json.dumps(response_payload),
            "reasoning": plan.categorization_reason,
            "model_version": plan.categorization_source,
            "outcome": outcome,
        },
    )


async def _query_cash_period_delta(
    session, *, entity_id: str, period_start: str, period_end: str
) -> int | None:
    """Query the journal's cash delta (DR − CR on the 1010 account) for a period.

    Returns the delta in cents for the half-open range [period_start, period_end],
    or None if no posted journal entries exist in the range.

    Per-period delta is used for reconciliation (design choice a — see module
    docstring) so that multi-year runs don't require an ever-growing cumulative
    balance to be accurate.
    """
    from sqlalchemy import text

    # asyncpg requires date objects (not strings) for date-typed columns; accept either.
    if isinstance(period_start, str):
        period_start = date.fromisoformat(period_start)
    if isinstance(period_end, str):
        period_end = date.fromisoformat(period_end)

    # The cash account is on the DEBIT side for inflows (DR cash, CR revenue)
    # and on the CREDIT side for outflows (DR expense, CR cash).
    # Delta = sum(amount WHERE debit LIKE '1010%') − sum(amount WHERE credit LIKE '1010%')
    # expressed in cents; excludes reversal/recategorization source rows (those are
    # category-side corrections, not new cash flows).
    result = (
        await session.execute(
            text(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN debit_account LIKE '1010%%' THEN amount ELSE 0 END), 0)
                  - COALESCE(SUM(CASE WHEN credit_account LIKE '1010%%' THEN amount ELSE 0 END), 0)
                    AS period_delta
                FROM journal_entry
                WHERE entity_id = :eid
                  AND effective_date >= :period_start
                  AND effective_date <= :period_end
                  AND status = 'posted'
                  AND source NOT IN ('reversal', 'recategorization')
                """
            ),
            {"eid": entity_id, "period_start": period_start, "period_end": period_end},
        )
    ).first()

    if result is None:
        return None
    net = result[0]
    if net is None:
        return None
    return int((Decimal(str(net)) * 100).to_integral_value())


# ---------------------------------------------------------------------------
# Statement-level live apply
# ---------------------------------------------------------------------------


@dataclass
class StatementResult:
    period_start: str
    period_end: str
    txn_count: int
    posted: int
    queued: int
    blocked: int
    skipped_idempotent: int
    recon_expected_delta_cents: int   # eb − bb from the bank statement
    recon_queued_drift_cents: int     # sum of signed amount_cents for queued/blocked plans
    recon_journal_delta_cents: int | None   # None if no journal data yet
    recon_real_drift_cents: int | None      # None if no journal data; 0 = clean
    llm_call_count: int
    llm_total_ms: int


async def apply_statement(
    stmt: dict,
    plans: list[TxnPlan],
    *,
    workspace_id: str,
    entity_id: str,
    live: bool,
    session,
) -> StatementResult:
    """Apply all planned transactions for one statement (live mode) or simulate (dry-run).

    Reconciliation uses per-period delta comparison (design choice a):
      journal_delta  = sum of signed cash-account net for posted entries in this period.
      queued_drift   = sum of signed amount_cents for plans that did NOT post
                       (queued + blocked).  The journal is EXPECTED to be short by
                       exactly this amount.
      expected_delta = (ending_balance − beginning_balance) from the bank statement.
      Real drift     = journal_delta − (expected_delta − queued_drift).
                     = 0 when everything is consistent.
      Halt only when real_drift ≠ 0.
    """
    period_start = stmt["statement_period_start"]
    period_end = stmt["statement_period_end"]
    bb_cents = _to_cents(stmt["beginning_balance"])
    eb_cents = _to_cents(stmt["ending_balance"])
    expected_delta = eb_cents - bb_cents

    posted = queued = blocked = skipped_idempotent = 0
    llm_call_count = sum(1 for p in plans if p.llm_suggested)
    llm_total_ms = sum(p.llm_duration_ms or 0 for p in plans if p.llm_suggested)

    # Track the sum of signed amount_cents for plans that do NOT post, so
    # reconciliation knows the expected shortfall in the journal.
    queued_drift_cents = 0

    for plan in plans:
        if not live:
            # Dry-run: just count.
            if plan.proposed_action == "would_post":
                posted += 1
            elif plan.proposed_action == "would_queue":
                queued += 1
                queued_drift_cents += plan.amount_cents
            else:
                blocked += 1
                queued_drift_cents += plan.amount_cents
            continue

        # ---- Live: attempt to post or queue ----
        if plan.proposed_action == "would_post":
            result = await post_journal_entry(
                session,
                workspace_id=workspace_id,
                entity_id=entity_id,
                source=SOURCE_LABEL,
                source_external_id=plan.source_external_id,
                effective_date=date.fromisoformat(plan.effective_date),
                amount_cents=plan.amount_cents,
                description=plan.description,
                payee=None,
                memo=None,
                category=plan.category,
                category_status=plan.category_status,
                entity_mapped=True,
                sign_matched=plan.sign_matched,
                external_account_id=COA_CASH_LINKED_EXT_ID,
                has_receipt_link=False,
                receipt_link_id=None,
                posted_by_uc_uid=INGEST_UC_UID,
                posted_by_agent_id=INGEST_AGENT_ID,
                user_confirmed=False,
                reasoning_trace=plan.categorization_reason,
                model_version=plan.categorization_source,
            )

            if isinstance(result, PostingOutcome):
                posted += 1
                await _write_audit(
                    session, workspace_id=workspace_id, entity_id=entity_id,
                    plan=plan, outcome="posted",
                    response_extra={"journal_entry_id": str(result.journal_entry_id)},
                )
                await session.commit()
            elif isinstance(result, PostingRefusal):
                if result.outcome == "blocked_idempotent":
                    skipped_idempotent += 1
                    log.debug("Idempotent skip: %s", plan.source_external_id)
                else:
                    # Posting was refused for a structural reason — queue for review.
                    queued += 1
                    queued_drift_cents += plan.amount_cents
                    log.warning(
                        "Post refused (%s): %s — queuing",
                        result.outcome, plan.source_external_id,
                    )
                    approval_id = await _insert_approval_queue_row(
                        session, workspace_id=workspace_id, entity_id=entity_id,
                        plan=plan, amount_cents=plan.amount_cents,
                    )
                    await _write_audit(
                        session, workspace_id=workspace_id, entity_id=entity_id,
                        plan=plan, outcome="queued",
                        response_extra={
                            "approval_id": str(approval_id) if approval_id else "dedup_skip",
                            "post_refusal_outcome": result.outcome,
                        },
                    )
                    await session.commit()

        elif plan.proposed_action in ("would_queue", "would_block"):
            queued += 1
            queued_drift_cents += plan.amount_cents
            approval_id = await _insert_approval_queue_row(
                session, workspace_id=workspace_id, entity_id=entity_id,
                plan=plan, amount_cents=plan.amount_cents,
            )
            if approval_id is not None:
                # New row inserted — write audit.
                await _write_audit(
                    session, workspace_id=workspace_id, entity_id=entity_id,
                    plan=plan, outcome="queued",
                    response_extra={"approval_id": str(approval_id)},
                )
                await session.commit()
            else:
                # Pre-check found an existing row → idempotent skip. NOTE:
                # queued_drift_cents is INTENTIONALLY NOT decremented here —
                # the item IS still queued (pending in approval_queue from a
                # prior run), so the journal IS still short by this amount.
                # Backing it out would break the per-period recon math.
                log.debug(
                    "Approval_queue dedup skip: %s", plan.source_external_id
                )
                skipped_idempotent += 1
                queued -= 1
        else:
            blocked += 1
            queued_drift_cents += plan.amount_cents
            await _write_audit(
                session, workspace_id=workspace_id, entity_id=entity_id,
                plan=plan, outcome="blocked",
            )
            await session.commit()

    # ---- Reconciliation (per-period delta, design choice a) ----
    # journal_delta  = what the journal actually recorded for this period's cash flows.
    # expected_delta = (eb − bb) from the bank statement = what SHOULD have been posted.
    # queued_drift   = sum of signed cents for transactions that intentionally did NOT
    #                  post (queued/blocked).  The journal is SHORT by exactly this.
    # real_drift     = journal_delta − (expected_delta − queued_drift)
    #                = 0 means "the journal posted exactly what it should have; the
    #                  rest is in the approval queue where it belongs."
    # Halt only when real_drift ≠ 0 (something truly unexpected happened).
    recon_journal_delta: int | None = None
    recon_real_drift: int | None = None
    if live:
        journal_delta = await _query_cash_period_delta(
            session,
            entity_id=entity_id,
            period_start=period_start,
            period_end=period_end,
        )
        if journal_delta is not None:
            recon_journal_delta = journal_delta
            # expected_delta − queued_drift = portion of the period delta that should
            # have posted.  Cash outflows are negative; queued_drift is the SUM of
            # those signed cents, so the shortfall is already sign-correct.
            recon_real_drift = journal_delta - (expected_delta - queued_drift_cents)

    return StatementResult(
        period_start=period_start,
        period_end=period_end,
        txn_count=len(plans),
        posted=posted,
        queued=queued,
        blocked=blocked,
        skipped_idempotent=skipped_idempotent,
        recon_expected_delta_cents=expected_delta,
        recon_queued_drift_cents=queued_drift_cents,
        recon_journal_delta_cents=recon_journal_delta,
        recon_real_drift_cents=recon_real_drift,
        llm_call_count=llm_call_count,
        llm_total_ms=llm_total_ms,
    )


# ---------------------------------------------------------------------------
# Dry-run plan output
# ---------------------------------------------------------------------------


def write_dry_run_plan(
    out_path: Path,
    *,
    workspace_id: str,
    entity_id: str,
    last4: str,
    statements: list[dict],
    all_plans: list[list[TxnPlan]],
    results: list[StatementResult],
) -> None:
    """Write the full dry-run plan JSON outside the worktree."""
    out_path.parent.mkdir(parents=True, exist_ok=True)

    stmt_summaries = []
    for stmt, plans, result in zip(statements, all_plans, results):
        stmt_summaries.append({
            "period_start": result.period_start,
            "period_end": result.period_end,
            "txn_count": result.txn_count,
            "proposed_post": result.posted,
            "proposed_queue": result.queued,
            "proposed_block": result.blocked,
            "llm_call_count": result.llm_call_count,
            "entries": [asdict(p) for p in plans],
        })

    payload = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "workspace_id": workspace_id,
        "entity_id": entity_id,
        "source_label": SOURCE_LABEL,
        "account_last4": last4,
        "cash_canonical": COA_CASH_CANONICAL,
        "statements": stmt_summaries,
    }
    with out_path.open("w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# Report (counts only — NO transaction-level data in stdout)
# ---------------------------------------------------------------------------


def print_report(
    results: list[StatementResult],
    *,
    workspace_id: str,
    entity_id: str,
    last4: str,
    live: bool,
    out_path: Path | None,
) -> None:
    """Counts-only report. No transaction text, no amounts, no payees."""
    total_txns = sum(r.txn_count for r in results)
    total_posted = sum(r.posted for r in results)
    total_queued = sum(r.queued for r in results)
    total_blocked = sum(r.blocked for r in results)
    total_skipped = sum(r.skipped_idempotent for r in results)
    total_llm_calls = sum(r.llm_call_count for r in results)
    total_llm_ms = sum(r.llm_total_ms for r in results)
    n_clean_recon = sum(1 for r in results if r.recon_real_drift_cents == 0)
    n_dirty_recon = sum(1 for r in results if r.recon_real_drift_cents not in (0, None))
    n_unknown_recon = sum(1 for r in results if r.recon_real_drift_cents is None)

    mode = "LIVE" if live else "DRY-RUN"

    print("=" * 78)
    print(f"BofA {last4} — Bank-Statement Reconstruction  [{mode}]")
    print("=" * 78)
    print(f"workspace_id    : {workspace_id}")
    print(f"entity_id       : {entity_id}")
    print(f"source label    : {SOURCE_LABEL}")
    print(f"cash account    : {COA_CASH_CANONICAL}")
    print(f"statements      : {len(results)}")
    print(f"total txns      : {total_txns}")
    print()

    print("ACTIONS (counts only, no $):")
    print(f"  {'would_post' if not live else 'posted'}      : {total_posted}")
    print(f"  {'would_queue' if not live else 'queued'}     : {total_queued}")
    print(f"  {'would_block' if not live else 'blocked'}    : {total_blocked}")
    if live:
        print(f"  idempotent skips : {total_skipped}")
    print()

    if live:
        print("RECONCILIATION (live — per-period delta vs statement eb−bb, accounting for queue):")
        print(f"  clean (real_drift=0) : {n_clean_recon}/{len(results)}")
        print(f"  DIRTY (real_drift≠0) : {n_dirty_recon}")
        print(f"  not yet posted       : {n_unknown_recon}")
        if n_dirty_recon:
            print("  *** REAL DRIFT DETECTED — review before proceeding ***")
            for r in results:
                if r.recon_real_drift_cents not in (0, None):
                    print(
                        f"    {r.period_start} → {r.period_end}: "
                        f"real_drift {_fmt_cents(r.recon_real_drift_cents)}"
                    )
        print()
    else:
        print("RECONCILIATION (dry-run — not available; use --live to get journal data):")
        print("  (run with --live on a test workspace to check reconciliation)")
        print()

    print("LLM FALLBACK:")
    if total_llm_calls == 0:
        print("  0 LLM calls — all transactions categorized by vendor rules")
    else:
        avg_ms = total_llm_ms // total_llm_calls if total_llm_calls else 0
        # Estimate wall-clock: total_llm_ms / concurrency (parallel slots).
        # Report as an estimate only — actual depends on queue depth.
        print(f"  calls           : {total_llm_calls}")
        print(f"  avg latency     : ~{avg_ms} ms/call")
        print(f"  total LLM time  : ~{total_llm_ms / 1000:.1f} s serial")
    print()

    print("PER-STATEMENT SUMMARY (counts only):")
    for r in results:
        recon_tag = ""
        if r.recon_real_drift_cents is not None:
            recon_tag = (
                " RECON:CLEAN" if r.recon_real_drift_cents == 0
                else " *** RECON:DIRTY ***"
            )
        elif live:
            recon_tag = " (not-yet-posted)"
        print(
            f"  {r.period_start} → {r.period_end}: "
            f"txns={r.txn_count} "
            f"post={r.posted} queue={r.queued} block={r.blocked}"
            f"{' llm='+str(r.llm_call_count) if r.llm_call_count else ''}"
            f"{recon_tag}"
        )
    print()

    if out_path and not live:
        print(f"Full plan written to: {out_path}")
        print("  (transaction-level data — NOT committed, stays local)")
        print()

    if live and n_dirty_recon:
        print("*** WARNING: real reconciliation drift detected. Do NOT continue ingesting")
        print("*** additional years until drift is investigated and resolved.")
        print()

    print("=" * 78)
    if live:
        print(f"Items waiting in /approvals for human review: {total_queued}")
    else:
        print("DRY-RUN complete. No DB writes. Pass --live to post.")
    print("=" * 78)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def _main_async(args: argparse.Namespace) -> int:
    workspace_id = args.workspace_id
    entity_id = args.entity_id
    last4 = args.source_account_last4
    input_dir = Path(args.input_dir)
    live = args.live
    use_llm = args.use_llm_fallback
    max_concurrency = args.max_llm_concurrency
    year = args.year

    if not input_dir.exists():
        log.error("Input dir does not exist: %s", input_dir)
        return 2

    # Load statements.
    statements = _load_statements(input_dir, last4, year)
    if not statements:
        log.error("No matching statements found (year=%s, last4=%s)", year, last4)
        return 2
    log.info("Loaded %d statements (year=%s, last4=%s)", len(statements), year, last4)

    # Instantiate LLM categorizer if requested.
    llm: LLMCategorizer | None = None
    llm_semaphore: asyncio.Semaphore | None = None
    if use_llm:
        llm = LLMCategorizer()
        llm_semaphore = asyncio.Semaphore(max_concurrency)
        log.info(
            "LLM fallback enabled — endpoint=%s model=%s concurrency=%d",
            llm._settings.llm_endpoint_url,
            llm._settings.llm_model,
            max_concurrency,
        )
    else:
        log.info("LLM fallback disabled (--use-llm-fallback not set)")

    # Phase 1: plan all statements (async, LLM calls run concurrently up to max_concurrency).
    log.info("Planning %d statements ...", len(statements))
    all_plans: list[list[TxnPlan]] = []
    for stmt in statements:
        plans = await plan_statement(
            stmt,
            last4=last4,
            llm=llm,
            llm_semaphore=llm_semaphore,
        )
        all_plans.append(plans)
        log.info(
            "  Planned %s → %s: %d txns  (post=%d queue=%d block=%d llm=%d)",
            stmt["statement_period_start"],
            stmt["statement_period_end"],
            len(plans),
            sum(1 for p in plans if p.proposed_action == "would_post"),
            sum(1 for p in plans if p.proposed_action == "would_queue"),
            sum(1 for p in plans if p.proposed_action == "would_block"),
            sum(1 for p in plans if p.llm_suggested),
        )

    if not live:
        # Dry-run: build results without a DB session.
        results = []
        for stmt, plans in zip(statements, all_plans):
            bb = _to_cents(stmt["beginning_balance"])
            eb = _to_cents(stmt["ending_balance"])
            expected_delta = eb - bb
            queued_drift = sum(
                p.amount_cents for p in plans if p.proposed_action != "would_post"
            )
            # Synthesize a dry-run StatementResult.
            r = StatementResult(
                period_start=stmt["statement_period_start"],
                period_end=stmt["statement_period_end"],
                txn_count=len(plans),
                posted=sum(1 for p in plans if p.proposed_action == "would_post"),
                queued=sum(1 for p in plans if p.proposed_action == "would_queue"),
                blocked=sum(1 for p in plans if p.proposed_action == "would_block"),
                skipped_idempotent=0,
                recon_expected_delta_cents=expected_delta,
                recon_queued_drift_cents=queued_drift,
                recon_journal_delta_cents=None,
                recon_real_drift_cents=None,
                llm_call_count=sum(1 for p in plans if p.llm_suggested),
                llm_total_ms=sum(p.llm_duration_ms or 0 for p in plans),
            )
            results.append(r)

        # Write full plan JSON.
        out_path = Path(args.out) if args.out else (
            Path.home()
            / "accounting-ops-ingest"
            / f"bofa-{last4}-{entity_id}-plan.json"
        )
        write_dry_run_plan(
            out_path,
            workspace_id=workspace_id,
            entity_id=entity_id,
            last4=last4,
            statements=statements,
            all_plans=all_plans,
            results=results,
        )
        print_report(
            results,
            workspace_id=workspace_id,
            entity_id=entity_id,
            last4=last4,
            live=False,
            out_path=out_path,
        )
        return 0

    # ---- LIVE mode ----
    log.warning("*** LIVE MODE: writing to the database ***")
    log.warning(
        "workspace=%s entity=%s source=%s", workspace_id, entity_id, SOURCE_LABEL
    )
    # Use the platform's set_workspace_search_path so the GUC name
    # (app.current_workspace) and schema format (accounting_ops_<uuid_underscored>)
    # always match the platform's own conventions — this script cannot drift.
    # We DON'T use workspace_session() here because that wraps everything in a
    # single outer BEGIN; apply_statement commits per-transaction for atomicity.
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

    engine = await _get_db_engine(workspace_id)
    SessionFactory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    try:
        results = []
        for stmt, plans in zip(statements, all_plans):
            log.info(
                "Applying %s → %s (%d txns) ...",
                stmt["statement_period_start"],
                stmt["statement_period_end"],
                len(plans),
            )
            # Fresh session per statement.  apply_statement commits per-transaction
            # for atomicity, so we cannot wrap the whole statement in a single
            # session.begin() context manager.  Instead we pin search_path and the
            # app.current_workspace GUC at the SESSION level (not transaction-local)
            # so they survive across the per-txn commits inside apply_statement.
            # This is safe because this session uses a dedicated one-shot engine
            # that is disposed after the run — no pool bleed.
            session: AsyncSession = SessionFactory()
            try:
                # Bootstrap transaction: set the schema + GUC at session scope.
                from sqlalchemy import text as _sql
                from app.workspace_session import SHARED_SCHEMA
                async with session.begin():
                    # Shared-schema model: constant search_path; the GUC below is
                    # the isolation key. SET (no LOCAL) = session-scoped; survives commits.
                    await session.execute(
                        _sql(f'SET search_path = "{SHARED_SCHEMA}", public')
                    )
                    # set_config(name, value, is_local=false) = session-scoped.
                    await session.execute(
                        _sql("SELECT set_config('app.current_workspace', :wid, false)"),
                        {"wid": workspace_id},
                    )
                # Session-level GUC + search_path now persist for all subsequent
                # transactions on this connection (single-connection batch session).
                result = await apply_statement(
                    stmt,
                    plans,
                    workspace_id=workspace_id,
                    entity_id=entity_id,
                    live=True,
                    session=session,
                )
            finally:
                await session.close()

            results.append(result)
            log.info(
                "  Done: post=%d queue=%d block=%d idempotent=%d "
                "real_drift=%s",
                result.posted,
                result.queued,
                result.blocked,
                result.skipped_idempotent,
                _fmt_cents(result.recon_real_drift_cents)
                if result.recon_real_drift_cents is not None
                else "n/a",
            )
            # Hard halt on REAL reconciliation drift in live mode.
            if result.recon_real_drift_cents not in (0, None):
                log.error(
                    "REAL RECONCILIATION DRIFT detected on %s → %s: real_drift=%s. "
                    "Halting live run. Check the journal before continuing.",
                    result.period_start,
                    result.period_end,
                    _fmt_cents(result.recon_real_drift_cents),
                )
                print_report(
                    results,
                    workspace_id=workspace_id,
                    entity_id=entity_id,
                    last4=last4,
                    live=True,
                    out_path=None,
                )
                return 1
    finally:
        await engine.dispose()

    print_report(
        results,
        workspace_id=workspace_id,
        entity_id=entity_id,
        last4=last4,
        live=True,
        out_path=None,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--workspace-id", default=DEFAULT_WORKSPACE_ID,
                        help="Workspace UUID (default: AO_WORKSPACE_ID env)")
    parser.add_argument("--entity-id", default=DEFAULT_ENTITY_ID,
                        help="Entity UUID (default: AO_ENTITY_ID env)")
    parser.add_argument("--source-account-last4", default=DEFAULT_LAST4,
                        help="Bank account last-4 suffix (default: 1792)")
    parser.add_argument(
        "--input-dir",
        default=DEFAULT_INPUT_DIR,
        help="Directory containing bofa_<last4>_<period_end>.json parser outputs",
    )
    parser.add_argument(
        "--year",
        type=int,
        default=None,
        help="Filter to a single tax year (e.g. 2022). RECOMMENDED for first runs.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        default=False,
        help=(
            "Write to the database. Without this flag the script is read-only "
            "(dry-run). The lead must pass this flag explicitly after reviewing "
            "the dry-run output."
        ),
    )
    parser.add_argument(
        "--use-llm-fallback",
        action="store_true",
        default=True,
        help="Use LLMCategorizer for Uncategorized transactions (default: on).",
    )
    parser.add_argument(
        "--no-llm-fallback",
        dest="use_llm_fallback",
        action="store_false",
        help="Disable LLM fallback (vendor rules only).",
    )
    parser.add_argument(
        "--max-llm-concurrency",
        type=int,
        default=4,
        help="Max parallel LLM calls (default: 4 — matches Qwen's P40 slot count).",
    )
    parser.add_argument(
        "--out",
        default=None,
        help=(
            "Path for the dry-run plan JSON. Default: ~/accounting-ops-ingest/. "
            "Always OUTSIDE the worktree to avoid accidental git add."
        ),
    )
    args = parser.parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
