"""§195 startup-cost election timing check.

Newly-incorporated entities (within their first tax year) get a one-time
§195 election that lets the first $5,000 of startup costs be deducted in
year 1 (rest amortized over 15 years). The election attaches to the FIRST
return filed — easy to miss.

If the entity is in its first tax year AND we haven't yet drafted a memo,
draft one.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from agents.sentinel.actions import ActionContext, execute_action
from agents.sentinel.checks._helpers import list_active_entities


async def run(ctx: ActionContext, **_) -> dict[str, Any]:
    entities = await list_active_entities(ctx.session)
    findings = 0
    actions = 0
    today = dt.date.today()

    for ent in entities:
        if ent.incorporated_at is None:
            continue
        # Is this entity in or recently exited its first tax year?
        years_since = today.year - ent.incorporated_at.year
        if years_since not in (0, 1):
            continue
        if ent.kind not in {"c_corp", "s_corp", "llc", "partnership"}:
            continue

        first_ty = ent.incorporated_at.year
        topic = f"sentinel-sec195-{ent.slug}-{first_ty}"

        title = f"§195 startup-cost election for {ent.display_name} TY{first_ty}"
        summary = (
            f"{ent.display_name} was incorporated on {ent.incorporated_at}. "
            f"The §195 election to deduct the first $5,000 of startup costs "
            f"(amortize the rest over 15 years) must be made on the FIRST tax "
            f"return filed (TY{first_ty}). If that return is still in draft, "
            f"verify the election is attached. If already filed without it, the "
            f"election is generally lost."
        )
        res = await execute_action(
            ctx,
            action_type="flag_for_cpa_review",
            entity_id=ent.id,
            title=title,
            summary=summary,
            severity="medium",
            details={
                "incorporated_at": ent.incorporated_at.isoformat(),
                "first_tax_year": first_ty,
                "section": "§195",
            },
            payload={
                "tax_year": first_ty,
                "topic": topic,
                "confidence": "medium",
                "recommendation": "Confirm §195 election language is on the first return.",
                "citations": [{"type": "section", "value": "§195"}],
            },
            reasoning=f"§195 timing: {ent.slug} in its first/second year ({first_ty}).",
        )
        findings += 1
        if res.get("ok"):
            actions += 1

    return {
        "findings": findings,
        "actions": actions,
        "summary": f"sec_195_election: {findings} flagged",
    }
