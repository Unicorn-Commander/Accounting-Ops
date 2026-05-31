"""Sentinel action primitives — graded by autonomy level + audit-trailed.

Every action goes through ``execute_action()`` which:

  1. Checks the kill switch (env SENTINEL_DISABLED=true).
  2. Verifies the workspace's autonomy_level permits the action_type.
  3. Honors the per-workspace daily rate limit (50 actions/day default).
  4. Writes a ``decision_audit`` row with ``actor_role='sentinel'`` AND parent
     reference to the check_run row that triggered it.
  5. Writes a ``sentinel_finding`` row for the activity feed.
  6. Performs the action.

Action types currently implemented:

  draft_memo            -> tax_review_queue insert (memo)
  flag_for_cpa_review   -> tax_review_queue insert (flagged_position)
  send_email            -> stub (logs the email; real mailer wiring deferred)
  schedule_reminder     -> sentinel_scheduled_action insert
  create_open_item      -> stub (open-items endpoint to be wired)
  send_invitation       -> stub (invitation API to be wired)
  run_specialist        -> stub (queues a specialist run for the coordinator)
  create_project_ops_task -> stub (Brigade/Project-Ops MCP integration)

Closed-period guard:
  Sentinel actions that target a specific period (memo on a closed quarter,
  draft on a year that's been signed off) must respect closed_period rows for
  the entity. ``_check_closed_period()`` raises ``ClosedPeriodError`` which
  ``execute_action()`` catches and downgrades to a "skipped" finding.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from agents.sentinel.config import AutonomyLevel, SentinelConfig, at_least

_log = logging.getLogger("acctops.sentinel.actions")


# ---------------------------------------------------------------------------
# Action vocabulary + autonomy-level gating
# ---------------------------------------------------------------------------
#
# A workspace at level L can take any action listed at ranks <= L. This is
# enforced inside execute_action — the agent's reasoning may *try* to take a
# higher-rank action, but the gate refuses.

ACTION_INFO = {
    # watch_only and above
    "log_finding":             {"min_level": AutonomyLevel.WATCH_ONLY},

    # draft_and_flag and above
    "draft_memo":              {"min_level": AutonomyLevel.DRAFT_AND_FLAG},
    "flag_for_cpa_review":     {"min_level": AutonomyLevel.DRAFT_AND_FLAG},
    "send_email":              {"min_level": AutonomyLevel.DRAFT_AND_FLAG},
    "send_invitation":         {"min_level": AutonomyLevel.DRAFT_AND_FLAG},
    "schedule_reminder":       {"min_level": AutonomyLevel.DRAFT_AND_FLAG},
    "create_open_item":        {"min_level": AutonomyLevel.DRAFT_AND_FLAG},
    "run_specialist":          {"min_level": AutonomyLevel.DRAFT_AND_FLAG},
    "create_project_ops_task": {"min_level": AutonomyLevel.DRAFT_AND_FLAG},

    # fully_autonomous only
    "bulk_decide_approvals":   {"min_level": AutonomyLevel.FULLY_AUTONOMOUS},
    "initiate_connector_sync": {"min_level": AutonomyLevel.FULLY_AUTONOMOUS},
    "provision_entity":        {"min_level": AutonomyLevel.FULLY_AUTONOMOUS},
}


class SentinelDisabledError(RuntimeError):
    """Raised when SENTINEL_DISABLED=true or the workspace autonomy is 'off'."""


class AutonomyLevelError(PermissionError):
    """Action denied because the workspace's autonomy level is too low."""


class RateLimitedError(RuntimeError):
    """Action denied because the workspace hit the daily action cap."""


class ClosedPeriodError(RuntimeError):
    """Action skipped because it would touch a closed period."""


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------


def kill_switch_active() -> bool:
    """True if the env-var kill switch is set."""
    val = (os.getenv("SENTINEL_DISABLED") or "").strip().lower()
    return val in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


async def _count_actions_today(
    session: AsyncSession, workspace_id: UUID
) -> int:
    """Count decision_audit rows tagged actor_role='sentinel' for today UTC.

    Today is computed as the current UTC date; the count includes any audit row
    whose request_payload->>'sentinel_action' is non-null OR whose model_version
    starts with 'sentinel/'. Sentinel always sets both for traceability.
    """
    row = (
        await session.execute(
            text(
                """
                SELECT count(*)::int AS c
                FROM decision_audit
                WHERE workspace_id = :wid
                  AND created_at >= date_trunc('day', now() AT TIME ZONE 'UTC')
                  AND (
                    model_version LIKE 'sentinel/%'
                    OR (request_payload ? 'sentinel_action')
                  )
                """
            ),
            {"wid": str(workspace_id)},
        )
    ).first()
    return int(row.c if row else 0)


# ---------------------------------------------------------------------------
# Closed-period guard
# ---------------------------------------------------------------------------


async def _check_closed_period(
    session: AsyncSession,
    *,
    entity_id: UUID | None,
    target_date: dt.date | None,
) -> None:
    """Raise ClosedPeriodError if (entity, date) lies inside a closed period."""
    if entity_id is None or target_date is None:
        return
    row = (
        await session.execute(
            text(
                """
                SELECT period_end
                FROM closed_period
                WHERE entity_id = :eid
                  AND period_end >= :d
                ORDER BY period_end ASC
                LIMIT 1
                """
            ),
            {"eid": str(entity_id), "d": target_date},
        )
    ).first()
    if row is not None:
        raise ClosedPeriodError(
            f"Period closed at {row[0]}; cannot touch entries on or before that date."
        )


# ---------------------------------------------------------------------------
# Audit + finding writers
# ---------------------------------------------------------------------------


SENTINEL_AGENT_ID = "sentinel/v1"


async def _write_audit(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    entity_id: UUID,
    action_type: str,
    request_payload: dict[str, Any],
    response_payload: dict[str, Any] | None,
    reasoning: str,
    outcome: str,
    parent_check_run_id: UUID | None,
) -> UUID:
    """Append a decision_audit row keyed to Sentinel.

    Marks the audit row with model_version='sentinel/v1' and embeds the parent
    check_run_id under request_payload['sentinel_check_run_id'] so the trail can
    be reconstructed top-down.
    """
    audit_id = uuid4()
    req = dict(request_payload)
    req["sentinel_action"] = action_type
    req["sentinel_check_run_id"] = str(parent_check_run_id) if parent_check_run_id else None
    await session.execute(
        text(
            """
            INSERT INTO decision_audit (
                id, workspace_id, entity_id, agent_session_id,
                tool_name, request_payload, response_payload,
                reasoning_trace, model_version, outcome
            ) VALUES (
                :id, :wid, :eid, NULL,
                :tool, CAST(:req AS jsonb), CAST(:res AS jsonb),
                :reasoning, :model_version, :outcome
            )
            """
        ),
        {
            "id": str(audit_id),
            "wid": str(workspace_id),
            "eid": str(entity_id),
            "tool": f"sentinel.{action_type}",
            "req": json.dumps(req),
            "res": json.dumps(response_payload) if response_payload is not None else None,
            "reasoning": reasoning or "",
            "model_version": SENTINEL_AGENT_ID,
            "outcome": outcome,
        },
    )
    return audit_id


async def _write_finding(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    entity_id: UUID | None,
    check_type: str,
    severity: str,
    title: str,
    summary: str,
    details: dict[str, Any] | None,
    action_type: str | None,
    action_ref_id: UUID | None,
    check_run_id: UUID | None,
    decision_audit_id: UUID | None,
) -> UUID:
    finding_id = uuid4()
    await session.execute(
        text(
            """
            INSERT INTO sentinel_finding (
                id, workspace_id, entity_id, check_type, severity,
                title, summary, details, action_type, action_ref_id,
                check_run_id, decision_audit_id, status
            ) VALUES (
                :id, :wid, :eid, :check_type, :severity,
                :title, :summary, CAST(:details AS jsonb),
                :action_type, :action_ref_id,
                :check_run_id, :decision_audit_id, 'open'
            )
            """
        ),
        {
            "id": str(finding_id),
            "wid": str(workspace_id),
            "eid": str(entity_id) if entity_id else None,
            "check_type": check_type,
            "severity": severity,
            "title": title[:200],
            "summary": summary,
            "details": json.dumps(details) if details is not None else None,
            "action_type": action_type,
            "action_ref_id": str(action_ref_id) if action_ref_id else None,
            "check_run_id": str(check_run_id) if check_run_id else None,
            "decision_audit_id": str(decision_audit_id) if decision_audit_id else None,
        },
    )
    return finding_id


# ---------------------------------------------------------------------------
# Action context — passed into every action
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionContext:
    session: AsyncSession
    config: SentinelConfig
    check_run_id: UUID
    check_type: str
    workspace_id: UUID


# ---------------------------------------------------------------------------
# Master executor
# ---------------------------------------------------------------------------


async def execute_action(
    ctx: ActionContext,
    *,
    action_type: str,
    entity_id: UUID | None,
    title: str,
    summary: str,
    severity: str = "info",
    details: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    reasoning: str = "",
    target_date: dt.date | None = None,
) -> dict[str, Any]:
    """Run one Sentinel action through the safety gates.

    Returns {ok, action_type, finding_id, audit_id?, ref_id?, skipped_reason?}.
    Never raises (logs + returns ok=False on failure).
    """
    if kill_switch_active():
        return {"ok": False, "skipped_reason": "kill_switch"}

    if ctx.config.autonomy_level == AutonomyLevel.OFF:
        return {"ok": False, "skipped_reason": "autonomy_off"}

    info = ACTION_INFO.get(action_type)
    if info is None:
        return {"ok": False, "skipped_reason": f"unknown_action:{action_type}"}

    if not at_least(ctx.config.autonomy_level, info["min_level"]):
        return {
            "ok": False,
            "skipped_reason": f"autonomy_too_low:need_{info['min_level'].value}",
        }

    # log_finding is always permitted (watch_only and up); skip the rate limit
    # for pure observation rows (they aren't 'actions' in the user-visible sense).
    if action_type != "log_finding":
        used = await _count_actions_today(ctx.session, ctx.workspace_id)
        if used >= ctx.config.max_actions_per_day:
            _log.warning(
                "Sentinel rate-limited for workspace %s (used %d/%d today)",
                ctx.workspace_id, used, ctx.config.max_actions_per_day,
            )
            return {"ok": False, "skipped_reason": "rate_limited"}

    # Closed-period guard
    try:
        await _check_closed_period(ctx.session, entity_id=entity_id, target_date=target_date)
    except ClosedPeriodError as e:
        return {"ok": False, "skipped_reason": f"closed_period:{e}"}

    # Dispatch to the action handler.
    handler = _ACTION_DISPATCH.get(action_type)
    if handler is None:
        # log_finding only — pure finding row with no side effect.
        return await _do_log_finding(
            ctx,
            entity_id=entity_id,
            title=title,
            summary=summary,
            severity=severity,
            details=details,
            reasoning=reasoning,
        )

    try:
        return await handler(
            ctx,
            entity_id=entity_id,
            title=title,
            summary=summary,
            severity=severity,
            details=details,
            payload=payload or {},
            reasoning=reasoning,
            target_date=target_date,
        )
    except Exception as e:  # noqa: BLE001 — never let an action crash a cycle
        _log.exception("Sentinel action %s failed: %s", action_type, e)
        # Still write a finding so the failure is visible.
        finding_id = await _write_finding(
            session=ctx.session,
            workspace_id=ctx.workspace_id,
            entity_id=entity_id,
            check_type=ctx.check_type,
            severity="medium",
            title=f"Sentinel action failed: {action_type}",
            summary=f"{type(e).__name__}: {e}"[:1000],
            details={"action_type": action_type, "error": str(e)[:1000]},
            action_type=action_type,
            action_ref_id=None,
            check_run_id=ctx.check_run_id,
            decision_audit_id=None,
        )
        return {
            "ok": False,
            "skipped_reason": "exception",
            "finding_id": str(finding_id),
            "action_type": action_type,
            "error": str(e)[:500],
        }


# ---------------------------------------------------------------------------
# Action handlers — each returns the same shape
# ---------------------------------------------------------------------------


async def _do_log_finding(
    ctx: ActionContext,
    *,
    entity_id: UUID | None,
    title: str,
    summary: str,
    severity: str,
    details: dict[str, Any] | None,
    reasoning: str = "",
    **_kw,
) -> dict[str, Any]:
    finding_id = await _write_finding(
        session=ctx.session,
        workspace_id=ctx.workspace_id,
        entity_id=entity_id,
        check_type=ctx.check_type,
        severity=severity,
        title=title,
        summary=summary,
        details=details,
        action_type="log_finding",
        action_ref_id=None,
        check_run_id=ctx.check_run_id,
        decision_audit_id=None,
    )
    return {
        "ok": True,
        "action_type": "log_finding",
        "finding_id": str(finding_id),
    }


async def _do_draft_memo(
    ctx: ActionContext,
    *,
    entity_id: UUID | None,
    title: str,
    summary: str,
    severity: str,
    details: dict[str, Any] | None,
    payload: dict[str, Any],
    reasoning: str = "",
    target_date: dt.date | None = None,
) -> dict[str, Any]:
    if entity_id is None:
        return {"ok": False, "skipped_reason": "entity_required_for_memo"}

    tax_year = int(payload.get("tax_year") or (target_date.year if target_date else dt.date.today().year))
    topic = payload.get("topic") or title
    confidence = (payload.get("confidence") or "medium").strip().lower()
    citations = payload.get("citations") or []

    memo_content = {"topic": topic, "text": summary}
    memo_id = uuid4()
    await ctx.session.execute(
        text(
            """
            INSERT INTO tax_review_queue (
                id, workspace_id, entity_id, tax_year,
                item_type, form_reference,
                content, confidence, citations,
                status, submitter_kind, submitter_id
            ) VALUES (
                :id, :wid, :eid, :ty,
                'memo', NULL,
                CAST(:content AS jsonb), :conf,
                CAST(:cites AS jsonb),
                'pending', 'agent', :sid
            )
            """
        ),
        {
            "id": str(memo_id),
            "wid": str(ctx.workspace_id),
            "eid": str(entity_id),
            "ty": tax_year,
            "content": json.dumps(memo_content),
            "conf": confidence if confidence in ("low", "medium", "high") else "medium",
            "cites": json.dumps(citations),
            "sid": SENTINEL_AGENT_ID,
        },
    )

    audit_id = await _write_audit(
        session=ctx.session,
        workspace_id=ctx.workspace_id,
        entity_id=entity_id,
        action_type="draft_memo",
        request_payload={"tax_year": tax_year, "topic": topic, "memo_id": str(memo_id)},
        response_payload={"memo_id": str(memo_id)},
        reasoning=reasoning or f"Sentinel check {ctx.check_type} drafted a memo.",
        outcome="ok",
        parent_check_run_id=ctx.check_run_id,
    )

    finding_id = await _write_finding(
        session=ctx.session,
        workspace_id=ctx.workspace_id,
        entity_id=entity_id,
        check_type=ctx.check_type,
        severity=severity,
        title=title,
        summary=summary,
        details=details,
        action_type="draft_memo",
        action_ref_id=memo_id,
        check_run_id=ctx.check_run_id,
        decision_audit_id=audit_id,
    )
    return {
        "ok": True,
        "action_type": "draft_memo",
        "finding_id": str(finding_id),
        "audit_id": str(audit_id),
        "ref_id": str(memo_id),
    }


async def _do_flag_for_cpa_review(
    ctx: ActionContext,
    *,
    entity_id: UUID | None,
    title: str,
    summary: str,
    severity: str,
    details: dict[str, Any] | None,
    payload: dict[str, Any],
    reasoning: str = "",
    target_date: dt.date | None = None,
) -> dict[str, Any]:
    if entity_id is None:
        return {"ok": False, "skipped_reason": "entity_required_for_flag"}

    tax_year = int(payload.get("tax_year") or (target_date.year if target_date else dt.date.today().year))
    affected_line = payload.get("affected_line")
    recommendation = payload.get("recommendation") or "See summary."
    confidence = (payload.get("confidence") or "medium").strip().lower()
    citations = payload.get("citations") or []

    flag_content = {
        "issue": title,
        "affected_line": affected_line,
        "context": summary,
        "recommendation": recommendation,
    }
    flag_id = uuid4()
    await ctx.session.execute(
        text(
            """
            INSERT INTO tax_review_queue (
                id, workspace_id, entity_id, tax_year,
                item_type, form_reference,
                content, confidence, citations,
                status, submitter_kind, submitter_id
            ) VALUES (
                :id, :wid, :eid, :ty,
                'flagged_position', :form_ref,
                CAST(:content AS jsonb), :conf,
                CAST(:cites AS jsonb),
                'pending', 'agent', :sid
            )
            """
        ),
        {
            "id": str(flag_id),
            "wid": str(ctx.workspace_id),
            "eid": str(entity_id),
            "ty": tax_year,
            "form_ref": payload.get("form_reference"),
            "content": json.dumps(flag_content),
            "conf": confidence if confidence in ("low", "medium", "high") else "medium",
            "cites": json.dumps(citations),
            "sid": SENTINEL_AGENT_ID,
        },
    )

    audit_id = await _write_audit(
        session=ctx.session,
        workspace_id=ctx.workspace_id,
        entity_id=entity_id,
        action_type="flag_for_cpa_review",
        request_payload={"tax_year": tax_year, "flag_id": str(flag_id)},
        response_payload={"flag_id": str(flag_id)},
        reasoning=reasoning or f"Sentinel check {ctx.check_type} flagged a position.",
        outcome="ok",
        parent_check_run_id=ctx.check_run_id,
    )

    finding_id = await _write_finding(
        session=ctx.session,
        workspace_id=ctx.workspace_id,
        entity_id=entity_id,
        check_type=ctx.check_type,
        severity=severity,
        title=title,
        summary=summary,
        details=details,
        action_type="flag_for_cpa_review",
        action_ref_id=flag_id,
        check_run_id=ctx.check_run_id,
        decision_audit_id=audit_id,
    )
    return {
        "ok": True,
        "action_type": "flag_for_cpa_review",
        "finding_id": str(finding_id),
        "audit_id": str(audit_id),
        "ref_id": str(flag_id),
    }


async def _do_schedule_reminder(
    ctx: ActionContext,
    *,
    entity_id: UUID | None,
    title: str,
    summary: str,
    severity: str,
    details: dict[str, Any] | None,
    payload: dict[str, Any],
    reasoning: str = "",
    target_date: dt.date | None = None,
) -> dict[str, Any]:
    """Insert a sentinel_scheduled_action row for a future tick to pick up."""
    scheduled_for = payload.get("scheduled_for")
    if scheduled_for is None and target_date is not None:
        scheduled_for = dt.datetime.combine(target_date, dt.time(13, 0, tzinfo=dt.timezone.utc))
    if scheduled_for is None:
        return {"ok": False, "skipped_reason": "scheduled_for_required"}
    if isinstance(scheduled_for, str):
        scheduled_for = dt.datetime.fromisoformat(scheduled_for)

    action_id = uuid4()
    await ctx.session.execute(
        text(
            """
            INSERT INTO sentinel_scheduled_action (
                id, workspace_id, entity_id, action_type, scheduled_for,
                payload, status, created_by_check_run_id
            ) VALUES (
                :id, :wid, :eid, :atype, :sched,
                CAST(:payload AS jsonb), 'pending', :crid
            )
            """
        ),
        {
            "id": str(action_id),
            "wid": str(ctx.workspace_id),
            "eid": str(entity_id) if entity_id else None,
            "atype": payload.get("action_type", "send_email"),
            "sched": scheduled_for,
            "payload": json.dumps(payload),
            "crid": str(ctx.check_run_id),
        },
    )

    audit_id = await _write_audit(
        session=ctx.session,
        workspace_id=ctx.workspace_id,
        entity_id=entity_id or ctx.workspace_id,  # decision_audit requires non-null
        action_type="schedule_reminder",
        request_payload={"scheduled_action_id": str(action_id), "scheduled_for": scheduled_for.isoformat()},
        response_payload={"scheduled_action_id": str(action_id)},
        reasoning=reasoning or "Sentinel scheduled a future reminder.",
        outcome="ok",
        parent_check_run_id=ctx.check_run_id,
    )

    finding_id = await _write_finding(
        session=ctx.session,
        workspace_id=ctx.workspace_id,
        entity_id=entity_id,
        check_type=ctx.check_type,
        severity=severity,
        title=title,
        summary=summary,
        details=details,
        action_type="schedule_reminder",
        action_ref_id=action_id,
        check_run_id=ctx.check_run_id,
        decision_audit_id=audit_id,
    )
    return {
        "ok": True,
        "action_type": "schedule_reminder",
        "finding_id": str(finding_id),
        "audit_id": str(audit_id),
        "ref_id": str(action_id),
    }


async def _do_send_email(
    ctx: ActionContext,
    *,
    entity_id: UUID | None,
    title: str,
    summary: str,
    severity: str,
    details: dict[str, Any] | None,
    payload: dict[str, Any],
    reasoning: str = "",
    target_date: dt.date | None = None,
) -> dict[str, Any]:
    """Log the email intent; real Stalwart wiring is a follow-up.

    Future: call out to kvm2-mail's REST relay (or directly to Postmark). For
    now we audit + finding so the action surface exists and the daily cap +
    autonomy gating works end-to-end.
    """
    recipients = payload.get("recipients") or []
    subject = payload.get("subject") or title
    body = payload.get("body") or summary
    _log.info(
        "Sentinel email (workspace=%s, recipients=%s, subject=%s)",
        ctx.workspace_id, recipients, subject,
    )
    audit_id = await _write_audit(
        session=ctx.session,
        workspace_id=ctx.workspace_id,
        entity_id=entity_id or ctx.workspace_id,
        action_type="send_email",
        request_payload={"recipients": recipients, "subject": subject},
        response_payload={"delivered": False, "stub": True, "body_chars": len(body)},
        reasoning=reasoning or "Sentinel queued an email (stub).",
        outcome="ok_stub",
        parent_check_run_id=ctx.check_run_id,
    )
    finding_id = await _write_finding(
        session=ctx.session,
        workspace_id=ctx.workspace_id,
        entity_id=entity_id,
        check_type=ctx.check_type,
        severity=severity,
        title=title,
        summary=summary,
        details={**(details or {}), "recipients": recipients, "subject": subject},
        action_type="send_email",
        action_ref_id=None,
        check_run_id=ctx.check_run_id,
        decision_audit_id=audit_id,
    )
    return {
        "ok": True,
        "action_type": "send_email",
        "finding_id": str(finding_id),
        "audit_id": str(audit_id),
    }


async def _do_send_invitation(
    ctx: ActionContext,
    *,
    entity_id: UUID | None,
    title: str,
    summary: str,
    severity: str,
    details: dict[str, Any] | None,
    payload: dict[str, Any],
    reasoning: str = "",
    target_date: dt.date | None = None,
) -> dict[str, Any]:
    """Stub: would call the invitations endpoint to invite a user/CPA."""
    audit_id = await _write_audit(
        session=ctx.session,
        workspace_id=ctx.workspace_id,
        entity_id=entity_id or ctx.workspace_id,
        action_type="send_invitation",
        request_payload=payload,
        response_payload={"stub": True},
        reasoning=reasoning,
        outcome="ok_stub",
        parent_check_run_id=ctx.check_run_id,
    )
    finding_id = await _write_finding(
        session=ctx.session,
        workspace_id=ctx.workspace_id,
        entity_id=entity_id,
        check_type=ctx.check_type,
        severity=severity,
        title=title,
        summary=summary,
        details=details,
        action_type="send_invitation",
        action_ref_id=None,
        check_run_id=ctx.check_run_id,
        decision_audit_id=audit_id,
    )
    return {"ok": True, "action_type": "send_invitation",
            "finding_id": str(finding_id), "audit_id": str(audit_id)}


async def _do_create_open_item(
    ctx: ActionContext,
    *,
    entity_id: UUID | None,
    title: str,
    summary: str,
    severity: str,
    details: dict[str, Any] | None,
    payload: dict[str, Any],
    reasoning: str = "",
    target_date: dt.date | None = None,
) -> dict[str, Any]:
    """Currently piggybacks on sentinel_finding (the UI open-items section).

    Future: POST /api/.../open-items/sentinel that surfaces in the dashboard.
    """
    return await _do_log_finding(
        ctx,
        entity_id=entity_id,
        title=title,
        summary=summary,
        severity=severity,
        details={**(details or {}), "open_item": True, "payload": payload},
        reasoning=reasoning,
    )


async def _do_run_specialist(
    ctx: ActionContext,
    *,
    entity_id: UUID | None,
    title: str,
    summary: str,
    severity: str,
    details: dict[str, Any] | None,
    payload: dict[str, Any],
    reasoning: str = "",
    target_date: dt.date | None = None,
) -> dict[str, Any]:
    """Stub: enqueues a specialist run. For now logs + audits."""
    specialist = payload.get("specialist") or "tax-coordinator"
    tax_year = payload.get("tax_year")
    audit_id = await _write_audit(
        session=ctx.session,
        workspace_id=ctx.workspace_id,
        entity_id=entity_id or ctx.workspace_id,
        action_type="run_specialist",
        request_payload={"specialist": specialist, "tax_year": tax_year, **payload},
        response_payload={"stub": True, "specialist": specialist},
        reasoning=reasoning,
        outcome="ok_stub",
        parent_check_run_id=ctx.check_run_id,
    )
    finding_id = await _write_finding(
        session=ctx.session,
        workspace_id=ctx.workspace_id,
        entity_id=entity_id,
        check_type=ctx.check_type,
        severity=severity,
        title=title,
        summary=summary,
        details={**(details or {}), "specialist": specialist, "tax_year": tax_year},
        action_type="run_specialist",
        action_ref_id=None,
        check_run_id=ctx.check_run_id,
        decision_audit_id=audit_id,
    )
    return {"ok": True, "action_type": "run_specialist",
            "finding_id": str(finding_id), "audit_id": str(audit_id)}


async def _do_create_project_ops_task(
    ctx: ActionContext,
    *,
    entity_id: UUID | None,
    title: str,
    summary: str,
    severity: str,
    details: dict[str, Any] | None,
    payload: dict[str, Any],
    reasoning: str = "",
    target_date: dt.date | None = None,
) -> dict[str, Any]:
    """Stub: would call Project-Ops MCP via Brigade. Logs intent + audit."""
    audit_id = await _write_audit(
        session=ctx.session,
        workspace_id=ctx.workspace_id,
        entity_id=entity_id or ctx.workspace_id,
        action_type="create_project_ops_task",
        request_payload=payload,
        response_payload={"stub": True},
        reasoning=reasoning,
        outcome="ok_stub",
        parent_check_run_id=ctx.check_run_id,
    )
    finding_id = await _write_finding(
        session=ctx.session,
        workspace_id=ctx.workspace_id,
        entity_id=entity_id,
        check_type=ctx.check_type,
        severity=severity,
        title=title,
        summary=summary,
        details=details,
        action_type="create_project_ops_task",
        action_ref_id=None,
        check_run_id=ctx.check_run_id,
        decision_audit_id=audit_id,
    )
    return {"ok": True, "action_type": "create_project_ops_task",
            "finding_id": str(finding_id), "audit_id": str(audit_id)}


_ACTION_DISPATCH = {
    "draft_memo":              _do_draft_memo,
    "flag_for_cpa_review":     _do_flag_for_cpa_review,
    "schedule_reminder":       _do_schedule_reminder,
    "send_email":              _do_send_email,
    "send_invitation":         _do_send_invitation,
    "create_open_item":        _do_create_open_item,
    "run_specialist":          _do_run_specialist,
    "create_project_ops_task": _do_create_project_ops_task,
    # log_finding is handled inline above
}


__all__ = [
    "ACTION_INFO",
    "ActionContext",
    "AutonomyLevelError",
    "ClosedPeriodError",
    "RateLimitedError",
    "SENTINEL_AGENT_ID",
    "SentinelDisabledError",
    "execute_action",
    "kill_switch_active",
]
