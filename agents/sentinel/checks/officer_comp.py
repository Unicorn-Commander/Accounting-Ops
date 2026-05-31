"""Officer compensation reasonableness check (corp entities).

For C-Corp / S-Corp entities with revenue > 0 in the last completed tax year,
remind the CPA to evaluate officer compensation reasonableness (IRS §162(a)(1)
and §1366 rules for S-Corps). The IRS specifically scrutinises S-Corp owners
taking $0 in W-2 wages while drawing distributions.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from agents.sentinel.actions import ActionContext, execute_action
from agents.sentinel.checks._helpers import list_active_entities

CORP_KINDS = {"c_corp", "s_corp"}
MIN_REVENUE_FOR_FLAG = Decimal("10000")


async def _entity_revenue(session, entity_id: str, tax_year: int) -> Decimal:
    """Sum credit-side amounts (revenue proxy) for the year."""
    row = (
        await session.execute(
            text(
                """
                SELECT coalesce(sum(usd_equivalent_at_event_time), 0) AS rev
                FROM journal_entry
                WHERE entity_id = :eid
                  AND extract(year FROM effective_date) = :ty
                  AND usd_equivalent_at_event_time > 0
                """
            ),
            {"eid": entity_id, "ty": tax_year},
        )
    ).first()
    return Decimal(str(row.rev or 0)) if row else Decimal(0)


async def run(ctx: ActionContext, **_) -> dict[str, Any]:
    entities = await list_active_entities(ctx.session)
    findings = 0
    actions = 0
    today = dt.date.today()
    last_ty = today.year - 1
    quarter_key = f"{today.year}Q{(today.month - 1) // 3 + 1}"

    for ent in entities:
        if ent.kind not in CORP_KINDS:
            continue
        if ent.incorporated_at and ent.incorporated_at.year > last_ty:
            continue
        revenue = await _entity_revenue(ctx.session, str(ent.id), last_ty)
        if revenue < MIN_REVENUE_FOR_FLAG:
            continue

        topic = f"sentinel-officer-comp-{ent.slug}-{quarter_key}-{last_ty}"
        title = f"Officer compensation reasonableness — {ent.display_name} TY{last_ty}"
        summary = (
            f"{ent.display_name} ({ent.kind.upper()}) had ${revenue:,.0f} in "
            f"revenue-like flows for TY{last_ty}. Confirm officer W-2 compensation "
            f"is reasonable given role, hours, and comparable industry pay. "
            + (
                "S-Corps with $0 owner W-2 + meaningful distributions are an IRS "
                "audit trigger — distributions can be recharacterized as wages."
                if ent.kind == "s_corp"
                else "C-Corps that pay all officer comp as distributions risk a "
                "constructive dividend reclassification."
            )
        )
        res = await execute_action(
            ctx,
            action_type="flag_for_cpa_review",
            entity_id=ent.id,
            title=title,
            summary=summary,
            severity="medium",
            details={
                "tax_year": last_ty,
                "revenue_proxy_usd": str(revenue),
                "kind": ent.kind,
            },
            payload={
                "tax_year": last_ty,
                "topic": topic,
                "confidence": "medium",
                "form_reference": "1120-line-12" if ent.kind == "c_corp" else "1120-S-line-7",
                "recommendation": (
                    "Pull comparable industry data and confirm officer comp is "
                    "documented in board minutes."
                ),
                "citations": [
                    {"type": "section", "value": "§162(a)(1)"},
                    {"type": "section", "value": "§1366"} if ent.kind == "s_corp" else {"type": "section", "value": "§162"},
                ],
            },
            reasoning=f"Officer comp check: {ent.slug} revenue ${revenue} for {last_ty}.",
        )
        findings += 1
        if res.get("ok"):
            actions += 1

    return {
        "findings": findings,
        "actions": actions,
        "summary": f"officer_comp: {findings} entities flagged",
    }
