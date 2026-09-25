"""MCP tools for the CPA Narrative subsystem.

The CPA Narrative is a single 2-5 page Markdown memo per (entity, tax_year)
that synthesises queue items + specialist drafts into a coherent CPA review
letter. It is stored as a memo row in tax_review_queue with topic
'cpa_narrative_{year}'.

Tools:
  - get_tax_narrative      — fetch the most recent narrative as Markdown text
  - trigger_tax_narrative  — kick off a background generation run

Mirrors the HTTP routes in app/api/tax_narrative.py. Both have viewer-floor on
read, accountant-floor on write (same as HTTP).
"""
from __future__ import annotations

import os
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from app.authz import assert_entity_in_scope
from app.db import SessionLocal
from app.mcp_auth import authorize_workspace
from app.narrative_config import narrative_child_env, resolve_narrative_llm
from app.workspace_session import workspace_session


def _narrative_topic(tax_year: int) -> str:
    return f"cpa_narrative_{tax_year}"


async def _fetch_latest_narrative(
    workspace_id: UUID,
    entity_id: UUID,
    tax_year: int,
) -> dict[str, Any] | None:
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


async def get_tax_narrative(
    workspace_id: UUID,
    entity_id: UUID,
    tax_year: int,
) -> dict[str, Any]:
    """Return the most recent CPA narrative for an (entity, tax_year).

    Mirrors GET /api/workspaces/{wid}/entities/{eid}/tax-narrative/{year}.
    The HTTP route returns raw Markdown with text/markdown Content-Type;
    this tool returns a JSON envelope so an MCP client can ingest it directly.

    Args:
        workspace_id: Workspace UUID.
        entity_id: Entity UUID.
        tax_year: Four-digit tax year.

    Returns:
        {
          "entity_id": "...", "tax_year": int,
          "item_id": "<uuid>", "submitter_id": "...",
          "created_at": "...", "status": "...",
          "markdown": "<full narrative text>",
          "_draft_notice": "..."
        }

    Raises LookupError if no narrative has been generated yet.

    Role floor: viewer.
    """
    ctx = await authorize_workspace(workspace_id, entity_id, min_role="viewer")
    assert_entity_in_scope(ctx, entity_id)

    row = await _fetch_latest_narrative(workspace_id, entity_id, tax_year)
    if row is None:
        raise LookupError(
            f"No CPA narrative found for entity {entity_id}, year {tax_year}. "
            "Trigger one via trigger_tax_narrative first."
        )

    content = row["content"]
    if isinstance(content, dict):
        markdown_text = content.get("text", str(content))
    else:
        markdown_text = str(content)

    return {
        "entity_id": str(entity_id),
        "tax_year": tax_year,
        "item_id": row["id"],
        "submitter_id": row.get("submitter_id", ""),
        "created_at": row.get("created_at", ""),
        "status": row.get("status", ""),
        "markdown": markdown_text,
        "_draft_notice": (
            "DRAFT — for CPA review only. The CPA narrative was synthesised "
            "by an LLM and is NOT a substitute for licensed CPA sign-off."
        ),
    }


async def trigger_tax_narrative(
    workspace_id: UUID,
    entity_id: UUID,
    tax_year: int,
    force_regenerate: bool = False,
) -> dict[str, Any]:
    """Trigger generation of a CPA narrative for an (entity, tax_year).

    Mirrors POST /api/workspaces/{wid}/entities/{eid}/tax-narrative/{year}.
    Kicks off the narrative_generator agent as a background subprocess on the
    server. Returns immediately; the caller should poll get_tax_narrative
    after ~30-60 seconds.

    If a narrative already exists and force_regenerate=False, returns
    {"status": "exists", ...} instead of triggering.

    Args:
        workspace_id: Workspace UUID.
        entity_id: Entity UUID.
        tax_year: Four-digit tax year.
        force_regenerate: When True, generate a new narrative even if one
            already exists.

    Returns:
        {"status": "started" | "exists", "run_id": "<uuid>" | null,
         "message": "...", ... }

    Role floor: accountant.
    """
    ctx = await authorize_workspace(workspace_id, entity_id, min_role="accountant")
    assert_entity_in_scope(ctx, entity_id)

    if not (1990 <= tax_year <= 2099):
        raise ValueError(f"tax_year {tax_year} is not a valid four-digit year")

    if not force_regenerate:
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

    # Delegate to the HTTP background-task implementation so we share the
    # subprocess invocation logic (and any future enhancements). We can't
    # access BackgroundTasks the way the HTTP handler does — we just inline
    # the subprocess spawn here as the HTTP handler does.
    import asyncio
    import tempfile
    import logging

    log = logging.getLogger(__name__)

    service_token = (
        os.environ.get("ACCTOPS_SERVICE_TOKEN")
        or os.environ.get("CPA_EXPORT_TOKEN")
        or ""
    )
    mcp_url = os.environ.get("ACCTOPS_MCP_URL", "http://localhost:8000/mcp")
    llm = resolve_narrative_llm()
    llm_endpoint_url = llm.endpoint_url
    llm_model = llm.model

    if not service_token:
        raise RuntimeError(
            "ACCTOPS_SERVICE_TOKEN not configured on the server; "
            "the narrative generator requires a service token to call MCP tools."
        )

    run_id = str(uuid4())

    async def _spawn() -> None:
        with tempfile.NamedTemporaryFile(
            suffix=".md",
            prefix=f"narrative-{str(entity_id)[:8]}-{tax_year}-",
            delete=False,
        ) as tmp:
            output_path = tmp.name
        cmd = [
            "python",
            "/app/agents/cpa_team/narrative_generator/run_narrative_generator.py",
            "--workspace-id", str(workspace_id),
            "--entity-id", str(entity_id),
            "--tax-year", str(tax_year),
            "--mcp-url", mcp_url,
            "--output", output_path,
            "--backend", "openai",
            "--llm-endpoint-url", llm_endpoint_url,
            "--llm-model", llm_model,
            "--verbose",
        ]
        env = narrative_child_env(
            service_token=service_token,
            mcp_url=mcp_url,
            config=llm,
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            # A full 11-section memo (large queue context + long-form output) runs
            # ~4-6 min on frontier; 180s killed it mid-generation. This is a
            # background job (caller polls get_tax_narrative), so a generous cap is
            # fine. Reduce later if the narrative is tuned to the 2-5 page spec.
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
            if proc.returncode != 0:
                log.error(
                    "[narrative] run_id=%s exited %d stderr=%s",
                    run_id, proc.returncode, stderr.decode()[:2000],
                )
            else:
                log.info(
                    "[narrative] run_id=%s completed stdout=%s",
                    run_id, stdout.decode()[:500],
                )
        except Exception as exc:  # noqa: BLE001
            log.exception("[narrative] run_id=%s failed: %s", run_id, exc)

    asyncio.create_task(_spawn())

    return {
        "status": "started",
        "run_id": run_id,
        "message": (
            f"CPA narrative generation started for entity {entity_id}, year {tax_year}. "
            "Poll get_tax_narrative in 30-60 seconds for the result."
        ),
        "_draft_notice": (
            "DRAFT — the generated narrative is for CPA review only and not a "
            "substitute for licensed CPA sign-off."
        ),
    }
