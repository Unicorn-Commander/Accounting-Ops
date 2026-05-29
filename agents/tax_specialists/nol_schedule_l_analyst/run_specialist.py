#!/usr/bin/env python3
"""NOL Carryforward + Schedule L Reconstruction Analyst — CLI runner.

Invokes the NOL + Schedule L Analyst specialist against the Accounting-Ops /mcp
endpoint. Supports two backends:

- **anthropic** (default when ``ANTHROPIC_API_KEY`` is set): uses the
  Anthropic SDK's native MCP client integration (``beta.messages.create``
  with ``mcp_servers``). No local tool-call loop needed.

- **openai** (default when ``LLM_ENDPOINT_URL`` is set): uses the OpenAI
  Python SDK (>= 1.0) aimed at any OpenAI-compatible local endpoint (e.g.
  llama-server / Qwen3.6-35B-A3B-Vision on midboy1:8088). Implements a
  manual MCP tool-call loop because local endpoints have no native MCP
  integration.

Usage — Anthropic path::

    export ANTHROPIC_API_KEY="sk-ant-..."
    export ACCTOPS_SERVICE_TOKEN="<keycloak-service-token>"

    uv run python agents/tax_specialists/nol_schedule_l_analyst/run_specialist.py \\
        --workspace-id <UUID> \\
        --entity-id    <UUID> \\
        --tax-year     2025 \\
        --output       ./drafts/mu-inc-2025-nol-schedule-l.md \\
        --model        sonnet

Usage — local Qwen path::

    export LLM_ENDPOINT_URL="http://midboy1:8088/v1"
    export LLM_MODEL="Qwen3.6-35B-A3B-Vision"
    export LLM_API_KEY="local-qwen"
    export ACCTOPS_SERVICE_TOKEN="<keycloak-service-token>"

    uv run python agents/tax_specialists/nol_schedule_l_analyst/run_specialist.py \\
        --backend      openai \\
        --workspace-id <UUID> \\
        --entity-id    <UUID> \\
        --tax-year     2025 \\
        --output       ./drafts/mu-inc-2025-nol-schedule-l.md

Environment variables:

    ACCTOPS_SERVICE_TOKEN   Bearer token with at least "viewer" scope on the
                            workspace. Required.
    ACCTOPS_MCP_URL         Override the MCP endpoint base URL.
                            Default: http://localhost:8000/mcp
    ANTHROPIC_API_KEY       Anthropic API key. Required for --backend anthropic.
    LLM_ENDPOINT_URL        Base URL for OpenAI-compatible endpoint.
                            Default: http://localhost:8088/v1
    LLM_MODEL               Model name served by the local endpoint.
                            Default: Qwen3.6-35B-A3B-Vision
    LLM_API_KEY             API key for the local endpoint (often a placeholder).
                            Default: local-qwen

Notes:

- Both paths share the same system prompt, worked examples, and MCP server
  target — only the model client differs.
- The OpenAI path implements a manual MCP tool-call loop: connect via
  ``mcp.client.streamable_http``, list tools, convert to OpenAI function
  schema, run the LLM, dispatch tool calls, loop until text-only response
  or the iteration cap (40) is hit. The cap is higher than the 1120 Preparer
  (30) because this specialist calls get_form_1120_data for multiple years.
- Local models that advertise function-calling support use it automatically;
  use ``--instruction-format json`` for models without native tool support
  (injects a structured JSON instruction into the system prompt instead).
- The user message explicitly instructs the model to retrieve data for all
  prior years needed to build the NOL stack, then reconstruct Schedule L.
- Output directory is created automatically if it does not exist.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import textwrap
from datetime import date
from typing import Any

# Bootstrap repo root so we can import shared runner utilities even when this
# script is invoked directly (uv run python agents/...).
_HERE = pathlib.Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from agents._shared.api_keys import HELP_TEXT_LLM_API_KEY, resolve_llm_api_key
from agents._shared.limits import MAX_OUTPUT_TOKENS

# ---------------------------------------------------------------------------
# Soft dependency checks — deferred to the backend that actually needs them
# ---------------------------------------------------------------------------

try:
    import anthropic
except ImportError:
    anthropic = None  # type: ignore[assignment]

try:
    import openai as _openai_module
except ImportError:
    _openai_module = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_MCP_URL = "http://localhost:8000/mcp"
_DEFAULT_LLM_ENDPOINT_URL = "http://localhost:8088/v1"
_DEFAULT_LLM_MODEL = "Qwen3.6-35B-A3B-Vision"
_DEFAULT_LLM_API_KEY = "local-qwen"

_MODEL_MAP = {
    "sonnet": "claude-sonnet-4-6",
    "opus":   "claude-opus-4-7",
}
_SYSTEM_PROMPT_PATH = pathlib.Path(__file__).parent / "system_prompt.md"

# MCP tool names this specialist is allowed to call.
# Matches the tools documented in system_prompt.md.
# Intentionally narrower than the full MCP server surface —
# no get_1099_payees, no propose_payment, no write tools.
_ALLOWED_TOOLS: list[str] = [
    "get_form_1120_data",
    "get_period_journal",
    "draft_memo",
    "flag_for_cpa_review",
]

# Maximum tool-call iterations in the OpenAI manual loop.
# Higher than Form 1120 Preparer (30) because this specialist calls
# get_form_1120_data for multiple years AND get_period_journal for each year.
_MAX_LOOP_ITERATIONS = 40

# JSON-mode system prompt suffix (same pattern as Form 1120 Preparer).
_JSON_FALLBACK_SUFFIX = textwrap.dedent("""

---

## TOOL CALL FORMAT (JSON mode — no native function calling available)

Because this model does not support OpenAI function-calling natively, you
MUST express every tool call as a JSON object on its own line inside your
response, prefixed with ``TOOL_CALL:``.  Wait for a ``TOOL_RESULT:`` line
before continuing.

Format::

    TOOL_CALL: {"name": "<tool_name>", "arguments": {<...>}}

After each tool result is shown to you as::

    TOOL_RESULT: {"tool": "<tool_name>", "result": <...>}

...continue your reasoning and emit the next TOOL_CALL or the final Markdown
draft when all tool calls are done.

Never emit ``TOOL_CALL`` lines and prose simultaneously in the same message.
""")


# ---------------------------------------------------------------------------
# Helpers — shared by both backends
# ---------------------------------------------------------------------------


def _load_system_prompt() -> str:
    """Read system_prompt.md from disk (same directory as this script)."""
    if not _SYSTEM_PROMPT_PATH.exists():
        raise FileNotFoundError(
            f"System prompt not found at {_SYSTEM_PROMPT_PATH}. "
            "Ensure you are running from the repo root or that the "
            "agents/tax_specialists/nol_schedule_l_analyst/ directory is intact."
        )
    return _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


def _build_user_message(
    workspace_id: str,
    entity_id: str,
    tax_year: int,
) -> str:
    """Construct the user-turn message sent to the NOL + Schedule L Analyst."""
    return textwrap.dedent(f"""\
        Please compute the NOL carryforward stack and reconstruct Schedule L for
        the following entity and tax year.

        workspace_id : {workspace_id}
        entity_id    : {entity_id}
        tax_year     : {tax_year}  (requested year — compute NOL stack back to entity formation)
        run_date     : {date.today().isoformat()}

        Follow the call sequence in your system prompt exactly:

        1. get_form_1120_data(entity_id, {tax_year})  — requested year first.
        2. get_form_1120_data(entity_id, <prior_year>) — repeat for each prior year
           needed for the NOL stack, back to the entity's first year of operations.
           Stop when you reach a year with no data (document as gap and flag) or a
           year that fully exhausts the carry-in NOL.
        3. get_period_journal(entity_id, ...) — one call per year needed for Schedule L
           reconstruction (BOY and EOY balances). Start with tax_year; call prior years
           as needed for BOY figures.
        4. draft_memo — write the NOL stack memo (topic: "nol_carryforward_stack").
        5. draft_memo — write the Schedule L reconstruction memo
           (topic: "schedule_l_reconstruction").
        6. flag_for_cpa_review — one call per flagged item (during processing, not
           batched at the end).
        7. Emit the full DRAFT output in the Markdown schema from your system prompt.

        Critical requirements:
        - Show the full NOL stack table with per-year rows: TI before NOL, NOL
          generated, NOL carry-in, NOL available, 80% cap computation, NOL used,
          NOL remaining, TI after NOL.
        - Show Schedule L in two tables: Assets (lines 1–14) and Liabilities + Equity
          (lines 16–28), with BOY and EOY columns.
        - Perform the balance check (Assets = L+E) for BOY and EOY. Flag if out of balance.
        - Cite §172(a)(2) and IRS Pub. 536 for all NOL positions.
        - Cite Notice 2020-26 to confirm the CARES Act 100% allowance is inapplicable
          (for an entity formed after 2020).
        - Flag §382 ownership-change check as mandatory (out of scope for this specialist).
        - Stamp the output DRAFT — CPA REVIEW REQUIRED at the top and bottom.
        - Include all memo IDs and flag IDs in the footer.
    """)


# ---------------------------------------------------------------------------
# Helpers — Anthropic path
# ---------------------------------------------------------------------------


def _extract_text_from_response(response: Any) -> str:
    """Pull the final text content from an Anthropic Messages response object."""
    parts: list[str] = []
    for block in response.content:
        if hasattr(block, "text"):
            parts.append(block.text)
    return "\n".join(parts)


def _collect_tool_use_summaries(response: Any) -> list[dict[str, Any]]:
    """Return a brief summary of each tool_use block in the Anthropic response."""
    summaries: list[dict[str, Any]] = []
    for block in response.content:
        if getattr(block, "type", None) == "tool_use":
            summaries.append({
                "tool": block.name,
                "id": block.id,
                "input_keys": list(block.input.keys()) if isinstance(block.input, dict) else [],
            })
    return summaries


def _mcp_server_config(mcp_url: str, service_token: str) -> dict[str, Any]:
    """Build the mcp_servers entry for the Anthropic SDK.

    Restricts the model to the four tools this specialist is permitted to use.
    Any tool registered on the MCP server that is NOT in _ALLOWED_TOOLS will not
    be visible to the model — defense in depth.
    """
    return {
        "type": "url",
        "url": mcp_url,
        "name": "accounting-ops",
        "authorization_token": service_token,
        "tool_filter": {"type": "include", "tool_names": _ALLOWED_TOOLS},
    }


# ---------------------------------------------------------------------------
# Helpers — OpenAI / local LLM path
# ---------------------------------------------------------------------------


def _mcp_tool_to_openai_function(tool: Any) -> dict[str, Any]:
    """Convert an MCP ``Tool`` descriptor to an OpenAI function-calling schema."""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": (
                tool.inputSchema
                if tool.inputSchema
                else {"type": "object", "properties": {}}
            ),
        },
    }


def _openai_tool_message(tool_call_id: str, result_json: str) -> dict[str, Any]:
    """Build an OpenAI-format tool result message."""
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": result_json,
    }


def _parse_json_mode_tool_calls(text: str) -> list[dict[str, Any]]:
    """Extract TOOL_CALL: {...} lines from a plain-text response."""
    calls = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("TOOL_CALL:"):
            payload = stripped[len("TOOL_CALL:"):].strip()
            try:
                obj = json.loads(payload)
                calls.append(obj)
            except json.JSONDecodeError:
                pass
    return calls


async def _run_openai_path(
    workspace_id: str,
    entity_id: str,
    tax_year: int,
    output_path: pathlib.Path,
    mcp_url: str,
    service_token: str,
    llm_endpoint_url: str,
    llm_model: str,
    llm_api_key: str,
    instruction_format: str,
    *,
    verbose: bool = False,
) -> None:
    """Run the specialist using an OpenAI-compatible local endpoint.

    Implements a manual MCP tool-call loop because local endpoints do not
    have native MCP integration.
    """
    try:
        import openai
    except ImportError:
        print(
            "ERROR: 'openai' package not found.\n"
            "Install the tax-agent extras:\n"
            "    uv pip install accounting-ops[tax-agents]\n"
            "or:\n"
            "    uv pip install 'openai>=1.40.0'",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        from mcp.client.streamable_http import streamablehttp_client
        from mcp import ClientSession
    except ImportError:
        print(
            "ERROR: 'mcp' package not found or is too old.\n"
            "Install the tax-agent extras:\n"
            "    uv pip install accounting-ops[tax-agents]\n"
            "or:\n"
            "    uv pip install 'mcp[cli]>=0.6.0'",
            file=sys.stderr,
        )
        sys.exit(1)

    system_prompt = _load_system_prompt()
    user_message = _build_user_message(workspace_id, entity_id, tax_year)

    if verbose:
        print(f"[runner] Backend    : openai (local endpoint)")
        print(f"[runner] Endpoint   : {llm_endpoint_url}")
        print(f"[runner] Model      : {llm_model}")
        print(f"[runner] MCP URL    : {mcp_url}")
        print(f"[runner] Entity     : {entity_id}")
        print(f"[runner] Tax year   : {tax_year}")
        print(f"[runner] Output     : {output_path}")
        print(f"[runner] Instr fmt  : {instruction_format}")
        print(f"[runner] Max iters  : {_MAX_LOOP_ITERATIONS}")
        print()

    headers = {"Authorization": f"Bearer {service_token}"}

    async with streamablehttp_client(mcp_url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools_response = await session.list_tools()
            raw_tools = tools_response.tools
            allowed_tools = [t for t in raw_tools if t.name in _ALLOWED_TOOLS]

            if verbose:
                print(f"[runner] MCP tools available: {[t.name for t in allowed_tools]}")

            use_function_calling = (instruction_format == "tools")
            openai_tools: list[dict[str, Any]] = []

            if use_function_calling:
                openai_tools = [_mcp_tool_to_openai_function(t) for t in allowed_tools]
                effective_system = system_prompt
            else:
                effective_system = system_prompt + _JSON_FALLBACK_SUFFIX

            client = openai.AsyncOpenAI(
                base_url=llm_endpoint_url,
                api_key=llm_api_key,
            )

            messages: list[dict[str, Any]] = [
                {"role": "system", "content": effective_system},
                {"role": "user", "content": user_message},
            ]

            tool_call_count = 0
            draft_text = ""
            last_response: Any = None

            for iteration in range(_MAX_LOOP_ITERATIONS):
                if verbose:
                    print(f"[runner] LLM iteration {iteration + 1}/{_MAX_LOOP_ITERATIONS} ...")

                kwargs: dict[str, Any] = {
                    "model": llm_model,
                    "messages": messages,
                    "max_tokens": MAX_OUTPUT_TOKENS,
                }
                if use_function_calling and openai_tools:
                    kwargs["tools"] = openai_tools
                    kwargs["tool_choice"] = "auto"

                last_response = await client.chat.completions.create(**kwargs)
                choice = last_response.choices[0]
                finish_reason = choice.finish_reason

                # ---- Native function-calling branch ----
                if use_function_calling and finish_reason == "tool_calls":
                    tool_calls = choice.message.tool_calls or []
                    if verbose:
                        for tc in tool_calls:
                            print(f"  [tool_call] {tc.function.name}  id={tc.id}")

                    messages.append(choice.message.model_dump())

                    for tc in tool_calls:
                        tool_name = tc.function.name
                        try:
                            tool_args = json.loads(tc.function.arguments or "{}")
                        except json.JSONDecodeError:
                            tool_args = {}

                        tool_call_count += 1
                        try:
                            mcp_result = await session.call_tool(tool_name, tool_args)
                            result_payload = json.dumps({
                                "content": [
                                    item.model_dump() if hasattr(item, "model_dump") else str(item)
                                    for item in mcp_result.content
                                ]
                            })
                        except Exception as exc:
                            result_payload = json.dumps({"error": str(exc)})

                        if verbose:
                            print(f"  [tool_result] {tool_name} -> {result_payload[:200]}")

                        messages.append(_openai_tool_message(tc.id, result_payload))

                    continue

                # ---- JSON-mode / text-only response ----
                text_content = choice.message.content or ""

                if not use_function_calling:
                    json_calls = _parse_json_mode_tool_calls(text_content)
                    if json_calls:
                        messages.append({"role": "assistant", "content": text_content})
                        for jc in json_calls:
                            jc_name = jc.get("name", "")
                            jc_args = jc.get("arguments", {})
                            tool_call_count += 1
                            try:
                                mcp_result = await session.call_tool(jc_name, jc_args)
                                result_payload = json.dumps({
                                    "content": [
                                        item.model_dump()
                                        if hasattr(item, "model_dump")
                                        else str(item)
                                        for item in mcp_result.content
                                    ]
                                })
                            except Exception as exc:
                                result_payload = json.dumps({"error": str(exc)})
                            if verbose:
                                print(f"  [json_tool] {jc_name} -> {result_payload[:200]}")
                            messages.append({
                                "role": "user",
                                "content": f"TOOL_RESULT: {result_payload}",
                            })
                        continue

                draft_text = text_content
                if verbose:
                    print(f"[runner] Final response received (iteration {iteration + 1}).")
                break

            else:
                print(
                    f"WARNING: reached the {_MAX_LOOP_ITERATIONS}-iteration cap. "
                    "The draft may be incomplete. Check the output.",
                    file=sys.stderr,
                )
                last_msg = messages[-1] if messages else {}
                draft_text = last_msg.get("content") or ""

            print(f"[runner] Tool calls dispatched: {tool_call_count}")

            if not draft_text.strip():
                print(
                    "ERROR: The specialist returned an empty response. "
                    "Check that the /mcp endpoint is reachable, the service "
                    "token is valid, and the local LLM endpoint is running.",
                    file=sys.stderr,
                )
                sys.exit(1)

            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(draft_text, encoding="utf-8")
            print(f"[runner] Draft written to: {output_path}")

            if last_response is not None and last_response.usage:
                usage = last_response.usage
                print(
                    f"[runner] Token usage -- prompt: {usage.prompt_tokens}  "
                    f"completion: {usage.completion_tokens}"
                )

            for line in draft_text.splitlines():
                if "Memo IDs:" in line or "Flag IDs:" in line:
                    print(f"[runner] {line.strip()}")
            # Emit NOL stack summary line if present.
            for line in draft_text.splitlines():
                if "NOL carry into" in line or "Line 29a" in line:
                    print(f"[runner] {line.strip()}")


# ---------------------------------------------------------------------------
# Anthropic path
# ---------------------------------------------------------------------------


def _run_anthropic_path(
    workspace_id: str,
    entity_id: str,
    tax_year: int,
    output_path: pathlib.Path,
    model_key: str,
    mcp_url: str,
    service_token: str,
    *,
    verbose: bool = False,
) -> None:
    """Run the specialist using the Anthropic SDK's native MCP integration."""
    if anthropic is None:
        print(
            "ERROR: 'anthropic' package not found.\n"
            "Install the tax-agent extras:\n"
            "    uv pip install accounting-ops[tax-agents]\n"
            "or:\n"
            "    uv pip install 'anthropic>=0.44.0'",
            file=sys.stderr,
        )
        sys.exit(1)

    model_id = _MODEL_MAP[model_key]
    system_prompt = _load_system_prompt()
    user_message = _build_user_message(workspace_id, entity_id, tax_year)

    if verbose:
        print(f"[runner] Backend    : anthropic")
        print(f"[runner] Model      : {model_id}")
        print(f"[runner] MCP URL    : {mcp_url}")
        print(f"[runner] Entity     : {entity_id}")
        print(f"[runner] Tax year   : {tax_year}")
        print(f"[runner] Output     : {output_path}")
        print()

    client = anthropic.Anthropic()

    response = client.beta.messages.create(
        model=model_id,
        max_tokens=MAX_OUTPUT_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
        mcp_servers=[_mcp_server_config(mcp_url, service_token)],
        betas=["mcp-client-2025-04-04"],
    )

    tool_summaries = _collect_tool_use_summaries(response)
    if tool_summaries:
        print(f"[runner] Tool calls made: {len(tool_summaries)}")
        for ts in tool_summaries:
            print(f"  [{ts['tool']}] id={ts['id']}  inputs={ts['input_keys']}")
        print()

    draft_text = _extract_text_from_response(response)
    if not draft_text.strip():
        print(
            "ERROR: The specialist returned an empty response. "
            "Check that the /mcp endpoint is reachable and the service token is valid.",
            file=sys.stderr,
        )
        sys.exit(1)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(draft_text, encoding="utf-8")
    print(f"[runner] Draft written to: {output_path}")

    usage = response.usage
    print(
        f"[runner] Token usage -- input: {usage.input_tokens}  "
        f"output: {usage.output_tokens}"
    )

    for line in draft_text.splitlines():
        if "Memo IDs:" in line or "Flag IDs:" in line:
            print(f"[runner] {line.strip()}")
    for line in draft_text.splitlines():
        if "NOL carry into" in line or "Line 29a" in line:
            print(f"[runner] {line.strip()}")


# ---------------------------------------------------------------------------
# Top-level run dispatcher
# ---------------------------------------------------------------------------


def run(
    workspace_id: str,
    entity_id: str,
    tax_year: int,
    output_path: pathlib.Path,
    model_key: str,
    mcp_url: str,
    service_token: str,
    backend: str,
    llm_endpoint_url: str,
    llm_model: str,
    llm_api_key: str,
    instruction_format: str,
    *,
    verbose: bool = False,
) -> None:
    """Dispatch to the correct backend and run the specialist end-to-end."""
    if backend == "anthropic":
        _run_anthropic_path(
            workspace_id=workspace_id,
            entity_id=entity_id,
            tax_year=tax_year,
            output_path=output_path,
            model_key=model_key,
            mcp_url=mcp_url,
            service_token=service_token,
            verbose=verbose,
        )
    elif backend == "openai":
        import asyncio
        asyncio.run(_run_openai_path(
            workspace_id=workspace_id,
            entity_id=entity_id,
            tax_year=tax_year,
            output_path=output_path,
            mcp_url=mcp_url,
            service_token=service_token,
            llm_endpoint_url=llm_endpoint_url,
            llm_model=llm_model,
            llm_api_key=llm_api_key,
            instruction_format=instruction_format,
            verbose=verbose,
        ))
    else:
        print(
            f"ERROR: Unknown backend '{backend}'. Choose 'anthropic' or 'openai'.",
            file=sys.stderr,
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the NOL Carryforward + Schedule L Reconstruction Analyst and "
            "write a DRAFT computation package to the specified output file."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(f"""\
            Environment variables:
              ACCTOPS_SERVICE_TOKEN   Bearer token for the /mcp endpoint              [required]
              ACCTOPS_MCP_URL         MCP endpoint base URL                           [default: http://localhost:8000/mcp]

              -- Anthropic backend --
              ANTHROPIC_API_KEY       Anthropic API key                               [required for --backend anthropic]

              -- OpenAI / local backend --
              LLM_ENDPOINT_URL        Base URL for OpenAI-compatible local endpoint   [default: http://localhost:8088/v1]
              LLM_MODEL               Model name at the local endpoint                [default: Qwen3.6-35B-A3B-Vision]
              LLM_API_KEY             API key for the local endpoint                  [default: local-qwen]
              OPENROUTER_API_KEY      Fleet-wide OpenRouter key (alternative to LLM_API_KEY)

            {HELP_TEXT_LLM_API_KEY}
        """),
    )
    parser.add_argument(
        "--workspace-id",
        required=True,
        help="Workspace UUID",
    )
    parser.add_argument(
        "--entity-id",
        required=True,
        help="Entity UUID within the workspace",
    )
    parser.add_argument(
        "--tax-year",
        required=True,
        type=int,
        help="Four-digit tax year (e.g. 2025). The specialist will build the NOL "
             "stack from the entity's first year through this year.",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=pathlib.Path,
        help="Output path for the DRAFT markdown file",
    )
    parser.add_argument(
        "--model",
        choices=list(_MODEL_MAP.keys()),
        default="sonnet",
        help=(
            "Model to use with --backend anthropic: 'sonnet' (claude-sonnet-4-6, default) "
            "or 'opus' (claude-opus-4-7, higher quality, higher cost). "
            "Ignored when --backend openai."
        ),
    )
    parser.add_argument(
        "--mcp-url",
        default=None,
        help=(
            "Override MCP endpoint URL "
            "(default: $ACCTOPS_MCP_URL or http://localhost:8000/mcp)"
        ),
    )
    parser.add_argument(
        "--backend",
        choices=["anthropic", "openai"],
        default=None,
        help=(
            "LLM backend: 'anthropic' (default when ANTHROPIC_API_KEY set) or "
            "'openai' (default when LLM_ENDPOINT_URL set)."
        ),
    )
    parser.add_argument(
        "--llm-endpoint-url",
        default=None,
        help="Base URL for the OpenAI-compatible local endpoint.",
    )
    parser.add_argument(
        "--llm-model",
        default=None,
        help="Model name served by the local endpoint.",
    )
    parser.add_argument(
        "--llm-api-key",
        default=None,
        help=(
            "API key for the local endpoint. DEPRECATED — visible via `ps`. "
            "Prefer $LLM_API_KEY, $OPENROUTER_API_KEY, or --llm-api-key-file."
        ),
    )
    parser.add_argument(
        "--llm-api-key-file",
        default=None,
        help=(
            "Path to a file (chmod 600 recommended) containing the LLM API "
            "key. Takes precedence over --llm-api-key."
        ),
    )
    parser.add_argument(
        "--instruction-format",
        choices=["tools", "json"],
        default="tools",
        help=(
            "How to express tools to the local model: "
            "'tools' (OpenAI function-calling, default) or "
            "'json' (structured text fallback for models without native tool support). "
            "Ignored when --backend anthropic."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Stream tool-use events and config details to stdout",
    )
    return parser.parse_args(argv)


def _resolve_backend(args: argparse.Namespace) -> str:
    """Determine backend from flags and environment variables.

    Priority:
    1. Explicit --backend flag.
    2. Env var heuristic: ANTHROPIC_API_KEY set -> anthropic;
       LLM_ENDPOINT_URL set -> openai.
    3. Fall through to "anthropic".
    """
    if args.backend:
        return args.backend
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("LLM_ENDPOINT_URL"):
        return "openai"
    return "anthropic"


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    service_token = os.environ.get("ACCTOPS_SERVICE_TOKEN", "")
    if not service_token:
        print(
            "ERROR: ACCTOPS_SERVICE_TOKEN environment variable is required.\n"
            "Obtain a service token from Keycloak (uchub realm, accounting-ops-app client).",
            file=sys.stderr,
        )
        sys.exit(1)

    mcp_url = (
        args.mcp_url
        or os.environ.get("ACCTOPS_MCP_URL")
        or _DEFAULT_MCP_URL
    )

    if not (1900 <= args.tax_year <= date.today().year):
        print(
            f"ERROR: --tax-year {args.tax_year} is not a plausible tax year.",
            file=sys.stderr,
        )
        sys.exit(1)

    backend = _resolve_backend(args)

    if backend == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "ERROR: ANTHROPIC_API_KEY environment variable is required for "
            "--backend anthropic.",
            file=sys.stderr,
        )
        sys.exit(1)

    llm_endpoint_url = (
        args.llm_endpoint_url
        or os.environ.get("LLM_ENDPOINT_URL")
        or _DEFAULT_LLM_ENDPOINT_URL
    )
    llm_model = (
        args.llm_model
        or os.environ.get("LLM_MODEL")
        or _DEFAULT_LLM_MODEL
    )
    llm_api_key = resolve_llm_api_key(
        args.llm_api_key,
        args.llm_api_key_file,
        default=_DEFAULT_LLM_API_KEY,
    )

    run(
        workspace_id=args.workspace_id,
        entity_id=args.entity_id,
        tax_year=args.tax_year,
        output_path=args.output,
        model_key=args.model,
        mcp_url=mcp_url,
        service_token=service_token,
        backend=backend,
        llm_endpoint_url=llm_endpoint_url,
        llm_model=llm_model,
        llm_api_key=llm_api_key,
        instruction_format=args.instruction_format,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    main()
