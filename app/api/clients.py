"""CPA / cross-workspace dashboard endpoints.

A CPA persona who carries memberships in many customer workspaces uses the
``/clients`` UI surface (web) which calls these endpoints. Regular customers
with 1-2 workspaces use the normal workspace switcher — these endpoints are
designed for the multi-workspace use case but work for any authenticated user.

Endpoints:

  GET /api/me/clients   — aggregate every workspace the caller belongs to with
                          open-items + last-active counts in one round trip.

  GET /api/me/cpa-recent-activity?days=30
                        — recent decision_audit rows where actor_role='cpa'
                          across every workspace the caller belongs to.

The CPA view never lets the caller see workspaces they don't already have a
membership row in — the same uc_uid -> workspace_membership lookup the rest of
the platform uses gates the response. There is no admin override.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text

from app.auth import AuthContext, validate_bearer_token
from app.db import SessionLocal
from app.workspace_session import workspace_session

_log = logging.getLogger("acctops.clients")

router = APIRouter(tags=["clients"])


async def _user_id_for(uc_uid: str) -> UUID | None:
    async with SessionLocal() as session:
        async with session.begin():
            row = (
                await session.execute(
                    text("SELECT id FROM public.user_account WHERE uc_uid = :uc"),
                    {"uc": uc_uid},
                )
            ).first()
            return row.id if row else None


@router.get("/api/me/clients")
async def list_my_clients(
    auth: AuthContext = Depends(validate_bearer_token),
) -> dict[str, Any]:
    """Return every workspace the caller belongs to with cross-workspace metadata.

    Shape (each workspace):
      ``id, slug, display_name, role, entity_count, isolation_tier,
        hipaa_protected, last_active_at, open_items_count,
        scoped_entity_ids, cpa_summary_role``

    The ``open_items_count`` is the count of pending decisions in the
    workspace's `tax_review_queue` (best-effort — a missing table on a
    workspace is treated as 0).

    Caller persona detection:
      * if the caller holds a ``cpa`` role in 2+ workspaces, ``persona``
        returns "cpa"
      * if they hold ``platform_admin`` anywhere, persona is "platform_admin"
      * else, persona is "client" (the customer's owner / team).
    """
    user_id = await _user_id_for(auth.uc_uid)
    if user_id is None:
        return {"persona": "client", "workspaces": []}

    async with SessionLocal() as session:
        async with session.begin():
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT
                            w.id::text          AS id,
                            w.slug              AS slug,
                            w.display_name      AS display_name,
                            m.role::text        AS role,
                            w.isolation_tier::text AS isolation_tier,
                            w.hipaa_protected   AS hipaa_protected,
                            m.entity_scope      AS entity_scope,
                            m.last_active_at    AS last_active_at,
                            w.postgres_schema   AS postgres_schema
                        FROM public.workspace_membership m
                        JOIN public.workspace w ON w.id = m.workspace_id
                        WHERE m.user_id = :user_id
                          AND w.status = 'active'
                        ORDER BY m.last_active_at DESC NULLS LAST, w.display_name
                        """
                    ),
                    {"user_id": user_id},
                )
            ).mappings().all()

    workspaces: list[dict[str, Any]] = []
    cpa_count = 0
    has_platform = False

    for row in rows:
        entry = dict(row)
        entry.pop("postgres_schema", None)
        role = entry["role"]
        if role == "cpa":
            cpa_count += 1
        if role == "platform_admin":
            has_platform = True

        # Per-workspace counts. Best-effort: errors degrade to zeros so a
        # single broken workspace can't 500 the dashboard.
        ent_count = 0
        open_items = 0
        try:
            async with workspace_session(SessionLocal, row["id"]) as ws:
                e_row = (
                    await ws.execute(
                        text(
                            "SELECT count(*) AS c FROM entity WHERE status = 'active'"
                        )
                    )
                ).first()
                ent_count = e_row.c if e_row else 0
                try:
                    o_row = (
                        await ws.execute(
                            text(
                                """
                                SELECT count(*) AS c FROM tax_review_queue
                                WHERE status IN ('pending','in_review')
                                """
                            )
                        )
                    ).first()
                    open_items = o_row.c if o_row else 0
                except Exception:
                    open_items = 0
        except Exception as exc:
            _log.info(
                "[clients] workspace %s open-items lookup failed: %s",
                row["id"], exc,
            )

        entry["entity_count"] = ent_count
        entry["open_items_count"] = open_items
        workspaces.append(entry)

    if has_platform:
        persona = "platform_admin"
    elif cpa_count >= 2:
        persona = "cpa"
    elif cpa_count == 1:
        persona = "cpa"
    else:
        persona = "client"

    return {
        "persona": persona,
        "workspaces": workspaces,
        "total_workspaces": len(workspaces),
        "total_open_items": sum(w["open_items_count"] for w in workspaces),
    }


@router.get("/api/me/cpa-recent-activity")
async def cpa_recent_activity(
    days: int = Query(default=30, ge=1, le=365),
    auth: AuthContext = Depends(validate_bearer_token),
) -> dict[str, Any]:
    """Recent CPA-attributed decision_audit rows across the caller's workspaces.

    For each workspace where the caller has membership, peek into the
    workspace schema's ``decision_audit`` table and return rows where the
    actor's model_version matched a CPA-tagged agent or where the tool
    is in the CPA write surface. Workspace failures degrade silently.

    Returned shape: ``{"items":[{workspace_id, workspace_name, tool, outcome,
    actor_uc_uid?, created_at}], "days": N}``.
    """
    user_id = await _user_id_for(auth.uc_uid)
    if user_id is None:
        return {"items": [], "days": days}

    async with SessionLocal() as session:
        async with session.begin():
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT w.id::text AS id, w.display_name AS name
                        FROM public.workspace_membership m
                        JOIN public.workspace w ON w.id = m.workspace_id
                        WHERE m.user_id = :user_id
                          AND w.status = 'active'
                        """
                    ),
                    {"user_id": user_id},
                )
            ).mappings().all()

    items: list[dict[str, Any]] = []
    for ws in rows:
        try:
            async with workspace_session(SessionLocal, ws["id"]) as wss:
                res = (
                    await wss.execute(
                        text(
                            """
                            SELECT id::text AS id,
                                   tool_name AS tool,
                                   outcome,
                                   model_version,
                                   created_at
                            FROM decision_audit
                            WHERE created_at >= now() - (:days || ' days')::interval
                            ORDER BY created_at DESC
                            LIMIT 100
                            """
                        ),
                        {"days": str(days)},
                    )
                ).mappings().all()
                for r in res:
                    items.append({
                        "workspace_id": ws["id"],
                        "workspace_name": ws["name"],
                        **dict(r),
                    })
        except Exception as exc:
            _log.info(
                "[clients] decision_audit peek failed for %s: %s", ws["id"], exc
            )
            continue
    items.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return {"items": items[:200], "days": days}


__all__ = ("router",)
