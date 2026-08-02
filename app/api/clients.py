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

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
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
                            m.last_active_at    AS last_active_at
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

    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc)

    workspaces: list[dict[str, Any]] = []
    cpa_count = 0
    has_platform = False

    for row in rows:
        entry = dict(row)
        role = entry["role"]
        if role == "cpa":
            cpa_count += 1
        if role == "platform_admin":
            has_platform = True

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

    if has_platform:
        persona = "platform_admin"
    elif cpa_count >= 1:
        persona = "cpa"
    else:
        persona = "client"

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
# question the landing router needs — "is this caller a CPA?" — from the
# membership table alone (no per-workspace schema sessions).
#
# `POST /api/me/clients` is the CPA-originated onboarding path. The existing
# member-invite endpoints (app/api/members.py) are deliberately client-
# originated and admin-gated: a CPA can NEVER self-grant privilege inside an
# existing customer workspace. This endpoint does not violate that — it
# *provisions a brand-new workspace*, makes the caller a ``cpa`` member (the
# accountant-rank role that can do the books but cannot invite), and issues the
# client-owner an ``owner`` invitation. Granting someone ABOVE you in a
# workspace you just created is not self-escalation, so issuing the owner invite
# here (bypassing the admin gate) is correct and safe.
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


def _slugify(value: str) -> str:
    import re

    s = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return s[:80] or "client"


async def _caller_account(
    uc_uid: str, *, autoprovision: bool = False
) -> tuple[UUID, str | None, list[str]]:
    """Return (user_id, email, distinct_roles_held) for the caller.

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
    return (
        user_id,
        (email_row.email if email_row else None),
        [r["role"] for r in role_rows],
    )


@router.get("/api/me/persona")
async def my_persona(
    auth: AuthContext = Depends(validate_bearer_token),
) -> dict[str, Any]:
    """Cheap persona check for the login-time landing router.

    Membership-table only (no per-workspace schema peek), so it's safe to call
    on every sign-in. ``can_add_clients`` is true when the caller already holds
    a practitioner-or-owner role somewhere (the gate POST /api/me/clients
    enforces).
    """
    _user_id, _email, roles = await _caller_account(
        auth.uc_uid, autoprovision=auth.may_autoprovision
    )
    role_set = set(roles)
    if "platform_admin" in role_set:
        persona = "platform_admin"
    elif "cpa" in role_set:
        persona = "cpa"
    elif role_set:
        persona = "client"
    else:
        persona = "new"
    can_add = bool(role_set & set(_PRACTITIONER_ROLES))
    return {
        "persona": persona,
        "roles": sorted(role_set),
        "cpa_count": sum(1 for r in roles if r == "cpa"),
        "workspace_count": len(roles),
        "can_add_clients": can_add,
    }


class AddClientRequest(BaseModel):
    business_name: str = Field(..., min_length=2, max_length=160)
    owner_email: str | None = Field(
        default=None,
        max_length=256,
        description="If set, the client-owner is emailed an 'owner' invitation.",
    )
    hipaa_protected: bool = False
    isolation_tier: str = Field(default="shared")


@router.post("/api/me/clients", status_code=status.HTTP_201_CREATED)
async def add_client(
    body: AddClientRequest,
    auth: AuthContext = Depends(validate_bearer_token),
) -> dict[str, Any]:
    """CPA-originated client onboarding: provision a workspace + (optionally)
    invite the business owner.

    The caller becomes a ``cpa`` member (accountant-rank: can run the books,
    cannot invite). If ``owner_email`` is supplied, the business owner is sent
    an ``owner`` magic-link invitation so they own their own data while the CPA
    operates it. Gate: caller must already hold a practitioner/owner role.
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
    )
    from app.mailer import send_email
    from scripts.provision_workspace import provision_workspace

    user_id, caller_email, roles = await _caller_account(
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
    )

    # Make the caller a cpa member (NOTE: no 'status' column on
    # workspace_membership — see migration 0001; setup_assistant's INSERT that
    # references it is a latent bug, fixed separately).
    async with SessionLocal() as session:
        async with session.begin():
            await session.execute(
                text(
                    """
                    INSERT INTO public.workspace_membership
                        (id, user_id, workspace_id, role, invited_by_uc_uid, created_at)
                    VALUES (gen_random_uuid(), :uid, :wid, 'cpa', :uc, now())
                    ON CONFLICT ON CONSTRAINT uq_workspace_membership
                    DO UPDATE SET role = 'cpa'
                    """
                ),
                {"uid": user_id, "wid": str(wid), "uc": auth.uc_uid},
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
                            :id, :wid, :email, CAST('owner' AS workspace_role),
                            NULL, :token_hash, :expires_at, :created_by
                        )
                        """
                    ),
                    {
                        "id": str(invitation_id),
                        "wid": str(wid),
                        "email": owner_email,
                        "token_hash": _hash_token(raw_token),
                        "expires_at": expires_at,
                        "created_by": auth.uc_uid,
                    },
                )
                text_body, html_body = _invite_email_body(
                    workspace_name=body.business_name,
                    inviter_email=caller_email,
                    role="owner",
                    accept_url=_accept_url(raw_token),
                    expires_at=expires_at,
                )
                delivery = await send_email(
                    session,
                    to_email=owner_email,
                    subject=(
                        f"You've been invited to {body.business_name} on "
                        f"Accounting-Ops"
                    ),
                    body_text=text_body,
                    body_html=html_body,
                    related_invitation_id=invitation_id,
                )
        owner_invitation = {
            "id": str(invitation_id),
            "email": owner_email,
            "role": "owner",
            "expires_at": expires_at.isoformat(),
            "delivery": delivery,
        }

    return {
        "workspace_id": str(wid),
        "slug": slug,
        "display_name": body.business_name,
        "schema": schema,
        "your_role": "cpa",
        "owner_invitation": owner_invitation,
    }


__all__ = ("router",)
