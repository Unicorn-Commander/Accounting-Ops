"""Integration tests for the specialist runs API (live stack).

Tests the full start → list → single → finish lifecycle against the real
DB + HTTP:

  (1)  Start a run record (POST /specialist-runs) — returns id.
  (2)  Finish a run as completed (PATCH .../finish).
  (3)  Start + finish as failed with error_summary.
  (4)  List with ?entity_id + ?tax_year filter — sees started runs.
  (5)  List with ?specialist_id filter — correct specialist visible.
  (6)  List with ?status=completed filter — completed visible, running hidden.
  (7)  GET single run — full row returned; endpoint_url is absent.
  (8)  GET unknown id → 404.
  (9)  Finish a non-running run → 409.
  (10) RBAC: viewer cannot start a run (403).
  (11) RLS isolation: workspace A's runs not visible to workspace B caller.

Seeds rows directly via the owner DB pool when needed (same pattern as
test_tax_review_queue.py). HTTP calls go through the real /api/workspaces/
.../specialist-runs routes.

Skips cleanly when the stack is not reachable.
"""
from __future__ import annotations

import json
import uuid
import datetime as dt

import asyncpg
import httpx
import pytest

from tests.conftest import (
    TEST_ENTITY_ID,
    TEST_WORKSPACE_B_ID,
    TEST_WORKSPACE_ID,
    StackConfig,
)
from tests.integration.test_mcp_write_tools import _membership_as

pytestmark = pytest.mark.integration

_SCHEMA = "books"  # shared-schema model (was per-workspace accounting_ops_<uuid>); mirrors app.workspace_session.SHARED_SCHEMA

_SPECIALIST_ID = "tax-1120-preparer"
_BACKEND = "anthropic"
_MODEL = "claude-sonnet-4-6"


def _run_tag() -> str:
    return dt.datetime.now(tz=dt.timezone.utc).strftime("%Y%m%d%H%M%S%f")


# ---------------------------------------------------------------------------
# DB-level seed helpers (owner pool, bypass RLS)
# ---------------------------------------------------------------------------


async def _seed_run(
    db: asyncpg.Pool,
    *,
    specialist_id: str = _SPECIALIST_ID,
    backend: str = _BACKEND,
    model: str = _MODEL,
    tax_year: int = 2023,
    status: str = "running",
    duration_ms: int | None = None,
    metrics: dict | None = None,
    output_summary: dict | None = None,
    error_summary: str | None = None,
) -> str:
    """Insert a specialist_run row directly via the owner pool. Returns run id."""
    run_id = str(uuid.uuid4())
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
            await conn.execute(
                """
                INSERT INTO specialist_run (
                    id, workspace_id, entity_id, tax_year,
                    specialist_id, invoker_uc_uid,
                    backend, model, status,
                    duration_ms, metrics, output_summary, error_summary
                ) VALUES (
                    $1, $2, $3, $4,
                    $5, 'test-invoker',
                    $6, $7, $8,
                    $9, $10::jsonb, $11::jsonb, $12
                )
                """,
                run_id,
                str(TEST_WORKSPACE_ID),
                str(TEST_ENTITY_ID),
                tax_year,
                specialist_id,
                backend,
                model,
                status,
                duration_ms,
                json.dumps(metrics) if metrics else None,
                json.dumps(output_summary) if output_summary else None,
                error_summary,
            )
    return run_id


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


async def _start_run(
    base_url: str,
    token: str,
    *,
    workspace_id=None,
    entity_id=None,
    tax_year: int = 2023,
    specialist_id: str = _SPECIALIST_ID,
    backend: str = _BACKEND,
    model: str = _MODEL,
) -> httpx.Response:
    wid = workspace_id or TEST_WORKSPACE_ID
    eid = str(entity_id or TEST_ENTITY_ID)
    body = {
        "entity_id": eid,
        "tax_year": tax_year,
        "specialist_id": specialist_id,
        "backend": backend,
        "model": model,
        "input_summary": {"workspace_id": str(wid), "entity_id": eid, "tax_year": tax_year},
    }
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=30) as c:
        return await c.post(f"/api/workspaces/{wid}/specialist-runs", json=body)


async def _finish_run(
    base_url: str,
    token: str,
    run_id: str,
    new_status: str,
    *,
    workspace_id=None,
    output_summary: dict | None = None,
    metrics: dict | None = None,
    error_summary: str | None = None,
    duration_ms: int | None = None,
) -> httpx.Response:
    wid = workspace_id or TEST_WORKSPACE_ID
    body: dict = {"status": new_status}
    if output_summary:
        body["output_summary"] = output_summary
    if metrics:
        body["metrics"] = metrics
    if error_summary:
        body["error_summary"] = error_summary
    if duration_ms is not None:
        body["duration_ms"] = duration_ms
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=30) as c:
        return await c.patch(
            f"/api/workspaces/{wid}/specialist-runs/{run_id}/finish",
            json=body,
        )


async def _list_runs(
    base_url: str,
    token: str,
    *,
    workspace_id=None,
    entity_id=None,
    tax_year=None,
    specialist_id=None,
    status_filter=None,
) -> httpx.Response:
    wid = workspace_id or TEST_WORKSPACE_ID
    params: dict = {}
    if entity_id:
        params["entity_id"] = str(entity_id)
    if tax_year:
        params["tax_year"] = tax_year
    if specialist_id:
        params["specialist_id"] = specialist_id
    if status_filter:
        params["status"] = status_filter
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=30) as c:
        return await c.get(f"/api/workspaces/{wid}/specialist-runs", params=params)


async def _get_run(
    base_url: str,
    token: str,
    run_id: str,
    *,
    workspace_id=None,
) -> httpx.Response:
    wid = workspace_id or TEST_WORKSPACE_ID
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=30) as c:
        return await c.get(f"/api/workspaces/{wid}/specialist-runs/{run_id}")


# ---------------------------------------------------------------------------
# (1) Start a run and get an id back
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_run_returns_id(seeded_workspace: StackConfig) -> None:
    """POST /specialist-runs returns a UUID id with 201 status."""
    base, token = seeded_workspace.base_url, seeded_workspace.token
    r = await _start_run(base, token)
    assert r.status_code == 201, r.text
    body = r.json()
    assert "id" in body
    # Must be a valid UUID
    uuid.UUID(body["id"])


# ---------------------------------------------------------------------------
# (2) Start then finish as completed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_finish_completed(seeded_workspace: StackConfig) -> None:
    """Full lifecycle: start → finish(completed) with metrics + output_summary."""
    base, token = seeded_workspace.base_url, seeded_workspace.token

    r = await _start_run(base, token)
    assert r.status_code == 201, r.text
    run_id = r.json()["id"]

    metrics = {
        "input_tokens": 12345,
        "output_tokens": 3456,
        "total_tool_calls": 5,
        "mcp_tool_breakdown": {"get_form_1120_data": 1, "draft_memo": 2, "flag_for_cpa_review": 2},
    }
    output_summary = {"output_file": "/drafts/mu-2023-1120.md", "output_bytes": 8200}

    r = await _finish_run(
        base,
        token,
        run_id,
        "completed",
        metrics=metrics,
        output_summary=output_summary,
        duration_ms=45320,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["status"] == "completed"
    assert body["id"] == run_id

    # Verify the row is now visible as completed
    r2 = await _get_run(base, token, run_id)
    assert r2.status_code == 200, r2.text
    row = r2.json()
    assert row["status"] == "completed"
    assert row["duration_ms"] == 45320
    assert row["metrics"]["input_tokens"] == 12345
    assert row["output_summary"]["output_file"] == "/drafts/mu-2023-1120.md"
    # endpoint_url must be absent (stripped by API)
    assert "endpoint_url" not in row


# ---------------------------------------------------------------------------
# (3) Start then finish as failed with error_summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_finish_failed(seeded_workspace: StackConfig) -> None:
    """A run that fails records error_summary and status='failed'."""
    base, token = seeded_workspace.base_url, seeded_workspace.token

    r = await _start_run(base, token)
    assert r.status_code == 201, r.text
    run_id = r.json()["id"]

    r = await _finish_run(
        base,
        token,
        run_id,
        "failed",
        error_summary="ConnectionError: Could not reach /mcp endpoint at http://localhost:8000/mcp",
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "failed"

    r2 = await _get_run(base, token, run_id)
    assert r2.status_code == 200, r2.text
    row = r2.json()
    assert row["status"] == "failed"
    assert "ConnectionError" in (row["error_summary"] or "")


# ---------------------------------------------------------------------------
# (4) List with entity_id + tax_year filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_entity_year_filter(
    db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    """List endpoint filters correctly by entity_id + tax_year."""
    base, token = seeded_workspace.base_url, seeded_workspace.token

    # Seed a 2023 run and a 2024 run
    run_2023 = await _seed_run(db, tax_year=2023, status="completed")
    run_2024 = await _seed_run(db, tax_year=2024, status="completed")

    r = await _list_runs(base, token, entity_id=TEST_ENTITY_ID, tax_year=2023)
    assert r.status_code == 200, r.text
    ids = {i["id"] for i in r.json()["items"]}
    assert run_2023 in ids, "2023 run must appear in 2023 filter"
    assert run_2024 not in ids, "2024 run must NOT appear in 2023 filter"


# ---------------------------------------------------------------------------
# (5) List with specialist_id filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_specialist_id_filter(
    db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    """List endpoint filters by specialist_id correctly."""
    base, token = seeded_workspace.base_url, seeded_workspace.token

    preparer_id = await _seed_run(db, specialist_id="tax-1120-preparer", tax_year=2025)
    sc_state_id = await _seed_run(db, specialist_id="tax-sc-state", tax_year=2025)

    r = await _list_runs(
        base, token, tax_year=2025, specialist_id="tax-1120-preparer"
    )
    assert r.status_code == 200, r.text
    ids = {i["id"] for i in r.json()["items"]}
    assert preparer_id in ids
    assert sc_state_id not in ids


# ---------------------------------------------------------------------------
# (6) List with status filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_status_filter(
    db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    """?status=completed returns only completed runs."""
    base, token = seeded_workspace.base_url, seeded_workspace.token

    completed_id = await _seed_run(db, status="completed", tax_year=2026)
    running_id = await _seed_run(db, status="running", tax_year=2026)

    r = await _list_runs(base, token, tax_year=2026, status_filter="completed")
    assert r.status_code == 200, r.text
    ids = {i["id"] for i in r.json()["items"]}
    assert completed_id in ids
    assert running_id not in ids


# ---------------------------------------------------------------------------
# (7) GET single run; endpoint_url stripped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_single_run_no_endpoint_url(
    db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    """GET single run returns the row without endpoint_url."""
    base, token = seeded_workspace.base_url, seeded_workspace.token
    run_id = await _seed_run(db, status="completed")

    r = await _get_run(base, token, run_id)
    assert r.status_code == 200, r.text
    row = r.json()
    assert row["id"] == run_id
    assert "endpoint_url" not in row, "endpoint_url must be stripped from responses"
    assert row["specialist_id"] == _SPECIALIST_ID


# ---------------------------------------------------------------------------
# (8) GET unknown id → 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_unknown_run_404(seeded_workspace: StackConfig) -> None:
    """A non-existent run id returns 404."""
    base, token = seeded_workspace.base_url, seeded_workspace.token
    r = await _get_run(base, token, str(uuid.uuid4()))
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# (9) Finish a completed run → 409
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finish_already_completed_409(
    db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    """Finishing an already-completed run returns 409 Conflict."""
    base, token = seeded_workspace.base_url, seeded_workspace.token
    run_id = await _seed_run(db, status="completed")

    r = await _finish_run(base, token, run_id, "completed")
    assert r.status_code == 409, r.text


# ---------------------------------------------------------------------------
# (10) RBAC: viewer cannot start a run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_denied_for_viewer(
    db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    """A viewer cannot start a specialist run (accountant floor required)."""
    base, token = seeded_workspace.base_url, seeded_workspace.token

    async with _membership_as(db, seeded_workspace.uc_uid, role="viewer", entity_scope=None):
        r = await _start_run(base, token)
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# (11) Cross-workspace isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_workspace_list_denied(seeded_workspace: StackConfig) -> None:
    """Workspace A's token cannot list runs from workspace B (403)."""
    base, token = seeded_workspace.base_url, seeded_workspace.token
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(base_url=base, headers=headers, timeout=30) as c:
        r = await c.get(f"/api/workspaces/{TEST_WORKSPACE_B_ID}/specialist-runs")
    assert r.status_code in (401, 403), r.text
