"""Smoke checks for the narrative trigger's config and process boundary."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import app.api.tax_narrative as http_narrative
import app.tools.tax_narrative as mcp_narrative


def test_http_and_mcp_use_the_same_narrative_resolver(monkeypatch):
    config = SimpleNamespace(
        narrative_llm_endpoint_url="https://narrative.example/v1",
        narrative_llm_model="narrative-model",
        narrative_llm_api_key="configured-key",
    )
    monkeypatch.setattr("app.narrative_config.get_settings", lambda: config)

    assert http_narrative.resolve_narrative_llm() == mcp_narrative.resolve_narrative_llm()
    assert (
        http_narrative.resolve_narrative_llm().endpoint_url
        == config.narrative_llm_endpoint_url
    )
    assert http_narrative.resolve_narrative_llm().model == config.narrative_llm_model


def test_narrative_child_receives_key_only_in_allowlisted_env(monkeypatch):
    captured: dict[str, object] = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_spawn(*cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs["env"]
        return FakeProcess()

    monkeypatch.setattr(http_narrative.asyncio, "create_subprocess_exec", fake_spawn)
    key = "fake-narrative-key"
    asyncio.run(
        http_narrative._run_narrative_generator(
            "workspace", "entity", 2024, "service-token", "http://mcp",
            "http://llm", "model", key, "run",
        )
    )

    cmd = captured["cmd"]
    env = captured["env"]
    assert key not in cmd
    assert env["NARRATIVE_LLM_API_KEY"] == key
    allowed = set(http_narrative.narrative_child_env.__globals__["_NARRATIVE_ENV_ALLOWLIST"])
    allowed.update(
        {
            "ACCTOPS_MCP_URL", "ACCTOPS_SERVICE_TOKEN",
            "NARRATIVE_LLM_ENDPOINT_URL", "NARRATIVE_LLM_MODEL",
            "NARRATIVE_LLM_API_KEY",
        }
    )
    assert set(env) <= allowed
