"""Cash runway forecast check.

Estimates burn rate from the last 90 days of journal activity and compares it
to current cash. If runway < 6 months, draft a high-severity memo so the
workspace owner can see it on the dashboard.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from agents.sentinel.actions import ActionContext, execute_action
from agents.sentinel.checks._helpers import list_active_entities

RUNWAY_THRESHOLD_MONTHS = 6


async def _burn_rate_monthly(session, entity_id: str) -> Decimal:
    """Average monthly NET burn (negative number) over last 90 days."""
    row = (
        await session.execute(
            text(
                """
                SELECT coalesce(sum(usd_equivalent_at_event_time), 0) AS net
                FROM journal_entry
                WHERE entity_id = :eid
                  AND effective_date >= current_date - interval '90 days'
                """
            ),
            {"eid": entity_id},
        )
    ).first()
    net_90d = Decimal(str(row.net or 0)) if row else Decimal(0)
    return net_90d / Decimal(3)  # roughly per-month


async def _cash_balance(session, entity_id: str) -> Decimal:
    """Sum of journal_entry against cash-flavored categories — proxy for balance."""
    row = (
        await session.execute(
            text(
                """
                SELECT coalesce(sum(usd_equivalent_at_event_time), 0) AS bal
                FROM journal_entry
                WHERE entity_id = :eid
                """
            ),
            {"eid": entity_id},
        )
    ).first()
    return Decimal(str(row.bal or 0)) if row else Decimal(0)


async def run(ctx: ActionContext, **_) -> dict[str, Any]:
    entities = await list_active_entities(ctx.session)
    findings = 0
    actions = 0
    today = dt.date.today()
    month_key = today.strftime("%Y-%m")

    for ent in entities:
        cash = await _cash_balance(ctx.session, str(ent.id))
        burn = await _burn_rate_monthly(ctx.session, str(ent.id))
        if burn >= 0:
            continue  # not burning — runway is irrelevant
        monthly_burn = abs(burn)
        runway_months = cash / monthly_burn if monthly_burn > 0 else Decimal("999")
        if runway_months >= RUNWAY_THRESHOLD_MONTHS:
            continue

        topic = f"sentinel-runway-{ent.slug}-{month_key}"
        title = f"Cash runway {runway_months:.1f} months — {ent.display_name}"
        summary = (
            f"{ent.display_name}: estimated monthly burn ${monthly_burn:,.0f} "
            f"against cash-equivalent of ${cash:,.0f} → "
            f"{runway_months:.1f} months runway (threshold "
            f"{RUNWAY_THRESHOLD_MONTHS}). This is a rough heuristic; the CPA "
            f"should validate against actual bank balances."
        )
        res = await execute_action(
            ctx,
            action_type="draft_memo",
            entity_id=ent.id,
            title=title,
            summary=summary,
            severity="high" if runway_months < 3 else "medium",
            details={
                "cash_proxy_usd": str(cash),
                "monthly_burn_usd": str(monthly_burn),
                "runway_months": str(runway_months.quantize(Decimal('0.1'))),
            },
            payload={"tax_year": today.year, "topic": topic, "confidence": "low"},
            reasoning=f"Cash runway: {runway_months:.1f} mo for {ent.slug}.",
        )
        findings += 1
        if res.get("ok"):
            actions += 1

    return {"findings": findings, "actions": actions, "summary": f"cash_runway: {findings}"}
