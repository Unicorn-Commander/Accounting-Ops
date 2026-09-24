"""HTTP routes for MCP tool surfaces, workspace + entity scoped.

URL pattern:
    /api/workspaces/{workspace_id}/entities/{entity_id}/...

Workspace-level routes (no entity scoping):
    /api/workspaces/{workspace_id}/consolidated/...

All routes resolve workspace + entity context via FastAPI dependency, enforce
RBAC via require_role, then delegate to app.tools functions.
"""
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.mcp_auth import bind_http_caller
from app.tenancy import (
    EntityContext,
    WorkspaceContext,
    require_role,
    resolve_entity,
    resolve_workspace,
)
from app.tools import (
    add_specialty_rule,
    ai_categorize_pending,
    categorize_transaction,
    get_consolidated_p_and_l,
    get_p_and_l,
    get_runway,
    get_transaction,
    link_receipt_to_transaction,
    list_entity_owners,
    list_transactions,
    propose_payment,
    query_specialty_rules,
    upsert_entity_owner,
)


router = APIRouter(
    prefix="/api/workspaces/{workspace_id}/entities/{entity_id}",
    tags=["tools"],
    dependencies=[Depends(bind_http_caller)],
)

consolidated_router = APIRouter(
    prefix="/api/workspaces/{workspace_id}/consolidated",
    tags=["consolidated"],
    dependencies=[Depends(bind_http_caller)],
)


# Pydantic request bodies ----------------------------------------------------


class CategorizeRequest(BaseModel):
    category: str
    reasoning: str


class LinkReceiptRequest(BaseModel):
    receipt_id: UUID
    tx_id: UUID


class SpecialtyRuleRequest(BaseModel):
    kind: str
    params: dict[str, Any]
    reasoning: str


class RunwayRequest(BaseModel):
    scenario: dict[str, Any]


class PaymentProposalRequest(BaseModel):
    amount: Decimal
    recipient: dict[str, Any]
    reasoning: str


class ConsolidatedPLRequest(BaseModel):
    period: str
    entity_ids: list[UUID] | None = None


class EntityOwnerUpsertRequest(BaseModel):
    """Body for POST /owners. Decimal fields are STRINGS (house pattern —
    JSON floats lose precision); dates are YYYY-MM-DD strings."""

    owner_name: str
    effective_from: str
    reasoning: str
    owner_id: UUID | None = None
    owner_type: str = "individual"
    tax_id_last4: str | None = None
    effective_to: str | None = None
    shares: str | None = None
    profit_pct: str | None = None
    loss_pct: str | None = None
    capital_pct: str | None = None
    is_general_partner: bool | None = None
    is_officer: bool | None = None
    allocation_basis: str | None = None
    notes: str | None = None


def _phase_1(exc: NotImplementedError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(exc)
    )


# Read endpoints (viewer+) ---------------------------------------------------


@router.get("/transactions")
async def http_list_transactions(
    filter: str | None = None,
    ctx: EntityContext = Depends(resolve_entity),
) -> list[dict[str, Any]]:
    try:
        return await list_transactions(
            ctx.workspace.workspace_id,
            ctx.entity_id,
            {"raw": filter} if filter else None,
        )
    except NotImplementedError as exc:
        raise _phase_1(exc) from exc


@router.get("/transactions/{id}")
async def http_get_transaction(
    id: UUID, ctx: EntityContext = Depends(resolve_entity)
) -> dict[str, Any]:
    try:
        return await get_transaction(
            ctx.workspace.workspace_id, ctx.entity_id, id
        )
    except NotImplementedError as exc:
        raise _phase_1(exc) from exc


@router.get("/specialty-rules")
async def http_query_specialty_rules(
    ctx: EntityContext = Depends(resolve_entity),
) -> list[dict[str, Any]]:
    try:
        return await query_specialty_rules(
            ctx.workspace.workspace_id, ctx.entity_id
        )
    except NotImplementedError as exc:
        raise _phase_1(exc) from exc


@router.get("/owners")
async def http_list_entity_owners(
    ctx: EntityContext = Depends(resolve_entity),
) -> list[dict[str, Any]]:
    """Ownership rows (shareholders / partners) for the entity — the Schedule
    K-1 input. Co-equal with the ``list_entity_owners`` MCP tool."""
    return await list_entity_owners(ctx.workspace.workspace_id, ctx.entity_id)


@router.get("/p-and-l")
async def http_get_p_and_l(
    period: str, ctx: EntityContext = Depends(resolve_entity)
) -> dict[str, Any]:
    try:
        return await get_p_and_l(
            ctx.workspace.workspace_id, ctx.entity_id, period
        )
    except NotImplementedError as exc:
        raise _phase_1(exc) from exc


@router.post("/runway")
async def http_get_runway(
    body: RunwayRequest, ctx: EntityContext = Depends(resolve_entity)
) -> dict[str, Any]:
    try:
        return await get_runway(
            ctx.workspace.workspace_id, ctx.entity_id, body.scenario
        )
    except NotImplementedError as exc:
        raise _phase_1(exc) from exc


# Write endpoints (accountant+) ----------------------------------------------


@router.post("/transactions/{id}/categorize")
async def http_categorize_transaction(
    id: UUID,
    body: CategorizeRequest,
    ctx: EntityContext = Depends(resolve_entity),
) -> dict:
    require_role(ctx.workspace, "bookkeeper")
    try:
        return await categorize_transaction(
            ctx.workspace.workspace_id,
            ctx.entity_id,
            id,
            body.category,
            body.reasoning,
        )
    except NotImplementedError as exc:
        raise _phase_1(exc) from exc


@router.post("/approvals/ai-categorize")
async def http_ai_categorize_pending(
    limit: int = 50,
    year: int | None = None,
    dry_run: bool = False,
    ctx: EntityContext = Depends(resolve_entity),
) -> dict:
    """Human/GUI trigger for LLM Tier-2 categorization of Uncategorized pending
    approvals. Co-equal with the ``ai_categorize_pending`` MCP tool (agents call
    that one); both delegate to the same engine. Writes ``status='suggest'``
    suggestions into the review queue — never auto-posts. Pass ``dry_run=true``
    to preview the model's suggestions without writing them.
    """
    require_role(ctx.workspace, "bookkeeper")
    return await ai_categorize_pending(
        ctx.workspace.workspace_id,
        ctx.entity_id,
        limit=limit,
        year=year,
        dry_run=dry_run,
    )


@router.post("/receipts/link")
async def http_link_receipt(
    body: LinkReceiptRequest, ctx: EntityContext = Depends(resolve_entity)
) -> dict:
    require_role(ctx.workspace, "bookkeeper")
    try:
        return await link_receipt_to_transaction(
            ctx.workspace.workspace_id,
            ctx.entity_id,
            body.receipt_id,
            body.tx_id,
        )
    except NotImplementedError as exc:
        raise _phase_1(exc) from exc


@router.post("/specialty-rules")
async def http_add_specialty_rule(
    body: SpecialtyRuleRequest,
    ctx: EntityContext = Depends(resolve_entity),
) -> dict:
    require_role(ctx.workspace, "admin")
    try:
        return await add_specialty_rule(
            ctx.workspace.workspace_id,
            ctx.entity_id,
            body.kind,
            body.params,
            body.reasoning,
        )
    except NotImplementedError as exc:
        raise _phase_1(exc) from exc


@router.post("/owners")
async def http_upsert_entity_owner(
    body: EntityOwnerUpsertRequest,
    ctx: EntityContext = Depends(resolve_entity),
) -> dict:
    """Create or correct one time-sliced ownership row (the Schedule K-1
    input). Co-equal with the ``upsert_entity_owner`` MCP tool (agents call
    that one); both delegate to the same implementation, which also appends
    the ``decision_audit`` row. Validation failures map to 422.
    """
    require_role(ctx.workspace, "admin")
    try:
        return await upsert_entity_owner(
            ctx.workspace.workspace_id, ctx.entity_id, **body.model_dump()
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


@router.post("/payments/propose")
async def http_propose_payment(
    body: PaymentProposalRequest,
    ctx: EntityContext = Depends(resolve_entity),
) -> dict:
    require_role(ctx.workspace, "bookkeeper")
    try:
        return await propose_payment(
            ctx.workspace.workspace_id,
            ctx.entity_id,
            body.amount,
            body.recipient,
            body.reasoning,
        )
    except NotImplementedError as exc:
        raise _phase_1(exc) from exc


# Workspace-level (consolidated) endpoints -----------------------------------


@consolidated_router.post("/p-and-l")
async def http_get_consolidated_p_and_l(
    body: ConsolidatedPLRequest,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    require_role(workspace, "admin")
    if workspace.entity_scope is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Consolidated views require unrestricted entity scope",
        )
    try:
        return await get_consolidated_p_and_l(
            workspace.workspace_id, body.period, body.entity_ids
        )
    except NotImplementedError as exc:
        raise _phase_1(exc) from exc
