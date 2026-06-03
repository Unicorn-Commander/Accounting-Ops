"""Year-over-year financial diff.

For each entity, compare last calendar year's totals vs the year prior.
If any major category swings >50% (and absolute change > $1000), flag for
review. This is a "what changed and is it expected?" sanity check.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from agents.sentinel.actions import ActionContext, execute_action
from agents.sentinel.checks._helpers import list_active_entities

PCT_THRESHOLD = Decimal("50")  # +/- 50%
ABS_THRESHOLD = Decimal("1000")


async def _yearly_totals(session, entity_id: str, year: int) -> tuple[Decimal, Decimal]:
    """Return (revenue_proxy_positive, expense_proxy_negative)."""
    row = (
        await session.execute(
            text(
                """
                SELECT
                  coalesce(sum(case when usd_equivalent_at_event_time > 0
                                    then usd_equivalent_at_event_time else 0 end), 0) AS rev,
                  coalesce(sum(case when usd_equivalent_at_event_time < 0
                                    then usd_equivalent_at_event_time else 0 end), 0) AS exp
                FROM journal_entry
                WHERE entity_id = :eid
                  AND extract(year FROM effective_date) = :ty
                """
            ),
            {"eid": entity_id, "ty": year},
        )
    ).first()
    rev = Decimal(str(row.rev or 0)) if row else Decimal(0)
    exp = Decimal(str(row.exp or 0)) if row else Decimal(0)
    return rev, exp


async def run(ctx: ActionContext, **_) -> dict[str, Any]:
    entities = await list_active_entities(ctx.session)
    findings = 0
    actions = 0
    today = dt.date.today()
    this_y = today.year - 1
    last_y = today.year - 2
    month_key = today.strftime("%Y-%m")

    for ent in entities:
        rev_this, exp_this = await _yearly_totals(ctx.session, str(ent.id), this_y)
        rev_last, exp_last = await _yearly_totals(ctx.session, str(ent.id), last_y)

        flagged = []
        for label, cur, last in (
            ("revenue", rev_this, rev_last),
            ("expense", exp_this, exp_last),
        ):
            if abs(cur) < ABS_THRESHOLD and abs(last) < ABS_THRESHOLD:
                continue
            base = abs(last) if abs(last) > 0 else Decimal(1)
            pct = ((abs(cur) - abs(last)) / base) * Decimal(100)
            if abs(pct) >= PCT_THRESHOLD and abs(abs(cur) - abs(last)) >= ABS_THRESHOLD:
                flagged.append({
                    "label": label,
                    "current": str(cur),
                    "prior": str(last),
                    "pct_change": str(pct.quantize(Decimal('0.1'))),
                })
        if not flagged:
            continue

        topic = f"sentinel-yoy-{ent.slug}-{month_key}"
        title = f"YoY swing on {ent.display_name} ({len(flagged)} category)"
        summary = (
            f"{ent.display_name} TY{this_y} vs TY{last_y}: "
            + "; ".join(
                f"{f['label']} {f['pct_change']}% (${f['prior']} -> ${f['current']})"
                for f in flagged
            )
            + ". Worth reviewing whether the change is expected (M&A, pricing, etc.)."
        )
        res = await execute_action(
            ctx,
            action_type="draft_memo",
            entity_id=ent.id,
            title=title,
            summary=summary,
            severity="medium",
            details={"flagged": flagged, "current_year": this_y, "prior_year": last_y},
            payload={"tax_year": this_y, "topic": topic, "confidence": "medium"},
            reasoning=f"YoY diff: {len(flagged)} swings on {ent.slug}.",
        )
        findings += 1
        if res.get("ok"):
            actions += 1

    return {"findings": findings, "actions": actions, "summary": f"yoy_diff: {findings}"}
