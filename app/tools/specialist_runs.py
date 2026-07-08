"""MCP tools for specialist run observability.

Mirrors the routes in app/api/specialist_runs.py:

Reads (viewer floor):
  - list_specialist_runs — paginated list with filters
  - get_specialist_run   — single run detail

Writes (accountant floor):
  - start_specialist_run  — runner registers a run at start
  - finish_specialist_run — runner marks completion/failure

The endpoint_url field is stripped from responses (mirrors the HTTP layer).
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.authz import assert_entity_in_scope
from app.db import SessionLocal
from app.mcp_auth import authorize_workspace
from app.workspace_session import workspace_session


_VALID_STATUSES = frozenset({"running", "completed", "failed", "cancelled", "all"})
_VALID_FINISH_STATUSES = frozenset({"completed", "failed", "cancelled"})


def _b(value: Any) -> Any:
    if value is None or isinstance(value, (int, float, bool, str, dict, list)):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _strip_endpoint(row: dict[str, Any]) -> dict[str, Any]:
    """Drop endpoint_url from a row dict (mirrors the HTTP layer)."""
    row.pop("endpoint_url", None)
    return row


async def list_specialist_runs(
    workspace_id: UUID,
    entity_id: UUID | None = None,
    tax_year: int | None = None,
    specialist_id: str | None = None,
    status_filter: str = "all",
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """List specialist runs for a workspace.

    Mirrors GET /api/workspaces/{wid}/specialist-runs.

    Args:
        workspace_id: Workspace UUID.
        entity_id: Optional — restrict to one entity.
        tax_year: Optional — restrict to one tax year.
        specialist_id: Optional — e.g. 'tax-1120-preparer'.
        status_filter: 'running' | 'completed' | 'failed' | 'cancelled' | 'all'.
        limit: Page size (max 200). Default 50.
        offset: Page offset.

    Returns: {"items": [<run rows>], "total": int}

    Role floor: viewer.
    """
    if status_filter and status_filter.lower() not in _VALID_STATUSES:
        raise ValueError(
            f"status_filter must be one of {sorted(_VALID_STATUSES)}; got {status_filter!r}"
        )

    ctx = await authorize_workspace(workspace_id, entity_id, min_role="viewer")
    scope_restriction = assert_entity_in_scope(ctx, entity_id)

    async with workspace_session(SessionLocal, workspace_id) as session:
        where = ["1=1"]
        params: dict[str, Any] = {
            "limit": min(max(int(limit), 1), 200),
            "offset": max(int(offset), 0),
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
            params["status"] = status_filter.lower()
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
        "items": [
            _strip_endpoint({k: _b(v) for k, v in dict(r).items()}) for r in rows
        ],
        "total": int(total),
    }


async def get_specialist_run(
    workspace_id: UUID,
    run_id: UUID,
) -> dict[str, Any]:
    """Fetch one specialist_run by id.

    Mirrors GET /specialist-runs/{id}. endpoint_url is stripped.

    Args:
        workspace_id: Workspace UUID.
        run_id: specialist_run id.

    Returns the row dict (or raises LookupError).

    Role floor: viewer.
    """
    ctx = await authorize_workspace(workspace_id, None, min_role="viewer")

    async with workspace_session(SessionLocal, workspace_id) as session:
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
        raise LookupError(f"Specialist run {run_id} not found")

    assert_entity_in_scope(ctx, UUID(row["entity_id"]))
    return _strip_endpoint({k: _b(v) for k, v in dict(row).items()})


async def start_specialist_run(
    workspace_id: UUID,
    entity_id: UUID,
    tax_year: int,
    specialist_id: str,
    backend: str,
    model: str,
    endpoint_url: str | None = None,
    input_summary: dict[str, Any] | None = None,
    agent_token_id: str | None = None,
) -> dict[str, Any]:
    """Insert a specialist_run row with status='running'.

    Mirrors POST /specialist-runs. Called by runner scripts immediately before
    invoking the LLM. Returns the new run id.

    Args:
        workspace_id: Workspace UUID.
        entity_id: Entity UUID.
        tax_year: Tax year (e.g. 2023).
        specialist_id: e.g. 'tax-1120-preparer', 'tax-1099-specialist'.
        backend: 'anthropic' | 'openai' | 'local'.
        model: Model id string (e.g. 'claude-sonnet-4-6').
        endpoint_url: Optional endpoint URL (stored for audit, never returned).
        input_summary: Optional dict summarising the run's inputs.
        agent_token_id: Optional agent token id; defaults to the caller's
            agent_id from the auth context.

    Returns: {"id": "<uuid>"}

    Role floor: accountant.
    """
    ctx = await authorize_workspace(workspace_id, entity_id, min_role="accountant")

    async with workspace_session(SessionLocal, workspace_id) as session:
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
                    "workspace_id": str(workspace_id),
                    "entity_id": str(entity_id),
                    "tax_year": tax_year,
                    "specialist_id": specialist_id,
                    "agent_token_id": agent_token_id or ctx.agent_id,
                    "invoker_uc_uid": ctx.user_uc_uid,
                    "backend": backend,
                    "model": model,
                    "endpoint_url": endpoint_url,
                    "input_summary": (
                        json.dumps(input_summary) if input_summary else None
                    ),
                },
            )
        ).mappings().one()

    return {"id": row["id"]}


async def finish_specialist_run(
    workspace_id: UUID,
    run_id: UUID,
    status: str,
    output_summary: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
    error_summary: str | None = None,
    duration_ms: int | None = None,
) -> dict[str, Any]:
    """Mark a specialist_run complete / failed / cancelled.

    Mirrors PATCH /specialist-runs/{id}/finish. Sets finished_at, duration_ms,
    status, output_summary, metrics, error_summary.

    Args:
        workspace_id: Workspace UUID.
        run_id: specialist_run id.
        status: 'completed' | 'failed' | 'cancelled'.
        output_summary: Optional dict with high-level result summary.
        metrics: Optional dict with token / cost / latency metrics.
        error_summary: Optional short error message on failure.
        duration_ms: Optional measured duration in milliseconds.

    Returns: {"ok": True, "id": "...", "status": "...", "finished_at": "..."}

    Role floor: accountant.
    """
    new_status = (status or "").strip().lower()
    if new_status not in _VALID_FINISH_STATUSES:
        raise ValueError(
            f"status must be one of {sorted(_VALID_FINISH_STATUSES)}; got {status!r}"
        )

    ctx = await authorize_workspace(workspace_id, None, min_role="accountant")

    async with workspace_session(SessionLocal, workspace_id) as session:
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
            raise LookupError(f"Specialist run {run_id} not found")

        assert_entity_in_scope(ctx, UUID(existing["entity_id"]))

        if existing["status"] not in ("running", "cancelled"):
            if new_status != "cancelled" and existing["status"] != "running":
                raise ValueError(
                    f"Run is already '{existing['status']}'; "
                    "only 'running' runs can be finished."
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
                "duration_ms": duration_ms,
                "output_summary": (
                    json.dumps(output_summary) if output_summary else None
                ),
                "metrics": (
                    json.dumps(metrics) if metrics else None
                ),
                "error_summary": error_summary,
                "id": str(run_id),
            },
        )

    return {
        "ok": True,
        "id": str(run_id),
        "status": new_status,
        "finished_at": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
    }
