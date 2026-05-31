"""Approval queue aging check.

Looks at approval_queue rows with status='pending' and ages them. If an item has
been waiting > 14 days, flag for CPA review with an escalation note. Items >30d
are surfaced as 'high' severity.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import text

from agents.sentinel.actions import ActionContext, execute_action

AGING_THRESHOLD_DAYS = 14
ESCALATION_THRESHOLD_DAYS = 30


async def run(ctx: ActionContext, **_) -> dict[str, Any]:
    rows = (
        await ctx.session.execute(
            text(
                """
                SELECT id::text AS id, entity_id::text AS entity_id,
                       subject_type, amount, reasoning,
                       extract(epoch FROM (now() - created_at))::int / 86400 AS age_days,
                       created_at
                FROM approval_queue
                WHERE status = 'pending'
                  AND created_at < now() - interval '14 days'
                ORDER BY created_at ASC
                LIMIT 100
                """
            )
        )
    ).mappings().all()

    findings = 0
    actions = 0

    for r in rows:
        age = int(r["age_days"] or 0)
        severity = "high" if age >= ESCALATION_THRESHOLD_DAYS else "medium"
        topic = f"sentinel-approval-aging-{r['id']}"

        # De-dup: skip if a memo with this topic already exists (any time, not just today)
        already = (
            await ctx.session.execute(
                text(
                    """
                    SELECT 1 FROM tax_review_queue
                    WHERE entity_id = :eid
                      AND submitter_id = 'sentinel/v1'
                      AND content->>'topic' = :topic
                    LIMIT 1
                    """
                ),
                {"eid": r["entity_id"], "topic": topic},
            )
        ).first()
        # Re-flag if escalation crossed threshold (was medium, now high) — encode in topic.
        if already and age < ESCALATION_THRESHOLD_DAYS:
            continue

        title = f"Approval pending {age} days ({r['subject_type']})"
        summary = (
            f"Approval queue row {r['id'][:8]} has been pending {age} days. "
            f"Subject: {r['subject_type']}. "
            f"Reasoning: {(r['reasoning'] or '')[:200]}"
        )
        res = await execute_action(
            ctx,
            action_type="flag_for_cpa_review",
            entity_id=__import__("uuid").UUID(r["entity_id"]),
            title=title,
            summary=summary,
            severity=severity,
            details={
                "approval_id": r["id"],
                "subject_type": r["subject_type"],
                "amount": str(r["amount"]) if r["amount"] is not None else None,
                "age_days": age,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            },
            payload={
                "tax_year": dt.date.today().year,
                "topic": topic,
                "confidence": "high",
                "form_reference": None,
                "recommendation": (
                    "Approve, reject, or request more information from the submitter."
                ),
            },
            reasoning=f"Approval aging: pending {age} days (threshold {AGING_THRESHOLD_DAYS}).",
        )
        findings += 1
        if res.get("ok"):
            actions += 1

    summary = f"approval_aging: {findings} aged items, {actions} flagged"
    return {"findings": findings, "actions": actions, "summary": summary}
