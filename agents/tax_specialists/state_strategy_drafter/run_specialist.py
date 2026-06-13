#!/usr/bin/env python3
"""State Tax Strategy Drafter — CLI runner.

Drafts multi-state corporate tax strategy memos covering nexus, apportionment,
state conformity, sales/use tax, state credits, VDA strategy, and state tax
controversy. Supports per-state rulesets (SC fully populated; GA/NY/CA/FL/TX
are placeholder scaffolding with TODO content).

Supports two backends:

- **anthropic** (default when ``ANTHROPIC_API_KEY`` is set): uses the Anthropic
  SDK's native MCP client integration (``beta.messages.create`` with
  ``mcp_servers``). No local tool-call loop needed.

- **openai** (default when ``CPATEAM_LLM_ENDPOINT_URL`` is set): uses the OpenAI
  Python SDK (>= 1.0) aimed at any OpenAI-compatible endpoint. The CPA-team
  frontier config defaults to OpenRouter (anthropic/claude-sonnet-4.6).
  Implements a manual MCP tool-call loop.

Usage — Anthropic path::

    export ANTHROPIC_API_KEY="sk-ant-..."
    export ACCTOPS_SERVICE_TOKEN="<keycloak-service-token>"

    uv run python agents/tax_specialists/state_strategy_drafter/run_specialist.py \\
        --workspace-id <UUID> \\
        --entity-id    <UUID> \\
        --tax-year     2024 \\
        --state        sc \\
        --output       ./drafts/mu-inc-2024-sc-strategy-draft.md \\
        --model        sonnet

Usage — OpenRouter / OpenAI-compatible path::

    export ACCTOPS_SERVICE_TOKEN="<keycloak-service-token>"
    OPENROUTER_KEY=$(cat /home/muut/.openrouter.key)

    uv run python agents/tax_specialists/state_strategy_drafter/run_specialist.py \\
        --backend      openai \\
        --workspace-id 77753ea5-efe9-5539-a846-6b20ddc841b9 \\
        --entity-id    1ab938df-43f2-567a-8566-800f8a14a466 \\
        --tax-year     2024 \\
        --state        sc \\
        --mcp-url      http://localhost:8000/mcp/ \\
        --output       /drafts/mu-inc-2024-sc-strategy-draft.md \\
        --llm-endpoint-url https://openrouter.ai/api/v1 \\
        --llm-model    anthropic/claude-sonnet-4-6 \\
        --llm-api-key  "$OPENROUTER_KEY" \\
        --verbose

Available state codes:
    sc   — South Carolina (fully populated ruleset)
    ga   — Georgia (placeholder / TODO)
    ny   — New York (placeholder / TODO)
    ca   — California (placeholder / TODO)
    fl   — Florida (placeholder / TODO)
    tx   — Texas (placeholder / TODO)

Environment variables:

    ACCTOPS_SERVICE_TOKEN   Bearer token with at least "viewer" scope on the
                            workspace. Required.
    ACCTOPS_MCP_URL         Override the MCP endpoint base URL.
                            Default: http://localhost:8000/mcp
    ANTHROPIC_API_KEY       Anthropic API key. Required for --backend anthropic.
    CPATEAM_LLM_ENDPOINT_URL  Base URL for the CPA-team frontier endpoint (the
                            --backend openai path). Default: OpenRouter
                            (https://openrouter.ai/api/v1).
    CPATEAM_LLM_MODEL       Frontier model name. Default: anthropic/claude-sonnet-4.6.
    CPATEAM_LLM_API_KEY     API key for the frontier endpoint. Falls back to
                            OPENROUTER_API_KEY when unset.

Notes:

- The system prompt (system_prompt.md) is the base. The per-state ruleset file
  (state_rulesets/<state>.md) is appended to the system prompt at runtime so the
  model sees both in a single system turn.
- Both paths share the same system prompt, worked examples, and MCP server target;
  only the model client differs.
- The OpenAI path implements a manual MCP tool-call loop using mcp.client.streamable_http.
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
from agents._shared.cpateam_llm import resolve_cpateam_llm
from agents._shared.limits import MAX_OUTPUT_TOKENS

# ---------------------------------------------------------------------------
# Soft dependency checks
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

_MODEL_MAP = {
    "sonnet": "claude-sonnet-4-6",
    "opus":   "claude-opus-4-7",
}

# Paths relative to this file.
_SPECIALIST_DIR = pathlib.Path(__file__).parent
_SYSTEM_PROMPT_PATH = _SPECIALIST_DIR / "system_prompt.md"
_RULESETS_DIR = _SPECIALIST_DIR / "state_rulesets"

# Supported state codes and their human-readable names.
_SUPPORTED_STATES: dict[str, str] = {
    "sc": "South Carolina",
    "ga": "Georgia",
    "ny": "New York",
    "ca": "California",
    "fl": "Florida",
    "tx": "Texas",
}

# MCP tool names the specialist is allowed to call.
_ALLOWED_TOOLS: list[str] = [
    "get_form_1120_data",
    "get_period_journal",
    "draft_memo",
    "flag_for_cpa_review",
]

# Maximum tool-call iterations in the OpenAI manual loop.
_MAX_LOOP_ITERATIONS = 30

# JSON-mode fallback suffix injected when the model lacks native function calling.
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
# Helpers — system prompt + ruleset loading
# ---------------------------------------------------------------------------


def _load_system_prompt(state_code: str) -> str:
    """Read system_prompt.md and append the per-state ruleset.

    The base system prompt contains the universal framework. The per-state
    ruleset (state_rulesets/<state_code>.md) is appended as a second section
    so the model sees one unified system prompt with state-specific rules at
    the bottom.
    """
    if not _SYSTEM_PROMPT_PATH.exists():
        raise FileNotFoundError(
            f"System prompt not found at {_SYSTEM_PROMPT_PATH}. "
            "Ensure you are running from the repo root or that the "
            "agents/tax_specialists/state_strategy_drafter/ directory is intact."
        )

    base_prompt = _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")

    ruleset_path = _RULESETS_DIR / f"{state_code}.md"
    if not ruleset_path.exists():
        raise FileNotFoundError(
            f"State ruleset not found at {ruleset_path}. "
            f"Supported states: {', '.join(_SUPPORTED_STATES.keys())}. "
            "To add a new state, create state_rulesets/<state_code>.md "
            "following the structure in state_rulesets/sc.md."
        )

    ruleset_content = ruleset_path.read_text(encoding="utf-8")
    state_name = _SUPPORTED_STATES[state_code]

    return (
        base_prompt
        + f"\n\n<!-- RUNTIME INJECTION: {state_name} ({state_code.upper()}) State Ruleset -->\n\n"
        + ruleset_content
    )


def _build_user_message(
    workspace_id: str,
    entity_id: str,
    tax_year: int,
    state_code: str,
) -> str:
    """Construct the user-turn message sent to the specialist."""
    state_name = _SUPPORTED_STATES[state_code]
    return textwrap.dedent(f"""\
        Please draft a STATE TAX STRATEGY MEMO for the following entity. This is a
        planning and strategy analysis — NOT a return preparation task. Draft strategy
        memos covering the applicable topics below, citing the {state_name} ({state_code.upper()})
        state ruleset loaded into your system prompt.

        workspace_id : {workspace_id}
        entity_id    : {entity_id}
        tax_year     : {tax_year}
        state        : {state_code.upper()} ({state_name})
        run_date     : {date.today().isoformat()}

        Follow the call sequence in your system prompt exactly:
        1. get_form_1120_data (federal data as the foundation for state strategy).
        2. get_period_journal with limit=80 — for revenue geography, payroll location,
           fixed assets, and sales/use tax sweep. Do NOT request the full journal.
        3. For each strategy topic that fires for this entity, call draft_memo with
           the appropriate topic prefix ("state_strategy_...").
        4. Call flag_for_cpa_review for each judgment item — one call per flag, during
           processing (not batched at end).
        5. Call draft_memo with topic "state_strategy_summary" as the final step.
        6. Emit the full DRAFT in the Markdown schema from your system prompt.

        Topics to address for {state_name}:
        - Multi-state nexus analysis (physical + economic + Wayfair)
        - Apportionment strategy (SC: single-factor; note any implications)
        - State conformity / non-conformity (esp. §168(k) bonus depreciation, NOL)
        - Sales and use tax planning (Wayfair thresholds, SaaS taxability)
        - State tax credits ({state_name}: R&D credit, Job Tax Credit, etc.)
        - VDA strategy (if any unregistered states with potential nexus)
        - Residency / nexus changes (remote employees, expansion plans)
        - Controversy strategy overview (if any open DOR notices)

        DO NOT duplicate federal-only analysis (§199A, federal §41 computation,
        federal NOL carryforward rules, etc.) — cross-reference the Federal Tax
        Strategy Drafter memo for federal topics.

        Stamp DRAFT — CPA REVIEW REQUIRED — NOT LEGAL ADVICE — NOT FOR FILING
        at the top and bottom of every section.
        Include all memo IDs and flag IDs in the footer.
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
    """Build the mcp_servers entry for the Anthropic SDK."""
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
    """Convert an MCP Tool descriptor to an OpenAI function-calling schema."""
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
    """Extract TOOL_CALL: {...} lines from a plain-text response (JSON mode fallback)."""
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
    state_code: str,
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
    """Run the specialist using an OpenAI-compatible endpoint."""
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

    system_prompt = _load_system_prompt(state_code)
    user_message = _build_user_message(workspace_id, entity_id, tax_year, state_code)

    if verbose:
        print(f"[runner] Backend         : openai (remote/local endpoint)")
        print(f"[runner] Endpoint        : {llm_endpoint_url}")
        print(f"[runner] Model           : {llm_model}")
        print(f"[runner] MCP URL         : {mcp_url}")
        print(f"[runner] Entity          : {entity_id}")
        print(f"[runner] Tax year        : {tax_year}")
        print(f"[runner] State           : {state_code.upper()} ({_SUPPORTED_STATES[state_code]})")
        print(f"[runner] Output          : {output_path}")
        print(f"[runner] Instr fmt       : {instruction_format}")
        print(f"[runner] System prompt   : {len(system_prompt)} chars "
              f"(base + {state_code}.md ruleset)")
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
                    # See agents/_shared/limits.py for the rationale behind
                    # MAX_OUTPUT_TOKENS (16384, not 8192).
                    "max_tokens": MAX_OUTPUT_TOKENS,
                }
                if use_function_calling and openai_tools:
                    kwargs["tools"] = openai_tools
                    kwargs["tool_choice"] = "auto"

                last_response = await client.chat.completions.create(**kwargs)
                choice = last_response.choices[0]
                finish_reason = choice.finish_reason

                # Native function-calling branch
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

                # Text / JSON-mode branch
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
                    "Check that the /mcp endpoint is reachable, the service token "
                    "is valid, and the LLM endpoint is running.",
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


# ---------------------------------------------------------------------------
# Anthropic path
# ---------------------------------------------------------------------------


def _run_anthropic_path(
    workspace_id: str,
    entity_id: str,
    tax_year: int,
    state_code: str,
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
    system_prompt = _load_system_prompt(state_code)
    user_message = _build_user_message(workspace_id, entity_id, tax_year, state_code)

    if verbose:
        print(f"[runner] Backend         : anthropic")
        print(f"[runner] Model           : {model_id}")
        print(f"[runner] MCP URL         : {mcp_url}")
        print(f"[runner] Entity          : {entity_id}")
        print(f"[runner] Tax year        : {tax_year}")
        print(f"[runner] State           : {state_code.upper()} ({_SUPPORTED_STATES[state_code]})")
        print(f"[runner] Output          : {output_path}")
        print(f"[runner] System prompt   : {len(system_prompt)} chars "
              f"(base + {state_code}.md ruleset)")
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


# ---------------------------------------------------------------------------
# Top-level dispatcher
# ---------------------------------------------------------------------------


def run(
    workspace_id: str,
    entity_id: str,
    tax_year: int,
    state_code: str,
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
    """Dispatch to the correct backend and run the specialist end-to-end.

    Args:
        workspace_id:       Workspace UUID.
        entity_id:          Entity UUID within the workspace.
        tax_year:           Four-digit tax year (e.g. 2024).
        state_code:         Two-letter state code (e.g. "sc", "ga"). Default: "sc".
        output_path:        Path to write the DRAFT markdown to.
        model_key:          "sonnet" or "opus" (Anthropic path only).
        mcp_url:            Base URL of the /mcp endpoint.
        service_token:      Bearer token for the MCP endpoint.
        backend:            "anthropic" or "openai".
        llm_endpoint_url:   Base URL for the OpenAI-compatible endpoint.
        llm_model:          Model name at the endpoint.
        llm_api_key:        API key for the endpoint.
        instruction_format: "tools" or "json".
        verbose:            If True, stream tool-use events to stdout.
    """
    if backend == "anthropic":
        _run_anthropic_path(
            workspace_id=workspace_id,
            entity_id=entity_id,
            tax_year=tax_year,
            state_code=state_code,
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
            state_code=state_code,
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
            "Run the State Tax Strategy Drafter and write a DRAFT strategy memo "
            "to the specified output file."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(f"""\
            State codes:
              sc   South Carolina (fully populated ruleset)
              ga   Georgia (placeholder / TODO)
              ny   New York (placeholder / TODO)
              ca   California (placeholder / TODO)
              fl   Florida (placeholder / TODO)
              tx   Texas (placeholder / TODO)

            Environment variables:
              ACCTOPS_SERVICE_TOKEN   Bearer token for the /mcp endpoint              [required]
              ACCTOPS_MCP_URL         MCP endpoint base URL                           [default: http://localhost:8000/mcp]

              -- Anthropic backend --
              ANTHROPIC_API_KEY       Anthropic API key                               [required for --backend anthropic]

              -- OpenAI / OpenRouter backend (CPA-team frontier) --
              CPATEAM_LLM_ENDPOINT_URL  Frontier endpoint base URL                    [default: https://openrouter.ai/api/v1]
              CPATEAM_LLM_MODEL         Frontier model name                           [default: anthropic/claude-sonnet-4.6]
              CPATEAM_LLM_API_KEY       Frontier API key (falls back to OPENROUTER_API_KEY)
              OPENROUTER_API_KEY        Fleet-wide OpenRouter key (used when CPATEAM_LLM_API_KEY unset)

            {HELP_TEXT_LLM_API_KEY}

            Examples:
              # SC strategy via OpenRouter (Claude Sonnet):
              python run_specialist.py \\
                --backend openai \\
                --workspace-id 77753ea5-efe9-5539-a846-6b20ddc841b9 \\
                --entity-id 1ab938df-43f2-567a-8566-800f8a14a466 \\
                --tax-year 2024 --state sc \\
                --llm-endpoint-url https://openrouter.ai/api/v1 \\
                --llm-model anthropic/claude-sonnet-4-6 \\
                --llm-api-key-file ~/.openrouter.key \\
                --output /drafts/mu-inc-2024-sc-strategy.md --verbose
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
        help="Four-digit tax year (e.g. 2024)",
    )
    parser.add_argument(
        "--state",
        default="sc",
        choices=list(_SUPPORTED_STATES.keys()),
        help=(
            "State code for the primary state analysis. "
            "Default: sc (South Carolina). "
            "sc is fully populated; ga/ny/ca/fl/tx are TODO placeholders."
        ),
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
            "Model to use with --backend anthropic: 'sonnet' (default) or 'opus'. "
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
            "'openai' (default when CPATEAM_LLM_ENDPOINT_URL set)."
        ),
    )
    parser.add_argument(
        "--llm-endpoint-url",
        default=None,
        help=(
            "Override the CPA-team frontier endpoint base URL. "
            "Default: $CPATEAM_LLM_ENDPOINT_URL or OpenRouter."
        ),
    )
    parser.add_argument(
        "--llm-model",
        default=None,
        help=(
            "Override the frontier model name. "
            "Default: $CPATEAM_LLM_MODEL or anthropic/claude-sonnet-4.6"
        ),
    )
    parser.add_argument(
        "--llm-api-key",
        default=None,
        help=(
            "API key for the frontier endpoint. DEPRECATED — visible via `ps`. "
            "Prefer $CPATEAM_LLM_API_KEY, $OPENROUTER_API_KEY, or --llm-api-key-file."
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
            "'tools' (native function-calling, default) or "
            "'json' (structured text fallback). "
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
    """Determine the backend from flags and environment variables."""
    if args.backend:
        return args.backend
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("CPATEAM_LLM_ENDPOINT_URL") or os.environ.get("LLM_ENDPOINT_URL"):
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

    # CPA-team FRONTIER config — dedicated CPATEAM_LLM_* (OpenRouter sonnet-4.6 by
    # default), NOT the vision-repurposed LLM_*/resolve_llm_api_key, which would
    # silently point this frontier synthesis at the receipt-VLM → 401.
    llm_endpoint_url, llm_model, llm_api_key = resolve_cpateam_llm(
        args.llm_endpoint_url, args.llm_model, args.llm_api_key, args.llm_api_key_file,
    )

    run(
        workspace_id=args.workspace_id,
        entity_id=args.entity_id,
        tax_year=args.tax_year,
        state_code=args.state,
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
