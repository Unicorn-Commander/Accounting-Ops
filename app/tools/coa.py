"""MCP tools for the chart of accounts (read + create + update).

Read tool ``list_chart_of_accounts`` lets an agent inspect the COA before
posting or recategorizing. Write tools ``create_coa_account`` and
``update_coa_account`` mirror the HTTP endpoints in app/api/journal.py with the
same role floor (accountant) and validations.

DELETE (deactivate) is intentionally NOT exposed via MCP — it's a destructive
admin path that should stay HTTP-only with explicit user intent. Same with
``coa/seed`` and ``coa/link`` (one-time provisioning operations).
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import text

from app.authz import assert_entity_in_scope
from app.db import SessionLocal
from app.mcp_auth import authorize_workspace
from app.workspace_session import workspace_session


_VALID_COA_TYPES = frozenset({
    "asset", "liability", "equity", "revenue", "cogs",
    "expense", "other_income", "other_expense", "tax",
})


async def list_chart_of_accounts(
    workspace_id: UUID,
    entity_id: UUID,
) -> dict[str, Any]:
    """List the chart of accounts for one entity.

    Returns every COA row (active and inactive) so an agent can see the full
    structure. Filter on ``is_active`` client-side if needed.

    Args:
        workspace_id: Workspace UUID.
        entity_id: Entity UUID.

    Returns:
        {
          "entity_id": "<uuid>",
          "accounts": [
            {
              "id": "<uuid>", "account_number": "1010",
              "account_name": "Cash · Primary Checking",
              "canonical_form": "1010·Cash · Primary Checking",
              "account_type": "asset", "normal_balance": "debit",
              "linked_external_source": "simplefin" | null,
              "linked_external_account_id": "..." | null,
              "is_active": true, "is_system": true, "notes": "..."
            },
            ...
          ]
        }

    Role floor: viewer.
    """
    await authorize_workspace(workspace_id, entity_id, min_role="viewer")
    async with workspace_session(SessionLocal, workspace_id) as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT id::text, account_number, account_name, canonical_form,
                           account_type, normal_balance,
                           linked_external_source, linked_external_account_id,
                           is_active, is_system, notes
                    FROM chart_of_account
                    WHERE entity_id = :eid
                    ORDER BY account_number
                    """
                ),
                {"eid": str(entity_id)},
            )
        ).mappings().all()
    return {"entity_id": str(entity_id), "accounts": [dict(r) for r in rows]}


async def create_coa_account(
    workspace_id: UUID,
    entity_id: UUID,
    account_number: str,
    account_name: str,
    account_type: str,
) -> dict[str, Any]:
    """Create a new chart-of-accounts entry for an entity.

    Mirrors POST /api/workspaces/{wid}/coa. Account_number must be unique
    within the entity. normal_balance is derived from account_type using the
    same rule the HTTP route uses (asset/expense/cogs/other_expense/tax -> debit,
    everything else -> credit).

    Args:
        workspace_id: Workspace UUID.
        entity_id: Entity UUID.
        account_number: 4-8 chars (e.g. "1010", "6210").
        account_name: 1-128 chars human label (e.g. "SaaS · AI").
        account_type: One of: asset, liability, equity, revenue, cogs, expense,
            other_income, other_expense, tax.

    Returns:
        {"id": "<uuid>", "account_number": "...", "account_name": "...",
         "canonical_form": "...", "account_type": "...", "is_active": true}

    Role floor: accountant.

    Raises:
        ValueError on invalid account_type or duplicate account_number.
    """
    await authorize_workspace(workspace_id, entity_id, min_role="accountant")

    account_number = str(account_number).strip()
    account_name = str(account_name).strip()
    account_type = str(account_type).strip().lower()

    if not (4 <= len(account_number) <= 8):
        raise ValueError("account_number must be 4-8 characters")
    if not (1 <= len(account_name) <= 128):
        raise ValueError("account_name must be 1-128 characters")
    if account_type not in _VALID_COA_TYPES:
        raise ValueError(
            f"account_type must be one of {sorted(_VALID_COA_TYPES)}; got {account_type!r}"
        )

    canonical = f"{account_number}·{account_name}"
    nb = (
        "debit"
        if account_type in ("asset", "expense", "cogs", "other_expense", "tax")
        else "credit"
    )

    async with workspace_session(SessionLocal, workspace_id) as session:
        # Check duplicates within this entity
        dup = (
            await session.execute(
                text(
                    "SELECT 1 FROM chart_of_account "
                    "WHERE entity_id = :eid AND account_number = :num"
                ),
                {"eid": str(entity_id), "num": account_number},
            )
        ).first()
        if dup:
            raise ValueError(
                f"Account number {account_number} already exists for this entity"
            )

        row = (
            await session.execute(
                text(
                    """
                    INSERT INTO chart_of_account (
                        workspace_id, entity_id, account_number, account_name,
                        canonical_form, account_type, normal_balance,
                        is_system, is_active
                    ) VALUES (
                        :wid, :eid, :num, :name, :canonical, :atype, :nb,
                        false, true
                    )
                    RETURNING id::text, account_number, account_name, canonical_form,
                              account_type, is_active
                    """
                ),
                {
                    "wid": str(workspace_id),
                    "eid": str(entity_id),
                    "num": account_number,
                    "name": account_name,
                    "canonical": canonical,
                    "atype": account_type,
                    "nb": nb,
                },
            )
        ).mappings().one()

    return dict(row)


async def update_coa_account(
    workspace_id: UUID,
    account_id: UUID,
    account_name: str | None = None,
    account_type: str | None = None,
) -> dict[str, Any]:
    """Rename / reclassify an existing COA account.

    Mirrors PATCH /api/workspaces/{wid}/coa/{id}. At least one of account_name
    or account_type must be provided. Soft-deactivation is not allowed here;
    use the HTTP DELETE endpoint with explicit user attestation.

    Args:
        workspace_id: Workspace UUID.
        account_id: COA account UUID.
        account_name: Optional new label.
        account_type: Optional new type (see _VALID_COA_TYPES).

    Returns the updated row projection.

    Role floor: accountant.

    Audit 2026-05-31 Bug 2: previously this authorized the workspace with
    entity_id=None and then updated any account in the workspace, letting a
    scoped CPA mutate another entity's COA by guessing/learning an account UUID.
    Now we load the account's entity_id FIRST, then enforce entity_scope before
    the UPDATE — matching the HTTP PATCH route's post-fix behavior.
    """
    # Authorize at the workspace level first; we'll re-check the entity once
    # we know which one this account belongs to.
    ctx = await authorize_workspace(workspace_id, None, min_role="accountant")

    if account_name is None and account_type is None:
        raise ValueError("at least one of account_name or account_type required")

    if account_type is not None:
        account_type = str(account_type).strip().lower()
        if account_type not in _VALID_COA_TYPES:
            raise ValueError(
                f"account_type must be one of {sorted(_VALID_COA_TYPES)}; got {account_type!r}"
            )

    async with workspace_session(SessionLocal, workspace_id) as session:
        existing = (
            await session.execute(
                text(
                    "SELECT id::text, entity_id::text AS entity_id, account_number, "
                    "account_name, account_type, is_active, is_system "
                    "FROM chart_of_account "
                    "WHERE id = :aid AND entity_id IN ("
                    "  SELECT id FROM entity WHERE workspace_id = :wid"
                    ")"
                ),
                {"aid": str(account_id), "wid": str(workspace_id)},
            )
        ).mappings().first()
        if not existing:
            raise LookupError(f"COA account {account_id} not found")
        # Audit 2026-05-31 Bug 2: enforce entity_scope on the LOADED entity_id.
        # ``assert_entity_in_scope`` raises HTTPException(403) on out-of-scope;
        # translate to a transport-neutral PermissionError so the MCP channel
        # returns a clean tool error (matches mcp_auth.authorize_workspace).
        try:
            assert_entity_in_scope(ctx, UUID(existing["entity_id"]))
        except HTTPException as exc:
            raise PermissionError(str(exc.detail)) from exc

        new_name = (
            account_name.strip()
            if account_name is not None
            else existing["account_name"]
        )
        new_type = account_type or existing["account_type"]
        new_canonical = f"{existing['account_number']}·{new_name}"
        new_nb = (
            "debit"
            if new_type in ("asset", "expense", "cogs", "other_expense", "tax")
            else "credit"
        )

        await session.execute(
            text(
                """
                UPDATE chart_of_account
                SET account_name = :name, canonical_form = :canonical,
                    account_type = :atype, normal_balance = :nb
                WHERE id = :aid
                """
            ),
            {
                "name": new_name,
                "canonical": new_canonical,
                "atype": new_type,
                "nb": new_nb,
                "aid": str(account_id),
            },
        )

    return {
        "id": existing["id"],
        "account_number": existing["account_number"],
        "account_name": new_name,
        "account_type": new_type,
        "canonical_form": new_canonical,
        "is_active": existing["is_active"],
    }
