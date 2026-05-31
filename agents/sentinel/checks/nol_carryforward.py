"""NOL carryforward expiration tracking.

Pre-2018 NOLs expire after 20 years; post-2017 NOLs don't expire but are
capped at 80% of taxable income. Sentinel surfaces a perennial annual reminder
to track unused NOLs on Form 1120 line 29a + Schedule M-2 if any prior loss
exists in the books.

Implementation note: full NOL tracking requires a dedicated table; for now we
heuristically flag any C-Corp/S-Corp that had a year with net loss in the last
five years.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from agents.sentinel.actions import ActionContext, execute_action
from agents.sentinel.checks._helpers import list_active_entities

CORP_KINDS = {"c_corp", "s_corp"}


async def _yearly_net(session, entity_id: str, years: list[int]) -> dict[int, Decimal]:
    rows = (
        await session.execute(
            text(
                """
                SELECT extract(year FROM effective_date)::int AS ty,
                       coalesce(sum(usd_equivalent_at_event_time), 0) AS net
                FROM journal_entry
                WHERE entity_id = :eid
                  AND extract(year FROM effective_date) = ANY(:years)
                GROUP BY 1
                """
            ),
            {"eid": entity_id, "years": years},
        )
    ).mappings().all()
    return {int(r["ty"]): Decimal(str(r["net"] or 0)) for r in rows}


async def run(ctx: ActionContext, **_) -> dict[str, Any]:
    entities = await list_active_entities(ctx.session)
    findings = 0
    actions = 0
    today = dt.date.today()
    quarter_key = f"{today.year}Q{(today.month - 1) // 3 + 1}"
    look_years = list(range(today.year - 5, today.year))  # last 5 closed years

    for ent in entities:
        if ent.kind not in CORP_KINDS:
            continue
        yearly = await _yearly_net(ctx.session, str(ent.id), look_years)
        loss_years = sorted([y for y, n in yearly.items() if n < 0])
        if not loss_years:
            continue

        topic = f"sentinel-nol-{ent.slug}-{quarter_key}"
        title = f"NOL carryforward tracking — {ent.display_name}"
        summary = (
            f"{ent.display_name} had net losses in: "
            f"{', '.join(map(str, loss_years))}. "
            f"Track NOL utilization on Form 1120 line 29a (subject to 80% TI cap "
            f"for post-2017 NOLs). Older NOLs (pre-2018) expire after 20 years."
        )
        res = await execute_action(
            ctx,
            action_type="flag_for_cpa_review",
            entity_id=ent.id,
            title=title,
            summary=summary,
            severity="medium",
            details={"loss_years": loss_years, "yearly_net": {str(k): str(v) for k, v in yearly.items()}},
            payload={
                "tax_year": today.year - 1,
                "topic": topic,
                "confidence": "medium",
                "form_reference": "1120-line-29a",
                "citations": [{"type": "form", "value": "Form 1120 line 29a"}],
            },
            reasoning=f"NOL tracking: {ent.slug} had losses in {loss_years}.",
        )
        findings += 1
        if res.get("ok"):
            actions += 1

    return {"findings": findings, "actions": actions, "summary": f"nol_carryforward: {findings}"}
