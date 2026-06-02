"""AI Activity timeline + Undo + Challenge endpoints.

The "AI Activity log" is Aaron's view into every action an AI agent took
on his behalf. It aggregates three append-only sources:

  * ``decision_audit``    — one row per agent-initiated MCP tool call.
                            Carries tool_name, request_payload, response_payload,
                            reasoning_trace, outcome, agent_session_id.
  * ``activity_event``    — coarser-grained "the agent did X" stream
                            (verdict + materiality). Older feed, kept for
                            backwards compat with the legacy /agent-activity
                            page mock data.
  * ``tax_review_queue``  — memos, flagged positions, drafted forms.
                            Surfaces what an agent WROTE into the human
                            review pile.

The timeline endpoint returns a chronologically-ordered, human-readable
list with enough context for the UI to:

  * group entries by agent_id
  * render an outcome badge + materiality
  * surface the reasoning trace ("Why?")
  * show whether the action is reversible
  * link back to the underlying artifact (memo body, journal entry,
    specialist run, etc.)

Reversibility classification — CONSERVATIVE BY DEFAULT (when in doubt,
mark challenge-only):

  +----------------------------+-----------+-------------------------------+
  | tool_name / item shape     | Reverse?  | Mechanism                     |
  +----------------------------+-----------+-------------------------------+
  | draft_memo                 | YES       | mark tax_review_queue memo    |
  |   (memo / flagged_position)|           |   status='rejected'           |
  | flag_for_cpa_review        | YES       | (same as draft_memo)          |
  | categorize / recat         | YES       | post reversal JE within 24h   |
  | approval_decision          | NO        | challenge-only; the decision  |
  |   (post a JE / apply rule) |           |   already mutated the ledger  |
  | add_specialty_rule         | NO        | challenge-only                |
  | specialist_run             | NO        | challenge-only; the run wrote |
  |   (1120 preparer, etc.)    |           |   many rows already           |
  | everything else            | NO        | challenge-only                |
  +----------------------------+-----------+-------------------------------+

  * "YES within 24h" means: only the human who SEES the action within 24
    hours of it being recorded can one-click undo. After that the action
    is treated as accepted and must be challenged (which routes a new
    memo to the CPA queue) instead of reverted.

Routes
------

  GET   /api/workspaces/{wid}/ai-activity
        ?entity_id=&since=&until=&limit=&agent=&action=
        — chronologically-ordered AI-initiated actions.

  GET   /api/workspaces/{wid}/ai-activity/stream
        — server-sent events of new activity rows. Long-poll; sends a
          ``heartbeat`` comment every 15s.

  POST  /api/workspaces/{wid}/ai-activity/{audit_id}/undo
        — reverse a reversible AI action.
        Body: {note?: str}.
        Returns 409 if the audit row isn't reversible or the 24h window
        has passed; 422 if a closed_period blocks the reversal.

  POST  /api/workspaces/{wid}/ai-activity/{audit_id}/challenge
        — file a tax_review_queue memo with topic='ai_output_challenged'
          linking back to the original audit row.
        Body: {reason: str}.

Role floors
-----------

  * List / stream:  viewer  — read-only audit feed.
  * Undo:           accountant — same floor as the original write surface.
  * Challenge:      viewer    — anyone can flag a concern for the CPA.

Audit trail
-----------

Every undo + challenge writes ITS OWN decision_audit row with
``actor_role='human'`` and ``tool_name='undo'`` / ``'challenge'``,
referencing the original audit row via ``request_payload.parent_audit_id``.
decision_audit is append-only — the original row is never modified.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from typing import Any, AsyncIterator
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import text

from app.authz import assert_entity_in_scope
from app.db import SessionLocal
from app.tenancy import WorkspaceContext, require_role, resolve_workspace
from app.workspace_session import workspace_session

router = APIRouter(tags=["ai-activity"])
_log = logging.getLogger("acctops.api.ai_activity")


# ---------------------------------------------------------------------------
# Reversibility classification
# ---------------------------------------------------------------------------
#
# Maps decision_audit.tool_name -> ("reverse_kind", reversible_window_hours).
# ``reverse_kind`` is one of:
#
#   * "memo_status_reject"          — flip tax_review_queue row to rejected
#   * "journal_post_contra"         — post a reversing JE within window
#   * "categorization_pending"      — flip approval_queue back to pending
#   * None                          — challenge-only
#
# IMPORTANT: when in doubt, return None — never make a write reversible
# unless we have a clear, scoped reversal mechanism.

_REVERSIBILITY: dict[str, tuple[str | None, int]] = {
    # MCP tool: drafts into tax_review_queue
    "draft_memo": ("memo_status_reject", 24 * 365),  # always reversible
    "flag_for_cpa_review": ("memo_status_reject", 24 * 365),
    # Auto-categorize / recategorize
    "categorize": ("journal_post_contra", 24),  # 24h grace window
    "post_correction": ("journal_post_contra", 24),
    # Approval decisions — the underlying JE/rule may already be live;
    # we treat as challenge-only by design. Aaron can post a manual contra.
    "approval_decision": (None, 0),
    "add_specialty_rule": (None, 0),
    # Specialist runs — they may have written dozens of rows
    "run_specialist": (None, 0),
    # CFO→specialist consultation (silent delegation). Read/draft-only and
    # produces no ledger mutation, but it's an analysis event, not something to
    # mechanically "undo" — challenge-only.
    "consult_specialist": (None, 0),
}


def reversibility_for(tool_name: str) -> tuple[str | None, int]:
    """Look up the reversal kind + window-hours for a tool. Defaults to
    challenge-only when the tool isn't explicitly classified.
    """
    return _REVERSIBILITY.get(tool_name, (None, 0))


def is_reversible(tool_name: str, created_at: dt.datetime) -> bool:
    """True iff the action is mechanically reversible AND inside the window."""
    kind, window_h = reversibility_for(tool_name)
    if kind is None:
        return False
    if window_h <= 0:
        return False
    if window_h >= 24 * 365:  # treated as "always" (no time bound)
        return True
    now = dt.datetime.now(tz=dt.timezone.utc)
    age = now - created_at
    return age.total_seconds() <= window_h * 3600


# ---------------------------------------------------------------------------
# Human-readable action labels
# ---------------------------------------------------------------------------
#
# Tool names are MCP-internal. Render something Aaron will actually read.
# Falls back to the raw tool_name for anything not listed.

_ACTION_LABELS: dict[str, str] = {
    "draft_memo": "Drafted memo",
    "flag_for_cpa_review": "Flagged for CPA review",
    "categorize": "Categorized transaction",
    "post_correction": "Posted recategorization",
    "approval_decision": "Decided approval",
    "add_specialty_rule": "Added compliance rule",
    "run_specialist": "Ran specialist agent",
    "consult_specialist": "CFO consulted specialist",
    "link_receipt": "Linked receipt",
    "post_journal_entry": "Posted journal entry",
    "undo": "Undid prior AI action",
    "challenge": "Challenged AI output",
}


def label_for(tool_name: str) -> str:
    return _ACTION_LABELS.get(tool_name, tool_name.replace("_", " ").capitalize())


# ---------------------------------------------------------------------------
# Response shapes
# ---------------------------------------------------------------------------


class AIActivityRow(BaseModel):
    """One row in the AI activity timeline."""

    id: str
    workspace_id: str
    entity_id: str
    entity_display_name: str | None
    agent_id: str
    agent_session_id: str | None
    tool_name: str
    action_label: str
    target: dict[str, Any]  # entity_id, tax_year, queue_item_id, je_id, etc.
    outcome: str
    auto_applied: bool  # True if no human decided
    reasoning_trace: str
    request_payload: dict[str, Any] | None
    response_payload: dict[str, Any] | None
    artifact: dict[str, Any] | None  # link metadata
    reversible: bool
    reverse_kind: str | None
    reverse_window_hours: int
    undone_at: str | None
    undone_by: str | None
    challenged_at: str | None
    challenged_by: str | None
    challenge_memo_id: str | None
    created_at: str
    latency_ms: int | None


class AIActivityResponse(BaseModel):
    rows: list[AIActivityRow]
    as_of: str
    total_returned: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_json(blob: Any) -> dict[str, Any] | None:
    """Coerce a JSONB column into a dict for the wire payload. None on empty."""
    if blob is None:
        return None
    if isinstance(blob, (bytes, bytearray)):
        try:
            blob = json.loads(blob.decode("utf-8"))
        except json.JSONDecodeError:
            return None
    if isinstance(blob, str):
        try:
            blob = json.loads(blob)
        except json.JSONDecodeError:
            return None
    if not isinstance(blob, dict):
        return None
    return blob


def _target_from(
    tool_name: str,
    request: dict[str, Any] | None,
    response: dict[str, Any] | None,
    entity_id: str,
) -> dict[str, Any]:
    """Extract a small dict of identifiers describing what the action touched."""
    target: dict[str, Any] = {"entity_id": entity_id}
    src = response or {}
    src_req = request or {}

    # tax_year
    ty = src.get("tax_year") or src_req.get("tax_year")
    if ty:
        target["tax_year"] = ty

    # queue item ids — different tools name them differently
    for key in ("queue_item_id", "memo_id", "id", "approval_id", "journal_entry_id",
                "rule_id", "run_id"):
        v = src.get(key) or src_req.get(key)
        if v:
            target[key] = v

    # form reference for review-queue memos
    form_ref = src.get("form_reference") or src_req.get("form_reference")
    if form_ref:
        target["form_reference"] = form_ref

    return target


def _artifact_link(tool_name: str, target: dict[str, Any]) -> dict[str, Any] | None:
    """Return a {kind, url, label} link to the artifact, for the UI."""
    eid = target.get("entity_id")
    ty = target.get("tax_year")
    if tool_name in ("draft_memo", "flag_for_cpa_review"):
        mid = target.get("memo_id") or target.get("queue_item_id") or target.get("id")
        if mid:
            return {
                "kind": "tax_review_queue_item",
                "label": f"Memo {str(mid)[:8]}",
                "queue_item_id": str(mid),
            }
    if tool_name in ("approval_decision",):
        aid = target.get("approval_id")
        if aid:
            return {
                "kind": "approval",
                "label": f"Approval {str(aid)[:8]}",
                "approval_id": str(aid),
            }
    if tool_name in ("post_correction", "categorize", "post_journal_entry"):
        jid = target.get("journal_entry_id")
        if jid:
            return {
                "kind": "journal_entry",
                "label": f"JE {str(jid)[:8]}",
                "journal_entry_id": str(jid),
            }
    if tool_name == "run_specialist":
        rid = target.get("run_id")
        if rid and ty and eid:
            return {
                "kind": "specialist_run",
                "label": f"Run {str(rid)[:8]}",
                "run_id": str(rid),
                "tax_year": ty,
                "entity_id": str(eid),
            }
    return None


def _auto_applied(tool_name: str, response: dict[str, Any] | None) -> bool:
    """An action is 'auto-applied' if no human is in the decided_by chain."""
    if tool_name == "approval_decision":
        return False  # always human
    # If the response has explicit applied=False, we kept it queued for human.
    if response and response.get("applied") is False:
        return False
    return True


# ---------------------------------------------------------------------------
# GET /ai-activity — timeline
# ---------------------------------------------------------------------------


@router.get("/api/workspaces/{workspace_id}/ai-activity")
async def get_ai_activity(
    workspace_id: UUID,
    entity_id: UUID | None = None,
    since: str | None = Query(default=None, description="ISO timestamp filter"),
    until: str | None = Query(default=None, description="ISO timestamp filter"),
    limit: int = Query(default=50, ge=1, le=500),
    agent: str | None = Query(default=None, description="Substring of agent_id"),
    action: str | None = Query(
        default=None, description="Substring of tool_name (e.g. 'memo')"
    ),
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> AIActivityResponse:
    """Return the AI activity timeline for a workspace.

    Joins ``decision_audit`` with ``agent_session`` + ``entity`` and
    filters to non-human submitters. The optional ``since``/``until``
    bounds are ISO timestamps (UTC). The ``agent`` and ``action`` filters
    do simple substring matching for the UI's filter chips.

    Adds:
      * undo/challenge state — by selecting any decision_audit row whose
        request_payload.parent_audit_id == this row's id with tool_name in
        ('undo', 'challenge').
      * reversibility classification.
      * a target dict + artifact link for the UI to navigate to.
    """
    require_role(workspace, "viewer")
    scope_restriction = assert_entity_in_scope(workspace, entity_id)

    since_dt = _parse_iso_or_none(since)
    until_dt = _parse_iso_or_none(until)

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        where: list[str] = ["1=1"]
        params: dict[str, Any] = {"limit": limit}

        if entity_id:
            where.append("da.entity_id = :eid")
            params["eid"] = str(entity_id)
        if scope_restriction is not None:
            where.append("da.entity_id = ANY(:entity_scope)")
            params["entity_scope"] = [str(e) for e in scope_restriction]
        if since_dt:
            where.append("da.created_at >= :since")
            params["since"] = since_dt
        if until_dt:
            where.append("da.created_at <= :until")
            params["until"] = until_dt
        if agent:
            where.append(
                "(COALESCE(da.model_version, '') ILIKE :agent_like "
                " OR COALESCE(s.agent_id, '') ILIKE :agent_like)"
            )
            params["agent_like"] = f"%{agent}%"
        if action:
            where.append("da.tool_name ILIKE :action_like")
            params["action_like"] = f"%{action}%"

        # AI activity == decision_audit rows that came from an AGENT, not a human.
        # Human-initiated undos/challenges ALSO live in decision_audit (we want
        # that — it's all the same audit trail), but they have tool_name in
        # ('undo','challenge') and are excluded from the AI timeline.
        where.append("da.tool_name NOT IN ('undo', 'challenge')")

        # An action is "AI-initiated" iff either:
        #   * there's no agent_session_id (the chat surface writes audit rows
        #     for in-chat tool calls and stores model_version=agent_id), OR
        #   * the linked agent_session was opened by an agent (user_uc_uid IS NULL
        #     or starts with 'agent:'), OR
        #   * model_version is set (humans don't carry a model_version).
        where.append(
            "(da.model_version IS NOT NULL AND da.model_version <> '')"
        )

        where_sql = " AND ".join(where)

        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT
                        da.id::text                    AS id,
                        da.workspace_id::text          AS workspace_id,
                        da.entity_id::text             AS entity_id,
                        e.display_name                 AS entity_display_name,
                        da.tool_name                   AS tool_name,
                        da.request_payload             AS request_payload,
                        da.response_payload            AS response_payload,
                        da.reasoning_trace             AS reasoning_trace,
                        da.outcome                     AS outcome,
                        da.model_version               AS model_version,
                        da.latency_ms                  AS latency_ms,
                        da.created_at                  AS created_at,
                        da.agent_session_id::text      AS agent_session_id,
                        s.agent_id                     AS session_agent_id
                    FROM decision_audit da
                    LEFT JOIN entity        e ON e.id = da.entity_id
                    LEFT JOIN agent_session s ON s.id = da.agent_session_id
                    WHERE {where_sql}
                    ORDER BY da.created_at DESC, da.id DESC
                    LIMIT :limit
                    """
                ),
                params,
            )
        ).mappings().all()

        # ---- Also pull agent-submitted tax_review_queue rows. The MCP
        # write tools (draft_memo, flag_for_cpa_review) write straight to
        # tax_review_queue with submitter_kind='agent' WITHOUT writing a
        # decision_audit row. Surface them here so the timeline reflects
        # every agent action, not just the ones that go through the audit
        # writer.
        #
        # Mapped to the same shape as the decision_audit rows so the
        # downstream rendering treats them identically.
        #
        # Filter mirrors the decision_audit filter (entity scope, since,
        # agent substring, action substring).
        trq_where: list[str] = [
            "trq.submitter_kind = 'agent'",
            # Exclude internal marker rows used by the open-items dashboard.
            "(trq.form_reference IS NULL OR "
            "(trq.form_reference NOT LIKE 'DOC_RECEIVED:%' AND "
            "trq.form_reference NOT LIKE 'DECISION_RESOLVED:%'))",
        ]
        trq_params: dict[str, Any] = {"limit": limit}
        if entity_id:
            trq_where.append("trq.entity_id = :eid")
            trq_params["eid"] = str(entity_id)
        if scope_restriction is not None:
            trq_where.append("trq.entity_id = ANY(:entity_scope)")
            trq_params["entity_scope"] = [str(e) for e in scope_restriction]
        if since_dt:
            trq_where.append("trq.created_at >= :since")
            trq_params["since"] = since_dt
        if until_dt:
            trq_where.append("trq.created_at <= :until")
            trq_params["until"] = until_dt
        if agent:
            trq_where.append("trq.submitter_id ILIKE :agent_like")
            trq_params["agent_like"] = f"%{agent}%"
        if action:
            # `action` filters the displayed action label. For tax_review_queue
            # we map item_type to a synthetic tool_name.
            #   memo / question -> 'draft_memo'
            #   flagged_position-> 'flag_for_cpa_review'
            #   form_line_proposal -> 'flag_for_cpa_review'
            # So a filter for 'memo' should catch memos/questions; for 'flag'
            # should catch the flag types.
            trq_where.append(
                "(:action_substr = '' "
                "OR trq.item_type ILIKE :action_like)"
            )
            trq_params["action_substr"] = action
            trq_params["action_like"] = f"%{action}%"

        trq_where_sql = " AND ".join(trq_where)
        trq_rows = (
            await session.execute(
                text(
                    f"""
                    SELECT
                        trq.id::text                AS id,
                        trq.workspace_id::text      AS workspace_id,
                        trq.entity_id::text         AS entity_id,
                        e.display_name              AS entity_display_name,
                        trq.tax_year                AS tax_year,
                        trq.item_type               AS item_type,
                        trq.form_reference          AS form_reference,
                        trq.content                 AS content,
                        trq.confidence              AS confidence,
                        trq.status                  AS status,
                        trq.submitter_id            AS submitter_id,
                        trq.created_at              AS created_at,
                        trq.resolved_at             AS resolved_at,
                        trq.reviewer_uc_uid         AS reviewer_uc_uid
                    FROM tax_review_queue trq
                    LEFT JOIN entity e ON e.id = trq.entity_id
                    WHERE {trq_where_sql}
                    ORDER BY trq.created_at DESC, trq.id DESC
                    LIMIT :limit
                    """
                ),
                trq_params,
            )
        ).mappings().all()

        # ---- Pull undo/challenge follow-ups in one query.
        audit_ids = [r["id"] for r in rows]
        followups_by_parent: dict[str, list[dict[str, Any]]] = {}
        if audit_ids:
            followups = (
                await session.execute(
                    text(
                        """
                        SELECT
                            id::text         AS id,
                            tool_name        AS tool_name,
                            request_payload  AS request_payload,
                            response_payload AS response_payload,
                            created_at       AS created_at,
                            model_version    AS model_version
                        FROM decision_audit
                        WHERE tool_name IN ('undo', 'challenge')
                          AND request_payload ? 'parent_audit_id'
                          AND request_payload ->> 'parent_audit_id' = ANY(:ids)
                        ORDER BY created_at ASC
                        """
                    ),
                    {"ids": audit_ids},
                )
            ).mappings().all()
            for f in followups:
                req = _safe_json(f["request_payload"]) or {}
                parent = str(req.get("parent_audit_id"))
                followups_by_parent.setdefault(parent, []).append(dict(f))

    out: list[AIActivityRow] = []
    for r in rows:
        req = _safe_json(r["request_payload"])
        resp = _safe_json(r["response_payload"])
        tool_name = r["tool_name"]
        agent_id = r["session_agent_id"] or r["model_version"] or "(unknown agent)"
        target = _target_from(tool_name, req, resp, r["entity_id"])
        artifact = _artifact_link(tool_name, target)
        rev_kind, rev_window = reversibility_for(tool_name)
        created_at = r["created_at"]
        if not isinstance(created_at, dt.datetime):
            # Defensive — SA should always yield datetime here.
            created_at = dt.datetime.fromisoformat(str(created_at))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=dt.timezone.utc)
        # Did anyone undo / challenge it?
        undone_at = None
        undone_by = None
        challenged_at = None
        challenged_by = None
        challenge_memo_id = None
        for f in followups_by_parent.get(r["id"], []):
            f_req = _safe_json(f["request_payload"]) or {}
            f_resp = _safe_json(f["response_payload"]) or {}
            actor = f_req.get("actor_uc_uid") or "(unknown user)"
            ts = f["created_at"]
            if isinstance(ts, dt.datetime):
                ts = ts.isoformat()
            if f["tool_name"] == "undo":
                undone_at = ts
                undone_by = actor
            elif f["tool_name"] == "challenge":
                challenged_at = ts
                challenged_by = actor
                challenge_memo_id = f_resp.get("memo_id")

        row = AIActivityRow(
            id=r["id"],
            workspace_id=r["workspace_id"],
            entity_id=r["entity_id"],
            entity_display_name=r["entity_display_name"],
            agent_id=agent_id,
            agent_session_id=r["agent_session_id"],
            tool_name=tool_name,
            action_label=label_for(tool_name),
            target=target,
            outcome=r["outcome"],
            auto_applied=_auto_applied(tool_name, resp),
            reasoning_trace=r["reasoning_trace"] or "",
            request_payload=req,
            response_payload=resp,
            artifact=artifact,
            reversible=(
                is_reversible(tool_name, created_at) and undone_at is None
            ),
            reverse_kind=rev_kind,
            reverse_window_hours=rev_window,
            undone_at=undone_at,
            undone_by=undone_by,
            challenged_at=challenged_at,
            challenged_by=challenged_by,
            challenge_memo_id=challenge_memo_id,
            created_at=created_at.isoformat(),
            latency_ms=r["latency_ms"],
        )
        out.append(row)

    # ---- Merge in agent-submitted tax_review_queue rows
    # The MCP draft_memo / flag_for_cpa_review tools write straight to the
    # queue without writing a decision_audit row. We synthesize an
    # AIActivityRow from each so the timeline reflects every agent action.
    #
    # The row id is the queue item id prefixed 'trq:' so it doesn't collide
    # with decision_audit uuids (which the undo/challenge endpoints expect).
    for r in trq_rows:
        item_type = (r["item_type"] or "memo").lower()
        # Map item_type -> synthetic tool_name so reversibility + labels work.
        if item_type in ("memo", "question"):
            syn_tool = "draft_memo"
        elif item_type in ("flagged_position", "form_line_proposal"):
            syn_tool = "flag_for_cpa_review"
        else:
            syn_tool = item_type
        content = _safe_json(r["content"]) or {}
        target = {
            "entity_id": r["entity_id"],
            "tax_year": r["tax_year"],
            "queue_item_id": r["id"],
            "memo_id": r["id"],
        }
        if r["form_reference"]:
            target["form_reference"] = r["form_reference"]
        artifact = _artifact_link(syn_tool, target)
        rev_kind, rev_window = reversibility_for(syn_tool)
        created_at = r["created_at"]
        if not isinstance(created_at, dt.datetime):
            created_at = dt.datetime.fromisoformat(str(created_at))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=dt.timezone.utc)
        # Treat queue rows that have moved out of 'pending' as effectively
        # undone (the CPA / human acted on them) — we mirror this in the
        # AIActivityRow so the UI shows them grayed out.
        status = (r["status"] or "pending").lower()
        if status in ("rejected", "resolved"):
            undone_at = (
                r["resolved_at"].isoformat()
                if isinstance(r["resolved_at"], dt.datetime)
                else None
            )
            undone_by = r["reviewer_uc_uid"]
            reversible = False
        else:
            undone_at = None
            undone_by = None
            reversible = is_reversible(syn_tool, created_at)
        agent_id = r["submitter_id"] or "(unknown agent)"
        reasoning = (
            content.get("text", "")
            if isinstance(content, dict)
            else str(content)
        )[:600]
        topic = content.get("topic") if isinstance(content, dict) else None
        action_label = label_for(syn_tool)
        if topic:
            action_label = f"{action_label}: {topic[:80]}"
        synthetic_id = f"trq:{r['id']}"
        out.append(
            AIActivityRow(
                id=synthetic_id,
                workspace_id=r["workspace_id"],
                entity_id=r["entity_id"],
                entity_display_name=r["entity_display_name"],
                agent_id=agent_id,
                agent_session_id=None,
                tool_name=syn_tool,
                action_label=action_label,
                target=target,
                outcome=(
                    "rejected" if status == "rejected"
                    else "resolved" if status == "resolved"
                    else "pending"
                ),
                # Specialist-drafted queue items are queued for human review;
                # never auto-applied.
                auto_applied=False,
                reasoning_trace=reasoning,
                request_payload={
                    "topic": topic,
                    "form_reference": r["form_reference"],
                    "tax_year": r["tax_year"],
                    "confidence": r["confidence"],
                },
                response_payload={
                    "queue_item_id": r["id"],
                    "status": status,
                },
                artifact=artifact,
                reversible=reversible,
                reverse_kind=rev_kind,
                reverse_window_hours=rev_window,
                undone_at=undone_at,
                undone_by=undone_by,
                challenged_at=None,
                challenged_by=None,
                challenge_memo_id=None,
                created_at=created_at.isoformat(),
                latency_ms=None,
            )
        )

    # ---- Final sort: chronological DESC across both sources, then trim
    out.sort(key=lambda r: r.created_at, reverse=True)
    out = out[:limit]

    return AIActivityResponse(
        rows=out,
        as_of=dt.datetime.now(tz=dt.timezone.utc).isoformat(),
        total_returned=len(out),
    )


def _parse_iso_or_none(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        # accept trailing Z
        v = value[:-1] + "+00:00" if value.endswith("Z") else value
        d = dt.datetime.fromisoformat(v)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid ISO timestamp: {value!r}",
        )


# ---------------------------------------------------------------------------
# GET /ai-activity/stream — SSE live updates
# ---------------------------------------------------------------------------


@router.get(
    "/api/workspaces/{workspace_id}/ai-activity/stream",
    response_class=StreamingResponse,
)
async def stream_ai_activity(
    workspace_id: UUID,
    entity_id: UUID | None = None,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> StreamingResponse:
    """Server-sent events stream of new AI activity rows.

    Polls decision_audit every 3s for any row newer than the last seen
    created_at. Sends one ``event: activity`` frame per new row. Sends a
    ``: heartbeat`` comment line every 15s so Traefik / reverse proxies
    don't time out idle connections.

    Role floor: viewer.
    """
    require_role(workspace, "viewer")
    scope_restriction = assert_entity_in_scope(workspace, entity_id)

    async def _generator() -> AsyncIterator[bytes]:
        last_ts = dt.datetime.now(tz=dt.timezone.utc)
        heartbeat_at = dt.datetime.now(tz=dt.timezone.utc)
        # Initial frame so the client knows the connection is alive.
        yield b": ai-activity-stream open\n\n"
        try:
            while True:
                await asyncio.sleep(3.0)
                now = dt.datetime.now(tz=dt.timezone.utc)
                async with workspace_session(
                    SessionLocal, workspace.workspace_id
                ) as session:
                    where = [
                        "da.tool_name NOT IN ('undo', 'challenge')",
                        "(da.model_version IS NOT NULL AND da.model_version <> '')",
                        "da.created_at > :last_ts",
                    ]
                    params: dict[str, Any] = {"last_ts": last_ts}
                    if entity_id:
                        where.append("da.entity_id = :eid")
                        params["eid"] = str(entity_id)
                    if scope_restriction is not None:
                        where.append("da.entity_id = ANY(:entity_scope)")
                        params["entity_scope"] = [
                            str(e) for e in scope_restriction
                        ]
                    where_sql = " AND ".join(where)
                    rows = (
                        await session.execute(
                            text(
                                f"""
                                SELECT
                                    da.id::text          AS id,
                                    da.entity_id::text   AS entity_id,
                                    da.tool_name         AS tool_name,
                                    da.model_version     AS model_version,
                                    da.outcome           AS outcome,
                                    da.created_at        AS created_at
                                FROM decision_audit da
                                WHERE {where_sql}
                                ORDER BY da.created_at ASC
                                LIMIT 50
                                """
                            ),
                            params,
                        )
                    ).mappings().all()
                for r in rows:
                    payload = {
                        "id": r["id"],
                        "entity_id": r["entity_id"],
                        "tool_name": r["tool_name"],
                        "action_label": label_for(r["tool_name"]),
                        "agent_id": r["model_version"] or "(unknown)",
                        "outcome": r["outcome"],
                        "created_at": r["created_at"].isoformat()
                        if isinstance(r["created_at"], dt.datetime)
                        else str(r["created_at"]),
                    }
                    yield (
                        b"event: activity\n"
                        + b"data: " + json.dumps(payload).encode("utf-8")
                        + b"\n\n"
                    )
                    last_ts = max(last_ts, r["created_at"])
                # Heartbeat every 15s of quiet time.
                if (now - heartbeat_at).total_seconds() >= 15:
                    yield b": heartbeat\n\n"
                    heartbeat_at = now
        except asyncio.CancelledError:
            return

    return StreamingResponse(
        _generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# POST /undo
# ---------------------------------------------------------------------------


class UndoRequest(BaseModel):
    note: str | None = None


async def _undo_queue_item(
    *,
    workspace: WorkspaceContext,
    queue_item_id: UUID,
    note: str | None,
) -> dict:
    """Undo path for agent-submitted tax_review_queue rows.

    Flips status='rejected', writes a decision_audit row with
    tool_name='undo' and request_payload.parent_audit_id='trq:<uuid>'.

    Returns the standard undo response shape.
    """
    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        # Look up the queue item so we can entity-scope-check it AND so
        # the audit row gets the right entity_id.
        row = (
            await session.execute(
                text(
                    """
                    SELECT id::text AS id, entity_id::text AS entity_id,
                           workspace_id::text AS workspace_id,
                           submitter_id, submitter_kind, status
                    FROM tax_review_queue
                    WHERE id = :qid
                    """
                ),
                {"qid": str(queue_item_id)},
            )
        ).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="Queue item not found")
        if row["submitter_kind"] != "agent":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "not_agent_submitted",
                    "message": (
                        "Only agent-submitted queue items can be undone via "
                        "the AI Activity endpoint. Human-submitted memos "
                        "should be resolved through the CPA review surface."
                    ),
                },
            )
        if row["status"] not in ("pending", "reviewed"):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "queue_item_already_decided",
                    "message": (
                        f"Queue item already {row['status']}; nothing to undo."
                    ),
                },
            )

        assert_entity_in_scope(workspace, UUID(row["entity_id"]))

        updated = (
            await session.execute(
                text(
                    """
                    UPDATE tax_review_queue
                    SET status = 'rejected',
                        reviewer_uc_uid = :uid,
                        resolution_notes = COALESCE(:note, resolution_notes),
                        resolved_at = now()
                    WHERE id = :qid
                      AND status IN ('pending', 'reviewed')
                    RETURNING id::text AS id, status
                    """
                ),
                {
                    "qid": str(queue_item_id),
                    "uid": workspace.user_uc_uid,
                    "note": (
                        f"Undone by {workspace.user_uc_uid}: "
                        f"{(note or '').strip()[:240]}"
                    ).strip(),
                },
            )
        ).mappings().first()
        if not updated:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "queue_item_already_decided",
                    "message": (
                        "The memo was decided between our read and write — "
                        "refresh and try again."
                    ),
                },
            )

        # Audit the undo. parent_audit_id is the synthetic timeline id so
        # the GET endpoint can match it back via the existing followup
        # join (it also LIKE-matches plain UUIDs because the join is
        # 'parent_audit_id = ANY(:ids)' — synthetic ids 'trq:<uuid>' won't
        # match plain uuid rows there, but the GET endpoint's trq path
        # already reads status from the queue row itself, so the undo
        # state is reflected without a parent_audit_id match).
        new_audit_id = uuid4()
        audit_req = {
            "parent_audit_id": f"trq:{queue_item_id}",
            "kind": "memo_status_reject",
            "note": note or "",
            "actor_uc_uid": workspace.user_uc_uid,
            "actor_role": "human",
            "queue_item_id": str(queue_item_id),
        }
        audit_resp = {
            "kind": "memo_status_reject",
            "queue_item_id": str(updated["id"]),
            "new_status": updated["status"],
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
                    'undo', CAST(:req AS jsonb), CAST(:resp AS jsonb),
                    :reasoning, NULL,
                    '', 'undone'
                )
                """
            ),
            {
                "id": str(new_audit_id),
                "wid": str(workspace.workspace_id),
                "eid": row["entity_id"],
                "req": json.dumps(audit_req),
                "resp": json.dumps(audit_resp),
                "reasoning": (
                    note
                    or f"Human undo of agent-submitted queue item {queue_item_id}"
                ),
            },
        )

    return {
        "ok": True,
        "audit_id": str(new_audit_id),
        "parent_audit_id": f"trq:{queue_item_id}",
        "kind": "memo_status_reject",
        "queue_item_id": str(queue_item_id),
        "new_status": "rejected",
    }


@router.post("/api/workspaces/{workspace_id}/ai-activity/{audit_id}/undo")
async def undo_ai_action(
    workspace_id: UUID,
    audit_id: str,
    body: UndoRequest,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """Reverse a reversible AI action.

    The audit row's tool_name must appear in ``_REVERSIBILITY`` with a
    non-None reverse_kind AND be inside its time window. Otherwise we
    return 409 with a hint to challenge instead.

    Mechanisms:

      * ``memo_status_reject`` — flip the tax_review_queue row identified
        in response.queue_item_id / memo_id to status='rejected'.

      * ``journal_post_contra`` — return 409 for now (a full contra-JE
        post requires the recategorization pipeline which lives outside
        this endpoint; we surface a clear "not yet implemented" message
        rather than silently no-op). The challenge path is always
        available as a fallback.

    Closed-period guard: if the entity has a closed_period whose
    period_end >= the original entry's effective_date, return 422.

    Always writes a NEW decision_audit row with tool_name='undo' and
    ``request_payload = {parent_audit_id, kind, note, actor_uc_uid}``.

    ``audit_id`` accepts either a raw UUID (decision_audit.id) OR the
    synthetic ``trq:<uuid>`` form the timeline emits for queue-only
    agent writes — in that case we look up the queue row directly.

    Role floor: accountant. (Undo is a write; mirrors the write surface
    that produced the original action.)
    """
    require_role(workspace, "accountant")

    # The timeline may hand us a synthetic id like 'trq:<uuid>' for
    # agent-submitted tax_review_queue rows that don't have a backing
    # decision_audit row.  Recognize the prefix and dispatch.
    audit_id_str = audit_id.strip()
    is_trq_synthetic = audit_id_str.startswith("trq:")
    if is_trq_synthetic:
        queue_item_str = audit_id_str.split(":", 1)[1]
        try:
            queue_item_uuid = UUID(queue_item_str)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid synthetic id")
        return await _undo_queue_item(
            workspace=workspace,
            queue_item_id=queue_item_uuid,
            note=body.note,
        )

    # Plain decision_audit id path.
    try:
        audit_uuid = UUID(audit_id_str)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=(
                "audit_id must be a UUID or 'trq:<uuid>' for queue-only items"
            ),
        )

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        # Load original audit row.
        row = (
            await session.execute(
                text(
                    """
                    SELECT
                        id::text                  AS id,
                        workspace_id::text        AS workspace_id,
                        entity_id::text           AS entity_id,
                        tool_name                 AS tool_name,
                        request_payload           AS request_payload,
                        response_payload          AS response_payload,
                        created_at                AS created_at
                    FROM decision_audit
                    WHERE id = :id
                    """
                ),
                {"id": str(audit_uuid)},
            )
        ).mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="Audit row not found")
        entity_uuid = UUID(row["entity_id"])
        assert_entity_in_scope(workspace, entity_uuid)

        tool_name = row["tool_name"]
        kind, window_h = reversibility_for(tool_name)
        if kind is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "not_reversible",
                    "message": (
                        f"Action '{tool_name}' is not reversible — use "
                        "POST /challenge to flag a disagreement."
                    ),
                },
            )

        created_at = row["created_at"]
        if isinstance(created_at, dt.datetime) and created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=dt.timezone.utc)
        if not is_reversible(tool_name, created_at):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "window_expired",
                    "message": (
                        f"The {window_h}-hour undo window for this action "
                        "has passed — use POST /challenge instead."
                    ),
                },
            )

        # Already undone? — refuse to double-undo. The list endpoint also
        # surfaces undone_at so the UI shouldn't even show the button.
        existing = (
            await session.execute(
                text(
                    """
                    SELECT 1 FROM decision_audit
                    WHERE tool_name = 'undo'
                      AND request_payload ->> 'parent_audit_id' = :pid
                    LIMIT 1
                    """
                ),
                {"pid": row["id"]},
            )
        ).first()
        if existing:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "already_undone",
                    "message": "This action was already undone.",
                },
            )

        req = _safe_json(row["request_payload"]) or {}
        resp = _safe_json(row["response_payload"]) or {}
        target = _target_from(tool_name, req, resp, row["entity_id"])

        # ---- Branch on the reversal mechanism --------------------------
        undo_response: dict[str, Any] = {"kind": kind}

        if kind == "memo_status_reject":
            queue_item_id = (
                target.get("queue_item_id")
                or target.get("memo_id")
                or target.get("id")
            )
            if not queue_item_id:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "no_target",
                        "message": (
                            "Original action did not record a queue_item_id; "
                            "cannot mechanically undo. Use challenge instead."
                        ),
                    },
                )
            updated = (
                await session.execute(
                    text(
                        """
                        UPDATE tax_review_queue
                        SET status = 'rejected',
                            reviewer_uc_uid = :uid,
                            resolution_notes = COALESCE(:note, resolution_notes),
                            resolved_at = now()
                        WHERE id = :qid
                          AND status IN ('pending', 'reviewed')
                        RETURNING id::text AS id, status
                        """
                    ),
                    {
                        "qid": str(queue_item_id),
                        "uid": workspace.user_uc_uid,
                        "note": (
                            f"Undone by {workspace.user_uc_uid}: "
                            f"{(body.note or '').strip()[:240]}"
                        ).strip(),
                    },
                )
            ).mappings().first()
            if updated is None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "queue_item_already_decided",
                        "message": (
                            "The memo was already reviewed/resolved/rejected "
                            "by someone — cannot undo. Open the queue item "
                            "to see the current state."
                        ),
                    },
                )
            undo_response["queue_item_id"] = str(updated["id"])
            undo_response["new_status"] = updated["status"]

        elif kind == "journal_post_contra":
            # Closed-period guard on the original entry's effective_date.
            # We don't try to find the original entry here (the JE id might
            # not be recorded in the response, and the recategorization
            # pipeline lives in app/journal/posting.py). Instead, we
            # short-circuit with a 409 + a guidance message so the UI
            # offers the challenge path.
            #
            # Future enhancement (P-XXXXX): when journal_entry_id is
            # captured in response_payload, call into the existing
            # `post_correction_journal_entries` to do a proper contra.
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "contra_not_supported_v1",
                    "message": (
                        "Ledger-level undo isn't wired in this release — "
                        "click 'Challenge this' to flag the disagreement "
                        "for CPA review."
                    ),
                },
            )
        elif kind == "categorization_pending":
            # Same story — we'd need a Catalog<->approval flip path.
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "categorization_undo_not_supported_v1",
                    "message": (
                        "Categorization undo isn't wired in this release — "
                        "click 'Challenge this' to flag the disagreement."
                    ),
                },
            )
        else:  # pragma: no cover
            raise HTTPException(
                status_code=500,
                detail=f"Unknown reverse kind: {kind!r}",
            )

        # ---- Append the audit row for the undo itself.
        # decision_audit is INSERT-only for the runtime role, so this row is
        # tamper-evident the same way the original is.
        audit_payload_req = {
            "parent_audit_id": row["id"],
            "kind": kind,
            "note": body.note or "",
            "actor_uc_uid": workspace.user_uc_uid,
            "actor_role": "human",  # the undo button is always human-initiated
        }
        new_audit_id = uuid4()
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
                    'undo', CAST(:req AS jsonb), CAST(:resp AS jsonb),
                    :reasoning, NULL,
                    '', 'undone'
                )
                """
            ),
            {
                "id": str(new_audit_id),
                "wid": str(workspace.workspace_id),
                "eid": row["entity_id"],
                "req": json.dumps(audit_payload_req),
                "resp": json.dumps(undo_response),
                "reasoning": (
                    body.note
                    or f"Human undo of AI action {row['id']} ({tool_name})"
                ),
            },
        )

    return {
        "ok": True,
        "audit_id": str(new_audit_id),
        "parent_audit_id": row["id"],
        "kind": kind,
        **undo_response,
    }


# ---------------------------------------------------------------------------
# POST /challenge
# ---------------------------------------------------------------------------


class ChallengeRequest(BaseModel):
    reason: str
    # Optional override; defaults to the original audit row's tax_year
    tax_year: int | None = None


@router.post("/api/workspaces/{workspace_id}/ai-activity/{audit_id}/challenge")
async def challenge_ai_action(
    workspace_id: UUID,
    audit_id: str,
    body: ChallengeRequest,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """File a tax_review_queue memo flagging disagreement with an AI output.

    Works for ANY audit row (reversible or not), AND for synthetic
    ``trq:<uuid>`` ids used for queue-only agent writes. Writes:

      * One tax_review_queue row with item_type='memo',
        content={topic: 'ai_output_challenged', text: <reason>, parent: ...}.
      * One decision_audit row with tool_name='challenge' linking back.

    Role floor: viewer. The CPA will see the challenge in their queue.
    """
    require_role(workspace, "viewer")

    reason = (body.reason or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="reason is required")

    audit_id_str = audit_id.strip()
    is_trq_synthetic = audit_id_str.startswith("trq:")

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        if is_trq_synthetic:
            queue_item_str = audit_id_str.split(":", 1)[1]
            try:
                queue_item_uuid = UUID(queue_item_str)
            except ValueError:
                raise HTTPException(
                    status_code=400, detail="Invalid synthetic id"
                )
            # Look up the queue row directly.
            q_row = (
                await session.execute(
                    text(
                        """
                        SELECT id::text AS id,
                               workspace_id::text AS workspace_id,
                               entity_id::text AS entity_id,
                               tax_year, item_type, form_reference,
                               content, submitter_id, created_at
                        FROM tax_review_queue
                        WHERE id = :qid
                        """
                    ),
                    {"qid": str(queue_item_uuid)},
                )
            ).mappings().first()
            if not q_row:
                raise HTTPException(
                    status_code=404, detail="Queue item not found"
                )
            row = {
                "id": audit_id_str,
                "workspace_id": q_row["workspace_id"],
                "entity_id": q_row["entity_id"],
                "tool_name": (
                    "draft_memo"
                    if q_row["item_type"] in ("memo", "question")
                    else "flag_for_cpa_review"
                ),
                "request_payload": {
                    "tax_year": q_row["tax_year"],
                    "form_reference": q_row["form_reference"],
                    "topic": (
                        (_safe_json(q_row["content"]) or {}).get("topic")
                    ),
                },
                "response_payload": {"queue_item_id": q_row["id"]},
                "model_version": q_row["submitter_id"],
                "created_at": q_row["created_at"],
            }
        else:
            try:
                audit_uuid = UUID(audit_id_str)
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "audit_id must be a UUID or 'trq:<uuid>' for "
                        "queue-only items"
                    ),
                )
            # Find original audit row.
            row = (
                await session.execute(
                    text(
                        """
                        SELECT id::text                  AS id,
                               workspace_id::text        AS workspace_id,
                               entity_id::text           AS entity_id,
                               tool_name                 AS tool_name,
                               request_payload           AS request_payload,
                               response_payload          AS response_payload,
                               model_version             AS model_version,
                               created_at                AS created_at
                        FROM decision_audit
                        WHERE id = :id
                        """
                    ),
                    {"id": str(audit_uuid)},
                )
            ).mappings().first()
            if not row:
                raise HTTPException(
                    status_code=404, detail="Audit row not found"
                )

        entity_uuid = UUID(row["entity_id"])
        assert_entity_in_scope(workspace, entity_uuid)

        req = (
            _safe_json(row["request_payload"]) or {}
            if not isinstance(row["request_payload"], dict)
            else row["request_payload"]
        )
        resp = (
            _safe_json(row["response_payload"]) or {}
            if not isinstance(row["response_payload"], dict)
            else row["response_payload"]
        )
        target = _target_from(row["tool_name"], req, resp, row["entity_id"])
        agent_id = row["model_version"] or "(unknown agent)"

        tax_year = body.tax_year or target.get("tax_year") or dt.date.today().year

        content_payload = {
            "topic": "ai_output_challenged",
            "text": reason,
            "source": {
                "kind": "ai_activity_challenge",
                "audit_id": row["id"],
                "agent_id": agent_id,
                "original_tool": row["tool_name"],
                "original_target": target,
                "original_created_at": (
                    row["created_at"].isoformat()
                    if isinstance(row["created_at"], dt.datetime)
                    else str(row["created_at"])
                ),
            },
        }

        memo_id_row = (
            await session.execute(
                text(
                    """
                    INSERT INTO tax_review_queue (
                        workspace_id, entity_id, tax_year,
                        item_type, form_reference,
                        content, confidence, citations,
                        status, submitter_kind, submitter_id
                    ) VALUES (
                        :wid, :eid, :ty,
                        'memo', :form_ref,
                        CAST(:content AS jsonb), 'medium',
                        CAST('[]' AS jsonb),
                        'pending', 'human', :uid
                    )
                    RETURNING id::text AS id
                    """
                ),
                {
                    "wid": str(workspace.workspace_id),
                    "eid": row["entity_id"],
                    "ty": int(tax_year),
                    "form_ref": f"CHALLENGE: {row['tool_name']} by {agent_id}",
                    "content": json.dumps(content_payload),
                    "uid": workspace.user_uc_uid,
                },
            )
        ).mappings().one()
        memo_id = memo_id_row["id"]

        # Audit the challenge action itself.
        new_audit_id = uuid4()
        audit_req = {
            "parent_audit_id": row["id"],
            "reason": reason,
            "tax_year": int(tax_year),
            "actor_uc_uid": workspace.user_uc_uid,
            "actor_role": "human",
        }
        audit_resp = {
            "memo_id": memo_id,
            "challenged_agent": agent_id,
            "challenged_tool": row["tool_name"],
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
                    'challenge', CAST(:req AS jsonb), CAST(:resp AS jsonb),
                    :reasoning, NULL,
                    '', 'challenged'
                )
                """
            ),
            {
                "id": str(new_audit_id),
                "wid": str(workspace.workspace_id),
                "eid": row["entity_id"],
                "req": json.dumps(audit_req),
                "resp": json.dumps(audit_resp),
                "reasoning": reason,
            },
        )

    return {
        "ok": True,
        "audit_id": str(new_audit_id),
        "parent_audit_id": row["id"],
        "memo_id": memo_id,
        "tax_year": int(tax_year),
    }
