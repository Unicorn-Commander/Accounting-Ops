"""MCP tools for specialty / compliance rules (one read, one write).

``query_specialty_rules`` (read) returns the active, temporally-valid rules for
an entity. ``add_specialty_rule`` (write) PROPOSES a rule: every addition queues
for human approval before it can take effect. We model "queued, not yet active"
with the schema's own activation gate — the rule is created under an inactive
``compliance_profile`` (``active = false``), which is exactly the predicate
``query_specialty_rules`` filters on, so a proposed rule is invisible to the
compliance engine until a human flips the profile active. An ``approval_queue``
row records the pending decision.

Both tools authorize via ``authorize_workspace`` (token-bound) and run inside
``entity_session`` (workspace search_path + RLS GUC). The write requires admin —
the same floor the HTTP add-rule route enforces. A decision_audit row is appended.
"""
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from app.db import SessionLocal
from app.mcp_auth import authorize_workspace
from app.tools._audit import write_decision_audit
from app.workspace_session import entity_session

# Enum domains from migrations/tenant/0001 — validated before insert so a bad
# override surfaces as a clean tool error rather than a DB enum violation.
_VALID_TRIGGERS = frozenset({"schedule", "event", "threshold"})
_VALID_ACTIONS = frozenset({"notify", "block", "require_approval", "tag"})

# The per-entity holding profile for agent-proposed rules. It is created
# inactive so its rules don't take effect until a human activates it.
_PENDING_PROFILE_NAME = "Agent-proposed (pending approval)"


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

    The rule is created under the entity's inactive "Agent-proposed (pending
    approval)" compliance_profile, so it is NOT yet enforced and is NOT returned
    by query_specialty_rules until a human activates the profile. `params` may
    carry optional `trigger` (schedule|event|threshold) and `action`
    (notify|block|require_approval|tag) overrides; the safe defaults are
    trigger=event, action=require_approval. Returns the proposed rule id, the
    holding profile id, and the approval_queue id.
    """
    ctx = await authorize_workspace(workspace_id, entity_id, min_role="admin")

    if not kind or not kind.strip():
        raise ValueError("kind is required for a specialty rule")
    params = params or {}
    trigger = str(params.get("trigger", "event")).lower()
    action = str(params.get("action", "require_approval")).lower()
    if trigger not in _VALID_TRIGGERS:
        raise ValueError(
            f"trigger must be one of {sorted(_VALID_TRIGGERS)} (got {trigger!r})"
        )
    if action not in _VALID_ACTIONS:
        raise ValueError(
            f"action must be one of {sorted(_VALID_ACTIONS)} (got {action!r})"
        )

    import json

    async with entity_session(SessionLocal, workspace_id, entity_id) as (session, eid):
        # Reuse the entity's inactive pending profile, or create it (inactive).
        profile = (
            await session.execute(
                text(
                    """
                    SELECT id::text AS id FROM compliance_profile
                    WHERE entity_id = :eid AND name = :name
                    LIMIT 1
                    """
                ),
                {"eid": str(eid), "name": _PENDING_PROFILE_NAME},
            )
        ).mappings().first()
        if profile is not None:
            profile_id = UUID(profile["id"])
        else:
            profile_id = uuid4()
            await session.execute(
                text(
                    """
                    INSERT INTO compliance_profile (
                        id, workspace_id, entity_id, name, active
                    ) VALUES (:id, :wid, :eid, :name, false)
                    """
                ),
                {
                    "id": str(profile_id),
                    "wid": str(workspace_id),
                    "eid": str(eid),
                    "name": _PENDING_PROFILE_NAME,
                },
            )

        rule_id = uuid4()
        await session.execute(
            text(
                """
                INSERT INTO compliance_rule (
                    id, workspace_id, entity_id, profile_id, kind, params,
                    trigger, action, effective_from, effective_to, source, reasoning
                ) VALUES (
                    :id, :wid, :eid, :pid, :kind, CAST(:params AS jsonb),
                    CAST(:trigger AS compliance_trigger),
                    CAST(:action AS compliance_action),
                    now(), NULL,
                    CAST('ai_inferred' AS compliance_source), :reasoning
                )
                """
            ),
            {
                "id": str(rule_id),
                "wid": str(workspace_id),
                "eid": str(eid),
                "pid": str(profile_id),
                "kind": kind.strip(),
                "params": json.dumps(params),
                "trigger": trigger,
                "action": action,
                "reasoning": reasoning or "",
            },
        )

        approval_id = uuid4()
        await session.execute(
            text(
                """
                INSERT INTO approval_queue (
                    id, workspace_id, entity_id, subject_type, subject_id,
                    status, reasoning, requested_by_identity
                ) VALUES (
                    :id, :wid, :eid, 'compliance_rule', :subject,
                    'pending', :reasoning, :identity
                )
                """
            ),
            {
                "id": str(approval_id),
                "wid": str(workspace_id),
                "eid": str(eid),
                "subject": str(rule_id),
                "reasoning": (
                    f"Proposed compliance rule '{kind.strip()}' "
                    f"(trigger={trigger}, action={action}). {reasoning}"
                ),
                "identity": ctx.user_uc_uid,
            },
        )

        payload = {
            "ok": True,
            "outcome": "queued_for_approval",
            "rule_id": str(rule_id),
            "profile_id": str(profile_id),
            "approval_id": str(approval_id),
            "active": False,
            "kind": kind.strip(),
            "trigger": trigger,
            "action": action,
        }
        await write_decision_audit(
            session,
            workspace_id=workspace_id,
            entity_id=eid,
            ctx=ctx,
            tool_name="add_rule",
            request_payload={"kind": kind, "params": params, "reasoning": reasoning},
            response_payload=payload,
            reasoning=reasoning,
            outcome="queued_for_approval",
        )

    return payload
