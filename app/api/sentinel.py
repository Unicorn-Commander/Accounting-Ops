"""Sentinel API: config, findings, scheduled actions, check runs.

Routes:

  GET  /api/workspaces/{wid}/sentinel/config
       Read the current sentinel_config row (creates with defaults if absent).
       Role floor: viewer.

  POST /api/workspaces/{wid}/sentinel/config
       Update autonomy level, check interval, enabled checks, notifications.
       Role floor: admin.

  GET  /api/workspaces/{wid}/sentinel/findings
       ?status=&check_type=&entity_id=&limit=
       List recent Sentinel findings. Role floor: viewer.

  POST /api/workspaces/{wid}/sentinel/findings/{id}/acknowledge
       Acknowledge a finding (status=acknowledged, stamps acknowledged_by_uc_uid).
       Role floor: accountant.

  GET  /api/workspaces/{wid}/sentinel/scheduled-actions
       ?status=&limit=
       List pending + recently executed scheduled actions. Role floor: viewer.

  POST /api/workspaces/{wid}/sentinel/scheduled-actions/{id}/cancel
       Cancel a pending scheduled action. Role floor: admin.

  GET  /api/workspaces/{wid}/sentinel/check-runs
       ?days=&limit=
       List check runs in a window for the activity-feed roll-up.
       Role floor: viewer.

  POST /api/workspaces/{wid}/sentinel/run
       Manually trigger a cycle (smoke / on-demand). Role floor: admin.
       Returns 202 + the cycle_id; runs in background.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.authz import assert_entity_in_scope
from app.db import SessionLocal
from app.tenancy import WorkspaceContext, require_role, resolve_workspace
from app.workspace_session import workspace_session

from agents.sentinel.config import ALL_CHECK_TYPES, AutonomyLevel, get_or_create_config

_log = logging.getLogger("acctops.sentinel.api")
router = APIRouter(tags=["sentinel"])


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class SentinelConfigOut(BaseModel):
    workspace_id: UUID
    autonomy_level: str
    check_interval_minutes: int
    max_actions_per_day: int
    enabled_checks: list[str]
    notify_owners: bool
    notify_accountants: bool
    last_run_at: dt.datetime | None
    all_check_types: list[str] = Field(default_factory=lambda: list(ALL_CHECK_TYPES))


class SentinelConfigUpdate(BaseModel):
    autonomy_level: str | None = None
    check_interval_minutes: int | None = Field(default=None, ge=15, le=10080)
    max_actions_per_day: int | None = Field(default=None, ge=0, le=1000)
    enabled_checks: list[str] | None = None
    notify_owners: bool | None = None
    notify_accountants: bool | None = None


@router.get("/api/workspaces/{workspace_id}/sentinel/config", response_model=SentinelConfigOut)
async def get_sentinel_config(
    workspace_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> SentinelConfigOut:
    require_role(workspace, "viewer")
    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        cfg = await get_or_create_config(session, workspace.workspace_id)
        row = (
            await session.execute(
                text(
                    "SELECT last_run_at FROM sentinel_config WHERE workspace_id = :wid"
                ),
                {"wid": str(workspace.workspace_id)},
            )
        ).mappings().first()
    return SentinelConfigOut(
        workspace_id=workspace.workspace_id,
        autonomy_level=cfg.autonomy_level.value,
        check_interval_minutes=cfg.check_interval_minutes,
        max_actions_per_day=cfg.max_actions_per_day,
        enabled_checks=list(cfg.enabled_checks),
        notify_owners=cfg.notify_owners,
        notify_accountants=cfg.notify_accountants,
        last_run_at=row["last_run_at"] if row else None,
    )


@router.post("/api/workspaces/{workspace_id}/sentinel/config", response_model=SentinelConfigOut)
async def update_sentinel_config(
    workspace_id: UUID,
    body: SentinelConfigUpdate,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> SentinelConfigOut:
    require_role(workspace, "admin")

    # Validate autonomy_level
    if body.autonomy_level is not None:
        try:
            AutonomyLevel(body.autonomy_level.strip().lower())
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"autonomy_level must be one of: "
                    f"{', '.join(a.value for a in AutonomyLevel)}"
                ),
            )

    # Validate enabled_checks (allow "all" sentinel)
    if body.enabled_checks is not None:
        for c in body.enabled_checks:
            if c.strip().lower() != "all" and c not in ALL_CHECK_TYPES:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Unknown check_type {c!r}. "
                        f"Valid: 'all' or one of {sorted(ALL_CHECK_TYPES)}."
                    ),
                )

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        await get_or_create_config(session, workspace.workspace_id)  # ensure exists

        updates: list[str] = []
        params: dict[str, Any] = {"wid": str(workspace.workspace_id)}
        if body.autonomy_level is not None:
            updates.append("autonomy_level = :al")
            params["al"] = body.autonomy_level.strip().lower()
        if body.check_interval_minutes is not None:
            updates.append("check_interval_minutes = :iv")
            params["iv"] = body.check_interval_minutes
        if body.max_actions_per_day is not None:
            updates.append("max_actions_per_day = :mx")
            params["mx"] = body.max_actions_per_day
        if body.enabled_checks is not None:
            import json
            updates.append("enabled_checks = CAST(:ec AS jsonb)")
            params["ec"] = json.dumps(body.enabled_checks)
        if body.notify_owners is not None:
            updates.append("notify_owners = :no")
            params["no"] = body.notify_owners
        if body.notify_accountants is not None:
            updates.append("notify_accountants = :na")
            params["na"] = body.notify_accountants

        if updates:
            updates.append("updated_at = now()")
            await session.execute(
                text(
                    f"UPDATE sentinel_config SET {', '.join(updates)} "
                    f"WHERE workspace_id = :wid"
                ),
                params,
            )

    # Return the fresh state
    return await get_sentinel_config(workspace_id, workspace)


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


@router.get("/api/workspaces/{workspace_id}/sentinel/findings")
async def list_findings(
    workspace_id: UUID,
    status_filter: str = Query("open", alias="status"),
    check_type: str | None = None,
    entity_id: UUID | None = None,
    days: int = 30,
    limit: int = 100,
    offset: int = 0,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    require_role(workspace, "viewer")
    scope = assert_entity_in_scope(workspace, entity_id)
    days = max(1, min(days, 365))
    limit = max(1, min(limit, 500))

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        # Qualify with f.* — the SELECT below joins sentinel_check_run cr
        # which also has a created_at column → AmbiguousColumnError otherwise.
        where = ["f.created_at >= now() - make_interval(days => :days)"]
        params: dict[str, Any] = {"days": days, "limit": limit, "offset": max(0, offset)}
        if status_filter.lower() != "all":
            where.append("f.status = :st")
            params["st"] = status_filter.lower()
        if check_type:
            where.append("f.check_type = :ct")
            params["ct"] = check_type
        if entity_id:
            where.append("f.entity_id = :eid")
            params["eid"] = str(entity_id)
        if scope is not None:
            where.append("f.entity_id = ANY(:scope) OR f.entity_id IS NULL")
            params["scope"] = [str(e) for e in scope]
        where_sql = " AND ".join(where)

        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT
                      f.id::text, f.entity_id::text, e.display_name AS entity_display_name,
                      f.check_type, f.severity, f.title, f.summary, f.details,
                      f.action_type, f.action_ref_id::text,
                      f.status, f.check_run_id::text, f.decision_audit_id::text,
                      f.acknowledged_by_uc_uid, f.acknowledged_at, f.created_at
                    FROM sentinel_finding f
                    LEFT JOIN entity e ON e.id = f.entity_id
                    WHERE {where_sql}
                    ORDER BY f.created_at DESC, f.id DESC
                    LIMIT :limit OFFSET :offset
                    """
                ),
                params,
            )
        ).mappings().all()

        total = (
            await session.execute(
                text(
                    f"SELECT count(*)::int AS c FROM sentinel_finding f WHERE {where_sql}"
                ),
                {k: v for k, v in params.items() if k not in ("limit", "offset")},
            )
        ).scalar_one()

    return {"items": [dict(r) for r in rows], "total": int(total)}


class AckFindingBody(BaseModel):
    notes: str | None = None


@router.post("/api/workspaces/{workspace_id}/sentinel/findings/{finding_id}/acknowledge")
async def acknowledge_finding(
    workspace_id: UUID,
    finding_id: UUID,
    body: AckFindingBody,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    require_role(workspace, "accountant")
    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT entity_id::text, status FROM sentinel_finding
                    WHERE id = :id FOR UPDATE
                    """
                ),
                {"id": str(finding_id)},
            )
        ).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="Finding not found")
        if row["entity_id"]:
            assert_entity_in_scope(workspace, UUID(row["entity_id"]))
        if row["status"] not in ("open",):
            raise HTTPException(
                status_code=409,
                detail=f"Finding already {row['status']}; cannot acknowledge.",
            )
        await session.execute(
            text(
                """
                UPDATE sentinel_finding
                SET status = 'acknowledged',
                    acknowledged_by_uc_uid = :uc,
                    acknowledged_at = now()
                WHERE id = :id
                """
            ),
            {"id": str(finding_id), "uc": workspace.user_uc_uid},
        )
    return {"ok": True, "finding_id": str(finding_id), "status": "acknowledged"}


# ---------------------------------------------------------------------------
# Scheduled actions
# ---------------------------------------------------------------------------


@router.get("/api/workspaces/{workspace_id}/sentinel/scheduled-actions")
async def list_scheduled_actions(
    workspace_id: UUID,
    status_filter: str = Query("pending", alias="status"),
    limit: int = 100,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    require_role(workspace, "viewer")
    limit = max(1, min(limit, 500))
    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        where = ["1=1"]
        params: dict[str, Any] = {"limit": limit}
        if status_filter.lower() != "all":
            where.append("status = :st")
            params["st"] = status_filter.lower()
        where_sql = " AND ".join(where)
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT id::text, entity_id::text, action_type, scheduled_for,
                           payload, status, execution_notes,
                           created_at, executed_at
                    FROM sentinel_scheduled_action
                    WHERE {where_sql}
                    ORDER BY scheduled_for ASC
                    LIMIT :limit
                    """
                ),
                params,
            )
        ).mappings().all()
    return {"items": [dict(r) for r in rows]}


@router.post("/api/workspaces/{workspace_id}/sentinel/scheduled-actions/{action_id}/cancel")
async def cancel_scheduled_action(
    workspace_id: UUID,
    action_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    require_role(workspace, "admin")
    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        row = (
            await session.execute(
                text(
                    "SELECT status FROM sentinel_scheduled_action "
                    "WHERE id = :id FOR UPDATE"
                ),
                {"id": str(action_id)},
            )
        ).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="Scheduled action not found")
        if row["status"] != "pending":
            raise HTTPException(status_code=409, detail=f"Already {row['status']}")
        await session.execute(
            text(
                """
                UPDATE sentinel_scheduled_action
                SET status = 'cancelled', execution_notes = 'Cancelled by ' || :uc
                WHERE id = :id
                """
            ),
            {"id": str(action_id), "uc": workspace.user_uc_uid},
        )
    return {"ok": True, "id": str(action_id), "status": "cancelled"}


# ---------------------------------------------------------------------------
# Check runs (activity feed)
# ---------------------------------------------------------------------------


@router.get("/api/workspaces/{workspace_id}/sentinel/check-runs")
async def list_check_runs(
    workspace_id: UUID,
    days: int = 7,
    limit: int = 200,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    require_role(workspace, "viewer")
    days = max(1, min(days, 90))
    limit = max(1, min(limit, 1000))
    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT id::text, check_type, cycle_id::text, started_at,
                           finished_at, duration_ms, status,
                           findings_count, actions_taken, summary, error_summary
                    FROM sentinel_check_run
                    WHERE started_at >= now() - make_interval(days => :days)
                    ORDER BY started_at DESC
                    LIMIT :limit
                    """
                ),
                {"days": days, "limit": limit},
            )
        ).mappings().all()
        cycles_row = (
            await session.execute(
                text(
                    """
                    SELECT count(DISTINCT cycle_id)::int AS c
                    FROM sentinel_check_run
                    WHERE started_at >= now() - make_interval(days => :days)
                    """
                ),
                {"days": days},
            )
        ).scalar_one()
    return {"items": [dict(r) for r in rows], "cycles": int(cycles_row)}


# ---------------------------------------------------------------------------
# Manual trigger
# ---------------------------------------------------------------------------


@router.post("/api/workspaces/{workspace_id}/sentinel/run", status_code=202)
async def trigger_run(
    workspace_id: UUID,
    background: BackgroundTasks,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    """Kick off a single Sentinel cycle for this workspace in the background.

    Returns 202 immediately; the cycle runs detached. Tail the app or sentinel
    container logs to follow progress; use GET /sentinel/check-runs to see
    results after.

    Runs with ``force=True``: this is an explicit human request, so it bypasses
    both cadence gates (the per-workspace ``check_interval_minutes`` and each
    check's ``CHECK_CADENCE``). Otherwise an admin clicking "Run now" a day
    into a monthly cadence would get a silent no-op.
    """
    require_role(workspace, "admin")
    from agents.sentinel.run_sentinel import run_workspace_cycle

    async def _bg() -> None:
        try:
            await run_workspace_cycle(
                workspace.workspace_id, workspace.workspace_slug, force=True
            )
        except Exception as e:  # noqa: BLE001
            _log.exception("Manual sentinel run failed: %s", e)

    background.add_task(_bg)
    return {"ok": True, "queued": True, "workspace_id": str(workspace.workspace_id)}
