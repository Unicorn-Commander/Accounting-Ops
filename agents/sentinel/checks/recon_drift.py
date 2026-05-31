"""Reconciliation drift check.

Counts journal_entry rows in the last 30 days that lack a matching recon_link
row. If drift > 10% of journal volume, flag for review.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import text

from agents.sentinel.actions import ActionContext, execute_action
from agents.sentinel.checks._helpers import list_active_entities

DRIFT_THRESHOLD_PCT = 10.0
MIN_VOLUME = 20  # don't flag low-volume entities


async def run(ctx: ActionContext, **_) -> dict[str, Any]:
    entities = await list_active_entities(ctx.session)
    findings = 0
    actions = 0

    for ent in entities:
        row = (
            await ctx.session.execute(
                text(
                    """
                    SELECT
                      count(je.id)::int AS total,
                      count(rl.id)::int AS recd
                    FROM journal_entry je
                    LEFT JOIN recon_link rl ON rl.journal_entry_id = je.id
                    WHERE je.entity_id = :eid
                      AND je.posted_at >= now() - interval '30 days'
                    """
                ),
                {"eid": str(ent.id)},
            )
        ).first()
        total = int(row.total or 0) if row else 0
        recd = int(row.recd or 0) if row else 0
        if total < MIN_VOLUME:
            continue
        drift = total - recd
        pct = (drift / total) * 100.0 if total else 0.0
        if pct < DRIFT_THRESHOLD_PCT:
            continue

        topic = f"sentinel-recon-drift-{ent.slug}-{dt.date.today().isoformat()}"
        title = f"Reconciliation drift {pct:.1f}% on {ent.display_name}"
        summary = (
            f"{ent.display_name}: {drift} of {total} journal entries in the past "
            f"30 days have no recon_link row ({pct:.1f}% drift, threshold "
            f"{DRIFT_THRESHOLD_PCT:.0f}%). Suggest running the receipt-reconciler "
            f"sweep or investigating un-linked entries."
        )
        res = await execute_action(
            ctx,
            action_type="draft_memo",
            entity_id=ent.id,
            title=title,
            summary=summary,
            severity="medium" if pct < 25 else "high",
            details={"total": total, "reconciled": recd, "drift_pct": pct},
            payload={"tax_year": dt.date.today().year, "topic": topic, "confidence": "high"},
            reasoning=f"Recon drift: {pct:.1f}% >= {DRIFT_THRESHOLD_PCT}%.",
        )
        findings += 1
        if res.get("ok"):
            actions += 1

    return {
        "findings": findings,
        "actions": actions,
        "summary": f"recon_drift: {findings} entities flagged",
    }
