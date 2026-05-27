"""Shared decision_audit writer for MCP write tools.

Every agent write tool (categorize / link_receipt / add_rule) appends a row to
``decision_audit`` — the entity-scoped, append-only agent decision trail (one of
the SELECT+INSERT-only tables in migrations/tenant/0005). It captures the tool
name, the request it was given, the response it produced, the reasoning the agent
supplied, and the outcome — the "show your work" record the platform promises.

The insert runs on the session the tool already holds (so it shares the tool's
transaction + the workspace search_path / RLS GUC set by ``entity_session``).
``decision_audit`` is INSERT-only for the runtime role, which keeps the trail
tamper-evident in the same spirit as the journal.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.tenancy import WorkspaceContext


async def write_decision_audit(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    entity_id: UUID,
    ctx: WorkspaceContext,
    tool_name: str,
    request_payload: dict[str, Any],
    response_payload: dict[str, Any] | None,
    reasoning: str,
    outcome: str,
    source_document_id: UUID | None = None,
) -> UUID:
    """Append one decision_audit row. Returns its id.

    JSONB columns are bound as text and cast with ``::jsonb`` so arbitrary
    request/response shapes are stored safely (parameterized — never f-string
    interpolated).
    """
    audit_id = uuid4()
    await session.execute(
        text(
            """
            INSERT INTO decision_audit (
                id, workspace_id, entity_id, agent_session_id,
                tool_name, request_payload, response_payload,
                reasoning_trace, source_document_id, model_version, outcome
            ) VALUES (
                :id, :wid, :eid, NULL,
                :tool_name, CAST(:request AS jsonb), CAST(:response AS jsonb),
                :reasoning, :source_document_id, :model_version, :outcome
            )
            """
        ),
        {
            "id": str(audit_id),
            "wid": str(workspace_id),
            "eid": str(entity_id),
            "tool_name": tool_name,
            "request": json.dumps(request_payload),
            "response": json.dumps(response_payload) if response_payload is not None else None,
            "reasoning": reasoning or "",
            "source_document_id": str(source_document_id) if source_document_id else None,
            "model_version": ctx.agent_id or "",
            "outcome": outcome,
        },
    )
    return audit_id
