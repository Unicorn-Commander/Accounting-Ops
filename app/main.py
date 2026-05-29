from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from app.auth import AuthContext, validate_bearer_token
from app.mcp_auth import MCPAuthMiddleware
from app.a2a.server import router as a2a_router
from app.api.account_mappings import router as account_mappings_router
from app.api.approvals import router as approvals_router
from app.api.specialist_runs import router as specialist_runs_router
from app.api.tax_review import router as tax_review_router
from app.api.cash_position import router as cash_position_router
from app.api.category_overrides import router as category_overrides_router
from app.api.credentials import router as credentials_router
from app.api.journal import router as journal_router
from app.api.reports import router as reports_router
from app.api.tools import consolidated_router, router as tools_router
from app.api.transactions import router as transactions_router
from app.api.workspaces import router as workspaces_router
from app.config import get_settings
from app.otel import audit_span_middleware, setup_otel

settings = get_settings()

# Build the FastMCP "Accounting-Ops" server once. Its streamable-HTTP ASGI app
# is mounted at /mcp (below) behind MCPAuthMiddleware. The session manager must
# run for the lifetime of the process; mounted sub-apps do NOT get their own
# lifespan from Starlette, so we drive it from the parent app's lifespan (same
# pattern as accounting-ops-imap-mcp).
from mcp_server import build_server  # noqa: E402  (after settings, before app)

_mcp_server = build_server()
_mcp_http_app = (
    _mcp_server.streamable_http_app()
    if hasattr(_mcp_server, "streamable_http_app")
    else None
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    session_manager = getattr(_mcp_server, "session_manager", None)
    if session_manager is None:
        yield
        return
    async with session_manager.run():
        yield


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)
app.middleware("http")(audit_span_middleware)
setup_otel(app)
if _mcp_http_app is not None:
    # /mcp is gated by MCPAuthMiddleware (validates the uchub Bearer and binds
    # the AuthContext that every tool authorizes against). Traefik already
    # routes /mcp -> app:8000.
    app.mount("/mcp", MCPAuthMiddleware(_mcp_http_app))
app.include_router(a2a_router)
app.include_router(workspaces_router)
app.include_router(credentials_router)
app.include_router(account_mappings_router)
app.include_router(category_overrides_router)
app.include_router(journal_router)
app.include_router(approvals_router)
app.include_router(specialist_runs_router)
app.include_router(tax_review_router)
app.include_router(reports_router)
app.include_router(cash_position_router)
app.include_router(transactions_router)
app.include_router(tools_router)
app.include_router(consolidated_router)


@app.get("/healthz", tags=["system"])
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": settings.otel_service_name}


@app.get("/readyz", tags=["system"])
async def readyz() -> dict[str, str]:
    return {"status": "ready"}


@app.get("/.well-known/mcp.json", tags=["discovery"])
async def well_known_mcp() -> dict:
    return {
        "name": "Accounting-Ops",
        "version": "0.1.0",
        "mcp_endpoint": f"{settings.public_base_url}/mcp",
        "auth": {
            "type": "oauth2",
            "issuer": str(settings.keycloak_realm_url),
            "authorization_url": f"{settings.keycloak_realm_url}/protocol/openid-connect/auth",
            "token_url": f"{settings.keycloak_realm_url}/protocol/openid-connect/token",
            "scopes": ["accounting:read", "accounting:write", "accounting:admin"],
        },
        "addressing": {
            "resource_pattern": "/api/workspaces/{workspace_id}/entities/{entity_id}/...",
            "consolidated_pattern": "/api/workspaces/{workspace_id}/consolidated/...",
        },
    }


@app.get("/api/me", tags=["auth"])
async def me(auth: AuthContext = Depends(validate_bearer_token)) -> dict:
    return {
        "uc_uid": auth.uc_uid,
        "agent_id": auth.agent_id,
        "scopes": auth.scopes,
    }
