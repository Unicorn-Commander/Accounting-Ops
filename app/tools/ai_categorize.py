"""MCP tool + service: ai_categorize_pending — Tier-2 LLM categorization over the
approval queue.

Finds pending ``bank_transaction_import`` rows that the deterministic rules left
**Uncategorized**, runs the model-agnostic ``LLMCategorizer`` (categorization
engine Tier-2 — by default ``qwen3.6-35b-a3b`` through the metered UC LiteLLM
gateway), and writes the suggested Chart-of-Accounts category back into each
row's ``payload`` as ``category_status='suggest'`` for a human to confirm in
/approvals.

This is the in-app activation of the LLM categorizer: until now the Tier-2 model
only fired from the ``scripts/ingest_bofa_live.py`` CLI batch — the live
connector/upload path categorizes with rules + learned patterns only. This tool
(and its REST sibling) lets a human OR an agent trigger AI categorization over
whatever the rules couldn't place, on demand.

ENGINES-AS-TRUTH (do not weaken):
  * The LLM only CLASSIFIES into the existing ``CATEGORY_TO_ACCOUNT`` allow-list.
    It never computes an amount, posts a journal entry, or decides an approval.
  * Hallucinated categories are rejected by the engine (``suggest`` returns
    ``None`` → the row is left untouched, still Uncategorized).
  * Every suggestion is written as ``status='suggest'`` and the approval row
    stays ``pending`` — a human still clicks Approve in /approvals, and may pick
    a different category via the existing ``category_override`` path.
  * Each suggestion appends a ``decision_audit`` row (the "show your work" trail
    that surfaces in the AI-activity stream).

Role floor: accountant.
"""
from __future__ import annotations

import json
import logging
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.categorization.engine import categorize_transaction_async
from app.categorization.llm import LLMCategorizer
from app.config import get_settings
from app.db import SessionLocal
from app.mcp_auth import authorize_workspace
from app.tools._audit import write_decision_audit
from app.workspace_session import workspace_session

_log = logging.getLogger("acctops.tools.ai_categorize")

# Hard ceiling on rows processed in one call — bounds the wall-clock + token cost
# of a single invocation (the caller pages through by calling again). The LLM
# calls run sequentially: the gateway alias is rpm-limited and the idle-unloaded
# 35B serves one request at a time, so fan-out would just queue + risk timeouts.
_MAX_ROWS = 200


async def ai_categorize_pending(
    workspace_id: UUID,
    entity_id: UUID,
    limit: int = 50,
    year: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Suggest COA categories for Uncategorized pending approvals using the LLM.

    Scans pending ``bank_transaction_import`` approval-queue rows for the entity
    whose ``payload.category`` is still ``Uncategorized`` (the rules + learned
    patterns couldn't place them), runs the categorization engine with its Tier-2
    LLM fallback, and writes any confident suggestion back into the row's payload
    (``category_status='suggest'``) for human review. Never auto-decides.

    Args:
        workspace_id: workspace to operate on.
        entity_id: entity within the workspace (entity-scoped).
        limit: max rows to process this call (1..200). Default 50.
        year: optional calendar-year filter (YYYY), matched against the ISO date
            embedded in ``payload.source_external_id`` or ``payload.effective_date``.
        dry_run: when True, compute + return the suggestions WITHOUT writing them
            to the queue or the audit trail. Used for safe verification and for a
            "preview" UX (show the human what the model would suggest before they
            commit). Default False.

    Returns:
        {
          "scanned": int,         # uncategorized rows examined
          "suggested": int,       # rows given an AI category suggestion
          "unchanged": int,       # rows the model couldn't confidently categorize
          "errors": int,          # rows where the LLM call raised
          "dry_run": bool,        # whether anything was actually written
          "model": str,           # the categorizer model used
          "endpoint": str,        # the categorizer endpoint used
          "results": [
            {"approval_id": str, "suggested_category": str,
             "source": str, "reasoning": str}, ...
          ]
        }

    Role floor: accountant.
    """
    ctx = await authorize_workspace(workspace_id, entity_id, min_role="accountant")

    # Allowed COA categories — the LLM may ONLY pick from these. Local import
    # avoids a circular import (posting <-> categorization).
    from app.journal.posting import CATEGORY_TO_ACCOUNT

    allowed = list(CATEGORY_TO_ACCOUNT.keys())
    categorizer = LLMCategorizer()
    settings = get_settings()

    capped = max(1, min(int(limit), _MAX_ROWS))

    scanned = 0
    suggested = 0
    unchanged = 0
    errors = 0
    results: list[dict[str, Any]] = []

    async with workspace_session(SessionLocal, workspace_id) as session:
        params: dict[str, Any] = {"eid": str(entity_id), "lim": capped}
        year_filter = ""
        if year:
            params["yp"] = str(year)
            year_filter = (
                "AND ((payload->>'source_external_id') LIKE :yp || '-%' "
                "OR (payload->>'effective_date') LIKE :yp || '-%') "
            )

        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT id::text AS approval_id, payload
                    FROM approval_queue
                    WHERE entity_id = :eid
                      AND subject_type = 'bank_transaction_import'
                      AND status = 'pending'
                      AND COALESCE(payload->>'category', 'Uncategorized') = 'Uncategorized'
                      -- skip rows the AI already looked at + couldn't place, so
                      -- repeated bulk runs make progress instead of re-grinding
                      -- the same un-categorizable newest rows every call.
                      AND NOT COALESCE((payload->>'ai_attempted')::boolean, false)
                      {year_filter}
                    ORDER BY created_at DESC
                    LIMIT :lim
                    """
                ),
                params,
            )
        ).mappings().all()

        for r in rows:
            scanned += 1
            payload = r["payload"]
            if isinstance(payload, (bytes, bytearray)):
                payload = json.loads(payload.decode("utf-8"))
            elif isinstance(payload, str):
                payload = json.loads(payload)
            if not isinstance(payload, dict):
                unchanged += 1
                continue

            amount = _amount_from_payload(payload)

            try:
                suggestion = await categorize_transaction_async(
                    payee=payload.get("payee"),
                    description=payload.get("description"),
                    memo=payload.get("memo"),
                    amount=amount,
                    overrides=None,
                    llm_fallback=categorizer,
                    allowed_categories=allowed,
                )
            except Exception as e:  # noqa: BLE001 — one bad row must not abort the batch
                errors += 1
                _log.warning("ai_categorize_pending: row %s failed: %s", r["approval_id"], e)
                continue

            # The engine guarantees suggestion.category is in `allowed` (or
            # "Uncategorized"). Only write when the model actually placed it.
            if suggestion.category == "Uncategorized" or suggestion.category not in CATEGORY_TO_ACCOUNT:
                unchanged += 1
                # Mark the row as AI-attempted so a later bulk run doesn't re-grind
                # it (the model — Qwen and, if configured, the frontier fallback —
                # already looked and couldn't confidently place it). A human can
                # still categorize it manually; clearing ai_attempted re-opens it.
                if not dry_run:
                    await session.execute(
                        text(
                            "UPDATE approval_queue "
                            "SET payload = payload || jsonb_build_object('ai_attempted', true) "
                            "WHERE id = CAST(:id AS uuid) AND status = 'pending'"
                        ),
                        {"id": r["approval_id"]},
                    )
                continue

            if not dry_run:
                # Write the suggestion into the payload. status stays 'pending'
                # (never auto-decided); category_status mirrors the engine verdict
                # so the /approvals UI shows it as a review-grade suggestion the
                # human confirms. jsonb_build_object is parameterized — no interp.
                await session.execute(
                    text(
                        """
                        UPDATE approval_queue
                        SET payload = payload || jsonb_build_object(
                                'category', CAST(:cat AS text),
                                'category_status', CAST(:cstatus AS text),
                                'categorization_source', CAST(:src AS text),
                                'sign_matched', CAST(:sm AS boolean),
                                'ai_suggested', true,
                                'ai_attempted', true
                            )
                        WHERE id = CAST(:id AS uuid) AND status = 'pending'
                        """
                    ),
                    {
                        "id": r["approval_id"],
                        "cat": suggestion.category,
                        "cstatus": suggestion.status,
                        "src": suggestion.source,
                        "sm": bool(suggestion.sign_matched),
                    },
                )

                await write_decision_audit(
                    session,
                    workspace_id=workspace_id,
                    entity_id=entity_id,
                    ctx=ctx,
                    tool_name="ai_categorize_pending",
                    request_payload={
                        "approval_id": r["approval_id"],
                        "description": payload.get("description"),
                        "payee": payload.get("payee"),
                        "amount_cents": payload.get("amount_cents"),
                    },
                    response_payload={
                        "category": suggestion.category,
                        "category_status": suggestion.status,
                        "source": suggestion.source,
                    },
                    reasoning=(suggestion.reason or "")[:1000],
                    outcome="suggested",
                )

            suggested += 1
            results.append(
                {
                    "approval_id": r["approval_id"],
                    "suggested_category": suggestion.category,
                    "source": suggestion.source,
                    "reasoning": suggestion.reason,
                }
            )

    return {
        "scanned": scanned,
        "suggested": suggested,
        "unchanged": unchanged,
        "errors": errors,
        "dry_run": dry_run,
        "model": settings.categorizer_model,
        "endpoint": settings.categorizer_endpoint_url,
        "results": results,
    }


def _amount_from_payload(payload: dict[str, Any]) -> float:
    """Best-effort signed dollar amount from an approval payload.

    Prefers ``amount_cents`` (the authoritative integer the ingest pipeline
    stored); falls back to ``amount``. Returns 0.0 when neither parses — the
    categorizer keys off the surface text, not the amount, so a missing amount
    only loses the sign-validation hint, it doesn't break categorization.
    """
    cents = payload.get("amount_cents")
    if cents is not None:
        try:
            return float(Decimal(str(cents)) / Decimal(100))
        except (InvalidOperation, ValueError, TypeError):
            pass
    amt = payload.get("amount")
    if amt is not None:
        try:
            return float(amt)
        except (ValueError, TypeError):
            pass
    return 0.0
