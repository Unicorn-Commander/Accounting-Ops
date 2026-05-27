"""Journal posting + chart-of-accounts admin endpoints.

  POST /api/workspaces/{wid}/coa/seed       — install default COA for all entities (idempotent)
  POST /api/workspaces/{wid}/coa/link       — link a COA account to a connected bank account
  GET  /api/workspaces/{wid}/coa            — list COA for an entity
  POST /api/workspaces/{wid}/coa            — create a new COA account
  PATCH /api/workspaces/{wid}/coa/{id}      — rename/reclassify a COA account
  DELETE /api/workspaces/{wid}/coa/{id}     — soft-deactivate a COA account
  POST /api/workspaces/{wid}/journal-entries — post a transaction to the journal
  POST /api/workspaces/{wid}/journal-entries/reverse — reversing entry
  GET  /api/workspaces/{wid}/journal-entries/verify-chain?entity_id={uuid}
  GET  /api/workspaces/{wid}/posting-log    — recent post attempts (audit feed)
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.db import SessionLocal
from app.journal import (
    PostingOutcome,
    PostingRefusal,
    canonical_form,
    post_journal_entry,
)
from app.journal.accounts import (
    DEFAULT_COA,
    link_external_account,
    normal_balance_for,
    seed_coa_for_entity,
)
from app.journal.hash_chain import chain_summary, verify_chain
from app.tenancy import WorkspaceContext, require_role, resolve_workspace
from app.workspace_session import workspace_session

router = APIRouter(tags=["journal"])


def _parse_iso_date(value: str, field: str) -> date:
    """Parse an ISO date string to a date object, 400 on malformed input."""
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field} must be an ISO date (YYYY-MM-DD): {value!r}",
        ) from exc


# ====================================================================
# Chart of accounts
# ====================================================================


class CoaLinkRequest(BaseModel):
    entity_id: UUID
    account_number: str = Field(..., min_length=4, max_length=8)
    source: str = Field(..., max_length=64)
    external_account_id: str = Field(..., max_length=256)


class CoaCreateRequest(BaseModel):
    entity_id: UUID
    account_number: str = Field(..., min_length=4, max_length=8)
    account_name: str = Field(..., min_length=1, max_length=128)
    account_type: str = Field(...)


class CoaUpdateRequest(BaseModel):
    account_name: str | None = None
    account_type: str | None = None


VALID_COA_TYPES = frozenset({
    "asset", "liability", "equity", "revenue", "cogs",
    "expense", "other_income", "other_expense", "tax",
})


@router.post("/api/workspaces/{workspace_id}/coa/seed")
async def seed_coa(
    workspace_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """Install the default chart of accounts for every active entity in this
    workspace. Idempotent — re-running on a seeded entity is a no-op.
    """
    require_role(workspace, "accountant")
    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        entities = (
            await session.execute(
                text("SELECT id::text, display_name FROM entity WHERE status = 'active'")
            )
        ).mappings().all()
        results = []
        for ent in entities:
            inserted = await seed_coa_for_entity(
                session,
                workspace_id=str(workspace.workspace_id),
                entity_id=ent["id"],
            )
            results.append(
                {
                    "entity_id": ent["id"],
                    "entity_name": ent["display_name"],
                    "accounts_inserted": inserted,
                    "status": "seeded" if inserted > 0 else "already_seeded",
                }
            )
    return {"entities": results, "template_size": len(DEFAULT_COA)}


@router.post("/api/workspaces/{workspace_id}/coa/link")
async def link_coa_account(
    workspace_id: UUID,
    body: CoaLinkRequest,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """Connect a COA account (e.g. 1010 Cash · Primary Checking) to a connected
    bank account (SimpleFIN/eBay/etc.). This links what the journal-posting
    code uses for the cash side of each entry to the right physical account.
    """
    require_role(workspace, "accountant")
    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        ok = await link_external_account(
            session,
            entity_id=str(body.entity_id),
            account_number=body.account_number,
            source=body.source,
            external_account_id=body.external_account_id,
        )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"COA account {body.account_number} not found for that entity",
        )
    return {"ok": True}


@router.get("/api/workspaces/{workspace_id}/coa")
async def list_coa(
    workspace_id: UUID,
    entity_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
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


@router.post("/api/workspaces/{workspace_id}/coa", status_code=status.HTTP_201_CREATED)
async def create_coa_account(
    workspace_id: UUID,
    body: CoaCreateRequest,
    workspace: WorkspaceContext = Depends(resolve_workspace),
):
    """Create a new chart-of-accounts entry for an entity."""
    require_role(workspace, "accountant")

    if body.account_type not in VALID_COA_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid account_type '{body.account_type}'. Must be one of: {', '.join(sorted(VALID_COA_TYPES))}",
        )

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        # Check that account_number is unique within this entity
        dup = (
            await session.execute(
                text(
                    "SELECT 1 FROM chart_of_account "
                    "WHERE entity_id = :eid AND account_number = :num"
                ),
                {"eid": str(body.entity_id), "num": body.account_number},
            )
        ).first()
        if dup:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Account number {body.account_number} already exists for this entity",
            )

        canonical = f"{body.account_number.strip()}·{body.account_name.strip()}"
        # Derive normal_balance from account_type
        nb = "debit" if body.account_type in ("asset", "expense", "cogs", "other_expense", "tax") else "credit"

        result = await session.execute(
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
                "eid": str(body.entity_id),
                "num": body.account_number,
                "name": body.account_name,
                "canonical": canonical,
                "atype": body.account_type,
                "nb": nb,
            },
        )
        row = result.mappings().one()

    return dict(row)


@router.patch("/api/workspaces/{workspace_id}/coa/{account_id}")
async def update_coa_account(
    workspace_id: UUID,
    account_id: UUID,
    body: CoaUpdateRequest,
    workspace: WorkspaceContext = Depends(resolve_workspace),
):
    """Rename and/or reclassify an existing COA account.
    Soft-deactivate (set is_active=false) is not allowed here; use DELETE endpoint.
    """
    require_role(workspace, "accountant")

    if body.account_type and body.account_type not in VALID_COA_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid account_type '{body.account_type}'. Must be one of: {', '.join(sorted(VALID_COA_TYPES))}",
        )

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        # Fetch existing account
        existing = (
            await session.execute(
                text(
                    "SELECT id::text, account_number, account_name, account_type, "
                    "is_active, is_system FROM chart_of_account "
                    "WHERE id = :aid AND entity_id IN ("
                    "  SELECT id FROM entity WHERE workspace_id = :wid"
                    ")"
                ),
                {"aid": str(account_id), "wid": str(workspace_id)},
            )
        ).mappings().first()
        if not existing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="COA account not found",
            )

        updates: dict[str, Any] = {}
        if body.account_name is not None:
            updates["account_name"] = body.account_name
        if body.account_type is not None:
            updates["account_type"] = body.account_type
            updates["normal_balance"] = (
                "debit" if body.account_type in ("asset", "expense", "cogs", "other_expense", "tax")
                else "credit"
            )

        if updates:
            # If account_name changed, update canonical_form
            new_name = updates.get("account_name", existing["account_name"])
            new_type = updates.get("account_type", existing["account_type"])
            updates["canonical_form"] = f"{existing['account_number']}·{new_name}"

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
                    "canonical": updates["canonical_form"],
                    "atype": new_type,
                    "nb": updates["normal_balance"],
                    "aid": str(account_id),
                },
            )

    return {
        "id": existing["id"],
        "account_number": existing["account_number"],
        "account_name": updates.get("account_name", existing["account_name"]),
        "account_type": updates.get("account_type", existing["account_type"]),
        "is_active": existing["is_active"],
    }


@router.delete("/api/workspaces/{workspace_id}/coa/{account_id}")
async def deactivate_coa_account(
    workspace_id: UUID,
    account_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
):
    """Soft-deactivate a COA account (set is_active=false).
    Prevents deactivation if the account has journal postings.
    """
    require_role(workspace, "accountant")

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        # Check if account has any journal entries
        postings = (
            await session.execute(
                text(
                    "SELECT count(*) FROM journal_entry "
                    "WHERE entity_id IN ("
                    "  SELECT entity_id FROM chart_of_account WHERE id = :aid"
                    ")"
                    " AND (debit_account = ("
                    "    SELECT canonical_form FROM chart_of_account WHERE id = :aid2"
                    "  ) OR credit_account = ("
                    "    SELECT canonical_form FROM chart_of_account WHERE id = :aid3"
                    "  ))"
                ),
                {"aid": str(account_id), "aid2": str(account_id), "aid3": str(account_id)},
            )
        ).scalar_one()

        if postings > 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot deactivate account with existing journal postings. Consider renaming it with a '(D)' suffix instead.",
            )

        # Soft-deactivate
        result = await session.execute(
            text(
                """
                UPDATE chart_of_account
                SET is_active = false
                WHERE id = :aid
                  AND entity_id IN (
                    SELECT id FROM entity WHERE workspace_id = :wid
                  )
                RETURNING id::text, account_number, account_name, is_active
                """
            ),
            {"aid": str(account_id), "wid": str(workspace_id)},
        )
        row = result.mappings().first()
        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="COA account not found",
            )

    return dict(row)


# ====================================================================
# Journal posting
# ====================================================================


class PostRequest(BaseModel):
    entity_id: UUID
    source: str = Field(..., max_length=64)
    source_external_id: str = Field(..., max_length=256)
    effective_date: date
    amount_cents: int = Field(..., description="Signed; positive=inflow, negative=outflow")
    description: str = Field(..., max_length=512)
    payee: str | None = None
    memo: str | None = None
    category: str = Field(..., max_length=64)
    category_status: str = Field(..., pattern="^(auto|suggest|unknown)$")
    sign_matched: bool = True
    external_account_id: str | None = None
    receipt_link_id: UUID | None = None
    user_confirmed: bool = False
    reasoning_trace: str = ""
    model_version: str = ""


@router.post("/api/workspaces/{workspace_id}/journal-entries")
async def post_entry(
    workspace_id: UUID,
    body: PostRequest,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """Atomically post a transaction to the journal.

    Returns 201 with entry_id on success; 200 with outcome='queued_for_confirm'
    or 'blocked_*' on refusal. Idempotent on (entity_id, source, source_external_id).
    """
    require_role(workspace, "accountant")

    # Check entity mapping: account_entity_mapping must connect the source+ext_id to this entity
    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        entity_mapped = False
        if body.external_account_id:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT 1 FROM account_entity_mapping
                        WHERE entity_id = :eid
                          AND source = :src
                          AND external_account_id = :ext
                        LIMIT 1
                        """
                    ),
                    {
                        "eid": str(body.entity_id),
                        "src": body.source,
                        "ext": body.external_account_id,
                    },
                )
            ).first()
            entity_mapped = row is not None

        result = await post_journal_entry(
            session,
            workspace_id=str(workspace.workspace_id),
            entity_id=str(body.entity_id),
            source=body.source,
            source_external_id=body.source_external_id,
            effective_date=body.effective_date,
            amount_cents=body.amount_cents,
            description=body.description,
            payee=body.payee,
            memo=body.memo,
            category=body.category,
            category_status=body.category_status,
            entity_mapped=entity_mapped,
            sign_matched=body.sign_matched,
            external_account_id=body.external_account_id,
            has_receipt_link=body.receipt_link_id is not None,
            receipt_link_id=body.receipt_link_id,
            posted_by_uc_uid=workspace.user_uc_uid,
            posted_by_agent_id=workspace.agent_id,
            user_confirmed=body.user_confirmed,
            reasoning_trace=body.reasoning_trace,
            model_version=body.model_version,
        )

    if isinstance(result, PostingOutcome):
        return {
            "ok": True,
            "outcome": "posted",
            "journal_entry_id": str(result.journal_entry_id),
            "materiality_tier": result.materiality.tier,
            "reason": result.materiality.reason,
        }
    # PostingRefusal
    return {
        "ok": False,
        "outcome": result.outcome,
        "reason": result.reason,
        "existing_entry_id": str(result.existing_entry_id) if result.existing_entry_id else None,
    }


@router.get("/api/workspaces/{workspace_id}/journal-entries")
async def list_entries(
    workspace_id: UUID,
    entity_id: UUID | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    category: str | None = None,
    status_filter: str | None = None,
    search: str | None = None,
    limit: int = 100,
    offset: int = 0,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """List journal entries with optional filters.

    Filters:
      - entity_id: scope to one entity
      - start_date / end_date: ISO date strings (effective_date range)
      - category: exact match on category column
      - status_filter: 'posted' | 'reversed'
      - search: substring against (payee + description + memo + debit + credit)
    """
    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        # Predicates are qualified with the `je` alias because the main query
        # JOINs `entity e`, and several column names (notably `status`) exist
        # on BOTH tables — an unqualified `status` is ambiguous and 500s. The
        # count query below uses the same `je` alias so the clause is shared.
        where = ["1=1"]
        params: dict[str, Any] = {"limit": min(limit, 500), "offset": offset}
        if entity_id:
            where.append("je.entity_id = :eid")
            params["eid"] = str(entity_id)
        if start_date:
            # Bind a real date object: asyncpg compares against the DATE column
            # natively and won't coerce a bound str (it raised
            # "'str' object has no attribute 'toordinal'").
            where.append("je.effective_date >= :start_date")
            params["start_date"] = _parse_iso_date(start_date, "start_date")
        if end_date:
            where.append("je.effective_date <= :end_date")
            params["end_date"] = _parse_iso_date(end_date, "end_date")
        if category:
            where.append("je.category = :category")
            params["category"] = category
        if status_filter:
            where.append("je.status = :status_filter")
            params["status_filter"] = status_filter
        if search:
            where.append(
                "(lower(coalesce(je.payee,'')) LIKE :s OR "
                "lower(coalesce(je.description,'')) LIKE :s OR "
                "lower(coalesce(je.memo,'')) LIKE :s OR "
                "lower(je.debit_account) LIKE :s OR "
                "lower(je.credit_account) LIKE :s)"
            )
            params["s"] = f"%{search.lower()}%"

        where_sql = " AND ".join(where)
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT je.id::text, je.entity_id::text,
                           e.display_name AS entity_display_name,
                           e.slug AS entity_slug,
                           je.posted_at, je.effective_date,
                           je.amount, je.currency_code, je.debit_account, je.credit_account,
                           je.category, je.materiality_tier, je.source, je.source_external_id,
                           je.description, je.payee, je.memo, je.status, je.identity,
                           je.approving_human_uc_uid,
                           encode(je.prev_hash, 'hex') AS prev_hash_hex,
                           encode(je.this_hash, 'hex') AS this_hash_hex
                    FROM journal_entry je
                    LEFT JOIN entity e ON e.id = je.entity_id
                    WHERE {where_sql}
                    ORDER BY je.effective_date DESC, je.posted_at DESC, je.id DESC
                    LIMIT :limit OFFSET :offset
                    """
                ),
                params,
            )
        ).mappings().all()
        total = (
            await session.execute(
                text(f"SELECT count(*) FROM journal_entry je WHERE {where_sql}"),
                {k: v for k, v in params.items() if k not in ("limit", "offset")},
            )
        ).scalar_one()

        # Distinct categories for filter dropdowns
        categories = (
            await session.execute(
                text(
                    "SELECT DISTINCT category FROM journal_entry "
                    "WHERE category IS NOT NULL ORDER BY category"
                )
            )
        ).scalars().all()

    return {
        "entries": [dict(r) for r in rows],
        "total": total,
        "categories": list(categories),
    }


@router.get("/api/workspaces/{workspace_id}/journal-entries/verify-chain")
async def verify_chain_endpoint(
    workspace_id: UUID,
    entity_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        tampered = await verify_chain(session, str(entity_id))
    return {"entity_id": str(entity_id), **chain_summary(tampered)}


@router.get("/api/workspaces/{workspace_id}/journal-entries/{entry_id}")
async def get_entry_detail(
    workspace_id: UUID,
    entry_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """Single entry with full detail + reversal lineage.

    NOTE: this dynamic /{entry_id} route MUST be declared AFTER all static
    sibling routes (e.g. /verify-chain) — FastAPI matches in declaration
    order, so a static path listed later would be swallowed by {entry_id}.
    """
    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT je.id::text, je.entity_id::text,
                           e.display_name AS entity_display_name,
                           e.slug AS entity_slug,
                           je.posted_at, je.effective_date,
                           je.amount, je.currency_code, je.debit_account, je.credit_account,
                           je.category, je.materiality_tier, je.source, je.source_external_id,
                           je.description, je.payee, je.memo, je.status, je.identity,
                           je.approving_human_uc_uid, je.reverses_entry_id::text AS reverses_entry_id,
                           je.reasoning_trace, je.model_version,
                           encode(je.prev_hash, 'hex') AS prev_hash_hex,
                           encode(je.this_hash, 'hex') AS this_hash_hex
                    FROM journal_entry je
                    LEFT JOIN entity e ON e.id = je.entity_id
                    WHERE je.id = :id
                    """
                ),
                {"id": str(entry_id)},
            )
        ).mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="Journal entry not found")
    return dict(row)


@router.get("/api/workspaces/{workspace_id}/posting-log")
async def list_posting_log(
    workspace_id: UUID,
    limit: int = 100,
    outcome: str | None = None,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        where = ["workspace_id = :wid"]
        params: dict[str, Any] = {"wid": str(workspace.workspace_id), "limit": min(limit, 500)}
        if outcome:
            where.append("outcome = :o")
            params["o"] = outcome
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT id::text, entity_id::text, source, source_external_id,
                           outcome, journal_entry_id::text, reason, actor_uc_uid,
                           actor_agent_id, amount_cents, category, created_at
                    FROM posting_log
                    WHERE {' AND '.join(where)}
                    ORDER BY created_at DESC, id DESC
                    LIMIT :limit
                    """
                ),
                params,
            )
        ).mappings().all()
    return {"entries": [dict(r) for r in rows]}
