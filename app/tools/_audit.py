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


def project_decision_audit(
    *,
    workspace_id: UUID,
    entity_id: UUID,
    audit_id: UUID,
    ctx: WorkspaceContext,
    tool_name: str,
    outcome: str,
    reasoning: str,
    subject_id: UUID | str | None = None,
    overrides_subject_id: UUID | str | None = None,
) -> None:
    """Mirror a committed ``decision_audit`` row into the FalkorDB graph (best-effort).

    THE single place the canonical agent-decision write is projected to the
    graph as a ``Decision`` node — call it AFTER the caller's transaction has
    committed (the ``decision_audit`` row is authoritative; this is its
    non-authoritative mirror). It is fire-and-forget via ``falkordb.project`` so
    a graph failure is logged + swallowed and never affects the request.

    ``Decision`` node fields are mapped from the audit row: ``decision_id`` =
    the audit id, ``agent_id`` = the acting agent (or the human uc_uid when no
    agent), ``verdict`` = the audit ``outcome``, ``reasoning`` = the supplied
    reasoning, ``model_version`` = the agent id (mirrors what the Postgres writer
    stores in decision_audit.model_version).

    ``subject_id`` / ``overrides_subject_id``: see ``falkordb.record_decision``.
    When this decision corrects a prior one about the same subject (e.g. a
    recategorization), pass ``overrides_subject_id`` so the
    ``(Decision)-[:OVERRIDES]->(Decision)`` edge is derived in-graph.
    """
    # Local import keeps app.falkordb optional + avoids any import cycle.
    from app import falkordb

    agent_or_human = ctx.agent_id or ctx.user_uc_uid or "unknown"
    falkordb.project(
        falkordb.record_decision(
            workspace_id,
            entity_id,
            decision_id=str(audit_id),
            agent_id=agent_or_human,
            verdict=outcome,
            reasoning=reasoning or "",
            model_version=ctx.agent_id or None,
            subject_id=str(subject_id) if subject_id is not None else None,
            overrides_subject_id=(
                str(overrides_subject_id) if overrides_subject_id is not None else None
            ),
        ),
        label=f"decision:{tool_name}:{audit_id}",
    )
