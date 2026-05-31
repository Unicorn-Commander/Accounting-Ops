from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from app.auth import AuthContext, validate_bearer_token
from app.mcp_auth import MCPAuthMiddleware
from app.a2a.server import router as a2a_router
from app.api.account_mappings import router as account_mappings_router
from app.api.ai_activity import router as ai_activity_router
from app.api.approvals import router as approvals_router
from app.api.approval_clusters import router as approval_clusters_router
from app.api.approval_recommendations import router as approval_recommendations_router
from app.api.chat import router as chat_router
from app.api.discovery import router as discovery_router
from app.api.ebay_oauth import router as ebay_oauth_router
from app.api.irs_forms import router as irs_forms_router
from app.api.open_items import router as open_items_router
from app.api.specialist_runs import router as specialist_runs_router
from app.api.specialist_pdf import router as specialist_pdf_router
from app.api.tax_knowledge import router as tax_knowledge_router
from app.api.tax_review import router as tax_review_router
from app.api.tax_narrative import router as tax_narrative_router
from app.api.tax_status import router as tax_status_router
from app.api.tax_filing_submissions import router as tax_filing_submissions_router
from app.api.usps_tracking import router as usps_tracking_router
from app.api.cash_position import router as cash_position_router
from app.api.category_overrides import router as category_overrides_router
from app.api.clients import router as clients_router
from app.api.credentials import router as credentials_router
from app.api.journal import router as journal_router
from app.api.members import router as members_router
from app.api.reports import router as reports_router
from app.api.setup import router as setup_router
from app.api.test_brigade_jwt import router as test_brigade_jwt_router
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
app.include_router(members_router)
app.include_router(clients_router)
app.include_router(credentials_router)
app.include_router(account_mappings_router)
app.include_router(category_overrides_router)
app.include_router(journal_router)
app.include_router(approvals_router)
app.include_router(approval_clusters_router)
app.include_router(approval_recommendations_router)
app.include_router(chat_router)
app.include_router(specialist_runs_router)
app.include_router(specialist_pdf_router)
app.include_router(irs_forms_router)
app.include_router(tax_narrative_router)
app.include_router(tax_review_router)
app.include_router(tax_status_router)
app.include_router(tax_filing_submissions_router)
app.include_router(usps_tracking_router)
app.include_router(tax_knowledge_router)
app.include_router(open_items_router)
app.include_router(ai_activity_router)
app.include_router(reports_router)
app.include_router(setup_router)
app.include_router(cash_position_router)
app.include_router(transactions_router)
app.include_router(tools_router)
app.include_router(consolidated_router)
# eBay Finances OAuth (callback is unauthenticated — security via signed state token)
app.include_router(ebay_oauth_router)
# Discovery (no auth — Brigade federation manifest at /.well-known/mcps.json)
app.include_router(discovery_router)
# Brigade JWT verifier test endpoint (admin-only; used by Brigade to confirm
# our verifier is wired correctly before pushing real federated traffic).
app.include_router(test_brigade_jwt_router)


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
