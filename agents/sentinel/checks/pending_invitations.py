"""Pending invitations / unfinished setup check.

Right now there is no invitations table. We treat 'workspace with zero
accountant-role members' as a setup gap and surface it. Future: when
invitations land, switch this to count stale invites.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import text

from agents.sentinel.actions import ActionContext, execute_action


async def run(ctx: ActionContext, **_) -> dict[str, Any]:
    row = (
        await ctx.session.execute(
            text(
                """
                SELECT count(*)::int AS c
                FROM public.workspace_membership
                WHERE workspace_id = :wid AND role IN ('accountant', 'admin', 'owner')
                """
            ),
            {"wid": str(ctx.workspace_id)},
        )
    ).first()
    accountant_like = int(row.c or 0) if row else 0
    if accountant_like >= 2:
        return {"findings": 0, "actions": 0, "summary": "pending_invitations: ok"}

    # Surface as a finding only (informational); avoid spamming a memo for what
    # is effectively a one-time configuration nudge.
    res = await execute_action(
        ctx,
        action_type="log_finding",
        entity_id=None,
        title="Workspace has no second admin or accountant",
        summary=(
            "Only one role at admin+ in this workspace. Consider inviting your "
            "CPA (accountant role) so they can review tax_review_queue items "
            "directly."
        ),
        severity="low",
        details={"admin_like_count": accountant_like},
        reasoning="Pending invitations check: < 2 admin/accountant members.",
    )
    return {
        "findings": 1,
        "actions": 1 if res.get("ok") else 0,
        "summary": "pending_invitations: 1 finding",
    }
