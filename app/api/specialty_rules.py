"""Workspace-scoped read + admin endpoints for specialty / compliance rules.

The MCP tools in ``app/tools/specialty_rules.py`` are ENTITY-SCOPED and write
through the agent proposal pipeline (every add queues for human approval).
The /specialty-rules **web page** is workspace-wide — Aaron lands on it from
the sidebar without picking an entity, and wants to see every active rule
across every entity in the workspace.

Routes
------

  GET  /api/workspaces/{wid}/specialty-rules
       — list rules across every entity in scope. Each row carries
         entity_id + entity_display_name so the UI can filter / group.
         Role floor: viewer.

  PATCH /api/workspaces/{wid}/specialty-rules/{rule_id}
       — deactivate a rule (set effective_to=now()). The rule row is not
         deleted; the temporal validity expires. The append-only audit row
         records the deactivation. Body: {action: "deactivate" | "reactivate"}.
         Role floor: accountant (rule-management write).

Notes on design
---------------

* CREATE of a new rule is intentionally NOT exposed at the workspace level.
  Rules are entity-scoped and the agent proposal pipeline (POST
  ``/entities/{eid}/specialty-rules``) is the canonical write surface. The
  workspace page links to /chat -> Compliance agent for a guided draft so a
  new rule still flows through the audit trail.

* Deactivation does NOT delete the row — it sets ``effective_to = now()``,
  which removes the rule from ``query_specialty_rules`` (which filters
  ``effective_to IS NULL OR effective_to > now()``). The history stays.

* Reactivation re-opens the temporal window by clearing ``effective_to``.
  Mirrors the same conservative path: nothing destructive.

* "hits / YTD" — counts decision_audit rows since Jan 1 whose
  ``response_payload`` references this rule_id. Best-effort; non-blocking
  if the join is empty.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

from app.authz import scoped_entity_ids
from app.db import SessionLocal
from app.tenancy import WorkspaceContext, require_role, resolve_workspace
from app.workspace_session import workspace_session

router = APIRouter(tags=["specialty-rules"])
_log = logging.getLogger("acctops.api.specialty_rules")


# ---------------------------------------------------------------------------
# Response shapes
# ---------------------------------------------------------------------------


class SpecialtyRuleRow(BaseModel):
    id: str
    workspace_id: str
    entity_id: str
    entity_display_name: str | None
    profile_id: str
    profile_name: str
    profile_active: bool
    kind: str
    params: dict[str, Any]
    trigger: str
    action: str
    source: str
    reasoning: str
    effective_from: str
    effective_to: str | None
    # active = profile_active AND effective_from <= now AND (effective_to IS NULL OR effective_to > now)
    active: bool
    hits_ytd: int


class SpecialtyRulesResponse(BaseModel):
    rows: list[SpecialtyRuleRow]
    total: int
    as_of: str


class DeactivateRequest(BaseModel):
    # "deactivate" -> set effective_to = now()
    # "reactivate" -> clear effective_to back to NULL
    action: str
    note: str | None = None


# ---------------------------------------------------------------------------
# GET — list rules across the workspace
# ---------------------------------------------------------------------------


@router.get("/api/workspaces/{workspace_id}/specialty-rules")
async def list_specialty_rules(
    workspace_id: UUID,
    entity_id: UUID | None = None,
    include_inactive: bool = False,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> SpecialtyRulesResponse:
    """List compliance + specialty rules across every entity in scope.

    Defaults to ACTIVE-only (the operating set). Pass include_inactive=true
    to see paused / expired rules in the history view.
    """
    require_role(workspace, "viewer")
    scope_restriction = scoped_entity_ids(workspace, entity_id)

    where: list[str] = ["1=1"]
    params: dict[str, Any] = {}

    if entity_id:
        where.append("r.entity_id = :eid")
        params["eid"] = str(entity_id)
    if scope_restriction is not None:
        where.append("r.entity_id = ANY(:entity_scope)")
        params["entity_scope"] = [str(e) for e in scope_restriction]
    if not include_inactive:
        where.append("p.active = true")
        where.append("r.effective_from <= now()")
        where.append("(r.effective_to IS NULL OR r.effective_to > now())")

    where_sql = " AND ".join(where)

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT
                        r.id::text          AS id,
                        r.workspace_id::text AS workspace_id,
                        r.entity_id::text   AS entity_id,
                        e.display_name      AS entity_display_name,
                        r.profile_id::text  AS profile_id,
                        p.name              AS profile_name,
                        p.active            AS profile_active,
                        r.kind              AS kind,
                        r.params            AS params,
                        r.trigger::text     AS trigger,
                        r.action::text      AS action,
                        r.source::text      AS source,
                        r.reasoning         AS reasoning,
                        r.effective_from    AS effective_from,
                        r.effective_to     AS effective_to
                    FROM compliance_rule r
                    JOIN compliance_profile p ON p.id = r.profile_id
                    LEFT JOIN entity e ON e.id = r.entity_id
                    WHERE {where_sql}
                    ORDER BY r.effective_from DESC, r.id DESC
                    """
                ),
                params,
            )
        ).mappings().all()

        # Best-effort YTD hits count via decision_audit rows whose
        # response_payload references the rule id. Slow lookup is OK — the
        # /specialty-rules page is admin-low-frequency, not in a hot path.
        ytd_start = dt.datetime(dt.date.today().year, 1, 1, tzinfo=dt.timezone.utc)
        hits_by_rule: dict[str, int] = {}
        rule_ids = [r["id"] for r in rows]
        if rule_ids:
            try:
                hits = (
                    await session.execute(
                        text(
                            """
                            SELECT
                                response_payload ->> 'rule_id' AS rule_id,
                                COUNT(*)::int AS hits
                            FROM decision_audit
                            WHERE created_at >= :since
                              AND response_payload ? 'rule_id'
                              AND response_payload ->> 'rule_id' = ANY(:ids)
                            GROUP BY response_payload ->> 'rule_id'
                            """
                        ),
                        {"since": ytd_start, "ids": rule_ids},
                    )
                ).mappings().all()
                hits_by_rule = {h["rule_id"]: h["hits"] for h in hits}
            except Exception as e:
                # Best-effort — never block the list on an aggregation failure.
                _log.warning("ytd hits aggregation failed: %s", e)

    out: list[SpecialtyRuleRow] = []
    now_utc = dt.datetime.now(tz=dt.timezone.utc)
    for r in rows:
        effective_from = r["effective_from"]
        effective_to = r["effective_to"]
        params_val = r["params"]
        if isinstance(params_val, (bytes, bytearray, str)):
            try:
                params_val = json.loads(
                    params_val.decode("utf-8")
                    if isinstance(params_val, (bytes, bytearray))
                    else params_val
                )
            except json.JSONDecodeError:
                params_val = {}
        elif params_val is None:
            params_val = {}

        is_active = bool(
            r["profile_active"]
            and (
                isinstance(effective_from, dt.datetime)
                and effective_from <= now_utc
            )
            and (
                effective_to is None
                or (isinstance(effective_to, dt.datetime) and effective_to > now_utc)
            )
        )

        out.append(
            SpecialtyRuleRow(
                id=r["id"],
                workspace_id=r["workspace_id"],
                entity_id=r["entity_id"],
                entity_display_name=r["entity_display_name"],
                profile_id=r["profile_id"],
                profile_name=r["profile_name"] or "",
                profile_active=bool(r["profile_active"]),
                kind=r["kind"] or "",
                params=params_val if isinstance(params_val, dict) else {},
                trigger=r["trigger"] or "",
                action=r["action"] or "",
                source=r["source"] or "",
                reasoning=r["reasoning"] or "",
                effective_from=(
                    effective_from.isoformat()
                    if isinstance(effective_from, dt.datetime)
                    else str(effective_from)
                ),
                effective_to=(
                    effective_to.isoformat()
                    if isinstance(effective_to, dt.datetime)
                    else None
                ),
                active=is_active,
                hits_ytd=hits_by_rule.get(r["id"], 0),
            )
        )

    return SpecialtyRulesResponse(
        rows=out,
        total=len(out),
        as_of=now_utc.isoformat(),
    )


# ---------------------------------------------------------------------------
# PATCH — deactivate / reactivate a rule
# ---------------------------------------------------------------------------


@router.patch("/api/workspaces/{workspace_id}/specialty-rules/{rule_id}")
async def patch_specialty_rule(
    workspace_id: UUID,
    rule_id: UUID,
    body: DeactivateRequest,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """Deactivate or reactivate a compliance rule by adjusting effective_to.

    Conservative — no DELETE, ever. The change writes a decision_audit row
    so the rollback is auditable.

    Role floor: accountant. (Same rank as the underlying ``add_specialty_rule``
    HTTP path which requires admin; deactivation is less destructive — we
    accept accountant since they manage day-to-day rule operations. Hard
    DELETE / archive would require admin.)
    """
    require_role(workspace, "accountant")

    action = (body.action or "").strip().lower()
    if action not in ("deactivate", "reactivate"):
        raise HTTPException(
            status_code=400,
            detail="action must be 'deactivate' or 'reactivate'",
        )

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        # Re-read the rule for entity-scope authz + the audit row entity_id.
        row = (
            await session.execute(
                text(
                    """
                    SELECT
                        r.id::text            AS id,
                        r.workspace_id::text  AS workspace_id,
                        r.entity_id::text     AS entity_id,
                        r.kind                AS kind,
                        r.effective_from      AS effective_from,
                        r.effective_to        AS effective_to
                    FROM compliance_rule r
                    WHERE r.id = :rid
                    """
                ),
                {"rid": str(rule_id)},
            )
        ).mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="Rule not found")

        # entity-scope check
        scoped_entity_ids(workspace, UUID(row["entity_id"]))

        if action == "deactivate":
            # Setting effective_to = now() ends the temporal validity.
            await session.execute(
                text(
                    """
                    UPDATE compliance_rule
                    SET effective_to = now()
                    WHERE id = :rid
                    """
                ),
                {"rid": str(rule_id)},
            )
            outcome = "deactivated"
            new_effective_to_marker = "now()"
        else:
            # Reactivate: clear effective_to. The profile_active state is
            # untouched (a rule whose profile is inactive stays gated by the
            # profile until that's flipped, but we don't surface profile
            # toggles from here; agent proposals do).
            await session.execute(
                text(
                    """
                    UPDATE compliance_rule
                    SET effective_to = NULL
                    WHERE id = :rid
                    """
                ),
                {"rid": str(rule_id)},
            )
            outcome = "reactivated"
            new_effective_to_marker = "NULL"

        # Append the audit row.
        audit_id = uuid4()
        audit_req = {
            "rule_id": str(rule_id),
            "kind": row["kind"],
            "action": action,
            "note": body.note or "",
            "actor_uc_uid": workspace.user_uc_uid,
            "actor_role": "human",
            "previous_effective_to": (
                row["effective_to"].isoformat()
                if isinstance(row["effective_to"], dt.datetime)
                else None
            ),
        }
        audit_resp = {
            "rule_id": str(rule_id),
            "new_effective_to": new_effective_to_marker,
            "outcome": outcome,
        }
        await session.execute(
            text(
                """
                INSERT INTO decision_audit (
                    id, workspace_id, entity_id, agent_session_id,
                    tool_name, request_payload, response_payload,
                    reasoning_trace, source_document_id,
                    model_version, outcome
                ) VALUES (
                    :id, :wid, :eid, NULL,
                    :tool_name, CAST(:req AS jsonb), CAST(:resp AS jsonb),
                    :reasoning, NULL,
                    '', :outcome
                )
                """
            ),
            {
                "id": str(audit_id),
                "wid": str(workspace.workspace_id),
                "eid": row["entity_id"],
                "tool_name": f"rule_{action}",
                "req": json.dumps(audit_req),
                "resp": json.dumps(audit_resp),
                "reasoning": (
                    body.note
                    or f"Rule {row['kind']} {outcome} by {workspace.user_uc_uid}"
                ),
                "outcome": outcome,
            },
        )

    return {
        "ok": True,
        "rule_id": str(rule_id),
        "action": action,
        "outcome": outcome,
        "audit_id": str(audit_id),
    }
