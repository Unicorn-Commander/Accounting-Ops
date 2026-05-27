from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.db import SessionLocal
from app.mcp_auth import authorize_workspace
from app.workspace_session import entity_session


async def query_specialty_rules(
    workspace_id: UUID,
    entity_id: UUID,
) -> list[dict[str, Any]]:
    """Return active compliance + specialty rules for this entity.

    Filters by temporal validity (effective_from <= now AND
    (effective_to IS NULL OR effective_to > now)) and the rule's profile being
    active. Returns each rule's kind/params/trigger/action/source + reasoning.
    The caller is authorized + entity-scoped before any query.
    """
    await authorize_workspace(workspace_id, entity_id, min_role="viewer")
    async with entity_session(SessionLocal, workspace_id, entity_id) as (session, _eid):
        rows = (
            await session.execute(
                text(
                    """
                    SELECT r.id, r.profile_id, p.name AS profile_name,
                           r.kind, r.params, r.trigger, r.action, r.source,
                           r.effective_from, r.effective_to, r.reasoning
                    FROM compliance_rule r
                    JOIN compliance_profile p ON p.id = r.profile_id
                    WHERE r.entity_id = :entity_id
                      AND p.active = true
                      AND r.effective_from <= now()
                      AND (r.effective_to IS NULL OR r.effective_to > now())
                    ORDER BY r.effective_from DESC
                    """
                ),
                {"entity_id": str(entity_id)},
            )
        ).mappings().all()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        # params is JSONB (already a dict); stringify the rest of the non-JSON
        # scalars (UUIDs, enums, timestamps) for a JSON-safe payload.
        out.append(
            {
                k: (v if (k == "params" or v is None or isinstance(v, (int, float, bool, str)))
                    else str(v))
                for k, v in d.items()
            }
        )
    return out


async def add_specialty_rule(
    workspace_id: UUID,
    entity_id: UUID,
    kind: str,
    params: dict[str, Any],
    reasoning: str,
) -> dict:
    """Propose a specialty rule for this entity.

    Every rule addition queues for human approval before activation.
    The agent's reasoning is captured for the audit trail.
    """
    raise NotImplementedError("Phase 1")
