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
from app.api.stripe_oauth import router as stripe_oauth_router
from app.api.irs_forms import router as irs_forms_router
from app.api.open_items import router as open_items_router
from app.api.specialist_runs import router as specialist_runs_router
from app.api.specialist_pdf import router as specialist_pdf_router
from app.api.specialty_rules import router as specialty_rules_router
from app.api.tax_knowledge import router as tax_knowledge_router
from app.api.tax_review import router as tax_review_router
from app.api.tax_narrative import router as tax_narrative_router
from app.api.tax_status import router as tax_status_router
from app.api.tax_filing_submissions import router as tax_filing_submissions_router
from app.api.usps_tracking import router as usps_tracking_router
from app.api.cash_position import router as cash_position_router
from app.api.cash_history import router as cash_history_router
from app.api.lineage import router as lineage_router
from app.api.cfo_brief import router as cfo_brief_router
from app.api.category_overrides import router as category_overrides_router
from app.api.clients import router as clients_router
from app.api.contacts import router as contacts_router
from app.api.work_inbox import router as work_inbox_router
from app.api.credentials import router as credentials_router
from app.api.documents import router as documents_router
from app.api.federation import router as federation_router
from app.api.inference_settings import router as inference_settings_router
from app.api.invoices import router as invoices_router
from app.api.journal import router as journal_router
from app.api.members import router as members_router
from app.api.metrics import router as metrics_router
from app.api.personal_access_tokens import router as personal_access_tokens_router
from app.api.reports import router as reports_router
from app.api.sentinel import router as sentinel_router
from app.api.submission import router as submission_router
from app.api.engagement import router as engagement_router
from app.api.work_products import router as work_products_router
from app.api.onboarding import router as onboarding_router
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
    # Brigade outbound (consumer) client. Safe no-op when BRIGADE_BASE_URL
    # is unset (the default until commander wires the hub up). When set,
    # this fires the first discovery + starts a background refresh loop.
    shutdown_brigade_client = None
    try:
        from app.security.brigade_client import (
            init_brigade_client,
            shutdown_brigade_client as _shutdown_brigade,
        )

        await init_brigade_client()
        shutdown_brigade_client = _shutdown_brigade
    except Exception:  # noqa: BLE001 - brigade client must never block startup
        pass

    try:
        session_manager = getattr(_mcp_server, "session_manager", None)
        if session_manager is None:
            yield
            return
        async with session_manager.run():
            yield
    finally:
        if shutdown_brigade_client is not None:
            try:
                await shutdown_brigade_client()
            except Exception:  # noqa: BLE001
                pass


app = FastAPI(
    title=settings.app_name,
    version="0.7.1",
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
app.include_router(onboarding_router)
app.include_router(members_router)
app.include_router(clients_router)
app.include_router(contacts_router)
# Cross-client CPA "work inbox" (read-only): one merged, stalest-first list of
# OPEN actionable items (pending approvals + open tax-review) across every
# workspace the caller can act in. Aggregator only — touches no ledger table.
app.include_router(work_inbox_router)
app.include_router(credentials_router)
app.include_router(documents_router)
app.include_router(account_mappings_router)
app.include_router(category_overrides_router)
app.include_router(journal_router)
app.include_router(invoices_router)
app.include_router(approvals_router)
app.include_router(approval_clusters_router)
app.include_router(approval_recommendations_router)
# Read-only accuracy metrics (AI-decision agreement rate / review-vs-redo KPI).
app.include_router(metrics_router)
app.include_router(chat_router)
app.include_router(specialist_runs_router)
app.include_router(specialist_pdf_router)
app.include_router(specialty_rules_router)
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
# Cross-app federation reads (M2M): Tax-Planning-Ops pulls document_extraction
# over the shared-KC trusted-caller rail. Mirrors the P&L federation read; uses
# resolve_workspace_for_federation (viewer floor, HIPAA-refused), no membership.
app.include_router(federation_router)
app.include_router(setup_router)
app.include_router(cash_position_router)
app.include_router(cash_history_router)
app.include_router(lineage_router)
app.include_router(cfo_brief_router)
app.include_router(transactions_router)
app.include_router(sentinel_router)
app.include_router(submission_router)
app.include_router(engagement_router)
app.include_router(work_products_router)
app.include_router(personal_access_tokens_router)
# BYOK inference settings (additive — status/store/remove a workspace's own LLM
# key; the chat path reads it via workspace_llm_keys.get_byok, untouched here).
app.include_router(inference_settings_router)
app.include_router(tools_router)
app.include_router(consolidated_router)
# chat router was already included earlier; do not double-register
# eBay Finances OAuth (callback is unauthenticated — security via signed state token)
app.include_router(ebay_oauth_router)
# Stripe API key + webhook (no OAuth; webhook is unauthenticated — security via HMAC signature verification)
app.include_router(stripe_oauth_router)
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


@app.get("/.well-known/mcps.json", tags=["discovery"])
async def well_known_mcps() -> dict:
    """Brigade-ingest manifest + PAT mint advertising.

    Brigade polls this URL and re-publishes our tools as ``account.<name>``
    in the aggregated catalog at brigade.unicorncommander.ai. The richer
    programmatic version lives in ``app/api/discovery.py``; this fallback
    publishes the curated tool list + the PAT mint flow so external AI
    clients (Claude Desktop, Cursor, etc.) can mint a bearer without
    leaving the user's app.

    The ``pat.mint_url`` + ``pat.prefix`` ARE the contract the
    /settings/pats UI delivers on.
    """
    tools_curated = [
        "analyze_pending_approvals",
        "list_transactions", "get_transaction",
        "categorize_transaction", "link_receipt_to_transaction",
        "query_specialty_rules", "add_specialty_rule",
        "get_p_and_l", "get_runway", "get_consolidated_p_and_l",
        "get_current_rule", "get_form_1120_data",
        "get_period_journal", "get_1099_payees",
        "draft_memo", "flag_for_cpa_review",
        "propose_payment_tool",
    ]
    return {
        "schema_version": "1.0",
        "app_id": "accounting-ops",
        "name": "Accounting-Ops",
        "version": "0.1.0",
        "description": (
            "Agent-first financial operations for Magic Unicorn — journal, "
            "approvals, tax review queue, runway, P&L, 1120 working data."
        ),
        "mcp_endpoint": f"{settings.public_base_url}/mcp",
        "tool_namespace": "account",
        "auth": {
            "type": "oauth2",
            "issuer": str(settings.keycloak_realm_url),
            "audience": settings.accepted_audience,
            "token_url": f"{settings.keycloak_realm_url}/protocol/openid-connect/token",
            "scopes": [
                "accounting:read",
                "accounting:write",
                "accounting:admin",
                "brigade:cross_app",
            ],
            "pat": {
                "prefix": "aops_pat_",
                "mint_url": f"{settings.public_base_url}/settings/pats",
            },
            "federated_auth": {
                "trusted_issuer": "https://brigade.unicorncommander.ai",
                "expected_audience": "accounting-ops",
                "jwks_url": "https://brigade.unicorncommander.ai/.well-known/jwks.json",
            },
        },
        "tools": [
            {"name": n, "description": "", "input_schema": {"type": "object"}}
            for n in tools_curated
        ],
        "tool_count": len(tools_curated),
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
