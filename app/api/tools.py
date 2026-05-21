from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.tenancy import TenantContext, resolve_tenant
from app.tools import (
    add_specialty_rule,
    categorize_transaction,
    get_p_and_l,
    get_runway,
    get_transaction,
    link_receipt_to_transaction,
    list_transactions,
    propose_payment,
    query_specialty_rules,
)

router = APIRouter(prefix="/api/tenants/{tenant_id}", tags=["tools"])


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


def phase_1(exc: NotImplementedError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(exc))


@router.get("/transactions")
async def http_list_transactions(
    tenant_id: UUID,
    filter: str | None = None,
    tenant: TenantContext = Depends(resolve_tenant),
) -> list[dict[str, Any]]:
    try:
        return await list_transactions(tenant.tenant_id, {"raw": filter} if filter else None)
    except NotImplementedError as exc:
        raise phase_1(exc) from exc


@router.get("/transactions/{id}")
async def http_get_transaction(
    tenant_id: UUID,
    id: UUID,
    tenant: TenantContext = Depends(resolve_tenant),
) -> dict[str, Any]:
    try:
        return await get_transaction(tenant.tenant_id, id)
    except NotImplementedError as exc:
        raise phase_1(exc) from exc


@router.post("/transactions/{id}/categorize")
async def http_categorize_transaction(
    tenant_id: UUID,
    id: UUID,
    body: CategorizeRequest,
    tenant: TenantContext = Depends(resolve_tenant),
) -> dict:
    try:
        return await categorize_transaction(tenant.tenant_id, id, body.category, body.reasoning)
    except NotImplementedError as exc:
        raise phase_1(exc) from exc


@router.post("/receipts/link")
async def http_link_receipt(
    tenant_id: UUID,
    body: LinkReceiptRequest,
    tenant: TenantContext = Depends(resolve_tenant),
) -> dict:
    try:
        return await link_receipt_to_transaction(tenant.tenant_id, body.receipt_id, body.tx_id)
    except NotImplementedError as exc:
        raise phase_1(exc) from exc


@router.get("/specialty-rules")
async def http_query_specialty_rules(
    tenant_id: UUID,
    tenant: TenantContext = Depends(resolve_tenant),
) -> list[dict[str, Any]]:
    try:
        return await query_specialty_rules(tenant.tenant_id)
    except NotImplementedError as exc:
        raise phase_1(exc) from exc


@router.post("/specialty-rules")
async def http_add_specialty_rule(
    tenant_id: UUID,
    body: SpecialtyRuleRequest,
    tenant: TenantContext = Depends(resolve_tenant),
) -> dict:
    try:
        return await add_specialty_rule(tenant.tenant_id, body.kind, body.params, body.reasoning)
    except NotImplementedError as exc:
        raise phase_1(exc) from exc


@router.get("/p-and-l")
async def http_get_p_and_l(
    tenant_id: UUID,
    entity_id: UUID,
    period: str,
    tenant: TenantContext = Depends(resolve_tenant),
) -> dict[str, Any]:
    try:
        return await get_p_and_l(tenant.tenant_id, entity_id, period)
    except NotImplementedError as exc:
        raise phase_1(exc) from exc


@router.post("/runway")
async def http_get_runway(
    tenant_id: UUID,
    body: RunwayRequest,
    tenant: TenantContext = Depends(resolve_tenant),
) -> dict[str, Any]:
    try:
        return await get_runway(tenant.tenant_id, body.scenario)
    except NotImplementedError as exc:
        raise phase_1(exc) from exc


@router.post("/payments/propose")
async def http_propose_payment(
    tenant_id: UUID,
    body: PaymentProposalRequest,
    tenant: TenantContext = Depends(resolve_tenant),
) -> dict:
    try:
        return await propose_payment(tenant.tenant_id, body.amount, body.recipient, body.reasoning)
    except NotImplementedError as exc:
        raise phase_1(exc) from exc
