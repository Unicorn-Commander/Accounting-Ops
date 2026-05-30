"""CPA Narrative endpoints: GET + POST per (workspace, entity, tax_year).

The CPA Narrative is a single 2-5 page Markdown memo per entity-year that
synthesises all queue items + specialist drafts into a coherent CPA review letter.
It is written by the narrative_generator agent and stored as a memo in
tax_review_queue with topic='cpa_narrative_{year}'.

Routes:
  GET  /api/workspaces/{wid}/entities/{eid}/tax-narrative/{year}
       Returns the most recent cpa_narrative_{year} memo as plain Markdown text.
       404 if none exists yet.
       Role floor: viewer.

  POST /api/workspaces/{wid}/entities/{eid}/tax-narrative/{year}
       Triggers a synchronous run of the narrative generator agent.
       Returns 202 + {run_id, message} while the run fires.
       Role floor: accountant (write / trigger).

The narrative generator is invoked via subprocess (docker exec pattern) in MVP.
The API returns 202 immediately when the subprocess is kicked off; the frontend
polls the GET endpoint until a narrative appears (or uses a loading skeleton with
a manual refresh button).

Design note: "synchronous from the API's perspective" means we actually await
the subprocess — the 202 response is returned immediately and the front-end
should treat it as "generation started" and poll/refresh. For MVP we run inline
in a background task so the HTTP response returns fast.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import tempfile
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy import text

from app.authz import assert_entity_in_scope
from app.db import SessionLocal
from app.tenancy import WorkspaceContext, require_role, resolve_workspace
from app.workspace_session import workspace_session

log = logging.getLogger(__name__)

router = APIRouter(tags=["tax-narrative"])

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _narrative_topic(tax_year: int) -> str:
    return f"cpa_narrative_{tax_year}"


async def _fetch_latest_narrative(
    workspace_id: UUID,
    entity_id: UUID,
    tax_year: int,
) -> dict[str, Any] | None:
    """Return the most recent cpa_narrative memo row, or None."""
    topic = _narrative_topic(tax_year)
    async with workspace_session(SessionLocal, workspace_id) as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT
                        id::text,
                        content,
                        created_at::text,
                        status,
                        submitter_id
                    FROM tax_review_queue
                    WHERE entity_id = :entity_id
                      AND tax_year   = :tax_year
                      AND item_type  = 'memo'
                      AND content->>'topic' = :topic
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                ),
                {
                    "entity_id": str(entity_id),
                    "tax_year": tax_year,
                    "topic": topic,
                },
            )
        ).mappings().first()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# GET /tax-narrative/{year}
# ---------------------------------------------------------------------------


@router.get(
    "/api/workspaces/{workspace_id}/entities/{entity_id}/tax-narrative/{tax_year}",
    response_class=Response,
)
async def get_tax_narrative(
    workspace_id: UUID,
    entity_id: UUID,
    tax_year: int,
    workspace: WorkspaceContext = Depends(resolve_workspace),
):
    """Return the most recent CPA narrative for an entity-year as plain Markdown.

    Role floor: viewer. Returns 404 if no narrative has been generated yet.
    The response Content-Type is text/markdown (UTF-8).
    """
    require_role(workspace, "viewer")
    assert_entity_in_scope(workspace, entity_id)

    row = await _fetch_latest_narrative(workspace_id, entity_id, tax_year)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No CPA narrative found for entity {entity_id}, year {tax_year}. "
                "Generate one first via POST to this endpoint."
            ),
        )

    content = row["content"]
    if isinstance(content, dict):
        markdown_text = content.get("text", str(content))
    else:
        markdown_text = str(content)

    from fastapi.responses import Response
    return Response(
        content=markdown_text,
        media_type="text/markdown; charset=utf-8",
        headers={
            "X-Narrative-Item-Id": row["id"],
            "X-Narrative-Created-At": row.get("created_at", ""),
            "X-Narrative-Submitter": row.get("submitter_id", ""),
        },
    )


# ---------------------------------------------------------------------------
# POST /tax-narrative/{year}  — trigger generation
# ---------------------------------------------------------------------------


class TriggerNarrativeRequest(BaseModel):
    force_regenerate: bool = False
    """If True, generate a new narrative even if one already exists for this year."""


async def _run_narrative_generator(
    workspace_id: str,
    entity_id: str,
    tax_year: int,
    service_token: str,
    mcp_url: str,
    llm_endpoint_url: str,
    llm_model: str,
    llm_api_key: str,
    run_id: str,
) -> None:
    """Background task: invoke the narrative generator subprocess."""
    with tempfile.NamedTemporaryFile(
        suffix=".md",
        prefix=f"narrative-{entity_id[:8]}-{tax_year}-",
        delete=False,
    ) as tmp:
        output_path = tmp.name

    cmd = [
        "python",
        "/app/agents/cpa_team/narrative_generator/run_narrative_generator.py",
        "--workspace-id", workspace_id,
        "--entity-id", entity_id,
        "--tax-year", str(tax_year),
        "--mcp-url", mcp_url,
        "--output", output_path,
        "--backend", "openai",
        "--llm-endpoint-url", llm_endpoint_url,
        "--llm-model", llm_model,
        "--llm-api-key", llm_api_key,
        "--verbose",
    ]

    env = {**os.environ, "ACCTOPS_SERVICE_TOKEN": service_token}

    log.info(
        "[narrative] Starting generator run_id=%s entity=%s year=%s",
        run_id, entity_id[:8], tax_year,
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
        if proc.returncode != 0:
            log.error(
                "[narrative] Generator exited %d for run_id=%s\nstderr: %s",
                proc.returncode, run_id, stderr.decode()[:2000],
            )
        else:
            log.info(
                "[narrative] Generator completed run_id=%s\n%s",
                run_id, stdout.decode()[:500],
            )
    except asyncio.TimeoutError:
        log.error("[narrative] Generator timed out after 180s for run_id=%s", run_id)
    except Exception as exc:
        log.exception("[narrative] Generator failed for run_id=%s: %s", run_id, exc)


@router.post(
    "/api/workspaces/{workspace_id}/entities/{entity_id}/tax-narrative/{tax_year}",
    status_code=202,
)
async def trigger_tax_narrative(
    workspace_id: UUID,
    entity_id: UUID,
    tax_year: int,
    body: TriggerNarrativeRequest = TriggerNarrativeRequest(),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """Trigger generation of a CPA narrative for an entity-year.

    Kicks off the narrative_generator agent as a background subprocess.
    Returns 202 immediately; the caller should poll the GET endpoint or
    refresh after ~30-60 seconds.

    Role floor: accountant (same as specialist run triggers).
    """
    require_role(workspace, "accountant")
    assert_entity_in_scope(workspace, entity_id)

    if not (1990 <= tax_year <= 2099):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"tax_year {tax_year} is not a valid four-digit year.",
        )

    # If a narrative already exists and force_regenerate is False, return the
    # existing one's metadata instead of triggering again.
    if not body.force_regenerate:
        existing = await _fetch_latest_narrative(workspace_id, entity_id, tax_year)
        if existing:
            return {
                "status": "exists",
                "message": (
                    f"A narrative already exists for {tax_year}. "
                    "Use force_regenerate=true to overwrite."
                ),
                "item_id": existing["id"],
                "created_at": existing.get("created_at"),
            }

    run_id = str(uuid4())

    # Resolve configuration from environment — same defaults as the CLI runner.
    service_token = (
        os.environ.get("ACCTOPS_SERVICE_TOKEN")
        or os.environ.get("CPA_EXPORT_TOKEN")
        or ""
    )
    mcp_url = os.environ.get("ACCTOPS_MCP_URL", "http://localhost:8000/mcp")
    llm_endpoint_url = os.environ.get("LLM_ENDPOINT_URL", "http://localhost:8088/v1")
    llm_model = os.environ.get("LLM_MODEL", "anthropic/claude-sonnet-4.6")
    llm_api_key = os.environ.get("LLM_API_KEY", os.environ.get("OPENROUTER_KEY", ""))

    if not service_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "ACCTOPS_SERVICE_TOKEN not configured on the server. "
                "The narrative generator requires a service token to call MCP tools."
            ),
        )

    background_tasks.add_task(
        _run_narrative_generator,
        workspace_id=str(workspace_id),
        entity_id=str(entity_id),
        tax_year=tax_year,
        service_token=service_token,
        mcp_url=mcp_url,
        llm_endpoint_url=llm_endpoint_url,
        llm_model=llm_model,
        llm_api_key=llm_api_key,
        run_id=run_id,
    )

    return {
        "status": "started",
        "run_id": run_id,
        "message": (
            f"CPA narrative generation started for entity {entity_id}, year {tax_year}. "
            "Poll GET /tax-narrative/{year} in 30-60 seconds for the result."
        ),
        "poll_url": (
            f"/api/workspaces/{workspace_id}/entities/{entity_id}"
            f"/tax-narrative/{tax_year}"
        ),
    }
