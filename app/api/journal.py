"""Journal posting + chart-of-accounts admin endpoints.

  POST /api/workspaces/{wid}/coa/seed       — install default COA for all entities (idempotent)
  POST /api/workspaces/{wid}/coa/link       — link a COA account to a connected bank account
  GET  /api/workspaces/{wid}/coa            — list COA for an entity
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


# ====================================================================
# Chart of accounts
# ====================================================================


class CoaLinkRequest(BaseModel):
    entity_id: UUID
    account_number: str = Field(..., min_length=4, max_length=8)
    source: str = Field(..., max_length=64)
    external_account_id: str = Field(..., max_length=256)


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
                    SELECT account_number, account_name, canonical_form,
                           account_type, normal_balance,
                           linked_external_source, linked_external_account_id,
                           is_active, is_system
                    FROM chart_of_account
                    WHERE entity_id = :eid
                    ORDER BY account_number
                    """
                ),
                {"eid": str(entity_id)},
            )
        ).mappings().all()
    return {"entity_id": str(entity_id), "accounts": [dict(r) for r in rows]}


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
    limit: int = 100,
    offset: int = 0,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        where = ["1=1"]
        params: dict[str, Any] = {"limit": min(limit, 500), "offset": offset}
        if entity_id:
            where.append("entity_id = :eid")
            params["eid"] = str(entity_id)
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT id::text, entity_id::text, posted_at, effective_date,
                           amount, currency_code, debit_account, credit_account,
                           category, materiality_tier, source, source_external_id,
                           description, payee, memo, status, identity,
                           approving_human_uc_uid,
                           encode(prev_hash, 'hex') AS prev_hash_hex,
                           encode(this_hash, 'hex') AS this_hash_hex
                    FROM journal_entry
                    WHERE {' AND '.join(where)}
                    ORDER BY posted_at DESC, id DESC
                    LIMIT :limit OFFSET :offset
                    """
                ),
                params,
            )
        ).mappings().all()
        total = (
            await session.execute(
                text(
                    "SELECT count(*) FROM journal_entry "
                    + ("WHERE entity_id = :eid" if entity_id else "")
                ),
                {"eid": str(entity_id)} if entity_id else {},
            )
        ).scalar_one()
    return {"entries": [dict(r) for r in rows], "total": total}


@router.get("/api/workspaces/{workspace_id}/journal-entries/verify-chain")
async def verify_chain_endpoint(
    workspace_id: UUID,
    entity_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        tampered = await verify_chain(session, str(entity_id))
    return {"entity_id": str(entity_id), **chain_summary(tampered)}


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
