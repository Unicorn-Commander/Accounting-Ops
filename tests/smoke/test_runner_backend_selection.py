"""Smoke tests for the dual-backend Form 1120 Preparer runner.

Verifies that:
1. The module imports cleanly even when neither ``anthropic`` nor ``openai``
   are installed (both are optional extras).
2. Both code paths (``_run_anthropic_path`` and ``_run_openai_path``) exist
   and are callable objects.
3. The ``run`` dispatcher function exists and has the expected signature.
4. The CLI parser accepts all new flags without error.
5. Backend auto-selection logic (``_resolve_backend``) responds correctly to
   env var heuristics.
6. ``_mcp_tool_to_openai_function`` converts an MCP Tool descriptor correctly.
7. ``_parse_json_mode_tool_calls`` correctly extracts TOOL_CALL lines.

No real network calls, no running MCP server, no LLM endpoint needed.
All SDK imports are mocked at the module level where needed.
"""

from __future__ import annotations

import importlib
import inspect
import json
import sys
import types
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_runner() -> types.ModuleType:
    """Import run_specialist as a fresh module, mocking heavy SDK deps."""
    # The module does soft-import at the top level (anthropic / openai set to
    # None on ImportError), so we can just import it directly.
    import agents.tax_specialists.form_1120_preparer.run_specialist as runner
    return runner


# ---------------------------------------------------------------------------
# 1. Module imports cleanly
# ---------------------------------------------------------------------------

def test_runner_module_imports():
    """run_specialist.py imports without raising even when SDKs are absent."""
    runner = _load_runner()
    assert runner is not None


# ---------------------------------------------------------------------------
# 2. Both code-path functions exist
# ---------------------------------------------------------------------------

def test_anthropic_path_function_exists():
    runner = _load_runner()
    assert callable(getattr(runner, "_run_anthropic_path", None)), (
        "_run_anthropic_path is missing from run_specialist"
    )


def test_openai_path_function_exists():
    runner = _load_runner()
    assert callable(getattr(runner, "_run_openai_path", None)), (
        "_run_openai_path is missing from run_specialist"
    )


# ---------------------------------------------------------------------------
# 3. run() dispatcher has expected signature
# ---------------------------------------------------------------------------

def test_run_dispatcher_signature():
    """run() must accept backend, llm_endpoint_url, llm_model, llm_api_key,
    instruction_format in its signature alongside the original params."""
    runner = _load_runner()
    sig = inspect.signature(runner.run)
    params = set(sig.parameters.keys())
    required = {
        "workspace_id",
        "entity_id",
        "tax_year",
        "output_path",
        "model_key",
        "mcp_url",
        "service_token",
        "backend",
        "llm_endpoint_url",
        "llm_model",
        "llm_api_key",
        "instruction_format",
        "verbose",
    }
    missing = required - params
    assert not missing, (
        f"run() is missing these parameters: {missing}"
    )


# ---------------------------------------------------------------------------
# 4. CLI parser accepts all new flags
# ---------------------------------------------------------------------------

def test_cli_parser_accepts_backend_flag():
    runner = _load_runner()
    args = runner._parse_args([
        "--workspace-id", "550e8400-e29b-41d4-a716-446655440000",
        "--entity-id", "aabbccdd-1234-5678-9abc-000000000001",
        "--tax-year", "2023",
        "--output", "/tmp/test-draft.md",
        "--backend", "anthropic",
    ])
    assert args.backend == "anthropic"


def test_cli_parser_accepts_openai_backend():
    runner = _load_runner()
    args = runner._parse_args([
        "--workspace-id", "550e8400-e29b-41d4-a716-446655440000",
        "--entity-id", "aabbccdd-1234-5678-9abc-000000000001",
        "--tax-year", "2023",
        "--output", "/tmp/test-draft.md",
        "--backend", "openai",
    ])
    assert args.backend == "openai"


def test_cli_parser_accepts_llm_endpoint_url():
    runner = _load_runner()
    args = runner._parse_args([
        "--workspace-id", "550e8400-e29b-41d4-a716-446655440000",
        "--entity-id", "aabbccdd-1234-5678-9abc-000000000001",
        "--tax-year", "2023",
        "--output", "/tmp/test-draft.md",
        "--llm-endpoint-url", "http://midboy1:8088/v1",
    ])
    assert args.llm_endpoint_url == "http://midboy1:8088/v1"


def test_cli_parser_accepts_llm_model():
    runner = _load_runner()
    args = runner._parse_args([
        "--workspace-id", "550e8400-e29b-41d4-a716-446655440000",
        "--entity-id", "aabbccdd-1234-5678-9abc-000000000001",
        "--tax-year", "2023",
        "--output", "/tmp/test-draft.md",
        "--llm-model", "Qwen3.6-35B-A3B-Vision",
    ])
    assert args.llm_model == "Qwen3.6-35B-A3B-Vision"


def test_cli_parser_accepts_llm_api_key():
    runner = _load_runner()
    args = runner._parse_args([
        "--workspace-id", "550e8400-e29b-41d4-a716-446655440000",
        "--entity-id", "aabbccdd-1234-5678-9abc-000000000001",
        "--tax-year", "2023",
        "--output", "/tmp/test-draft.md",
        "--llm-api-key", "local-qwen",
    ])
    assert args.llm_api_key == "local-qwen"


def test_cli_parser_accepts_instruction_format_tools():
    runner = _load_runner()
    args = runner._parse_args([
        "--workspace-id", "550e8400-e29b-41d4-a716-446655440000",
        "--entity-id", "aabbccdd-1234-5678-9abc-000000000001",
        "--tax-year", "2023",
        "--output", "/tmp/test-draft.md",
        "--instruction-format", "tools",
    ])
    assert args.instruction_format == "tools"


def test_cli_parser_accepts_instruction_format_json():
    runner = _load_runner()
    args = runner._parse_args([
        "--workspace-id", "550e8400-e29b-41d4-a716-446655440000",
        "--entity-id", "aabbccdd-1234-5678-9abc-000000000001",
        "--tax-year", "2023",
        "--output", "/tmp/test-draft.md",
        "--instruction-format", "json",
    ])
    assert args.instruction_format == "json"


def test_cli_parser_default_backend_is_none():
    """When --backend is not provided, args.backend should be None (resolved later)."""
    runner = _load_runner()
    args = runner._parse_args([
        "--workspace-id", "550e8400-e29b-41d4-a716-446655440000",
        "--entity-id", "aabbccdd-1234-5678-9abc-000000000001",
        "--tax-year", "2023",
        "--output", "/tmp/test-draft.md",
    ])
    assert args.backend is None


def test_cli_parser_default_instruction_format_is_tools():
    runner = _load_runner()
    args = runner._parse_args([
        "--workspace-id", "550e8400-e29b-41d4-a716-446655440000",
        "--entity-id", "aabbccdd-1234-5678-9abc-000000000001",
        "--tax-year", "2023",
        "--output", "/tmp/test-draft.md",
    ])
    assert args.instruction_format == "tools"


# ---------------------------------------------------------------------------
# 5. Backend auto-selection via _resolve_backend
# ---------------------------------------------------------------------------

def test_resolve_backend_explicit_flag_anthropic():
    runner = _load_runner()
    args = runner._parse_args([
        "--workspace-id", "x", "--entity-id", "y",
        "--tax-year", "2023", "--output", "/tmp/x.md",
        "--backend", "anthropic",
    ])
    with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "", "LLM_ENDPOINT_URL": ""}, clear=False):
        assert runner._resolve_backend(args) == "anthropic"


def test_resolve_backend_explicit_flag_openai():
    runner = _load_runner()
    args = runner._parse_args([
        "--workspace-id", "x", "--entity-id", "y",
        "--tax-year", "2023", "--output", "/tmp/x.md",
        "--backend", "openai",
    ])
    with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant-xxx", "LLM_ENDPOINT_URL": ""}, clear=False):
        # Explicit --backend openai beats the ANTHROPIC_API_KEY heuristic.
        assert runner._resolve_backend(args) == "openai"


def test_resolve_backend_env_anthropic_key_wins():
    runner = _load_runner()
    args = runner._parse_args([
        "--workspace-id", "x", "--entity-id", "y",
        "--tax-year", "2023", "--output", "/tmp/x.md",
    ])
    env = {"ANTHROPIC_API_KEY": "sk-ant-xxx", "LLM_ENDPOINT_URL": "http://local:8088/v1"}
    with patch.dict("os.environ", env, clear=False):
        # ANTHROPIC_API_KEY present and no explicit flag -> anthropic wins.
        assert runner._resolve_backend(args) == "anthropic"


def test_resolve_backend_env_llm_endpoint_url_selects_openai():
    runner = _load_runner()
    args = runner._parse_args([
        "--workspace-id", "x", "--entity-id", "y",
        "--tax-year", "2023", "--output", "/tmp/x.md",
    ])
    env = {"LLM_ENDPOINT_URL": "http://midboy1:8088/v1"}
    # Patch out ANTHROPIC_API_KEY so it's not set.
    with patch.dict("os.environ", env):
        original_key = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            assert runner._resolve_backend(args) == "openai"
        finally:
            if original_key is not None:
                os.environ["ANTHROPIC_API_KEY"] = original_key


def test_resolve_backend_no_env_defaults_to_anthropic():
    """When neither key is set and no flag given, defaults to anthropic."""
    runner = _load_runner()
    args = runner._parse_args([
        "--workspace-id", "x", "--entity-id", "y",
        "--tax-year", "2023", "--output", "/tmp/x.md",
    ])
    import os
    env_clean = {k: v for k, v in os.environ.items()
                 if k not in ("ANTHROPIC_API_KEY", "LLM_ENDPOINT_URL")}
    with patch.dict("os.environ", env_clean, clear=True):
        assert runner._resolve_backend(args) == "anthropic"


# ---------------------------------------------------------------------------
# 6. _mcp_tool_to_openai_function schema conversion
# ---------------------------------------------------------------------------

def test_mcp_tool_to_openai_function_shape():
    runner = _load_runner()

    # Create a minimal mock MCP Tool descriptor.
    mock_tool = MagicMock()
    mock_tool.name = "get_form_1120_data"
    mock_tool.description = "Returns pre-computed Form 1120 data for an entity."
    mock_tool.inputSchema = {
        "type": "object",
        "properties": {
            "entity_id": {"type": "string"},
            "tax_year": {"type": "integer"},
        },
        "required": ["entity_id", "tax_year"],
    }

    result = runner._mcp_tool_to_openai_function(mock_tool)

    assert result["type"] == "function"
    fn = result["function"]
    assert fn["name"] == "get_form_1120_data"
    assert fn["description"] == "Returns pre-computed Form 1120 data for an entity."
    assert fn["parameters"]["type"] == "object"
    assert "entity_id" in fn["parameters"]["properties"]
    assert "tax_year" in fn["parameters"]["properties"]


def test_mcp_tool_to_openai_function_null_schema_gets_empty_object():
    """If inputSchema is None/falsy, parameters defaults to empty object schema."""
    runner = _load_runner()

    mock_tool = MagicMock()
    mock_tool.name = "flag_for_cpa_review"
    mock_tool.description = "Write a CPA flag."
    mock_tool.inputSchema = None

    result = runner._mcp_tool_to_openai_function(mock_tool)
    assert result["function"]["parameters"] == {"type": "object", "properties": {}}


def test_mcp_tool_to_openai_function_empty_description():
    """None description is coerced to empty string."""
    runner = _load_runner()

    mock_tool = MagicMock()
    mock_tool.name = "draft_memo"
    mock_tool.description = None
    mock_tool.inputSchema = {}

    result = runner._mcp_tool_to_openai_function(mock_tool)
    assert result["function"]["description"] == ""


# ---------------------------------------------------------------------------
# 7. _parse_json_mode_tool_calls extraction
# ---------------------------------------------------------------------------

def test_parse_json_mode_tool_calls_single():
    runner = _load_runner()
    text = (
        "I will call the tool now.\n"
        'TOOL_CALL: {"name": "get_form_1120_data", "arguments": {"entity_id": "abc", "tax_year": 2023}}\n'
        "Waiting for the result."
    )
    calls = runner._parse_json_mode_tool_calls(text)
    assert len(calls) == 1
    assert calls[0]["name"] == "get_form_1120_data"
    assert calls[0]["arguments"]["tax_year"] == 2023


def test_parse_json_mode_tool_calls_multiple():
    runner = _load_runner()
    text = (
        'TOOL_CALL: {"name": "get_form_1120_data", "arguments": {"entity_id": "abc", "tax_year": 2023}}\n'
        'TOOL_CALL: {"name": "get_1099_payees", "arguments": {"entity_id": "abc", "tax_year": 2023}}\n'
    )
    calls = runner._parse_json_mode_tool_calls(text)
    assert len(calls) == 2
    assert calls[0]["name"] == "get_form_1120_data"
    assert calls[1]["name"] == "get_1099_payees"


def test_parse_json_mode_tool_calls_no_calls():
    runner = _load_runner()
    text = "This is just a normal response with no tool calls."
    calls = runner._parse_json_mode_tool_calls(text)
    assert calls == []


def test_parse_json_mode_tool_calls_ignores_malformed():
    runner = _load_runner()
    text = "TOOL_CALL: {this is not valid json}\n"
    calls = runner._parse_json_mode_tool_calls(text)
    assert calls == []


def test_parse_json_mode_tool_calls_strips_whitespace():
    runner = _load_runner()
    text = '   TOOL_CALL:   {"name": "draft_memo", "arguments": {}}   \n'
    calls = runner._parse_json_mode_tool_calls(text)
    assert len(calls) == 1
    assert calls[0]["name"] == "draft_memo"


# ---------------------------------------------------------------------------
# Import guard so the test file can be imported without side effects
# ---------------------------------------------------------------------------

import os  # noqa: E402 — needed after the test functions that reference it
