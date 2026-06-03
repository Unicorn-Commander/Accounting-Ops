"""Corporate formalities check.

For corp entities, surface a perennial annual reminder for:
  - Annual board minutes / shareholder meeting (state-dependent).
  - Registered agent renewal (state-dependent).
  - Annual report filing with the Secretary of State.

Light implementation: emits one informational finding per corp entity per year.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from agents.sentinel.actions import ActionContext, execute_action
from agents.sentinel.checks._helpers import list_active_entities

CORP_KINDS = {"c_corp", "s_corp", "llc"}


async def run(ctx: ActionContext, **_) -> dict[str, Any]:
    entities = await list_active_entities(ctx.session)
    findings = 0
    actions = 0
    today = dt.date.today()
    year_key = str(today.year)

    for ent in entities:
        if ent.kind not in CORP_KINDS:
            continue
        if ent.country_code != "US":
            continue
        title = f"Corporate formalities checklist — {ent.display_name} {year_key}"
        summary = (
            f"{ent.display_name} ({ent.kind.upper()}): annual corporate-formality "
            f"checklist for {year_key}:\n\n"
            f"  1. Hold annual board / shareholder meeting + record minutes.\n"
            f"  2. Confirm registered agent for {today.year} is paid + active.\n"
            f"  3. File state annual report (varies by state; SC annual report due "
            f"by 15th day of 4th month after fiscal year-end for corps).\n"
            f"  4. Update foreign-qualification filings if entity operates in "
            f"states beyond domicile.\n\n"
            f"Confirm each item with the registered agent / corporate counsel."
        )
        res = await execute_action(
            ctx,
            action_type="log_finding",
            entity_id=ent.id,
            title=title,
            summary=summary,
            severity="info",
            details={"year": year_key, "kind": ent.kind},
            reasoning=f"Corporate formalities annual reminder for {ent.slug}.",
        )
        findings += 1
        if res.get("ok"):
            actions += 1

    return {"findings": findings, "actions": actions, "summary": f"corporate_formalities: {findings}"}
