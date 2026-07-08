"""§1202 QSBS holding-period tracking.

For C-Corp entities that may qualify as QSBS issuers, surface a perennial
reminder to track founders' / early investors' 5-year holding periods (§1202
excludes up to $10M of gain on qualifying stock held >5 years).

This is a structural reminder; the actual tracking belongs on a separate
stockholder ledger. Sentinel just makes sure the topic comes up annually.
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
        if ent.kind != "c_corp":
            continue
        if ent.country_code != "US":
            continue
        title = f"§1202 QSBS holding-period tracking — {ent.display_name}"

        # When did the entity incorporate? Holding period starts at issuance.
        if ent.incorporated_at:
            years_old = today.year - ent.incorporated_at.year
            crosses_5yr = ent.incorporated_at + dt.timedelta(days=5 * 365)
            milestone = (
                f"Earliest possible §1202-eligible stock holder crosses 5-year "
                f"mark on or around {crosses_5yr.isoformat()} "
                f"({years_old} years since incorporation)."
            )
        else:
            milestone = "Set entity.incorporated_at to enable §1202 milestone tracking."

        summary = (
            f"{ent.display_name} is a C-Corp and may have issued §1202 Qualified "
            f"Small Business Stock to founders / early investors. §1202 excludes "
            f"up to the greater of $10M or 10x basis on gain at sale, provided "
            f"the stock was held >5 years AND the entity met the 80% active-"
            f"business test continuously. {milestone}"
        )
        res = await execute_action(
            ctx,
            action_type="log_finding",
            entity_id=ent.id,
            title=title,
            summary=summary,
            severity="info",
            details={
                "incorporated_at": ent.incorporated_at.isoformat() if ent.incorporated_at else None,
                "qsbs_5yr_milestone": (ent.incorporated_at + dt.timedelta(days=5 * 365)).isoformat() if ent.incorporated_at else None,
            },
            reasoning=f"QSBS milestone tracking: {ent.slug} C-Corp.",
        )
        findings += 1
        if res.get("ok"):
            actions += 1

    return {"findings": findings, "actions": actions, "summary": f"qsbs_holding: {findings}"}
