"""Uncategorized transactions check.

Counts approval_queue rows of subject_type='bank_transaction_import' that are
'pending'. If the pile exceeds a workspace-wide threshold (default 25),
draft a memo asking for a categorization session.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import text

from agents.sentinel.actions import ActionContext, execute_action
from agents.sentinel.checks._helpers import list_active_entities

PILEUP_THRESHOLD = 25


async def run(ctx: ActionContext, **_) -> dict[str, Any]:
    entities = await list_active_entities(ctx.session)
    findings = 0
    actions = 0

    for ent in entities:
        row = (
            await ctx.session.execute(
                text(
                    """
                    SELECT count(*)::int AS c
                    FROM approval_queue
                    WHERE entity_id = :eid
                      AND status = 'pending'
                      AND subject_type IN (
                        'bank_transaction_import',
                        'journal_recategorization'
                      )
                    """
                ),
                {"eid": str(ent.id)},
            )
        ).first()
        cnt = int(row.c or 0) if row else 0
        if cnt < PILEUP_THRESHOLD:
            continue

        topic = f"sentinel-uncategorized-{ent.slug}-{dt.date.today().isoformat()}"
        title = f"{cnt} uncategorized transactions for {ent.display_name}"
        summary = (
            f"{ent.display_name} has {cnt} pending bank-import or recategorization "
            f"approvals (threshold {PILEUP_THRESHOLD}). Suggest running the "
            f"batch pre-analyzer (`analyze_pending_approvals`) and a focused "
            f"categorization session."
        )
        res = await execute_action(
            ctx,
            action_type="draft_memo",
            entity_id=ent.id,
            title=title,
            summary=summary,
            severity="medium",
            details={"pending_count": cnt, "threshold": PILEUP_THRESHOLD},
            payload={
                "tax_year": dt.date.today().year,
                "topic": topic,
                "confidence": "high",
            },
            reasoning=f"Uncategorized check: {cnt} pending >= {PILEUP_THRESHOLD}.",
        )
        findings += 1
        if res.get("ok"):
            actions += 1

    return {
        "findings": findings,
        "actions": actions,
        "summary": f"uncategorized: {findings} entities over threshold",
    }
