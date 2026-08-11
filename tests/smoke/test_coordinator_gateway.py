"""Pin the Tax Operations Coordinator's LLM routing contract.

Doctrine: ALL LLM calls ride the LiteLLM gateway. The coordinator historically
constructed ``anthropic.Anthropic()`` unconditionally, so its own two LLM calls
(entity sanity check + coordinator memo) bypassed the gateway even when
``--backend openai`` was passed — and failed silently once the fleet's direct
Anthropic key went invalid. These tests pin the fix.
"""
from __future__ import annotations

import argparse
import pathlib

from agents.tax_specialists.coordinator import run_coordinator as rc


def _ns(backend: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(backend=backend)


def test_backend_heuristic_matches_specialists(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CPATEAM_LLM_ENDPOINT_URL", raising=False)
    monkeypatch.delenv("LLM_ENDPOINT_URL", raising=False)
    assert rc._resolve_backend(_ns()) == "anthropic"

    monkeypatch.setenv("CPATEAM_LLM_ENDPOINT_URL", "http://gateway:4000/v1")
    assert rc._resolve_backend(_ns()) == "openai"

    # ANTHROPIC_API_KEY wins the heuristic (same precedence as specialists) …
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert rc._resolve_backend(_ns()) == "anthropic"

    # … but an explicit flag always wins.
    assert rc._resolve_backend(_ns("openai")) == "openai"


def test_single_gated_anthropic_construction():
    """The ONLY ``anthropic.Anthropic()`` construction must live inside the
    CoordinatorLLM anthropic branch — never unconditional module/run-level."""
    src = pathlib.Path(rc.__file__.replace(".pyc", ".py")).read_text(encoding="utf-8")
    assert src.count("anthropic.Anthropic()") == 1


def test_openai_path_resolves_via_cpateam_config():
    """The gateway path must resolve endpoint/model/key via the shared
    CPATEAM_LLM_* resolver (never the vision LLM_* knobs)."""
    src = pathlib.Path(rc.__file__.replace(".pyc", ".py")).read_text(encoding="utf-8")
    assert "resolve_cpateam_llm(" in src


def test_openai_runtime_needs_no_anthropic_sdk_or_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    llm = rc.CoordinatorLLM(
        "openai",
        workspace_id="00000000-0000-0000-0000-000000000000",
        endpoint_url="http://gateway:4000/v1",
        model="qwen3.6-35b",
        api_key="test-key",
    )
    assert llm.backend == "openai"
    assert llm._anthropic_client is None


def test_specialist_launcher_survives_missing_uv(monkeypatch):
    """In-container there is no ``uv``; the launcher must fall back to the
    current interpreter instead of crashing every specialist subprocess."""
    src = pathlib.Path(rc.__file__.replace(".pyc", ".py")).read_text(encoding="utf-8")
    assert 'shutil.which("uv")' in src
    assert "sys.executable" in src
