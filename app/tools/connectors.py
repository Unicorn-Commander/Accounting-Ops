"""MCP tools for the bank-connector + workspace-listing surfaces.

These tools expose the live operational state an AI needs to understand the
workspace it's working in:

Workspaces / Entities (auth-scoped discovery):
  - list_my_workspaces       — which workspaces the caller can reach
  - list_workspace_entities  — entities visible inside one workspace

Cash + transactions (live SimpleFIN-backed views):
  - get_cash_position        — current balances per account, per entity
  - list_recent_transactions — flattened transaction feed (last N days)

Credentials (metadata only — never plaintext):
  - list_credentials         — connector + label + last_used_at

Account mappings (bank account ↔ entity):
  - list_account_mappings    — live accounts + current entity mappings
  - upsert_account_mappings  — bulk upsert (accountant)

Category overrides (user-trained category corrections):
  - list_category_overrides       — viewer
  - create_category_override      — accountant

DELETE / claim / POST-credentials remain HTTP-only:
  * deleting credentials, claiming a SimpleFIN setup token, and uploading
    plaintext secrets are too sensitive to surface to an MCP caller.

Role floor: viewer for reads, accountant for upserts (mirrors HTTP).
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import text

from app.db import SessionLocal
from app.mcp_auth import authorize_workspace, current_auth
from app.tenancy import canonical_role
from app.workspace_session import workspace_session


def _b(value: Any) -> Any:
    if value is None or isinstance(value, (int, float, bool, str, dict, list)):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


# ---------------------------------------------------------------------------
# Workspace discovery
# ---------------------------------------------------------------------------


async def list_my_workspaces() -> list[dict[str, Any]]:
    """List the workspaces the current caller belongs to.

    Mirrors GET /api/me/workspaces. Returns a row per membership with:
    {id, workspace_id, slug, display_name, role, role_canonical, is_default,
     isolation_tier, hipaa_protected, entity_count, last_active_at}.

    `role` is the AO app-role; `role_canonical` is the SUITE-IDENTITY §7 suite
    role Brigade/MCP speak (federation contract: {workspace_id, slug,
    display_name, role(canonical), is_default}). `workspace_id` is the canonical
    alias of `id`. Both additions are additive — the existing keys are unchanged.

    Useful as a discovery call: an agent that just received an OAuth token can
    enumerate the workspaces it can operate against without needing to be told.

    Role floor: any authenticated caller (no membership required to ask, but
    the response only contains workspaces the caller IS a member of, and only
    those with an ACTIVE membership).
    """
    auth = current_auth()
    if auth is None:
        raise PermissionError("No authenticated caller for this MCP request")

    from app.api.workspaces import _ensure_user_account

    try:
        user_id = await _ensure_user_account(
            auth.uc_uid, autoprovision=auth.may_autoprovision
        )
    except HTTPException as exc:
        # Transport-neutral denial for the MCP channel (mirrors
        # authorize_workspace): an unprovisioned/orphaned caller identity is a
        # clean tool error, never a silent [] from a ghost-provisioned row.
        raise PermissionError(str(exc.detail)) from exc

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
                            m.is_default        AS is_default,
                            w.isolation_tier::text AS isolation_tier,
                            w.hipaa_protected   AS hipaa_protected,
                            m.last_active_at    AS last_active_at
                        FROM public.workspace_membership m
                        JOIN public.workspace w ON w.id = m.workspace_id
                        WHERE m.user_id = :user_id
                          AND m.status = 'active'
                          AND w.status = 'active'
                        ORDER BY w.display_name
                        """
                    ),
                    {"user_id": user_id},
                )
            ).mappings().all()

    workspaces: list[dict[str, Any]] = []
    for r in rows:
        entry = {k: _b(v) for k, v in dict(r).items()}
        # Canonical suite role + canonical workspace key alias (SUITE-IDENTITY
        # §7) — additive; the raw `role` + `id` keys are kept for back-compat.
        entry["role_canonical"] = canonical_role(entry["role"])
        entry["workspace_id"] = entry["id"]
        try:
            async with workspace_session(SessionLocal, entry["id"]) as ws:
                count_row = (
                    await ws.execute(
                        text("SELECT count(*) AS c FROM entity WHERE status = 'active'")
                    )
                ).first()
                entry["entity_count"] = count_row.c if count_row else 0
        except Exception:
            entry["entity_count"] = 0
        workspaces.append(entry)
    return workspaces


async def list_workspace_entities(
    workspace_id: UUID,
) -> list[dict[str, Any]]:
    """List active entities in a workspace, filtered by the caller's entity_scope.

    Mirrors GET /api/workspaces/{wid}/entities. Useful for an agent that needs
    to discover which entity_id to pass to other tools.

    Args:
        workspace_id: Workspace UUID.

    Returns:
        List of {id, slug, display_name, legal_name, kind, country_code,
                  functional_currency, hipaa_protected, status, created_at}.

    Role floor: viewer.
    """
    ctx = await authorize_workspace(workspace_id, None, min_role="viewer")
    async with workspace_session(SessionLocal, workspace_id) as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT
                        id::text            AS id,
                        slug                AS slug,
                        display_name        AS display_name,
                        legal_name          AS legal_name,
                        kind::text          AS kind,
                        country_code        AS country_code,
                        functional_currency AS functional_currency,
                        hipaa_protected     AS hipaa_protected,
                        status::text        AS status,
                        created_at
                    FROM entity
                    WHERE status = 'active'
                    ORDER BY display_name
                    """
                )
            )
        ).mappings().all()

    entities = [{k: _b(v) for k, v in dict(r).items()} for r in rows]
    if ctx.entity_scope is not None:
        allowed = {str(eid) for eid in ctx.entity_scope}
        entities = [e for e in entities if e["id"] in allowed]
    return entities


# ---------------------------------------------------------------------------
# Cash + transactions (live)
# ---------------------------------------------------------------------------


async def get_cash_position(
    workspace_id: UUID,
    entity_id: str | None = None,
) -> dict[str, Any]:
    """Return real-time account balances aggregated from SimpleFIN.

    Mirrors GET /api/workspaces/{wid}/cash-position.

    Args:
        workspace_id: Workspace UUID.
        entity_id: Optional filter — UUID string, 'consolidated', 'unmapped',
            or omitted (all accounts).

    Returns the live cash-position payload (total_usd, accounts, per-entity
    assets/liabilities, etc.). If the workspace has no SimpleFIN credential
    yet, returns {configured: false}.

    Role floor: viewer.
    """
    # Authorize via the same probe semantics as the HTTP endpoint.
    probe = entity_id if entity_id and entity_id not in ("consolidated", "unmapped") else None
    try:
        probe_uuid = UUID(probe) if probe else None
    except ValueError:
        probe_uuid = None
    workspace = await authorize_workspace(workspace_id, probe_uuid, min_role="viewer")

    from app.api.cash_position import workspace_cash_position as _http
    return await _http(
        workspace_id=workspace_id,
        entity_id=entity_id,
        workspace=workspace,
    )


async def list_recent_transactions(
    workspace_id: UUID,
    days: int = 30,
    limit: int = 50,
    entity_id: str | None = None,
) -> dict[str, Any]:
    """List the most recent live transactions (SimpleFIN + eBay if configured).

    Mirrors GET /api/workspaces/{wid}/recent-transactions. Returns a flattened
    list across all visible accounts sorted newest-first. Each row is annotated
    with the entity it maps to (via account_entity_mapping) when one exists.

    Args:
        workspace_id: Workspace UUID.
        days: Lookback window in days (default 30). The live transaction path
            is expected to cap requests at 90 days and report that cap only in
            ``errors[]``; this wrapper currently passes the requested value
            through unchanged, so verify the backing source before relying on
            a cap.
        limit: Max rows in the response (default 50).
        entity_id: Optional — UUID string, 'consolidated', 'unmapped', or omitted.

    Returns:
        {"configured": bool, "source": "simplefin+ebay_finances" | "none",
         "days_window": int, "total_count": int,
         "transactions": [...], "errors": [...]}

    Role floor: viewer.
    """
    probe = entity_id if entity_id and entity_id not in ("consolidated", "unmapped") else None
    try:
        probe_uuid = UUID(probe) if probe else None
    except ValueError:
        probe_uuid = None
    workspace = await authorize_workspace(workspace_id, probe_uuid, min_role="viewer")

    from app.api.transactions import workspace_recent_transactions as _http
    return await _http(
        workspace_id=workspace_id,
        days=days,
        limit=limit,
        entity_id=entity_id,
        workspace=workspace,
    )


# ---------------------------------------------------------------------------
# Credentials (metadata only — never plaintext)
# ---------------------------------------------------------------------------


async def list_credentials(
    workspace_id: UUID,
) -> list[dict[str, Any]]:
    """List credential metadata for a workspace (connector / label / last_used).

    Mirrors GET /api/workspaces/{wid}/credentials. Returns metadata only —
    plaintext values are NEVER exposed via MCP or HTTP. There is intentionally
    no tool to upload or delete credentials over MCP; those operations stay
    HTTP-only with explicit user auth.

    Args:
        workspace_id: Workspace UUID.

    Returns:
        List of {id, connector, label, key_version, encrypted_at,
                  last_used_at, expires_at}.

    Role floor: accountant (mirrors HTTP, which raised the floor in audit
    finding H4).
    """
    await authorize_workspace(workspace_id, None, min_role="accountant")

    from app.security.credentials import list_credentials as _list

    async with SessionLocal() as session:
        async with session.begin():
            metas = await _list(session, workspace_id)
    return [
        {
            "id": str(m.id),
            "connector": m.connector,
            "label": m.label,
            "key_version": m.key_version,
            "encrypted_at": _b(m.encrypted_at),
            "last_used_at": _b(m.last_used_at),
            "expires_at": _b(m.expires_at),
        }
        for m in metas
    ]


# ---------------------------------------------------------------------------
# Account mappings
# ---------------------------------------------------------------------------


async def list_account_mappings(
    workspace_id: UUID,
) -> dict[str, Any]:
    """Discover live bank accounts + return current entity mappings.

    Mirrors GET /api/workspaces/{wid}/account-mappings.

    Args:
        workspace_id: Workspace UUID.

    Returns:
        {"live_accounts": [...], "mappings": [...], "entities": [...],
         "unmapped_count": int}

    Role floor: viewer.
    """
    workspace = await authorize_workspace(workspace_id, None, min_role="viewer")
    from app.api.account_mappings import list_account_mappings as _http
    return await _http(workspace_id=workspace_id, workspace=workspace)


async def upsert_account_mappings(
    workspace_id: UUID,
    mappings: list[dict[str, Any]],
) -> dict[str, Any]:
    """Bulk upsert account-entity mappings.

    Mirrors PUT /api/workspaces/{wid}/account-mappings. Idempotent on
    (source, external_account_id) — a second call with the same key updates
    the row.

    Args:
        workspace_id: Workspace UUID.
        mappings: List of mapping dicts, each with:
          {"entity_id": UUID-str, "source": "simplefin"|"ebay"|...,
           "external_account_id": "...",
           "external_org": "..." | null, "external_account_name": "..." | null,
           "label": "..." | null, "notes": "..." | null,
           "auto_categorize": true | false}

    Returns:
        {"upserted": int}

    Role floor: accountant.
    """
    workspace = await authorize_workspace(workspace_id, None, min_role="accountant")

    from app.api.account_mappings import (
        AccountMappingRow,
        BulkMappingRequest,
        upsert_account_mappings as _http,
    )

    body = BulkMappingRequest(
        mappings=[AccountMappingRow(**m) for m in (mappings or [])]
    )
    return await _http(
        workspace_id=workspace_id,
        body=body,
        workspace=workspace,
    )


# ---------------------------------------------------------------------------
# Category overrides
# ---------------------------------------------------------------------------


async def list_category_overrides(
    workspace_id: UUID,
) -> dict[str, Any]:
    """List user-trained category overrides.

    Mirrors GET /api/workspaces/{wid}/category-overrides. Returns each override
    + its match_count so an agent can see which patterns are most heavily used.

    Args:
        workspace_id: Workspace UUID.

    Returns: {"overrides": [...]}

    Role floor: viewer.
    """
    await authorize_workspace(workspace_id, None, min_role="viewer")
    async with workspace_session(SessionLocal, workspace_id) as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT id::text, entity_id::text, pattern, category,
                           debit_account, credit_account, created_by_uc_uid,
                           created_at, match_count
                    FROM category_override
                    ORDER BY created_at DESC
                    """
                )
            )
        ).mappings().all()
    return {"overrides": [{k: _b(v) for k, v in dict(r).items()} for r in rows]}


async def create_category_override(
    workspace_id: UUID,
    pattern: str,
    category: str,
    entity_id: UUID | None = None,
    debit_account: str | None = None,
    credit_account: str | None = None,
) -> dict[str, Any]:
    """Create a category override (user-trained category correction).

    Mirrors POST /api/workspaces/{wid}/category-overrides. Idempotent on
    (workspace_id, entity_id, pattern); a second post with the same pattern
    updates the category instead of duplicating.

    Args:
        workspace_id: Workspace UUID.
        pattern: 2-256 chars; case-folded substring match against payee/description.
        category: 1-64 chars category key (e.g. "SaaS · AI").
        entity_id: Optional — restrict the override to one entity.
        debit_account: Optional COA canonical_form for the debit side.
        credit_account: Optional COA canonical_form for the credit side.

    Returns: {"id": "...", "pattern": "...", "category": "..."}

    Role floor: accountant.
    """
    workspace = await authorize_workspace(
        workspace_id, entity_id, min_role="accountant"
    )

    from app.api.category_overrides import (
        CategoryOverrideRequest,
        create_override as _http,
    )

    body = CategoryOverrideRequest(
        pattern=pattern,
        category=category,
        entity_id=entity_id,
        debit_account=debit_account,
        credit_account=credit_account,
    )
    return await _http(
        workspace_id=workspace_id,
        body=body,
        workspace=workspace,
    )
