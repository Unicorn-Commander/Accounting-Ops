"""Specialist run observability: record and query specialist run history.

Each time a tax specialist (1120 Preparer, 1099, SC State, Coordinator, etc.)
runs, it POSTs a start record here, then PATCHes it with the finish metadata.
CPAs and customers can then review run history with token/cost metrics.

Routes:
  POST  /api/workspaces/{wid}/specialist-runs
        — start a run record (accountant floor). Returns {id}.
          Used by runner scripts at run-start.

  PATCH /api/workspaces/{wid}/specialist-runs/{id}/finish
        — mark complete or failed (accountant floor).
          Body: {status, output_summary, metrics, error_summary, duration_ms}.

  GET   /api/workspaces/{wid}/specialist-runs
        ?entity_id=&tax_year=&specialist_id=&status=&limit=&offset=
        — list runs. Viewer floor.

  GET   /api/workspaces/{wid}/specialist-runs/{id}
        — single run. Viewer floor.

Role floors:
  * List / get: viewer — CPAs + customers can review history without write perms.
  * Start / finish: accountant — runner scripts are service-token-authenticated;
    service tokens are provisioned at accountant level to allow MCP write ops.
    Admin/owner also pass (roles are cumulative).

Entity-scope (BOLA): mirrors tax_review.py. A scoped member can only see and
record runs for their entities.

endpoint_url: stored in DB but stripped from list and single-item responses
to avoid leaking internal DNS / private API URLs to end users. Only recorded
for audit purposes; retrievable by querying the DB directly.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import text

from app.authz import assert_entity_in_scope
from app.db import SessionLocal
from app.tenancy import WorkspaceContext, require_role, resolve_workspace
from app.workspace_session import workspace_session

router = APIRouter(tags=["specialist-runs"])

# ---------------------------------------------------------------------------
# Valid enum values
# ---------------------------------------------------------------------------

_VALID_STATUSES = frozenset({"queued", "running", "completed", "failed", "cancelled", "all"})
_VALID_FINISH_STATUSES = frozenset({"completed", "failed", "cancelled"})

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class StartRunRequest(BaseModel):
    entity_id: UUID
    tax_year: int
    specialist_id: str      # e.g. 'tax-1120-preparer'
    backend: str            # 'anthropic' | 'openai' | 'local'
    model: str              # model ID string
    endpoint_url: str | None = None
    input_summary: dict | None = None
    agent_token_id: str | None = None


class FinishRunRequest(BaseModel):
    status: str             # 'completed' | 'failed' | 'cancelled'
    output_summary: dict | None = None
    metrics: dict | None = None
    error_summary: str | None = None
    duration_ms: int | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_endpoint(row: dict) -> dict:
    """Remove endpoint_url from a row dict before returning to clients."""
    row.pop("endpoint_url", None)
    return row


# ---------------------------------------------------------------------------
# POST /specialist-runs — start a run
# ---------------------------------------------------------------------------


@router.post("/api/workspaces/{workspace_id}/specialist-runs", status_code=201)
async def start_specialist_run(
    workspace_id: UUID,
    body: StartRunRequest,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """Create a specialist_run row with status='running'.

    Called by runner scripts immediately before invoking the LLM. Returns the
    new run id so the runner can PATCH it when finished.

    Role floor: accountant (service tokens for runners are provisioned at this level).
    """
    require_role(workspace, "accountant")
    assert_entity_in_scope(workspace, body.entity_id)

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        row = (
            await session.execute(
                text(
                    """
                    INSERT INTO specialist_run (
                        workspace_id, entity_id, tax_year,
                        specialist_id, agent_token_id, invoker_uc_uid,
                        backend, model, endpoint_url,
                        status, input_summary
                    ) VALUES (
                        :workspace_id, :entity_id, :tax_year,
                        :specialist_id, :agent_token_id, :invoker_uc_uid,
                        :backend, :model, :endpoint_url,
                        'running', :input_summary
                    )
                    RETURNING id::text
                    """
                ),
                {
                    "workspace_id": str(workspace.workspace_id),
                    "entity_id": str(body.entity_id),
                    "tax_year": body.tax_year,
                    "specialist_id": body.specialist_id,
                    "agent_token_id": body.agent_token_id or workspace.agent_id,
                    "invoker_uc_uid": workspace.user_uc_uid,
                    "backend": body.backend,
                    "model": body.model,
                    "endpoint_url": body.endpoint_url,
                    "input_summary": (
                        json.dumps(body.input_summary) if body.input_summary else None
                    ),
                },
            )
        ).mappings().one()

    return {"id": row["id"]}


# ---------------------------------------------------------------------------
# PATCH /specialist-runs/{id}/finish — mark complete / failed
# ---------------------------------------------------------------------------


@router.patch("/api/workspaces/{workspace_id}/specialist-runs/{run_id}/finish")
async def finish_specialist_run(
    workspace_id: UUID,
    run_id: UUID,
    body: FinishRunRequest,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """Update a specialist_run row to its final state.

    Called by runner scripts when the LLM run ends (success or exception).
    Sets finished_at, duration_ms, status, output_summary, metrics, error_summary.

    Role floor: accountant (same as start).
    """
    require_role(workspace, "accountant")

    new_status = (body.status or "").strip().lower()
    if new_status not in _VALID_FINISH_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"status must be one of: "
                f"{', '.join(sorted(_VALID_FINISH_STATUSES))}"
            ),
        )

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        # Lock + validate row exists and belongs to this workspace (RLS covers it,
        # but explicit check gives a friendly 404 vs. silent no-op).
        existing = (
            await session.execute(
                text(
                    """
                    SELECT id::text, entity_id::text, status
                    FROM specialist_run
                    WHERE id = :id
                    FOR UPDATE
                    """
                ),
                {"id": str(run_id)},
            )
        ).mappings().first()

        if existing is None:
            raise HTTPException(status_code=404, detail="Specialist run not found")

        # Entity-scope check
        assert_entity_in_scope(workspace, UUID(existing["entity_id"]))

        if existing["status"] not in ("running", "cancelled"):
            # Allow cancelling in any state; otherwise only running can be finished.
            if new_status != "cancelled" and existing["status"] != "running":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"Run is already '{existing['status']}'; "
                        "only 'running' runs can be finished."
                    ),
                )

        await session.execute(
            text(
                """
                UPDATE specialist_run
                SET status          = :status,
                    finished_at     = now(),
                    duration_ms     = :duration_ms,
                    output_summary  = :output_summary,
                    metrics         = :metrics,
                    error_summary   = :error_summary
                WHERE id = :id
                """
            ),
            {
                "status": new_status,
                "duration_ms": body.duration_ms,
                "output_summary": (
                    json.dumps(body.output_summary) if body.output_summary else None
                ),
                "metrics": (
                    json.dumps(body.metrics) if body.metrics else None
                ),
                "error_summary": body.error_summary,
                "id": str(run_id),
            },
        )

    return {
        "ok": True,
        "id": str(run_id),
        "status": new_status,
        "finished_at": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# GET /specialist-runs — list
# ---------------------------------------------------------------------------


@router.get("/api/workspaces/{workspace_id}/specialist-runs")
async def list_specialist_runs(
    workspace_id: UUID,
    entity_id: UUID | None = None,
    tax_year: int | None = None,
    specialist_id: str | None = None,
    status_filter: str = Query("all", alias="status"),
    limit: int = 50,
    offset: int = 0,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """List specialist runs for a workspace.

    Read floor: viewer. Defaults to all statuses.
    endpoint_url is stripped from responses.

    Entity-scope (BOLA): mirrors tax_review.py.
    """
    require_role(workspace, "viewer")
    scope_restriction = assert_entity_in_scope(workspace, entity_id)

    if status_filter and status_filter.lower() not in _VALID_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"status must be one of: {', '.join(sorted(_VALID_STATUSES))}"
            ),
        )

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        where = ["1=1"]
        params: dict[str, Any] = {
            "limit": min(max(limit, 1), 200),
            "offset": max(offset, 0),
        }

        if entity_id:
            where.append("sr.entity_id = :eid")
            params["eid"] = str(entity_id)

        if scope_restriction is not None:
            where.append("sr.entity_id = ANY(:entity_scope)")
            params["entity_scope"] = [str(e) for e in scope_restriction]

        if tax_year:
            where.append("sr.tax_year = :tax_year")
            params["tax_year"] = tax_year

        if specialist_id:
            where.append("sr.specialist_id = :specialist_id")
            params["specialist_id"] = specialist_id

        if status_filter and status_filter.lower() != "all":
            where.append("sr.status = :status")
            params["status"] = status_filter

        where_sql = " AND ".join(where)

        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT
                        sr.id::text,
                        sr.workspace_id::text,
                        sr.entity_id::text,
                        e.display_name AS entity_display_name,
                        e.slug AS entity_slug,
                        sr.tax_year,
                        sr.specialist_id,
                        sr.agent_token_id,
                        sr.invoker_uc_uid,
                        sr.backend,
                        sr.model,
                        sr.started_at,
                        sr.finished_at,
                        sr.duration_ms,
                        sr.status,
                        sr.input_summary,
                        sr.output_summary,
                        sr.metrics,
                        sr.error_summary,
                        sr.trace_id
                    FROM specialist_run sr
                    LEFT JOIN entity e ON e.id = sr.entity_id
                    WHERE {where_sql}
                    ORDER BY sr.started_at DESC, sr.id DESC
                    LIMIT :limit OFFSET :offset
                    """
                ),
                params,
            )
        ).mappings().all()

        count_params = {k: v for k, v in params.items() if k not in ("limit", "offset")}
        total = (
            await session.execute(
                text(f"SELECT count(*) FROM specialist_run sr WHERE {where_sql}"),
                count_params,
            )
        ).scalar_one()

    return {
        "items": [_strip_endpoint(dict(r)) for r in rows],
        "total": total,
    }


# ---------------------------------------------------------------------------
# GET /specialist-runs/{id} — single
# ---------------------------------------------------------------------------


@router.get("/api/workspaces/{workspace_id}/specialist-runs/{run_id}")
async def get_specialist_run(
    workspace_id: UUID,
    run_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """Fetch a single specialist run.

    Read floor: viewer. endpoint_url is stripped.
    """
    require_role(workspace, "viewer")

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT
                        sr.id::text,
                        sr.workspace_id::text,
                        sr.entity_id::text,
                        e.display_name AS entity_display_name,
                        e.slug AS entity_slug,
                        sr.tax_year,
                        sr.specialist_id,
                        sr.agent_token_id,
                        sr.invoker_uc_uid,
                        sr.backend,
                        sr.model,
                        sr.started_at,
                        sr.finished_at,
                        sr.duration_ms,
                        sr.status,
                        sr.input_summary,
                        sr.output_summary,
                        sr.metrics,
                        sr.error_summary,
                        sr.trace_id
                    FROM specialist_run sr
                    LEFT JOIN entity e ON e.id = sr.entity_id
                    WHERE sr.id = :id
                    """
                ),
                {"id": str(run_id)},
            )
        ).mappings().first()

    if row is None:
        raise HTTPException(status_code=404, detail="Specialist run not found")

    # Entity-scope check
    assert_entity_in_scope(workspace, UUID(row["entity_id"]))

    return _strip_endpoint(dict(row))
