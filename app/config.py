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

    falkordb_host: str = "192.168.10.10"
    falkordb_port: int = 6379
    garage_s3_endpoint: str = "http://garage:3900"
    garage_bucket_prefix: str = "accounting-ops"

    otel_exporter_otlp_endpoint: str | None = None
    otel_service_name: str = "accounting-ops"

    qwen_local_url: str = "http://midboy1:8000/v1"
    qwen_vlm_url: str = "http://midboy1:8087/v1"
    # Default workspace+entity for single-tenant test deployments; production
    # always resolves these per-request from JWT/path.
    default_workspace_id: str | None = None
    default_entity_id: str | None = None

    # ---- LLM Categorizer (Tier-2 fallback) ----
    # All fields are env-only — no API key ever in code.
    #
    # The endpoint is OpenAI-compatible (/v1/chat/completions). Point at:
    #   - Local Ollama / llama.cpp / vLLM (no key needed, llm_api_key="")
    #   - OpenRouter / OpenAI / Anthropic proxy (set LLM_API_KEY in the environment)
    #
    # Default targets the local Qwen3.6-35B on midboy1's P40 GPU pair.
    llm_endpoint_url: str = "http://100.121.80.175:8088/v1"
    llm_model: str = "Qwen3.6-35B-A3B-Vision"
    llm_api_key: str = ""          # blank = local/no-auth; set in env for cloud
    llm_timeout_s: int = 30

    # ---- CPA Export service ----
    # URL of the accounting-ops-cpa-export sidecar. Defaults to the docker DNS
    # name used in the compose stack; override with CPA_EXPORT_URL in production.
    cpa_export_url: str = "http://accounting-ops-cpa-export:8001"
    # 512 gives the JSON response room even if thinking suppression is unavailable.
    # With chat_template_kwargs enable_thinking=False (sent by the categorizer),
    # the actual output is ~150 tokens; the higher ceiling is a safety net for
    # endpoints that ignore the thinking-suppression param.
    llm_max_tokens: int = 512


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
        falkordb_host=os.getenv("FALKORDB_HOST", defaults.falkordb_host),
        falkordb_port=int(os.getenv("FALKORDB_PORT", str(defaults.falkordb_port))),
        garage_s3_endpoint=os.getenv("GARAGE_S3_ENDPOINT", defaults.garage_s3_endpoint),
        garage_bucket_prefix=os.getenv("GARAGE_BUCKET_PREFIX", defaults.garage_bucket_prefix),
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
        cpa_export_url=os.getenv("CPA_EXPORT_URL", defaults.cpa_export_url),
    )
