"""Chart-of-Accounts coverage check.

Counts journal_entry rows whose category_id corresponds to the catch-all
"Uncategorized" account (or whose reasoning_trace indicates no category was
chosen). If > 5% of the last 90 days are uncategorized, draft a memo.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import text

from agents.sentinel.actions import ActionContext, execute_action
from agents.sentinel.checks._helpers import list_active_entities

THRESHOLD_PCT = 5.0


async def run(ctx: ActionContext, **_) -> dict[str, Any]:
    entities = await list_active_entities(ctx.session)
    findings = 0
    actions = 0
    today = dt.date.today()
    month_key = today.strftime("%Y-%m")

    for ent in entities:
        row = (
            await ctx.session.execute(
                text(
                    """
                    SELECT
                      count(*)::int AS total,
                      sum(case when reasoning_trace ILIKE '%uncategor%' then 1 else 0 end)::int AS uncat
                    FROM journal_entry
                    WHERE entity_id = :eid
                      AND posted_at >= now() - interval '90 days'
                    """
                ),
                {"eid": str(ent.id)},
            )
        ).first()
        total = int(row.total or 0) if row else 0
        uncat = int(row.uncat or 0) if row else 0
        if total < 20:
            continue
        pct = (uncat / total) * 100.0
        if pct < THRESHOLD_PCT:
            continue
        topic = f"sentinel-coa-{ent.slug}-{month_key}"
        title = f"COA coverage gap on {ent.display_name} ({pct:.1f}%)"
        summary = (
            f"{ent.display_name}: {uncat} of {total} entries in the last 90 days "
            f"appear uncategorized ({pct:.1f}%, threshold {THRESHOLD_PCT}%). "
            f"Consider running the categorization MCP tool over the pile."
        )
        res = await execute_action(
            ctx,
            action_type="draft_memo",
            entity_id=ent.id,
            title=title,
            summary=summary,
            severity="medium",
            details={"total": total, "uncategorized": uncat, "pct": pct},
            payload={"tax_year": today.year, "topic": topic, "confidence": "medium"},
            reasoning=f"COA coverage: {pct:.1f}% uncategorized on {ent.slug}.",
        )
        findings += 1
        if res.get("ok"):
            actions += 1

    return {"findings": findings, "actions": actions, "summary": f"coa_coverage: {findings}"}
