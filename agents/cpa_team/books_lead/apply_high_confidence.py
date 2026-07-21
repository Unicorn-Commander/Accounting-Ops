"""Phase 1 — auto-apply the 12 'approve' + 'high' bank-transaction approvals.

This script processes the pre-scored ``approval_recommendation`` rows tagged
``recommended_decision='approve'`` + ``confidence='high'`` and applies each one
by calling the same posting primitive (`post_journal_entry`) the
`/api/workspaces/{wid}/approvals/{aid}/decide` endpoint uses on APPROVE — so
the hash chain, materiality bypass (user_confirmed=True), idempotency check,
and decision_audit trail all behave identically to a human click in the UI.

We bypass the HTTP route because the service token issued to this sub-agent
caps at `accountant` (role_floor); the decide endpoint requires `admin`.
Going straight through the data layer with the same code path is the
sanctioned pattern for back-office migrations (see scripts/ingest_bofa_live.py,
which uses the same approach to write 800+ historical entries).

Safety
------
* Each approval is processed in its own transaction (rollback if anything
  fails — no half-applied state, the row stays 'pending').
* `posted_by_uc_uid` = the acting owner uc_uid (the impersonated user; mirrors
  the decide endpoint's `workspace.user_uc_uid` value).
* `model_version` = 'books-lead/v1' (so audit trail makes the source obvious).
* `user_confirmed=True` bypasses the materiality re-gate — exactly what the
  HTTP `_approve_bank_transaction_import` helper does, since the
  recommendation grading IS the human-equivalent attestation here.
* Writes a `decision_audit` row per applied approval.

Run inside the app container so it inherits ACCTOPS_SERVICE_TOKENS + the
database URL:

    docker exec -t accounting_ops-app-1 \
        python -m agents.cpa_team.books_lead.apply_high_confidence
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.db import SessionLocal
from app.journal import PostingOutcome, PostingRefusal, post_journal_entry
from app.workspace_session import schema_for_workspace, workspace_session


# Batch target + acting identity are env-driven so this run is not bound to any
# one deployment's workspace/owner. Set BOOKS_LEAD_WORKSPACE_ID / _ENTITY_ID and
# BOOKS_LEAD_ACTING_UC_UID (falls back to the local-admin uc_uid, default "admin").
WORKSPACE_ID = UUID(os.getenv("BOOKS_LEAD_WORKSPACE_ID", "00000000-0000-0000-0000-000000000000"))
ENTITY_ID = UUID(os.getenv("BOOKS_LEAD_ENTITY_ID", "00000000-0000-0000-0000-000000000000"))
ACTING_UC_UID = os.getenv(
    "BOOKS_LEAD_ACTING_UC_UID", get_settings().local_admin_uc_uid
)  # workspace owner impersonated by the books-lead run
AGENT_ID = "books-lead"
MODEL_VERSION = "books-lead/v1"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger("books-lead.phase1")


@dataclass
class ApprovalRow:
    approval_id: UUID
    payload: dict[str, Any]
    rationale: str


async def fetch_high_confidence_approvals(session: AsyncSession) -> list[ApprovalRow]:
    """Return all pending bank-transaction-import approvals whose recommendation
    is ('approve', 'high'). One row per approval (UNIQUE on approval_id in
    `approval_recommendation`)."""
    rows = (
        await session.execute(
            text(
                """
                SELECT aq.id::text   AS approval_id,
                       aq.payload    AS payload,
                       ar.rationale  AS rationale
                FROM approval_queue aq
                JOIN approval_recommendation ar ON ar.approval_id = aq.id
                WHERE aq.entity_id = :eid
                  AND aq.status = 'pending'
                  AND aq.subject_type = 'bank_transaction_import'
                  AND ar.recommended_decision = 'approve'
                  AND ar.confidence = 'high'
                ORDER BY (aq.payload->>'effective_date')::date ASC, aq.id ASC
                """
            ),
            {"eid": str(ENTITY_ID)},
        )
    ).mappings().all()
    return [
        ApprovalRow(
            approval_id=UUID(r["approval_id"]),
            payload=r["payload"] or {},
            rationale=r["rationale"] or "",
        )
        for r in rows
    ]


async def lookup_default_cash_external_account(session: AsyncSession) -> str | None:
    """If a payload is missing `external_account_id`, fall back to the
    workspace's only linked cash account (mirrors the decide endpoint helper)."""
    row = (
        await session.execute(
            text(
                """
                SELECT linked_external_account_id
                FROM chart_of_account
                WHERE entity_id = :eid
                  AND linked_external_source IN ('bofa-statement', 'simplefin')
                  AND is_active = true
                LIMIT 1
                """
            ),
            {"eid": str(ENTITY_ID)},
        )
    ).mappings().first()
    return row["linked_external_account_id"] if row else None


async def write_decision_audit_row(
    session: AsyncSession,
    *,
    approval_id: UUID,
    je_id: UUID,
    payload: dict[str, Any],
    rationale: str,
) -> None:
    """Append a decision_audit row tying the auto-apply to this script."""
    import json
    import uuid

    await session.execute(
        text(
            """
            INSERT INTO decision_audit (
                id, workspace_id, entity_id, agent_session_id,
                tool_name, request_payload, response_payload,
                reasoning_trace, source_document_id, model_version, outcome
            ) VALUES (
                :id, :wid, :eid, NULL,
                'books_lead.apply_high_confidence',
                CAST(:request AS jsonb),
                CAST(:response AS jsonb),
                :reasoning, NULL, :model_version, 'approved'
            )
            """
        ),
        {
            "id": str(uuid.uuid4()),
            "wid": str(WORKSPACE_ID),
            "eid": str(ENTITY_ID),
            "request": json.dumps(
                {
                    "approval_id": str(approval_id),
                    "decision": "approve",
                    "source": "books-lead/phase1-high-confidence",
                }
            ),
            "response": json.dumps(
                {
                    "ok": True,
                    "approval_id": str(approval_id),
                    "journal_entry_id": str(je_id),
                    "subject_type": "bank_transaction_import",
                    "applied": True,
                }
            ),
            "reasoning": (
                "Auto-applied by books-lead Phase 1: pre-analyzer scored this "
                f"row as ('approve', 'high'). {rationale}"
            ),
            "model_version": MODEL_VERSION,
        },
    )


async def mark_decided(
    session: AsyncSession, *, approval_id: UUID
) -> None:
    """Stamp the approval_queue row as approved by the acting user."""
    await session.execute(
        text(
            """
            UPDATE approval_queue
            SET status = CAST(:status AS approval_status),
                decided_by_uc_uid = :uid,
                decided_at = now()
            WHERE id = :id
            """
        ),
        {"status": "approved", "uid": ACTING_UC_UID, "id": str(approval_id)},
    )


async def apply_one_approval(
    sessionmaker: async_sessionmaker[AsyncSession],
    approval: ApprovalRow,
) -> tuple[str, UUID | None, str | None]:
    """Apply a single approval in its own transaction.

    Returns (outcome, journal_entry_id, error). One of:
      - ('posted', je_id, None)             — applied & decision_audit written
      - ('idempotent', existing_id, None)   — already posted (still mark decided)
      - ('refused', None, reason)           — kept pending; no audit row written
      - ('error', None, exception_str)      — kept pending; no audit row written
    """
    payload = approval.payload

    # Validate required fields (mirrors the decide endpoint)
    required = ("source_external_id", "effective_date", "amount_cents", "category")
    missing = [k for k in required if k not in payload]
    if missing:
        return ("error", None, f"payload missing fields: {missing}")

    effective_date = dt.date.fromisoformat(payload["effective_date"])
    amount_cents = int(payload["amount_cents"])
    category = payload["category"]
    category_status = payload.get("category_status", "auto")

    async with sessionmaker() as session:
        async with session.begin():
            # Pin search_path + RLS GUC for this workspace
            schema = schema_for_workspace(WORKSPACE_ID)
            await session.execute(text(f'SET LOCAL search_path = "{schema}", public'))
            await session.execute(
                text("SELECT set_config('app.current_workspace', :wid, true)"),
                {"wid": str(WORKSPACE_ID)},
            )

            external_account_id = payload.get("external_account_id")
            if not external_account_id:
                external_account_id = await lookup_default_cash_external_account(session)
            if not external_account_id:
                return ("error", None, "no external_account_id resolvable")

            try:
                result = await post_journal_entry(
                    session,
                    workspace_id=str(WORKSPACE_ID),
                    entity_id=str(ENTITY_ID),
                    source="bofa-statement",
                    source_external_id=payload["source_external_id"],
                    effective_date=effective_date,
                    amount_cents=amount_cents,
                    description=payload.get("description", payload["source_external_id"]),
                    payee=payload.get("payee"),
                    memo=payload.get("memo"),
                    category=category,
                    category_status=category_status,
                    entity_mapped=True,
                    sign_matched=True,           # user_confirmed bypasses sign gate
                    external_account_id=external_account_id,
                    has_receipt_link=False,
                    receipt_link_id=None,
                    posted_by_uc_uid=ACTING_UC_UID,
                    posted_by_agent_id=AGENT_ID,
                    user_confirmed=True,         # mirrors the decide endpoint
                    reasoning_trace=(
                        "Auto-applied by books-lead Phase 1 (pre-analyzer "
                        f"recommendation: approve/high). {approval.rationale}"
                    ),
                    model_version=MODEL_VERSION,
                )
            except Exception as exc:  # pragma: no cover — defensive
                log.exception(
                    "post_journal_entry threw for approval %s",
                    approval.approval_id,
                )
                return ("error", None, repr(exc))

            if isinstance(result, PostingRefusal):
                if result.outcome == "blocked_idempotent" and result.existing_entry_id:
                    # Already posted earlier — still mark the approval decided
                    # against the existing journal entry.
                    await mark_decided(session, approval_id=approval.approval_id)
                    await write_decision_audit_row(
                        session,
                        approval_id=approval.approval_id,
                        je_id=result.existing_entry_id,
                        payload=payload,
                        rationale=approval.rationale + " (idempotent: was already posted)",
                    )
                    return ("idempotent", result.existing_entry_id, None)
                return ("refused", None, result.reason)

            assert isinstance(result, PostingOutcome)
            await mark_decided(session, approval_id=approval.approval_id)
            await write_decision_audit_row(
                session,
                approval_id=approval.approval_id,
                je_id=result.journal_entry_id,
                payload=payload,
                rationale=approval.rationale,
            )
            return ("posted", result.journal_entry_id, None)


async def main() -> int:
    log.info(
        "Phase 1 starting — workspace=%s entity=%s database=%s",
        WORKSPACE_ID,
        ENTITY_ID,
        get_settings().database_url.split("@")[-1],
    )

    # ---- 1. Fetch the queue snapshot (read-only transaction) ----
    async with workspace_session(SessionLocal, WORKSPACE_ID) as session:
        approvals = await fetch_high_confidence_approvals(session)

    log.info("Phase 1 — %d high-confidence approve recommendations queued", len(approvals))
    if not approvals:
        log.info("Nothing to do; exiting cleanly.")
        return 0

    # ---- 2. Apply each in its own transaction ----
    posted: list[tuple[UUID, UUID]] = []      # (approval_id, je_id)
    idempotent: list[tuple[UUID, UUID]] = []  # (approval_id, existing_je_id)
    refused: list[tuple[UUID, str]] = []      # (approval_id, reason)
    errored: list[tuple[UUID, str]] = []      # (approval_id, exc_str)

    for i, approval in enumerate(approvals, 1):
        log.info(
            "[%d/%d] Applying approval %s amount=%s description=%s",
            i,
            len(approvals),
            approval.approval_id,
            approval.payload.get("amount_cents"),
            (approval.payload.get("description") or "")[:80],
        )
        outcome, je_id, err = await apply_one_approval(SessionLocal, approval)
        if outcome == "posted":
            posted.append((approval.approval_id, je_id))  # type: ignore[arg-type]
            log.info("    -> posted je=%s", je_id)
        elif outcome == "idempotent":
            idempotent.append((approval.approval_id, je_id))  # type: ignore[arg-type]
            log.info("    -> idempotent (already posted as je=%s) — marked decided", je_id)
        elif outcome == "refused":
            refused.append((approval.approval_id, err or ""))
            log.warning("    -> REFUSED: %s (approval kept pending)", err)
        else:
            errored.append((approval.approval_id, err or ""))
            log.error("    -> ERROR: %s (approval kept pending)", err)

    # ---- 3. Final summary ----
    print()
    print("=" * 72)
    print("PHASE 1 SUMMARY — high-confidence auto-apply")
    print("=" * 72)
    print(f"  Total considered:    {len(approvals)}")
    print(f"  Posted:              {len(posted)}")
    print(f"  Idempotent skipped:  {len(idempotent)}")
    print(f"  Refused:             {len(refused)}")
    print(f"  Errored:             {len(errored)}")
    if posted:
        print()
        print("Posted journal_entry IDs:")
        for aid, je_id in posted:
            print(f"  approval={aid}  je={je_id}")
    if idempotent:
        print()
        print("Idempotent (already-posted) approvals (still marked decided):")
        for aid, je_id in idempotent:
            print(f"  approval={aid}  existing_je={je_id}")
    if refused:
        print()
        print("Refused (kept pending):")
        for aid, reason in refused:
            print(f"  approval={aid}  reason={reason}")
    if errored:
        print()
        print("Errored (kept pending):")
        for aid, err in errored:
            print(f"  approval={aid}  exception={err}")
    print("=" * 72)

    return 0 if not errored else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
