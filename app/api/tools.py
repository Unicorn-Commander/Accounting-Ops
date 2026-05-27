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
    categorize_transaction,
    get_consolidated_p_and_l,
    get_p_and_l,
    get_runway,
    get_transaction,
    link_receipt_to_transaction,
    list_transactions,
    propose_payment,
    query_specialty_rules,
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
    require_role(ctx.workspace, "accountant")
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


@router.post("/receipts/link")
async def http_link_receipt(
    body: LinkReceiptRequest, ctx: EntityContext = Depends(resolve_entity)
) -> dict:
    require_role(ctx.workspace, "accountant")
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


@router.post("/payments/propose")
async def http_propose_payment(
    body: PaymentProposalRequest,
    ctx: EntityContext = Depends(resolve_entity),
) -> dict:
    require_role(ctx.workspace, "accountant")
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
