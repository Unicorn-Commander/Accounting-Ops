"""Submission-rail API — list channels + propose (NO transmit).

The submission rail is DORMANT by default: the only enabled channel is
``paper_print`` (print + mail / hand to recipient — today's behavior). The four
electronic transmission channels (efile_irs / state_portal / eftps / esign) are
present as scaffolding and report ``enabled: false`` until an admin both enables
them in the workspace EngagementPolicy AND an independent legal/risk gate is
satisfied — and even then nothing transmits yet (see app/submission/rail.py).

Routes:

  GET  /api/workspaces/{wid}/submission/channels
       For each channel: {channel, enabled, transmits, reason}. paper_print is
       always enabled; the four transmission channels reflect
       ``policy.channel_enabled(...)`` (false by default). Role floor: viewer.

  POST /api/workspaces/{wid}/entities/{eid}/submission/{channel}/propose
       Body {work_product_id}. Returns the adapter's propose() result (a
       human-confirmable description of what WOULD happen — no side effects) for
       paper_print; for a disabled / gated transmission channel returns 403 with
       the FeatureDisabled message. There is deliberately NO execute route that
       transmits. Role floor: accountant.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

from app.authz import assert_entity_in_scope
from app.db import SessionLocal
from app.engagement.policy import get_or_create_policy
from app.submission.rail import (
    ALL_CHANNELS,
    FeatureDisabled,
    SubmissionChannel,
    is_transmission_channel,
    resolve_adapter,
)
from app.tenancy import WorkspaceContext, require_role, resolve_workspace
from app.workspace_session import workspace_session

_log = logging.getLogger("acctops.submission.api")
router = APIRouter(tags=["submission"])


# ---------------------------------------------------------------------------
# Channels listing
# ---------------------------------------------------------------------------


@router.get("/api/workspaces/{workspace_id}/submission/channels")
async def list_submission_channels(
    workspace_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    require_role(workspace, "viewer")
    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        policy = await get_or_create_policy(session, workspace.workspace_id)

    channels: list[dict[str, Any]] = []
    for channel in ALL_CHANNELS:
        if not is_transmission_channel(channel):
            # paper_print — the always-on default.
            channels.append(
                {
                    "channel": channel,
                    "enabled": True,
                    "transmits": False,
                    "reason": "Paper printing is always available (the default).",
                }
            )
            continue
        enabled = policy.channel_enabled(channel)
        if enabled:
            # Policy-enabled, but the rail is still dormant (an independent legal/
            # risk gate blocks it and execute is a no-op-raise). Be explicit.
            reason = (
                "Enabled in the engagement policy, but transmission is not yet "
                "legally enabled (dormant scaffolding)."
            )
        elif policy.rail_kill_switch:
            reason = "Blocked: the submission-rail kill-switch is engaged."
        else:
            reason = "Disabled (default OFF — not enabled in the engagement policy)."
        channels.append(
            {
                "channel": channel,
                "enabled": enabled,
                "transmits": True,
                "reason": reason,
            }
        )

    return {
        "workspace_id": str(workspace.workspace_id),
        "rail_kill_switch": policy.rail_kill_switch,
        "channels": channels,
    }


# ---------------------------------------------------------------------------
# Propose (NO transmit)
# ---------------------------------------------------------------------------


class ProposeBody(BaseModel):
    work_product_id: UUID


async def _fetch_work_product_min(session, entity_id: UUID, wp_id: UUID) -> dict | None:
    return (
        await session.execute(
            text(
                """
                SELECT id::text, entity_id::text, kind, tax_year, title, status
                FROM work_product
                WHERE id = :id AND entity_id = :eid
                """
            ),
            {"id": str(wp_id), "eid": str(entity_id)},
        )
    ).mappings().first()


@router.post(
    "/api/workspaces/{workspace_id}/entities/{entity_id}/submission/{channel}/propose"
)
async def propose_submission(
    workspace_id: UUID,
    entity_id: UUID,
    channel: str,
    body: ProposeBody,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    require_role(workspace, "bookkeeper")
    assert_entity_in_scope(workspace, entity_id)

    # Validate the channel name up front (clear 404 vs a generic FeatureDisabled).
    valid_channels = {c.value for c in SubmissionChannel}
    if channel not in valid_channels:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown submission channel {channel!r}. "
            f"Valid: {sorted(valid_channels)}.",
        )

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        wp = await _fetch_work_product_min(session, entity_id, body.work_product_id)
        if wp is None:
            raise HTTPException(status_code=404, detail="Work product not found")
        policy = await get_or_create_policy(session, workspace.workspace_id)

    # Resolve + propose. A disabled / dormant transmission channel raises
    # FeatureDisabled at resolve OR propose — caught and surfaced as 403. There is
    # intentionally no execute path here: at most we describe what WOULD happen.
    try:
        adapter = resolve_adapter(channel, policy)
        result = adapter.propose(dict(wp))
    except FeatureDisabled as e:
        # 403 Forbidden — the channel is disabled / not legally enabled. The
        # message explains why (default OFF, kill-switch, or dormant scaffolding).
        raise HTTPException(status_code=403, detail=e.message)

    _log.info(
        "submission.propose channel=%s wp=%s ws=%s entity=%s by=%s",
        channel, wp["id"], workspace.workspace_id, entity_id, workspace.user_uc_uid,
    )
    return {"ok": True, "channel": channel, "proposal": result}
