from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request, status

from app.auth import AuthContext, validate_bearer_token


@dataclass(frozen=True)
class TenantContext:
    tenant_id: UUID
    user_uc_uid: str
    agent_id: str | None


async def resolve_tenant(
    request: Request,
    auth: AuthContext = Depends(validate_bearer_token),
    tenant_id_header: str | None = Header(default=None, alias="X-Tenant-ID"),
) -> TenantContext:
    raw_tenant_id = request.path_params.get("tenant_id") or tenant_id_header or auth.claims.get("tenant_id")
    if not raw_tenant_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tenant is required")
    try:
        tenant_id = UUID(str(raw_tenant_id))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tenant must be a UUID") from exc
    allowed = auth.claims.get("tenants")
    if allowed and str(tenant_id) not in allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tenant is outside token scope")
    return TenantContext(tenant_id=tenant_id, user_uc_uid=auth.uc_uid, agent_id=auth.agent_id)
