"""Connector health check.

Reads connector_state rows and flags any connector whose last_sync_at is
older than its expected cadence (default: 24h). Helps surface dead Plaid /
SimpleFIN / eBay / Stripe connections quickly.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import text

from agents.sentinel.actions import ActionContext, execute_action

CONNECTOR_STALE_HOURS = 48


async def run(ctx: ActionContext, **_) -> dict[str, Any]:
    rows = (
        await ctx.session.execute(
            text(
                """
                SELECT id::text, entity_id::text, connector_name, connector_instance_name,
                       last_sync_at,
                       extract(epoch FROM (now() - coalesce(last_sync_at, created_at))) / 3600 AS hours_since
                FROM connector_state
                ORDER BY last_sync_at NULLS FIRST
                LIMIT 100
                """
            )
        )
    ).mappings().all()

    findings = 0
    actions = 0
    today = dt.date.today()

    for r in rows:
        hours = float(r["hours_since"] or 999)
        if hours < CONNECTOR_STALE_HOURS:
            continue
        topic = f"sentinel-connector-{r['connector_name']}-{r['id'][:8]}-{today.isoformat()}"
        title = f"Connector stale: {r['connector_name']} ({r['connector_instance_name']})"
        summary = (
            f"Connector '{r['connector_instance_name']}' ({r['connector_name']}) "
            f"has not synced for {hours:.0f} hours (threshold {CONNECTOR_STALE_HOURS}h). "
            f"Reconnect or check credentials."
        )
        res = await execute_action(
            ctx,
            action_type="log_finding",
            entity_id=__import__("uuid").UUID(r["entity_id"]),
            title=title,
            summary=summary,
            severity="medium" if hours < 168 else "high",
            details={
                "connector_id": r["id"],
                "connector_name": r["connector_name"],
                "connector_instance_name": r["connector_instance_name"],
                "hours_since_sync": round(hours, 1),
                "last_sync_at": r["last_sync_at"].isoformat() if r["last_sync_at"] else None,
            },
            reasoning=f"Connector health: {r['connector_name']} stale {hours:.0f}h.",
        )
        findings += 1
        if res.get("ok"):
            actions += 1

    return {"findings": findings, "actions": actions, "summary": f"connector_health: {findings} stale"}
