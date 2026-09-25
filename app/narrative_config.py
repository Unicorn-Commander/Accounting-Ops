"""Shared configuration and child environment for CPA narrative generation."""
from __future__ import annotations

import os
from dataclasses import dataclass

from app.config import get_settings


@dataclass(frozen=True)
class NarrativeLLMConfig:
    endpoint_url: str
    model: str
    api_key: str


def resolve_narrative_llm() -> NarrativeLLMConfig:
    """Resolve the dedicated narrative settings, never the receipt-VLM knobs."""
    settings = get_settings()
    return NarrativeLLMConfig(
        endpoint_url=settings.narrative_llm_endpoint_url,
        model=settings.narrative_llm_model,
        api_key=settings.narrative_llm_api_key or os.environ.get("OPENROUTER_API_KEY", ""),
    )


_NARRATIVE_ENV_ALLOWLIST = (
    "PATH", "HOME", "LANG", "LC_ALL", "TZ", "PYTHONPATH", "PYTHONUNBUFFERED",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
)


def narrative_child_env(
    *, service_token: str, mcp_url: str, config: NarrativeLLMConfig,
) -> dict[str, str]:
    """Build the allow-listed environment used by the narrative generator."""
    env = {k: os.environ[k] for k in _NARRATIVE_ENV_ALLOWLIST if k in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["PYTHONUNBUFFERED"] = "1"
    env.update(
        ACCTOPS_MCP_URL=mcp_url,
        ACCTOPS_SERVICE_TOKEN=service_token,
        NARRATIVE_LLM_ENDPOINT_URL=config.endpoint_url,
        NARRATIVE_LLM_MODEL=config.model,
        NARRATIVE_LLM_API_KEY=config.api_key,
    )
    return env
