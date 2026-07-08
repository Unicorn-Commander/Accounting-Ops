"""Read-only accuracy metrics: the AI-decision agreement rate (review-vs-redo).

  GET /api/workspaces/{wid}/metrics/ai-agreement
        ?entity_id=   (optional — entity-scoped; a scoped member is restricted
                       to their entities, an out-of-scope concrete entity -> 403)
        &days=N       (optional — window on approval_queue.decided_at; default
                       all-time. last-90d is ALWAYS computed alongside it)
        &category_top_n=  (optional — cap the category slice; default 10)

Role floor: ``viewer`` (read-only — this only reads two append-only tables and
aggregates; it changes nothing). Mirrors the auth + scoping of
``app/api/approval_recommendations.py``: resolve_workspace -> require_role ->
assert_entity_in_scope -> all SQL runs inside ``workspace_session`` so the
search_path + ``app.current_workspace`` RLS GUC are pinned (tenancy is never
bypassed). All SQL is parameterized.

The math itself lives in :mod:`app.metrics.ai_agreement` (a pure, DB-free
function). This module is just the fetch + window orchestration + the secondary
informational "learned corrections" tally from ``category_override``.

See :mod:`app.metrics.ai_agreement` for the exact metric definition and the
``flag_for_review`` exclusion rationale.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import text

from app.authz import assert_entity_in_scope
from app.db import SessionLocal
from app.metrics.ai_agreement import compute_ai_agreement
from app.tenancy import WorkspaceContext, require_role, resolve_workspace
from app.workspace_session import workspace_session

router = APIRouter(tags=["metrics"])

# Always report this trailing window alongside the requested one.
_TRAILING_WINDOW_DAYS = 90


async def _fetch_scored_rows(
    session,
    *,
    entity_id: UUID | None,
    entity_scope: list[UUID] | None,
    days: int | None,
) -> list[dict[str, Any]]:
    """Fetch one ``(recommended_decision, human_status, recommended_category)``
    row per decided approval that has an AI recommendation.

    Restricts to terminal human statuses (``approved`` / ``rejected``) at the SQL
    level so only ruled-on approvals cross into Python; the aggregator excludes
    ``flag_for_review`` from the rate (and counts it as a deferral). The INNER
    JOIN on the UNIQUE ``approval_recommendation.approval_id`` keeps this 1:1.

    When ``days`` is given, the window is applied to ``aq.decided_at`` (the
    moment the human ruled). Fully parameterized; entity-scope is enforced both
    by RLS (the workspace GUC) and explicitly here for the BOLA path.
    """
    where: list[str] = ["aq.status IN ('approved', 'rejected')"]
    params: dict[str, Any] = {}

    if entity_id is not None:
        where.append("ar.entity_id = :eid")
        params["eid"] = str(entity_id)

    if entity_scope is not None:
        where.append("ar.entity_id = ANY(:entity_scope)")
        params["entity_scope"] = [str(e) for e in entity_scope]

    if days is not None and days > 0:
        # decided_at within the last N days (inclusive). make_interval keeps the
        # bound parameterized rather than string-built.
        where.append(
            "aq.decided_at >= now() - make_interval(days => :days)"
        )
        params["days"] = days

    where_sql = " AND ".join(where)

    rows = (
        await session.execute(
            text(
                f"""
                SELECT
                    ar.recommended_decision AS recommended_decision,
                    aq.status               AS human_status,
                    ar.recommended_category AS recommended_category
                FROM approval_recommendation ar
                JOIN approval_queue aq ON aq.id = ar.approval_id
                WHERE {where_sql}
                """
            ),
            params,
        )
    ).mappings().all()
    return [dict(r) for r in rows]


async def _fetch_learned_corrections(
    session,
    *,
    entity_id: UUID | None,
    entity_scope: list[UUID] | None,
) -> dict[str, int]:
    """Secondary, INFORMATIONAL only — NOT part of the agreement rate.

    Counts user-trained ``category_override`` rows and sums their
    ``match_count`` (how often each correction has since auto-applied). This is a
    rough "learned corrections" signal: how much the human's past fixes are now
    doing work without re-asking. Entity_id on category_override is nullable
    (workspace-wide overrides), so an entity filter here also keeps NULL-entity
    overrides via ``entity_id IS NULL``.
    """
    where: list[str] = ["1=1"]
    params: dict[str, Any] = {}

    if entity_id is not None:
        where.append("(co.entity_id = :eid OR co.entity_id IS NULL)")
        params["eid"] = str(entity_id)
    elif entity_scope is not None:
        where.append("(co.entity_id = ANY(:entity_scope) OR co.entity_id IS NULL)")
        params["entity_scope"] = [str(e) for e in entity_scope]

    where_sql = " AND ".join(where)
    row = (
        await session.execute(
            text(
                f"""
                SELECT
                    count(*)::int                       AS override_count,
                    COALESCE(sum(co.match_count), 0)::int AS total_match_count
                FROM category_override co
                WHERE {where_sql}
                """
            ),
            params,
        )
    ).mappings().first()
    return {
        "override_count": int(row["override_count"]) if row else 0,
        "total_match_count": int(row["total_match_count"]) if row else 0,
    }


@router.get("/api/workspaces/{workspace_id}/metrics/ai-agreement")
async def ai_agreement_metric(
    workspace_id: UUID,
    entity_id: UUID | None = None,
    days: int | None = None,
    category_top_n: int = 10,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """AI-decision agreement rate (v1) — the review-vs-redo accuracy KPI.

    Compares each AI ``recommended_decision`` against the human's terminal
    ``approval_queue.status`` and reports how often they agreed. See
    :mod:`app.metrics.ai_agreement` for the precise definition; in short:
    denominator = decided (approved/rejected) approvals that have a
    recommendation and are NOT ``flag_for_review``; agreement = approve<->approved
    / reject<->rejected; flags are excluded and reported as deferrals.

    Always returns an ``all_time`` block and a ``last_90d`` block. If ``days`` is
    passed, a ``windowed`` block for that custom window is added too.

    Read floor is ``viewer`` (read-only). Entity-scope (BOLA): a scoped member is
    restricted to their entities; an out-of-scope concrete ``entity_id`` -> 403.
    """
    require_role(workspace, "viewer")
    scope_restriction = assert_entity_in_scope(workspace, entity_id)

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        # All-time (no window).
        all_rows = await _fetch_scored_rows(
            session,
            entity_id=entity_id,
            entity_scope=scope_restriction,
            days=None,
        )
        all_time = compute_ai_agreement(all_rows, category_top_n=category_top_n)

        # Trailing 90 days — always computed so the dashboard has a recent read.
        rows_90 = await _fetch_scored_rows(
            session,
            entity_id=entity_id,
            entity_scope=scope_restriction,
            days=_TRAILING_WINDOW_DAYS,
        )
        last_90d = compute_ai_agreement(rows_90, category_top_n=category_top_n)

        # Optional custom window.
        windowed: dict | None = None
        if days is not None and days > 0:
            rows_n = await _fetch_scored_rows(
                session,
                entity_id=entity_id,
                entity_scope=scope_restriction,
                days=days,
            )
            windowed = compute_ai_agreement(rows_n, category_top_n=category_top_n)

        # Secondary, informational only (NOT part of the rate).
        learned = await _fetch_learned_corrections(
            session,
            entity_id=entity_id,
            entity_scope=scope_restriction,
        )

    result: dict[str, Any] = {
        "workspace_id": str(workspace.workspace_id),
        "entity_id": str(entity_id) if entity_id else None,
        "metric": "ai_decision_agreement_rate",
        "metric_version": 1,
        "all_time": all_time,
        "last_90d": last_90d,
        "learned_corrections": learned,
    }
    if windowed is not None:
        result["windowed"] = {"days": days, **windowed}
    return result
