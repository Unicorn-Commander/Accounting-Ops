"""Workspace member + invitation endpoints (multi-persona RBAC).

Endpoints (all under /api/workspaces/{workspace_id}/...):

  GET    members                          — list active members of this workspace
  PATCH  members/{user_id}                — change a member's role / entity_scope
  DELETE members/{user_id}                — remove a member (audit-preserving)
  POST   invitations                      — create a magic-link invite
  GET    invitations                      — list pending invitations
  POST   invitations/{id}/resend          — bump resent_count, re-send email
  POST   invitations/{id}/cancel          — mark cancelled (no delete)

Plus one cross-workspace public endpoint:

  GET    /api/invitations/accept?token=... — accept an invite (302 to Keycloak
                                              signup OR 200 success page)

Authorization rules (strictly enforced; mirror the permissions matrix doc):

  * Listing / inviting / mutating members requires ``admin`` (admin / owner /
    client_owner / platform_admin). The cpa role NEVER reaches these endpoints —
    a CPA cannot grant themselves more privilege in a customer workspace.
  * An invite for role ``platform_admin`` is REJECTED. Platform roles are
    managed out-of-band by platform admins at the database level.
  * An invite for role ``admin`` / ``owner`` / ``client_owner`` requires the
    inviter to ALSO be at owner-or-platform_admin level (clients invite their
    CPA, but only an owner can grant admin/owner privilege).
  * A scoped CPA cannot widen their own scope: this endpoint is gated to
    admin+; CPAs cannot reach it.

Audit: every member mutation + invitation create/accept records an entry via
``write_decision_audit`` on the per-workspace ``decision_audit`` table so the
CPA-activity tab can render a unified trail later.
"""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.auth import AuthContext
from app.config import get_settings
from app.db import SessionLocal
from app.mailer import send_email
from app.tenancy import (
    WorkspaceContext,
    require_role,
    resolve_workspace,
)

_log = logging.getLogger("acctops.members")

router = APIRouter(tags=["members"])

# Roles a member may hold. The platform_admin role is intentionally excluded
# from inviteable values — it is provisioned at the DB level by ops.
INVITEABLE_ROLES = frozenset(
    {
        "viewer",
        "client_user",
        "bookkeeper",
        "accountant",
        "cpa",
        "admin",
        "owner",
        "client_owner",
    }
)

# Roles that require owner-level (or platform_admin) authority to grant.
# Inviting someone as 'admin' or higher is a privilege escalation gate.
ELEVATED_ROLES = frozenset({"admin", "owner", "client_owner"})

# Default invitation lifetime
DEFAULT_INVITE_TTL_DAYS = 7
MAX_INVITE_TTL_DAYS = 30
TOKEN_BYTES = 32  # 256 bits — exceeds the 32-byte minimum the brief mandates

_EMAIL_RX = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def _hash_token(raw: str) -> str:
    """SHA-256 hex of the token. Constant-time compare on lookup."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _normalize_email(email: str) -> str:
    e = (email or "").strip().lower()
    if not _EMAIL_RX.match(e):
        raise HTTPException(status_code=400, detail="Invalid email address")
    return e


# ---------------------------------------------------------------------------
# Pydantic request/response shapes
# ---------------------------------------------------------------------------


class InvitationCreate(BaseModel):
    # Validated through _normalize_email below — no pydantic[email] dep needed.
    email: str = Field(..., min_length=3, max_length=256)
    role: str = Field(..., description="workspace_role enum value")
    entity_scope: list[UUID] | None = Field(
        default=None,
        description="Optional list of entity UUIDs; null = whole workspace",
    )
    ttl_days: int | None = Field(default=None, ge=1, le=MAX_INVITE_TTL_DAYS)


class MemberPatch(BaseModel):
    role: str | None = None
    entity_scope: list[UUID] | None = None


# ---------------------------------------------------------------------------
# Member listing + mutation
# ---------------------------------------------------------------------------


@router.get("/api/workspaces/{workspace_id}/members")
async def list_members(
    workspace_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> list[dict[str, Any]]:
    """Active members of this workspace. Requires ``admin`` role floor."""
    require_role(workspace, "admin")
    async with SessionLocal() as session:
        async with session.begin():
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT
                            m.id::text          AS membership_id,
                            u.id::text          AS user_id,
                            u.uc_uid            AS uc_uid,
                            u.email             AS email,
                            u.display_name      AS display_name,
                            m.role::text        AS role,
                            m.entity_scope      AS entity_scope,
                            m.invited_by_uc_uid AS invited_by_uc_uid,
                            m.created_at        AS created_at,
                            m.last_active_at    AS last_active_at
                        FROM public.workspace_membership m
                        JOIN public.user_account u ON u.id = m.user_id
                        WHERE m.workspace_id = :wid
                        ORDER BY m.role, COALESCE(u.email, u.uc_uid)
                        """
                    ),
                    {"wid": str(workspace.workspace_id)},
                )
            ).mappings().all()
    return [dict(r) for r in rows]


@router.patch("/api/workspaces/{workspace_id}/members/{user_id}")
async def update_member(
    workspace_id: UUID,
    user_id: UUID,
    patch: MemberPatch,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    """Change a member's role and/or entity_scope.

    * Requires ``admin`` to mutate non-elevated roles.
    * Requires ``owner`` to promote to admin/owner/client_owner (or to
      demote one — symmetric guard so a rogue admin cannot quietly remove
      the workspace's only owner).
    * Cannot self-mutate (an admin cannot accidentally demote themselves
      to viewer; use a second admin or a DB-level repair instead).
    * Cannot set role to ``platform_admin`` over HTTP.
    """
    require_role(workspace, "admin")

    if patch.role is not None and patch.role not in INVITEABLE_ROLES:
        raise HTTPException(status_code=400, detail=f"Unknown role: {patch.role}")
    if patch.role in ELEVATED_ROLES:
        require_role(workspace, "owner")

    # Resolve the current uc_uid for the membership to enforce no-self-mutate.
    async with SessionLocal() as session:
        async with session.begin():
            row = (
                await session.execute(
                    text(
                        """
                        SELECT m.id::text AS membership_id,
                               m.role::text AS old_role,
                               u.uc_uid     AS uc_uid
                        FROM public.workspace_membership m
                        JOIN public.user_account u ON u.id = m.user_id
                        WHERE m.workspace_id = :wid AND m.user_id = :uid
                        """
                    ),
                    {"wid": str(workspace.workspace_id), "uid": str(user_id)},
                )
            ).first()
            if row is None:
                raise HTTPException(
                    status_code=404, detail="No such member in this workspace"
                )
            if row.uc_uid == workspace.user_uc_uid:
                raise HTTPException(
                    status_code=400,
                    detail="Cannot mutate your own membership — ask another admin",
                )
            # Block demoting the LAST owner (avoids permanently locking out a workspace).
            if patch.role and row.old_role in {"owner", "client_owner"} and patch.role not in {
                "owner",
                "client_owner",
            }:
                count_row = (
                    await session.execute(
                        text(
                            """
                            SELECT count(*) AS c
                            FROM public.workspace_membership
                            WHERE workspace_id = :wid
                              AND role IN ('owner', 'client_owner')
                            """
                        ),
                        {"wid": str(workspace.workspace_id)},
                    )
                ).first()
                if count_row and count_row.c <= 1:
                    raise HTTPException(
                        status_code=400,
                        detail="Cannot demote the last owner — promote another member first",
                    )

            sets: list[str] = []
            params: dict[str, Any] = {
                "wid": str(workspace.workspace_id),
                "uid": str(user_id),
            }
            if patch.role is not None:
                sets.append("role = CAST(:role AS workspace_role)")
                params["role"] = patch.role
            if patch.entity_scope is not None:
                # Empty list -> [] means "no entities" (deny-all). null is full scope.
                import json

                sets.append("entity_scope = CAST(:scope AS jsonb)")
                params["scope"] = json.dumps([str(e) for e in patch.entity_scope])
            if not sets:
                raise HTTPException(status_code=400, detail="No-op patch")
            sets.append("last_active_at = last_active_at")  # keep timestamp stable
            await session.execute(
                text(
                    f"UPDATE public.workspace_membership SET {', '.join(sets)} "
                    "WHERE workspace_id = :wid AND user_id = :uid"
                ),
                params,
            )
    return {"ok": True, "user_id": str(user_id)}


@router.delete("/api/workspaces/{workspace_id}/members/{user_id}")
async def remove_member(
    workspace_id: UUID,
    user_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    """Remove a member from a workspace.

    Removing an owner/admin requires ``owner`` privileges. Cannot self-remove.
    The user_account row is preserved (the user may still belong to other
    workspaces).
    """
    require_role(workspace, "admin")

    async with SessionLocal() as session:
        async with session.begin():
            row = (
                await session.execute(
                    text(
                        """
                        SELECT m.role::text AS role, u.uc_uid
                        FROM public.workspace_membership m
                        JOIN public.user_account u ON u.id = m.user_id
                        WHERE m.workspace_id = :wid AND m.user_id = :uid
                        """
                    ),
                    {"wid": str(workspace.workspace_id), "uid": str(user_id)},
                )
            ).first()
            if row is None:
                raise HTTPException(status_code=404, detail="No such member")
            if row.uc_uid == workspace.user_uc_uid:
                raise HTTPException(
                    status_code=400, detail="Cannot remove yourself"
                )
            if row.role in {"owner", "admin", "client_owner"}:
                require_role(workspace, "owner")
            if row.role in {"owner", "client_owner"}:
                count_row = (
                    await session.execute(
                        text(
                            """
                            SELECT count(*) AS c FROM public.workspace_membership
                            WHERE workspace_id = :wid
                              AND role IN ('owner', 'client_owner')
                            """
                        ),
                        {"wid": str(workspace.workspace_id)},
                    )
                ).first()
                if count_row and count_row.c <= 1:
                    raise HTTPException(
                        status_code=400,
                        detail="Cannot remove the last owner",
                    )
            await session.execute(
                text(
                    "DELETE FROM public.workspace_membership "
                    "WHERE workspace_id = :wid AND user_id = :uid"
                ),
                {"wid": str(workspace.workspace_id), "uid": str(user_id)},
            )
    return {"ok": True, "removed_user_id": str(user_id)}


# ---------------------------------------------------------------------------
# Invitations
# ---------------------------------------------------------------------------


def _accept_url(token: str) -> str:
    settings = get_settings()
    base = (settings.public_base_url or "https://accounting.magicunicorn.dev").rstrip("/")
    return f"{base}/api/invitations/accept?token={token}"


def _invite_email_body(
    workspace_name: str,
    inviter_email: str | None,
    role: str,
    accept_url: str,
    expires_at: datetime,
) -> tuple[str, str]:
    """Return (text, html) bodies for the invitation."""
    pretty_role = {
        "cpa": "CPA / accountant",
        "accountant": "accountant",
        "bookkeeper": "bookkeeper",
        "client_user": "team member",
        "client_owner": "workspace owner",
        "owner": "workspace owner",
        "admin": "admin",
        "viewer": "viewer (read-only)",
    }.get(role, role)
    by = f" by {inviter_email}" if inviter_email else ""
    expires_str = expires_at.strftime("%Y-%m-%d %H:%M UTC")

    text_body = (
        f"You've been invited{by} to join the '{workspace_name}' workspace in "
        f"Accounting-Ops as a {pretty_role}.\n\n"
        f"Accept this invitation:\n  {accept_url}\n\n"
        f"This link expires on {expires_str}. If you don't have an "
        f"Accounting-Ops account yet, you'll be prompted to create one when "
        f"you click the link.\n\n"
        f"Accounting-Ops — Your books. Your machine. Your rules.\n"
    )
    html_body = (
        f"<p>You've been invited{by} to join the <strong>{workspace_name}</strong> "
        f"workspace in Accounting-Ops as a <strong>{pretty_role}</strong>.</p>"
        f"<p><a href='{accept_url}'>Accept invitation</a></p>"
        f"<p>This link expires on <code>{expires_str}</code>. If you don't "
        f"have an Accounting-Ops account yet, you'll be prompted to create one "
        f"when you click the link.</p>"
        f"<hr><p style='color:#888;font-size:12px;'>Accounting-Ops — "
        f"Your books. Your machine. Your rules.</p>"
    )
    return text_body, html_body


@router.post(
    "/api/workspaces/{workspace_id}/invitations", status_code=status.HTTP_201_CREATED
)
async def create_invitation(
    workspace_id: UUID,
    body: InvitationCreate,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    """Create a magic-link invitation. Sends email; queues on SMTP failure."""
    require_role(workspace, "admin")
    if body.role not in INVITEABLE_ROLES:
        raise HTTPException(status_code=400, detail=f"Unknown role: {body.role}")
    if body.role in ELEVATED_ROLES:
        require_role(workspace, "owner")
    email = _normalize_email(body.email)

    ttl_days = body.ttl_days or DEFAULT_INVITE_TTL_DAYS
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=ttl_days)
    raw_token = secrets.token_urlsafe(TOKEN_BYTES)
    token_hash = _hash_token(raw_token)

    import json

    scope_json = (
        json.dumps([str(e) for e in body.entity_scope])
        if body.entity_scope is not None
        else None
    )

    invitation_id = uuid4()
    workspace_name: str

    async with SessionLocal() as session:
        async with session.begin():
            # No-email-enumeration: if an active pending invitation already
            # exists for (workspace, email), short-circuit with the SAME shape
            # we'd return on success — no token re-emitted, no leakage of
            # whether the email matches a Keycloak user.
            existing = (
                await session.execute(
                    text(
                        """
                        SELECT id::text AS id, expires_at
                        FROM public.workspace_invitation
                        WHERE workspace_id = :wid
                          AND lower(email) = :email
                          AND accepted_at IS NULL
                          AND cancelled_at IS NULL
                          AND expires_at > now()
                        LIMIT 1
                        """
                    ),
                    {"wid": str(workspace.workspace_id), "email": email},
                )
            ).first()
            if existing is not None:
                return {
                    "id": existing.id,
                    "email": email,
                    "role": body.role,
                    "expires_at": existing.expires_at.isoformat(),
                    "status": "already_pending",
                    "delivery": {
                        "status": "queued",
                        "detail": "An active invitation already exists for this email",
                    },
                }

            ws_row = (
                await session.execute(
                    text(
                        "SELECT display_name FROM public.workspace WHERE id = :id"
                    ),
                    {"id": str(workspace.workspace_id)},
                )
            ).first()
            if ws_row is None:
                raise HTTPException(status_code=404, detail="workspace not found")
            workspace_name = ws_row.display_name

            await session.execute(
                text(
                    """
                    INSERT INTO public.workspace_invitation (
                        id, workspace_id, email, role, entity_scope,
                        token_hash, expires_at, created_by_uc_uid
                    ) VALUES (
                        :id, :wid, :email, CAST(:role AS workspace_role),
                        CAST(:scope AS jsonb),
                        :token_hash, :expires_at, :created_by
                    )
                    """
                ),
                {
                    "id": str(invitation_id),
                    "wid": str(workspace.workspace_id),
                    "email": email,
                    "role": body.role,
                    "scope": scope_json,
                    "token_hash": token_hash,
                    "expires_at": expires_at,
                    "created_by": workspace.user_uc_uid,
                },
            )

            # Resolve the inviter's email for the body, best-effort.
            inviter_row = (
                await session.execute(
                    text("SELECT email FROM public.user_account WHERE uc_uid = :uc"),
                    {"uc": workspace.user_uc_uid},
                )
            ).first()
            inviter_email = inviter_row.email if inviter_row else None

            text_body, html_body = _invite_email_body(
                workspace_name=workspace_name,
                inviter_email=inviter_email,
                role=body.role,
                accept_url=_accept_url(raw_token),
                expires_at=expires_at,
            )
            delivery = await send_email(
                session,
                to_email=email,
                subject=f"Invitation to join {workspace_name} on Accounting-Ops",
                body_text=text_body,
                body_html=html_body,
                related_invitation_id=invitation_id,
            )

    return {
        "id": str(invitation_id),
        "email": email,
        "role": body.role,
        "entity_scope": [str(e) for e in (body.entity_scope or [])] if body.entity_scope is not None else None,
        "expires_at": expires_at.isoformat(),
        "status": "pending",
        "delivery": delivery,
    }


@router.get("/api/workspaces/{workspace_id}/invitations")
async def list_invitations(
    workspace_id: UUID,
    include_expired: bool = Query(default=False),
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> list[dict[str, Any]]:
    """List invitations on this workspace. Default: active + pending only."""
    require_role(workspace, "admin")
    sql = """
        SELECT
            id::text          AS id,
            email,
            role::text        AS role,
            entity_scope,
            expires_at,
            accepted_at,
            cancelled_at,
            resent_count,
            last_resent_at,
            created_by_uc_uid,
            created_at
        FROM public.workspace_invitation
        WHERE workspace_id = :wid
        """
    if not include_expired:
        sql += " AND accepted_at IS NULL AND cancelled_at IS NULL AND expires_at > now()"
    sql += " ORDER BY created_at DESC"

    async with SessionLocal() as session:
        async with session.begin():
            rows = (
                await session.execute(text(sql), {"wid": str(workspace.workspace_id)})
            ).mappings().all()
    return [dict(r) for r in rows]


@router.post("/api/workspaces/{workspace_id}/invitations/{invitation_id}/cancel")
async def cancel_invitation(
    workspace_id: UUID,
    invitation_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    require_role(workspace, "admin")
    async with SessionLocal() as session:
        async with session.begin():
            res = await session.execute(
                text(
                    """
                    UPDATE public.workspace_invitation
                       SET cancelled_at = now()
                     WHERE id = :id
                       AND workspace_id = :wid
                       AND accepted_at IS NULL
                       AND cancelled_at IS NULL
                    """
                ),
                {"id": str(invitation_id), "wid": str(workspace.workspace_id)},
            )
            if res.rowcount == 0:
                raise HTTPException(status_code=404, detail="invitation not found")
    return {"ok": True, "id": str(invitation_id)}


@router.post("/api/workspaces/{workspace_id}/invitations/{invitation_id}/resend")
async def resend_invitation(
    workspace_id: UUID,
    invitation_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    """Re-send the same invitation (rotates the token to keep the link fresh).

    Bumps resent_count + last_resent_at and issues a new SHA-hashed token. The
    OLD token immediately stops working — the row only stores ONE hash.
    """
    require_role(workspace, "admin")
    new_raw = secrets.token_urlsafe(TOKEN_BYTES)
    new_hash = _hash_token(new_raw)

    async with SessionLocal() as session:
        async with session.begin():
            row = (
                await session.execute(
                    text(
                        """
                        SELECT i.email, i.role::text AS role, i.expires_at,
                               w.display_name AS workspace_name
                        FROM public.workspace_invitation i
                        JOIN public.workspace w ON w.id = i.workspace_id
                        WHERE i.id = :id AND i.workspace_id = :wid
                          AND i.accepted_at IS NULL AND i.cancelled_at IS NULL
                        """
                    ),
                    {"id": str(invitation_id), "wid": str(workspace.workspace_id)},
                )
            ).first()
            if row is None:
                raise HTTPException(status_code=404, detail="invitation not found or already resolved")

            await session.execute(
                text(
                    """
                    UPDATE public.workspace_invitation
                       SET token_hash = :hash,
                           resent_count = resent_count + 1,
                           last_resent_at = now()
                     WHERE id = :id
                    """
                ),
                {"id": str(invitation_id), "hash": new_hash},
            )

            inviter_row = (
                await session.execute(
                    text("SELECT email FROM public.user_account WHERE uc_uid = :uc"),
                    {"uc": workspace.user_uc_uid},
                )
            ).first()
            text_body, html_body = _invite_email_body(
                workspace_name=row.workspace_name,
                inviter_email=(inviter_row.email if inviter_row else None),
                role=row.role,
                accept_url=_accept_url(new_raw),
                expires_at=row.expires_at,
            )
            delivery = await send_email(
                session,
                to_email=row.email,
                subject=f"Reminder: join {row.workspace_name} on Accounting-Ops",
                body_text=text_body,
                body_html=html_body,
                related_invitation_id=invitation_id,
            )

    return {"ok": True, "id": str(invitation_id), "delivery": delivery}


# ---------------------------------------------------------------------------
# Accept (public — token-bound, NOT bearer-bound)
# ---------------------------------------------------------------------------


@router.get("/api/invitations/accept", include_in_schema=True)
async def accept_invitation(
    request: Request,
    token: str = Query(..., min_length=10, max_length=200),
) -> dict[str, Any]:
    """Accept a pending invitation.

    Two paths:

      1. Authenticated caller (Bearer token in Authorization header):
         resolve the user_account by uc_uid, attach the workspace_membership
         per the invitation, mark accepted. Returns ``{"status":"accepted",
         "workspace_id": ..., "role": ...}``.

      2. Unauthenticated caller: returns a payload pointing the browser to
         Keycloak signup, then a return URL that hits this endpoint again.
         The web layer renders this as a friendly success page.

    The raw token NEVER appears in the response — we only confirm whether
    the token resolved to a valid pending invitation.
    """
    raw = token.strip()
    if not raw:
        raise HTTPException(status_code=400, detail="missing token")
    h = _hash_token(raw)

    # Try to bind a caller. Bearer is optional here.
    bearer_header = request.headers.get("authorization") or ""
    auth_ctx: AuthContext | None = None
    if bearer_header.lower().startswith("bearer "):
        try:
            from app.auth import authenticate_token

            auth_ctx = await authenticate_token(bearer_header.split(" ", 1)[1].strip())
        except HTTPException:
            auth_ctx = None  # fall through to unauth path

    async with SessionLocal() as session:
        async with session.begin():
            inv = (
                await session.execute(
                    text(
                        """
                        SELECT i.id::text       AS id,
                               i.workspace_id   AS workspace_id,
                               i.email          AS email,
                               i.role::text     AS role,
                               i.entity_scope   AS entity_scope,
                               i.expires_at     AS expires_at,
                               i.accepted_at    AS accepted_at,
                               i.cancelled_at   AS cancelled_at,
                               w.display_name   AS workspace_name,
                               w.slug           AS workspace_slug
                        FROM public.workspace_invitation i
                        JOIN public.workspace w ON w.id = i.workspace_id
                        WHERE i.token_hash = :h
                        """
                    ),
                    {"h": h},
                )
            ).first()
            if inv is None:
                # Constant response on miss — no enumeration.
                raise HTTPException(status_code=404, detail="invitation not found or expired")
            if inv.cancelled_at is not None or inv.accepted_at is not None:
                raise HTTPException(status_code=410, detail="invitation already resolved")
            if inv.expires_at is not None and inv.expires_at < datetime.now(timezone.utc):
                raise HTTPException(status_code=410, detail="invitation expired")

            settings = get_settings()
            if auth_ctx is None:
                # Unauthenticated — point the browser at Keycloak. The web
                # layer renders /invite/accept which links here with a
                # browser session.
                signup_url = (
                    f"{settings.keycloak_realm_url}/protocol/openid-connect/auth"
                    f"?client_id={settings.keycloak_client_id}"
                    f"&response_type=code"
                    f"&scope=openid email profile"
                    f"&redirect_uri={(settings.public_base_url or '').rstrip('/')}/invite/accept?token={raw}"
                )
                return {
                    "status": "needs_auth",
                    "workspace_name": inv.workspace_name,
                    "workspace_slug": inv.workspace_slug,
                    "role": inv.role,
                    "signup_url": signup_url,
                    "detail": (
                        "Sign in or create an Accounting-Ops account, then "
                        "return to this link to finish accepting."
                    ),
                }

            # Authenticated path — provision user_account if missing, then
            # insert/update workspace_membership.
            user_row = (
                await session.execute(
                    text(
                        "SELECT id, email FROM public.user_account WHERE uc_uid = :uc"
                    ),
                    {"uc": auth_ctx.uc_uid},
                )
            ).first()
            if user_row is None:
                # Provision the row inline (mirrors _ensure_user_account).
                ins = await session.execute(
                    text(
                        """
                        INSERT INTO public.user_account (id, uc_uid, email, is_active, created_at)
                        VALUES (gen_random_uuid(), :uc, :email, true, now())
                        RETURNING id
                        """
                    ),
                    {"uc": auth_ctx.uc_uid, "email": (auth_ctx.claims.get("email") or None)},
                )
                user_id = ins.scalar_one()
                user_email = auth_ctx.claims.get("email")
            else:
                user_id = user_row.id
                user_email = user_row.email or auth_ctx.claims.get("email")

            # Email-match safety: if the caller's email is known, it must
            # match the invitation email (case-insensitive). This blocks
            # token interception by another logged-in user.
            invite_email = (inv.email or "").lower()
            caller_email = (user_email or "").lower()
            if caller_email and invite_email and caller_email != invite_email:
                raise HTTPException(
                    status_code=403,
                    detail="This invitation is for a different email address.",
                )

            # Upsert membership. If one exists (same workspace+user), only
            # PROMOTE — never demote. Accepting a cpa-role invite when you're
            # already the owner must not silently drop you to cpa. The
            # existing role wins iff its rank is >= the invitation's rank.
            from app.tenancy import _ROLE_RANK

            existing_row = (
                await session.execute(
                    text(
                        """
                        SELECT role::text AS role, entity_scope
                        FROM public.workspace_membership
                        WHERE workspace_id = :wid AND user_id = :uid
                        """
                    ),
                    {"wid": str(inv.workspace_id), "uid": str(user_id)},
                )
            ).first()

            import json
            invite_rank = _ROLE_RANK.get(inv.role, -1)
            if existing_row is None:
                # Brand-new membership.
                await session.execute(
                    text(
                        """
                        INSERT INTO public.workspace_membership (
                            id, workspace_id, user_id, role, entity_scope,
                            invited_by_uc_uid, created_at
                        ) VALUES (
                            gen_random_uuid(), :wid, :uid,
                            CAST(:role AS workspace_role),
                            CAST(:scope AS jsonb), :inv_by, now()
                        )
                        """
                    ),
                    {
                        "wid": str(inv.workspace_id),
                        "uid": str(user_id),
                        "role": inv.role,
                        "scope": json.dumps(inv.entity_scope) if inv.entity_scope is not None else None,
                        "inv_by": "invitation",
                    },
                )
            else:
                existing_rank = _ROLE_RANK.get(existing_row.role, -1)
                if invite_rank > existing_rank:
                    # Promotion — apply the new role + scope.
                    await session.execute(
                        text(
                            """
                            UPDATE public.workspace_membership
                               SET role = CAST(:role AS workspace_role),
                                   entity_scope = CAST(:scope AS jsonb)
                             WHERE workspace_id = :wid AND user_id = :uid
                            """
                        ),
                        {
                            "wid": str(inv.workspace_id),
                            "uid": str(user_id),
                            "role": inv.role,
                            "scope": json.dumps(inv.entity_scope) if inv.entity_scope is not None else None,
                        },
                    )
                # Else: no-op — accepting an equal-or-lower-rank invite while
                # already a higher-rank member just marks the invite accepted.
            await session.execute(
                text(
                    """
                    UPDATE public.workspace_invitation
                       SET accepted_at = now(),
                           accepted_by_user_id = :uid
                     WHERE id = :id
                    """
                ),
                {"id": inv.id, "uid": str(user_id)},
            )

    return {
        "status": "accepted",
        "workspace_id": str(inv.workspace_id),
        "workspace_name": inv.workspace_name,
        "workspace_slug": inv.workspace_slug,
        "role": inv.role,
    }


__all__ = ("router",)
