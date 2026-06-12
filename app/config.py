from functools import lru_cache
import os

from pydantic import BaseModel


class Settings(BaseModel):
    app_name: str = "Accounting-Ops"
    public_base_url: str = "https://accounting.magicunicorn.dev"
    # Runtime DB URL. In production this is the LEAST-PRIVILEGE role
    # (accounting_ops_app): SELECT/INSERT broadly, UPDATE only on mutable
    # tables, NO ownership, NO ability to ALTER/DISABLE triggers, TRUNCATE,
    # or UPDATE/DELETE the append-only ledger tables. See migrations
    # tenant/0005 + public/0003 and DEPLOY.md "DB roles".
    database_url: str = "postgresql+asyncpg://accounting_ops:accounting_ops@localhost/accounting_ops"
    # DDL/owner DB URL used ONLY by migrations + workspace provisioning
    # (CREATE SCHEMA, alembic upgrade). This is the table-OWNING role
    # (accounting_ops). Falls back to database_url when unset so a
    # single-role deployment (e.g. the integration stack pre-split, or local
    # dev) keeps working unchanged.
    admin_database_url: str | None = None

    keycloak_realm_url: str = "https://auth.unicorncommander.ai/realms/uchub"
    keycloak_client_id: str = "accounting-ops-app"
    keycloak_client_secret: str | None = None
    # OAuth resource-server audience this app validates incoming bearer tokens
    # against (finding H3). A uchub token is accepted iff this value appears in
    # the token's `aud` (string or list) OR under `resource_access` — so tokens
    # minted by OTHER uchub clients (agents, Brigade) are accepted when they
    # carry this app's resource id via a Keycloak Audience mapper. Defaults to
    # keycloak_client_id (the app's own client id) when ACCEPTED_AUDIENCE unset.
    accepted_audience: str = "accounting-ops-app"

    # Contact-Ops federation (pull a user's contacts → AO counterparties).
    # AO obtains an on-behalf-of token via uchub RFC-8693 token-exchange, then
    # calls the Contact-Ops MCP. ``contact_ops_audience`` is the audience the
    # exchanged token must carry (must match what Contact-Ops validates). The
    # exchange uses contact_ops_client_id/secret if set, else the app's own
    # keycloak_client_id/secret. Sync is DISABLED (endpoint returns a clear
    # 503) until a client secret + audience are present — go-live is gated on
    # the uchub client registration + tenant alignment, not on this code.
    contact_ops_mcp_url: str = "https://mcp.contacts.magicunicorn.dev/mcp"
    contact_ops_audience: str = "contact-ops-mcp"
    contact_ops_client_id: str | None = None
    contact_ops_client_secret: str | None = None

    falkordb_host: str = "192.168.10.10"
    falkordb_port: int = 6379
    falkordb_password: str = ""  # set via FALKORDB_PASSWORD; "" = connect without AUTH
    garage_s3_endpoint: str = "http://garage:3900"
    garage_bucket_prefix: str = "accounting-ops"
    # S3 (Garage) access credentials for the document-upload path. These are the
    # ONLY S3 secrets the app holds; they are env-only (never in code) and used
    # solely to PUT uploaded documents into the workspace's Garage bucket. Blank
    # = uploads are disabled (the endpoint returns a clear 503 rather than
    # silently dropping the file). region matches garage.toml's s3_region.
    garage_s3_access_key: str = ""
    garage_s3_secret_key: str = ""
    garage_s3_region: str = "garage"
    # Hard ceiling on a single uploaded document (bytes). Defends the app + the
    # object store from a giant-file DoS. 25 MB comfortably covers a multi-page
    # PDF receipt or a high-res phone photo; CSV statements are far smaller.
    document_upload_max_bytes: int = 25 * 1024 * 1024

    otel_exporter_otlp_endpoint: str | None = None
    otel_service_name: str = "accounting-ops"

    qwen_local_url: str = "http://midboy1:8000/v1"
    qwen_vlm_url: str = "http://midboy1:8087/v1"
    # Default workspace+entity for single-tenant test deployments; production
    # always resolves these per-request from JWT/path.
    default_workspace_id: str | None = None
    default_entity_id: str | None = None

    # ---- LLM Categorizer / Receipt-VLM ----
    # All fields are env-only — no API key ever in code.
    #
    # Two deployment modes (both env-overridable via LLM_ENDPOINT_URL / LLM_MODEL /
    # LLM_API_KEY):
    #
    #   STANDALONE (default) — direct to the local llama.cpp / vLLM server:
    #     LLM_ENDPOINT_URL = http://100.121.80.175:8088/v1   (midboy1)
    #     LLM_MODEL        = Qwen3.6-35B-A3B-Vision-p40      (P40 backend — image-safe)
    #     LLM_API_KEY      = (blank — no auth on local server)
    #
    #   SUITE / GATEWAY — through the UC LiteLLM gateway (load-balances P40+3090,
    #     no-think default, per-org metering):
    #     LLM_ENDPOINT_URL = http://<gateway-host>:4000/v1
    #     LLM_MODEL        = qwen3.6-vision
    #     LLM_API_KEY      = <virtual-key>
    #
    # ⚠️  Do NOT default to "Qwen3.6-35B-A3B-Vision" (the RTX-6000 alias): it
    # OOM-crashes on image input and returns 502. The P40 alias below is the safe
    # standalone default for receipt OCR. Override via LLM_MODEL for the gateway.
    llm_endpoint_url: str = "http://100.121.80.175:8088/v1"
    llm_model: str = "Qwen3.6-35B-A3B-Vision-p40"
    llm_api_key: str = ""          # blank = local/no-auth; set in env for cloud
    llm_timeout_s: int = 30

    # ---- bbox-oracle (traditional OCR cross-check) ----
    # URL of the bbox-oracle service.  POST {url}/ocr with multipart field "file"
    # (PNG/JPG) → {"text": "...", "lines": [{"text", "bbox", "score"}, ...]}.
    #
    # Two deployment modes (both env-overridable via OCR_BBOX_SERVICE_URL):
    #
    #   STANDALONE (default) — direct to the live bbox-oracle on bigboy:
    #     OCR_BBOX_SERVICE_URL = http://100.126.207.157:8097
    #
    #   SUITE / GATEWAY — point at whatever host exposes the oracle internally.
    #     OCR_BBOX_SERVICE_URL = http://ocr-oracle.internal:8097
    #
    # Set to an empty string ("") to disable the cross-check tier entirely; the
    # VLM-only result is returned without the geometric reconcile pass.
    ocr_bbox_service_url: str = "http://100.126.207.157:8097"

    # ---- CPA Export service ----
    # URL of the accounting-ops-cpa-export sidecar. Defaults to the docker DNS
    # name used in the compose stack; override with CPA_EXPORT_URL in production.
    cpa_export_url: str = "http://accounting-ops-cpa-export:8001"
    # 512 gives the JSON response room even if thinking suppression is unavailable.
    # With chat_template_kwargs enable_thinking=False (sent by the categorizer),
    # the actual output is ~150 tokens; the higher ceiling is a safety net for
    # endpoints that ignore the thinking-suppression param.
    llm_max_tokens: int = 512

    # ---- Chat/agent inference gateway (UC LiteLLM) ----
    # SEPARATE from the categorizer block above. The chat/CFO/specialist path
    # routes frontier inference through the UC LiteLLM gateway (per-tenant virtual
    # keys + Lago metering) ONLY when route_chat_through_gateway is enabled.
    # DEFAULT OFF ⇒ the chat path behaves exactly as today (OpenRouter-direct).
    # See docs/INFERENCE-AND-BILLING.md + agents/chat/inference_config.py.
    llm_gateway_base_url: str = "https://llm.unicorncommander.ai"
    # SCOPED admin key (NOT the gateway master) used only to mint per-workspace
    # virtual keys. Blank ⇒ minting disabled (the platform key / OpenRouter
    # fallback still work).
    llm_gateway_admin_key: str = ""
    # The deployment's own gateway virtual key (e.g. the internal magicunicorn.dev
    # ring). Used when a workspace has no BYOK + no per-workspace virtual key.
    # Blank ⇒ no platform key (fall through to OpenRouter).
    llm_platform_virtual_key: str = ""
    # MASTER feature flag. False ⇒ chat path is OpenRouter-direct (today's
    # behavior, byte-identical). True ⇒ route frontier inference through the
    # gateway per the resolver precedence (BYOK > platform/UC-credit > OpenRouter).
    route_chat_through_gateway: bool = False

    # ---- UC Ops-Center (platform billing/entitlement control plane) ----
    # Ops-Center owns subscribe/pay (Stripe) + org identity + per-app entitlement
    # (org_features/tier_features). AO READS entitlement + links out for plan
    # management; it NEVER duplicates billing or mutates Lago/Stripe. See
    # docs/INFERENCE-AND-BILLING.md.
    ops_center_base_url: str = "https://unicorncommander.ai"
    # Deep-link target where a user manages their plan / billing (Ops-Center
    # portal — Stripe lives there, behind the shared uchub SSO session).
    ops_center_billing_url: str = "https://unicorncommander.ai/billing"

    # ---- Server-to-server (M2M) trusted callers ----
    # Space-separated list of Keycloak client IDs (azp claim values) that are
    # permitted to call federation read endpoints without a workspace membership
    # row. These are Keycloak client-credentials-grant callers (machine-to-machine,
    # no end user). They MUST still present a valid uchub JWT with
    # aud=accounting-ops-app; the azp value is the additional gate.
    #
    # Access is restricted to READ-ONLY operations on specific endpoints. Each
    # endpoint that accepts trusted M2M callers explicitly opts in via the
    # resolve_workspace_for_federation dependency (not the default resolve_workspace).
    #
    # Example .env:
    #   ACCTOPS_TRUSTED_M2M_CALLERS=tax-planning-ops
    trusted_m2m_callers: frozenset[str] = frozenset()


def get_admin_database_url() -> str:
    """DB URL for DDL/owner operations (migrations + provisioning).

    Prefers ADMIN_DATABASE_URL (the table-owning `accounting_ops` role);
    falls back to the runtime DATABASE_URL when unset so single-role setups
    are unaffected.
    """
    s = get_settings()
    return s.admin_database_url or s.database_url


@lru_cache
def get_settings() -> Settings:
    defaults = Settings()
    # Resolve client_id first so accepted_audience can default to the EFFECTIVE
    # client id (env-overridden or not), not the static class default.
    keycloak_client_id = os.getenv("KEYCLOAK_CLIENT_ID", defaults.keycloak_client_id)
    return Settings(
        public_base_url=os.getenv("PUBLIC_BASE_URL", defaults.public_base_url),
        database_url=os.getenv("DATABASE_URL", defaults.database_url),
        admin_database_url=os.getenv("ADMIN_DATABASE_URL"),
        keycloak_realm_url=os.getenv("KEYCLOAK_REALM_URL", defaults.keycloak_realm_url),
        keycloak_client_id=keycloak_client_id,
        keycloak_client_secret=os.getenv("KEYCLOAK_CLIENT_SECRET"),
        accepted_audience=os.getenv("ACCEPTED_AUDIENCE", keycloak_client_id),
        contact_ops_mcp_url=os.getenv("CONTACT_OPS_MCP_URL", defaults.contact_ops_mcp_url),
        contact_ops_audience=os.getenv("CONTACT_OPS_AUDIENCE", defaults.contact_ops_audience),
        contact_ops_client_id=os.getenv("CONTACT_OPS_CLIENT_ID"),
        contact_ops_client_secret=os.getenv("CONTACT_OPS_CLIENT_SECRET"),
        falkordb_host=os.getenv("FALKORDB_HOST", defaults.falkordb_host),
        falkordb_port=int(os.getenv("FALKORDB_PORT", str(defaults.falkordb_port))),
        falkordb_password=os.getenv("FALKORDB_PASSWORD", defaults.falkordb_password),
        garage_s3_endpoint=os.getenv("GARAGE_S3_ENDPOINT", defaults.garage_s3_endpoint),
        garage_bucket_prefix=os.getenv("GARAGE_BUCKET_PREFIX", defaults.garage_bucket_prefix),
        garage_s3_access_key=os.getenv("GARAGE_S3_ACCESS_KEY", defaults.garage_s3_access_key),
        garage_s3_secret_key=os.getenv("GARAGE_S3_SECRET_KEY", defaults.garage_s3_secret_key),
        garage_s3_region=os.getenv("GARAGE_S3_REGION", defaults.garage_s3_region),
        document_upload_max_bytes=int(
            os.getenv("DOCUMENT_UPLOAD_MAX_BYTES", str(defaults.document_upload_max_bytes))
        ),
        otel_exporter_otlp_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"),
        otel_service_name=os.getenv("OTEL_SERVICE_NAME", defaults.otel_service_name),
        qwen_local_url=os.getenv("QWEN_LOCAL_URL", defaults.qwen_local_url),
        qwen_vlm_url=os.getenv("QWEN_VLM_URL", defaults.qwen_vlm_url),
        default_workspace_id=os.getenv("DEFAULT_WORKSPACE_ID") or os.getenv("TENANT_ID"),
        default_entity_id=os.getenv("DEFAULT_ENTITY_ID"),
        llm_endpoint_url=os.getenv("LLM_ENDPOINT_URL", defaults.llm_endpoint_url),
        llm_model=os.getenv("LLM_MODEL", defaults.llm_model),
        llm_api_key=os.getenv("LLM_API_KEY", defaults.llm_api_key),
        llm_timeout_s=int(os.getenv("LLM_TIMEOUT_S", str(defaults.llm_timeout_s))),
        llm_max_tokens=int(os.getenv("LLM_MAX_TOKENS", str(defaults.llm_max_tokens))),
        ocr_bbox_service_url=os.getenv("OCR_BBOX_SERVICE_URL", defaults.ocr_bbox_service_url),
        cpa_export_url=os.getenv("CPA_EXPORT_URL", defaults.cpa_export_url),
        llm_gateway_base_url=os.getenv(
            "LLM_GATEWAY_BASE_URL", defaults.llm_gateway_base_url
        ),
        llm_gateway_admin_key=os.getenv("LLM_GATEWAY_ADMIN_KEY", ""),
        llm_platform_virtual_key=os.getenv("LLM_PLATFORM_VIRTUAL_KEY", ""),
        route_chat_through_gateway=os.getenv(
            "AO_ROUTE_CHAT_THROUGH_GATEWAY", ""
        ).strip().lower() in ("1", "true", "yes", "on"),
        ops_center_base_url=os.getenv(
            "OPS_CENTER_BASE_URL", defaults.ops_center_base_url
        ),
        ops_center_billing_url=os.getenv(
            "OPS_CENTER_BILLING_URL", defaults.ops_center_billing_url
        ),
        trusted_m2m_callers=frozenset(
            c.strip()
            for c in os.getenv("ACCTOPS_TRUSTED_M2M_CALLERS", "").split()
            if c.strip()
        ),
    )
