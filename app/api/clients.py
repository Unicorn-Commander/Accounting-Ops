"""CPA / cross-workspace dashboard endpoints.

A CPA persona who carries memberships in many customer workspaces uses the
``/clients`` UI surface (web) which calls these endpoints. Regular customers
with 1-2 workspaces use the normal workspace switcher — these endpoints are
designed for the multi-workspace use case but work for any authenticated user.

Endpoints:

  GET /api/me/clients   — aggregate every workspace the caller belongs to with
                          open-items + last-active counts in one round trip.

  GET /api/me/clients/triage
                        — same workspace list, enriched with CPA-triage signals
                          (needs_cpa, waiting_on_client, upcoming_deadline,
                          last_client_activity_at, suggested_route). Drives the
                          /clients triage cards. Backward-compat: the original
                          /api/me/clients is still served as-is.

  GET /api/me/cpa-recent-activity?days=30
                        — recent decision_audit rows where actor_role='cpa'
                          across every workspace the caller belongs to. Now
                          also returns ``deep_link_path`` per item so the UI
                          can deep-link into the SPECIFIC workspace + page
                          after switching (not just /dashboard).

The CPA view never lets the caller see workspaces they don't already have a
membership row in — the same uc_uid -> workspace_membership lookup the rest of
the platform uses gates the response. There is no admin override.
"""
from __future__ import annotations

import json
import logging
import secrets
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
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

    Caller persona detection (same rule as GET /api/me/persona, ``_persona_for``):
      * ``platform_admin`` anywhere -> "platform_admin"
      * a ``cpa`` seat anywhere, OR at least one active workspace the caller
        onboarded through POST /api/me/clients (where they sit as ``owner``)
        -> "cpa"
      * else "client" (the customer's owner / team).

    ``onboarded_by_me`` marks the workspaces the caller set up from the console.
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
                            COALESCE(w.onboarded_by_user_id = :user_id, false)
                                                AS onboarded_by_me,
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
            clients_onboarded = await _clients_onboarded_count(session, user_id)

    workspaces: list[dict[str, Any]] = []

    for row in rows:
        entry = dict(row)
        entry.pop("postgres_schema", None)

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

    persona = _persona_for(
        (r["role"] for r in rows), clients_onboarded, empty="client"
    )

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
                    enriched = {
                        "workspace_id": ws["id"],
                        "workspace_name": ws["name"],
                        **dict(r),
                    }
                    # Deep-link mapping: route the activity row to the most
                    # specific page in the target workspace based on the
                    # tool that produced it. The web layer cookie-swaps the
                    # workspace, then router.push(deep_link_path).
                    enriched["deep_link_path"] = _deep_link_for_tool(
                        enriched.get("tool")
                    )
                    items.append(enriched)
        except Exception as exc:
            _log.info(
                "[clients] decision_audit peek failed for %s: %s", ws["id"], exc
            )
            continue
    items.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return {"items": items[:200], "days": days}


# ---------------------------------------------------------------------------
# Deep-link map: tool_name -> target page after workspace switch.
# The audit row carries only the tool name; the UI knows where each tool
# "belongs" so users land at the right surface rather than always /dashboard.
# ---------------------------------------------------------------------------

_TOOL_TO_PATH: dict[str, str] = {
    # Approvals surface
    "approval_decision": "/approvals",
    "approve_approval": "/approvals",
    "reject_approval": "/approvals",
    "skip_approval": "/approvals",
    "apply_held_recategorization": "/approvals",
    # Tax surfaces
    "draft_memo": "/taxes",
    "flag_for_cpa_review": "/taxes",
    "resolve_tax_review_item": "/taxes",
    "get_form_1120_data": "/taxes",
    # Journal / reports
    "post_journal_entry": "/journal",
    "post_correction": "/journal",
    "verify_chain": "/journal",
    "get_p_and_l": "/reports",
    "get_balance_sheet": "/reports",
    "get_trial_balance": "/reports/trial-balance",
    # COA / entity
    "create_entity_with_coa": "/coa",
    "create_coa_account": "/coa",
    # Setup / connectors
    "suggest_connectors": "/connectors",
    "mark_intake_complete": "/dashboard",
}


def _deep_link_for_tool(tool: str | None) -> str:
    """Map an audit row tool name to a target page path. Default: /dashboard."""
    if not tool:
        return "/dashboard"
    return _TOOL_TO_PATH.get(tool, "/dashboard")


# ---------------------------------------------------------------------------
# Triage endpoint — same workspace list, enriched with CPA-triage signals.
# ---------------------------------------------------------------------------


@router.get("/api/me/clients/triage")
async def list_my_clients_triage(
    auth: AuthContext = Depends(validate_bearer_token),
) -> dict[str, Any]:
    """CPA command-center version of /api/me/clients.

    Enriches each workspace row with:
      * ``needs_cpa_count``         — tax_review_queue items in 'pending' status
      * ``waiting_on_client_count`` — approval_queue rows in 'pending' that are
                                      bank_transaction_import (the user has to
                                      decide whether to post them) — proxy for
                                      "we're waiting on the workspace owner"
      * ``upcoming_deadline``       — earliest future tax_filing_submission
                                      ``expected_response_by`` (None if none)
      * ``days_to_deadline``        — int days from now to upcoming_deadline,
                                      negative if already past
      * ``last_client_activity_at`` — most recent journal_entry.created_at
                                      not authored by an AI agent. Proxy for
                                      "is the client still feeding us data?"
      * ``suggested_route``         — one of "needs_cpa" | "waiting_on_client"
                                      | "deadline_soon" | "stale" | "ok",
                                      derived from the signals above

    Plus the persona detection / role flags from the base endpoint. Returns
    the workspaces sorted urgent-first (deadline soon > needs_cpa > waiting >
    stale > ok). Best-effort: workspace-level failures degrade silently.
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
                            COALESCE(w.onboarded_by_user_id = :user_id, false)
                                                AS onboarded_by_me
                        FROM public.workspace_membership m
                        JOIN public.workspace w ON w.id = m.workspace_id
                        WHERE m.user_id = :user_id
                          AND w.status = 'active'
                        ORDER BY w.display_name
                        """
                    ),
                    {"user_id": user_id},
                )
            ).mappings().all()
            clients_onboarded = await _clients_onboarded_count(session, user_id)

    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc)

    workspaces: list[dict[str, Any]] = []

    for row in rows:
        entry = dict(row)

        ent_count = 0
        open_items = 0
        needs_cpa = 0
        waiting_client = 0
        upcoming_deadline: dt.datetime | None = None
        last_client_activity: dt.datetime | None = None

        try:
            async with workspace_session(SessionLocal, row["id"]) as ws:
                # entity count
                e_row = (
                    await ws.execute(
                        text(
                            "SELECT count(*) AS c FROM entity WHERE status = 'active'"
                        )
                    )
                ).first()
                ent_count = e_row.c if e_row else 0

                # CPA review queue (needs_cpa)
                try:
                    r = (
                        await ws.execute(
                            text(
                                """
                                SELECT count(*) AS c FROM tax_review_queue
                                WHERE status IN ('pending','in_review')
                                """
                            )
                        )
                    ).first()
                    needs_cpa = r.c if r else 0
                    open_items += needs_cpa
                except Exception:
                    pass

                # Pending bank-imports as a proxy for "waiting on workspace owner"
                try:
                    r = (
                        await ws.execute(
                            text(
                                """
                                SELECT count(*) AS c FROM approval_queue
                                WHERE status = 'pending'
                                  AND subject_type = 'bank_transaction_import'
                                """
                            )
                        )
                    ).first()
                    waiting_client = r.c if r else 0
                    open_items += waiting_client
                except Exception:
                    pass

                # Upcoming filing deadline
                try:
                    r = (
                        await ws.execute(
                            text(
                                """
                                SELECT min(expected_response_by) AS d
                                FROM tax_filing_submission
                                WHERE expected_response_by IS NOT NULL
                                  AND status NOT IN ('closed','acknowledged')
                                  AND expected_response_by > now() - interval '60 days'
                                """
                            )
                        )
                    ).first()
                    upcoming_deadline = r.d if r and r.d else None
                except Exception:
                    pass

                # Last NON-agent journal entry == client activity heartbeat
                try:
                    r = (
                        await ws.execute(
                            text(
                                """
                                SELECT max(created_at) AS d
                                FROM journal_entry
                                WHERE posted_by_uc_uid IS NOT NULL
                                """
                            )
                        )
                    ).first()
                    last_client_activity = r.d if r and r.d else None
                except Exception:
                    pass

        except Exception as exc:
            _log.info(
                "[clients/triage] workspace %s failed: %s", row["id"], exc
            )

        days_to_deadline: int | None = None
        if upcoming_deadline:
            days_to_deadline = (upcoming_deadline.date() - now.date()).days

        # Suggested route — first applicable, in priority order.
        suggested = "ok"
        if days_to_deadline is not None and days_to_deadline <= 14:
            suggested = "deadline_soon"
        elif needs_cpa > 0:
            suggested = "needs_cpa"
        elif waiting_client > 0:
            suggested = "waiting_on_client"
        elif last_client_activity is None or (
            (now - last_client_activity).days > 30
        ):
            suggested = "stale"

        entry["entity_count"] = ent_count
        entry["open_items_count"] = open_items
        entry["needs_cpa_count"] = needs_cpa
        entry["waiting_on_client_count"] = waiting_client
        entry["upcoming_deadline"] = (
            upcoming_deadline.isoformat() if upcoming_deadline else None
        )
        entry["days_to_deadline"] = days_to_deadline
        entry["last_client_activity_at"] = (
            last_client_activity.isoformat() if last_client_activity else None
        )
        entry["suggested_route"] = suggested
        workspaces.append(entry)

    persona = _persona_for(
        (r["role"] for r in rows), clients_onboarded, empty="client"
    )

    # Sort: deadline_soon (by days asc) > needs_cpa (desc count) > waiting >
    #       stale > ok. We do this in one sort key for stability.
    _route_rank = {
        "deadline_soon": 0,
        "needs_cpa": 1,
        "waiting_on_client": 2,
        "stale": 3,
        "ok": 4,
    }

    def _sort_key(w: dict[str, Any]) -> tuple:
        r = _route_rank.get(w["suggested_route"], 5)
        # Deadline: nearest first; missing -> large
        d = w.get("days_to_deadline")
        d_key = d if d is not None else 9999
        return (r, d_key, -w.get("open_items_count", 0), w.get("display_name", ""))

    workspaces.sort(key=_sort_key)

    return {
        "persona": persona,
        "workspaces": workspaces,
        "total_workspaces": len(workspaces),
        "total_open_items": sum(w["open_items_count"] for w in workspaces),
        "total_needs_cpa": sum(w["needs_cpa_count"] for w in workspaces),
        "total_waiting_on_client": sum(
            w["waiting_on_client_count"] for w in workspaces
        ),
    }


# ---------------------------------------------------------------------------
# Persona (cheap) + CPA-originated client onboarding.
#
# `/api/me/clients` (GET) and `/triage` peek into every workspace schema, which
# is too heavy for a login-time redirect. `GET /api/me/persona` answers the one
# question the landing router needs — "is this caller a practitioner?" — from
# the registry tables alone (no per-workspace schema sessions).
#
# `POST /api/me/clients` is the CPA-originated onboarding path. The existing
# member-invite endpoints (app/api/members.py) are deliberately client-
# originated and admin-gated: a CPA can NEVER self-grant privilege inside an
# existing customer workspace. This endpoint does not violate that — it
# *provisions a brand-new workspace* for a client of the caller's practice and
# seats the caller as its ``owner`` (CLIENT_SEAT_ROLE). The practice set the
# books up, so the practice runs them end to end: bank connections (admin
# floor), member invitations including the client-owner seat (owner floor),
# period close, data rights. If the practitioner supplies the business owner's
# email, that person is invited as ``client_owner`` (CLIENT_OWNER_INVITE_ROLE):
# the same rank as owner, so the client holds full authority over their own
# data while the practice operates it. Seating yourself in a workspace you just
# created is not self-escalation inside someone else's workspace, which is why
# the admin gate does not apply here.
#
# Because the practitioner holds ``owner`` (not ``cpa``) in the workspaces they
# onboard, "holds a cpa seat" no longer identifies a practice. The workspace
# registry records who onboarded it (``workspace.onboarded_by_user_id``, public
# migration 0019) and ``_persona_for`` counts those too.
# ---------------------------------------------------------------------------

# Roles that mark an "established practitioner" allowed to onboard new clients.
# A truly cold account (zero memberships) cannot self-provision — that open-
# registration path is gated on pricing/billing and handled separately.
_PRACTITIONER_ROLES = (
    "cpa",
    "accountant",
    "admin",
    "owner",
    "client_owner",
    "platform_admin",
)

# The seat the onboarding practitioner gets in the client workspace they
# provision, and the seat the business owner is invited to. Both are rank 5 in
# app/tenancy.py, so either side can run the workspace in full.
CLIENT_SEAT_ROLE = "owner"
CLIENT_OWNER_INVITE_ROLE = "client_owner"
# membership_audit.action for the console's self-seat. Must be admitted by
# ck_membership_audit_action (public migration 0019 widens 0017's list).
CONSOLE_ONBOARD_AUDIT_ACTION = "console_onboard"


def _persona_for(
    roles: Iterable[str], clients_onboarded: int, *, empty: str = "new"
) -> str:
    """platform_admin > cpa > client > ``empty``.

    A caller reads as a practitioner ("cpa") when they hold a ``cpa`` seat
    anywhere OR have onboarded at least one active client workspace through
    POST /api/me/clients. ``empty`` is what a caller with no active membership
    at all reads as: "new" for the landing router, "client" for the list
    endpoints' older contract.
    """
    role_set = set(roles)
    if "platform_admin" in role_set:
        return "platform_admin"
    if "cpa" in role_set or clients_onboarded > 0:
        return "cpa"
    if role_set:
        return "client"
    return empty


async def _clients_onboarded_count(session: Any, user_id: UUID) -> int:
    """Active workspaces this user provisioned through POST /api/me/clients."""
    row = (
        await session.execute(
            text(
                """
                SELECT count(*) AS c FROM public.workspace
                WHERE onboarded_by_user_id = :uid AND status = 'active'
                """
            ),
            {"uid": user_id},
        )
    ).first()
    return int(row.c) if row else 0


def _slugify(value: str) -> str:
    import re

    s = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return s[:80] or "client"


# client_kind lives on public.workspace.setup_progress (migration 0011), the
# workspace row's only JSON document. The checklist PATCH merges keys, so this
# sibling survives later setup writes. Absent on a row means business — the
# historical default — so no new column and no backfill.
CLIENT_KIND_PROFILE_KEY = "client_kind"
CLIENT_KINDS = ("business", "individual")
# Kind the Setup Assistant seeds for the first entity today (setup bootstrap).
# An individual client changes only that default, to the 1040 entity kind.
PLACEHOLDER_ENTITY_KIND = "other"
INDIVIDUAL_ENTITY_KIND = "personal"


def client_kind_profile_patch(client_kind: str) -> dict[str, str]:
    """The profile fragment merged into workspace.setup_progress."""
    if client_kind not in CLIENT_KINDS:
        raise ValueError(
            f"client_kind must be 'business' or 'individual', got {client_kind!r}"
        )
    return {CLIENT_KIND_PROFILE_KEY: client_kind}


def client_kind_patch_json(client_kind: str) -> str:
    return json.dumps(client_kind_profile_patch(client_kind))


def client_kind_from_profile(profile: Any) -> str:
    """Read client_kind off a workspace profile. Missing or unknown → business."""
    if isinstance(profile, str):
        try:
            profile = json.loads(profile)
        except json.JSONDecodeError:
            return "business"
    if not isinstance(profile, dict):
        return "business"
    raw = profile.get(CLIENT_KIND_PROFILE_KEY)
    if raw in CLIENT_KINDS:
        return raw
    return "business"


def first_entity_kind_for(client_kind: str | None) -> str:
    """Entity kind for the workspace's first seeded entity.

    Business (and any caller that omits client_kind) keeps ``other``.
    An individual defaults to ``personal``.
    """
    if client_kind == "individual":
        return INDIVIDUAL_ENTITY_KIND
    return PLACEHOLDER_ENTITY_KIND


def invitation_subject(display_name: str) -> str:
    """Invitation subject. Names the client; does not add the word business."""
    return f"You've been invited to {display_name} on Accounting-Ops"


def individual_invite_bodies(
    display_name: str,
    inviter_email: str | None,
    accept_url: str,
    expires_at: datetime,
) -> tuple[str, str]:
    """Text and HTML for an individual. Lead line is 'invited to <name>'."""
    by = f" by {inviter_email}" if inviter_email else ""
    expires_str = expires_at.strftime("%Y-%m-%d %H:%M UTC")
    text_body = (
        f"You've been invited{by} to {display_name} on Accounting-Ops "
        f"as a workspace owner.\n\n"
        f"Accept this invitation:\n  {accept_url}\n\n"
        f"This link expires on {expires_str}. If you don't have an "
        f"Accounting-Ops account yet, you'll be prompted to create one when "
        f"you click the link.\n\n"
        f"Accounting-Ops — Your books. Your machine. Your rules.\n"
    )
    html_body = (
        f"<p>You've been invited{by} to <strong>{display_name}</strong> "
        f"on Accounting-Ops as a workspace owner.</p>"
        f"<p><a href='{accept_url}'>Accept invitation</a></p>"
        f"<p>This link expires on <code>{expires_str}</code>. If you don't "
        f"have an Accounting-Ops account yet, you'll be prompted to create one "
        f"when you click the link.</p>"
        f"<hr><p style='color:#888;font-size:12px;'>Accounting-Ops — "
        f"Your books. Your machine. Your rules.</p>"
    )
    return text_body, html_body


async def _caller_account(
    uc_uid: str, *, autoprovision: bool = False
) -> tuple[UUID, str | None, list[str], int]:
    """Return (user_id, email, distinct_roles_held, clients_onboarded) for the caller.

    ``clients_onboarded`` counts the active workspaces the caller provisioned
    through POST /api/me/clients (see ``_persona_for``).

    JIT-provisions a ``user_account`` row only for caller classes allowed to
    auto-provision (``AuthContext.may_autoprovision``: Keycloak login / CE
    local). Other callers (service tokens, PATs, Brigade) resolve-or-403 —
    a ghost row here would have no roles anyway.
    """
    from app.api.workspaces import _ensure_user_account

    user_id = await _ensure_user_account(uc_uid, autoprovision=autoprovision)
    async with SessionLocal() as session:
        async with session.begin():
            email_row = (
                await session.execute(
                    text("SELECT email FROM public.user_account WHERE id = :id"),
                    {"id": user_id},
                )
            ).first()
            role_rows = (
                await session.execute(
                    text(
                        """
                        SELECT DISTINCT m.role::text AS role
                        FROM public.workspace_membership m
                        JOIN public.workspace w ON w.id = m.workspace_id
                        WHERE m.user_id = :uid AND w.status = 'active'
                        """
                    ),
                    {"uid": user_id},
                )
            ).mappings().all()
            clients_onboarded = await _clients_onboarded_count(session, user_id)
    return (
        user_id,
        (email_row.email if email_row else None),
        [r["role"] for r in role_rows],
        clients_onboarded,
    )


@router.get("/api/me/persona")
async def my_persona(
    auth: AuthContext = Depends(validate_bearer_token),
) -> dict[str, Any]:
    """Cheap persona check for the login-time landing router.

    Registry tables only (no per-workspace schema peek), so it's safe to call
    on every sign-in. ``can_add_clients`` is true when the caller already holds
    a practitioner-or-owner role somewhere (the gate POST /api/me/clients
    enforces). ``clients_onboarded`` is how many active workspaces the caller
    set up from the console; one or more makes the persona "cpa" (see
    ``_persona_for``).
    """
    _user_id, _email, roles, clients_onboarded = await _caller_account(
        auth.uc_uid, autoprovision=auth.may_autoprovision
    )
    role_set = set(roles)
    persona = _persona_for(roles, clients_onboarded)
    can_add = bool(role_set & set(_PRACTITIONER_ROLES))
    return {
        "persona": persona,
        "roles": sorted(role_set),
        "cpa_count": sum(1 for r in roles if r == "cpa"),
        "clients_onboarded": clients_onboarded,
        "workspace_count": len(roles),
        "can_add_clients": can_add,
    }


class AddClientRequest(BaseModel):
    business_name: str = Field(
        ...,
        min_length=2,
        max_length=160,
        description=(
            "The client's name — a business or an individual the practice files "
            "for. Becomes the workspace display name."
        ),
    )
    owner_email: str | None = Field(
        default=None,
        max_length=256,
        description=(
            "If set, the client (business owner or individual) is emailed a "
            "'client_owner' invitation (owner rank) so they hold full authority "
            "over their own books."
        ),
    )
    client_kind: Literal["business", "individual"] = Field(
        default="business",
        description=(
            "Whether the client is a business or an individual person. "
            "Omitted or null means business, so existing callers are unchanged. "
            "An individual is stored on the workspace profile and the first "
            "seeded entity defaults to kind personal."
        ),
    )
    hipaa_protected: bool = False
    isolation_tier: str = Field(default="shared")

    @field_validator("client_kind", mode="before")
    @classmethod
    def _blank_client_kind_is_business(cls, value: object) -> object:
        # Explicit null or "" is the same as omitting the field.
        if value is None or value == "":
            return "business"
        return value


@router.post("/api/me/clients", status_code=status.HTTP_201_CREATED)
async def add_client(
    body: AddClientRequest,
    auth: AuthContext = Depends(validate_bearer_token),
) -> dict[str, Any]:
    """CPA-originated client onboarding: provision a workspace + (optionally)
    invite the business owner.

    The caller becomes the new workspace's ``owner`` (CLIENT_SEAT_ROLE): they
    can run the books, connect the client's bank, invite their own team and
    the client, and close periods without waiting on anyone. If
    ``owner_email`` is supplied, the business owner is sent a ``client_owner``
    magic-link invitation (owner rank) so they hold full authority over their
    own data while the practice operates it. Gate: caller must already hold a
    practitioner/owner role somewhere.
    """
    # Reuse the proven invite helpers + provisioning path. Imported lazily to
    # avoid import cycles (members.py imports tenancy which imports nothing here).
    from app.api.members import (
        DEFAULT_INVITE_TTL_DAYS,
        TOKEN_BYTES,
        _accept_url,
        _hash_token,
        _invite_email_body,
        _normalize_email,
        _write_membership_audit,
    )
    from app.mailer import send_email
    from app.tenancy import WorkspaceContext
    from scripts.provision_workspace import provision_workspace

    user_id, caller_email, roles, _onboarded = await _caller_account(
        auth.uc_uid, autoprovision=auth.may_autoprovision
    )
    if not (set(roles) & set(_PRACTITIONER_ROLES)):
        raise HTTPException(
            status_code=403,
            detail=(
                "Onboarding a client requires an existing CPA, accountant, or "
                "owner role. Ask a platform admin to seed your practice workspace "
                "first."
            ),
        )

    owner_email = _normalize_email(body.owner_email) if body.owner_email else None

    # Provision the new workspace. Slug carries an id suffix so two clients with
    # the same name never collide on the unique slug.
    wid = uuid4()
    slug = f"{_slugify(body.business_name)}-{str(wid)[:8]}"
    schema, _ = await provision_workspace(
        engine=None,
        workspace_id=wid,
        display_name=body.business_name,
        slug=slug,
        hipaa_protected=body.hipaa_protected,
        isolation_tier=body.isolation_tier,
        entities=None,
        onboarded_by_user_id=user_id,
    )

    # Seat the caller as the workspace's owner. invited_by_uc_uid = the caller
    # themself records that the seat came from console onboarding, not from an
    # invitation. (workspace_membership.status defaults to 'active' — 0015.)
    # The membership audit trail (0017) gets the same rows the members API
    # would write, in the same transaction, so this self-seat is accountable.
    audit_ctx = WorkspaceContext(
        workspace_id=wid,
        workspace_slug=slug,
        role=CLIENT_SEAT_ROLE,
        user_uc_uid=auth.uc_uid,
        agent_id=auth.agent_id,
        entity_scope=None,
        hipaa_protected=body.hipaa_protected,
    )
    async with SessionLocal() as session:
        async with session.begin():
            await session.execute(
                text(
                    """
                    INSERT INTO public.workspace_membership
                        (id, user_id, workspace_id, role, invited_by_uc_uid, created_at)
                    VALUES (
                        gen_random_uuid(), :uid, :wid,
                        CAST(:role AS workspace_role), :uc, now()
                    )
                    ON CONFLICT ON CONSTRAINT uq_workspace_membership
                    DO UPDATE SET role = CAST(:role AS workspace_role)
                    """
                ),
                {
                    "uid": user_id,
                    "wid": str(wid),
                    "role": CLIENT_SEAT_ROLE,
                    "uc": auth.uc_uid,
                },
            )
            await _write_membership_audit(
                session,
                workspace=audit_ctx,
                action=CONSOLE_ONBOARD_AUDIT_ACTION,
                target_user_id=user_id,
                new_role=CLIENT_SEAT_ROLE,
            )
            # Merge, don't replace: a re-provision must not wipe checklist steps.
            await session.execute(
                text(
                    """
                    UPDATE public.workspace
                    SET setup_progress = COALESCE(setup_progress, '{}'::jsonb)
                        || CAST(:patch AS jsonb)
                    WHERE id = :id
                    """
                ),
                {
                    "patch": client_kind_patch_json(body.client_kind),
                    "id": str(wid),
                },
            )

    owner_invitation: dict[str, Any] | None = None
    if owner_email:
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(days=DEFAULT_INVITE_TTL_DAYS)
        raw_token = secrets.token_urlsafe(TOKEN_BYTES)
        invitation_id = uuid4()
        async with SessionLocal() as session:
            async with session.begin():
                await session.execute(
                    text(
                        """
                        INSERT INTO public.workspace_invitation (
                            id, workspace_id, email, role, entity_scope,
                            token_hash, expires_at, created_by_uc_uid
                        ) VALUES (
                            :id, :wid, :email, CAST(:role AS workspace_role),
                            NULL, :token_hash, :expires_at, :created_by
                        )
                        """
                    ),
                    {
                        "id": str(invitation_id),
                        "wid": str(wid),
                        "email": owner_email,
                        "role": CLIENT_OWNER_INVITE_ROLE,
                        "token_hash": _hash_token(raw_token),
                        "expires_at": expires_at,
                        "created_by": auth.uc_uid,
                    },
                )
                await _write_membership_audit(
                    session,
                    workspace=audit_ctx,
                    action="invitation_create",
                    invited_email=owner_email,
                    invitation_id=str(invitation_id),
                    new_role=CLIENT_OWNER_INVITE_ROLE,
                )
                accept_url = _accept_url(raw_token)
                if body.client_kind == "individual":
                    # "invited to <name>" — the shared template is for a
                    # business workspace and this copy must not say business.
                    text_body, html_body = individual_invite_bodies(
                        body.business_name,
                        caller_email,
                        accept_url,
                        expires_at,
                    )
                else:
                    text_body, html_body = _invite_email_body(
                        workspace_name=body.business_name,
                        inviter_email=caller_email,
                        role=CLIENT_OWNER_INVITE_ROLE,
                        accept_url=accept_url,
                        expires_at=expires_at,
                    )
                delivery = await send_email(
                    session,
                    to_email=owner_email,
                    subject=invitation_subject(body.business_name),
                    body_text=text_body,
                    body_html=html_body,
                    related_invitation_id=invitation_id,
                )
        owner_invitation = {
            "id": str(invitation_id),
            "email": owner_email,
            "role": CLIENT_OWNER_INVITE_ROLE,
            "expires_at": expires_at.isoformat(),
            "delivery": delivery,
        }

    return {
        "workspace_id": str(wid),
        "slug": slug,
        "display_name": body.business_name,
        "schema": schema,
        "your_role": CLIENT_SEAT_ROLE,
        "owner_invitation": owner_invitation,
    }


__all__ = ("router",)
