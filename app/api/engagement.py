"""Engagement policy API — the per-workspace professional-substitute switch.

Routes:

  GET  /api/workspaces/{wid}/engagement/policy
       Read the current engagement_policy (creates with safe defaults if absent).
       Role floor: viewer.

  POST /api/workspaces/{wid}/engagement/policy
       Update review_mode / has_licensed_pro / prefer_local_inference /
       submission_channels / rail_kill_switch. Role floor: admin.

review_mode = pro_required (default — classic CPA gate) | self_attest_allowed
(a user with no pro may self-certify) | autonomous_off.
"""
from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.db import SessionLocal
from app.engagement.policy import RAIL_CHANNELS, get_or_create_policy, update_policy
from app.models.ledger import ReviewMode
from app.tenancy import WorkspaceContext, require_role, resolve_workspace
from app.workspace_session import workspace_session

_log = logging.getLogger("acctops.engagement.api")
router = APIRouter(tags=["engagement"])


class EngagementPolicyOut(BaseModel):
    workspace_id: UUID
    review_mode: str
    has_licensed_pro: bool
    prefer_local_inference: bool
    submission_channels: dict
    rail_kill_switch: bool
    valid_review_modes: list[str] = [m.value for m in ReviewMode]
    rail_channels: list[str] = list(RAIL_CHANNELS)


class EngagementPolicyUpdate(BaseModel):
    review_mode: str | None = None
    has_licensed_pro: bool | None = None
    prefer_local_inference: bool | None = None
    submission_channels: dict | None = None
    rail_kill_switch: bool | None = None


@router.get(
    "/api/workspaces/{workspace_id}/engagement/policy",
    response_model=EngagementPolicyOut,
)
async def get_engagement_policy(
    workspace_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> EngagementPolicyOut:
    require_role(workspace, "viewer")
    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        policy = await get_or_create_policy(session, workspace.workspace_id)
    return EngagementPolicyOut(
        workspace_id=workspace.workspace_id,
        review_mode=policy.review_mode.value,
        has_licensed_pro=policy.has_licensed_pro,
        prefer_local_inference=policy.prefer_local_inference,
        submission_channels=policy.submission_channels,
        rail_kill_switch=policy.rail_kill_switch,
    )


@router.post(
    "/api/workspaces/{workspace_id}/engagement/policy",
    response_model=EngagementPolicyOut,
)
async def post_engagement_policy(
    workspace_id: UUID,
    body: EngagementPolicyUpdate,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> EngagementPolicyOut:
    require_role(workspace, "admin")

    if body.review_mode is not None:
        try:
            ReviewMode(body.review_mode.strip().lower())
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=(
                    "review_mode must be one of: "
                    + ", ".join(m.value for m in ReviewMode)
                ),
            )

    if body.submission_channels is not None:
        unknown = set(body.submission_channels) - set(RAIL_CHANNELS)
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unknown submission channel(s): {sorted(unknown)}. "
                    f"Valid: {list(RAIL_CHANNELS)}."
                ),
            )

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        await update_policy(
            session,
            workspace.workspace_id,
            review_mode=body.review_mode,
            has_licensed_pro=body.has_licensed_pro,
            prefer_local_inference=body.prefer_local_inference,
            submission_channels=body.submission_channels,
            rail_kill_switch=body.rail_kill_switch,
        )

    return await get_engagement_policy(workspace_id, workspace)
