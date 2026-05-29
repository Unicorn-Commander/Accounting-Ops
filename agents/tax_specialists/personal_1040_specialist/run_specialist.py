#!/usr/bin/env python3
"""Personal 1040 Specialist — CLI runner.

Invokes the Personal 1040 Specialist against the Accounting-Ops /mcp endpoint.
Supports two backends:

- **anthropic** (default when ``ANTHROPIC_API_KEY`` is set): uses the Anthropic SDK's
  native MCP client integration (``beta.messages.create`` with ``mcp_servers``).

- **openai** (default when ``LLM_ENDPOINT_URL`` is set): uses the OpenAI Python SDK
  (>= 1.0) aimed at any OpenAI-compatible endpoint (e.g. OpenRouter with
  anthropic/claude-sonnet-4.6). Implements a manual MCP tool-call loop.

Default entity: Aaron Stransky personal (a5dbe5e7-3c1f-5cb2-9f7c-95b5f3f1d4e2)
Default workspace: 77753ea5-efe9-5539-a846-6b20ddc841b9

Usage — Anthropic backend::

    export ANTHROPIC_API_KEY="sk-ant-..."
    export ACCTOPS_SERVICE_TOKEN="<keycloak-service-token>"

    python agents/tax_specialists/personal_1040_specialist/run_specialist.py \\
        --workspace-id 77753ea5-efe9-5539-a846-6b20ddc841b9 \\
        --entity-id    a5dbe5e7-3c1f-5cb2-9f7c-95b5f3f1d4e2 \\
        --tax-year     2024 \\
        --output       ./drafts/aaron-personal-2024-1040-draft.md \\
        --model        sonnet

Usage — OpenRouter (openai-compatible) backend::

    export ACCTOPS_SERVICE_TOKEN="<keycloak-service-token>"
    export OPENROUTER_KEY="sk-or-..."

    python agents/tax_specialists/personal_1040_specialist/run_specialist.py \\
        --workspace-id 77753ea5-efe9-5539-a846-6b20ddc841b9 \\
        --entity-id    a5dbe5e7-3c1f-5cb2-9f7c-95b5f3f1d4e2 \\
        --tax-year     2024 \\
        --mcp-url      http://localhost:8000/mcp/ \\
        --output       /drafts/aaron-personal-2024-1040-draft.md \\
        --backend      openai \\
        --llm-endpoint-url https://openrouter.ai/api/v1 \\
        --llm-model    anthropic/claude-sonnet-4.6 \\
        --llm-api-key  "$OPENROUTER_KEY" \\
        --verbose

Usage — dogfood run on midboy1::

    ssh muut@midboy1
    TOKEN=$(cat /home/muut/.tax-1120-preparer.token)
    OPENROUTER_KEY=$(cat /home/muut/.openrouter.key)
    docker exec -e ACCTOPS_SERVICE_TOKEN="$TOKEN" accounting_ops-app-1 \\
      python /app/agents/tax_specialists/personal_1040_specialist/run_specialist.py \\
        --workspace-id 77753ea5-efe9-5539-a846-6b20ddc841b9 \\
        --entity-id    a5dbe5e7-3c1f-5cb2-9f7c-95b5f3f1d4e2 \\
        --tax-year     2024 \\
        --mcp-url      http://localhost:8000/mcp/ \\
        --output       /drafts/aaron-personal-2024-1040-draft.md \\
        --backend      openai \\
        --llm-endpoint-url https://openrouter.ai/api/v1 \\
        --llm-model    anthropic/claude-sonnet-4.6 \\
        --llm-api-key  "$OPENROUTER_KEY" \\
        --verbose

Environment variables:

    ACCTOPS_SERVICE_TOKEN   Bearer token with at least "viewer" scope on the
                            workspace. Required.
    ACCTOPS_MCP_URL         Override the MCP endpoint base URL.
                            Default: http://localhost:8000/mcp
    ANTHROPIC_API_KEY       Anthropic API key. Required for --backend anthropic.
    LLM_ENDPOINT_URL        Base URL for OpenAI-compatible endpoint.
                            Default: http://localhost:8088/v1
    LLM_MODEL               Model name served by the endpoint.
                            Default: anthropic/claude-sonnet-4.6
    LLM_API_KEY             API key for the endpoint.
                            Default: local-qwen

Notes:

- Both paths share the same system_prompt.md and worked examples — only the LLM
  client differs.
- The personal entity (a5dbe5e7) may have sparse journal data. The specialist handles
  this gracefully: it flags all missing documents and uses placeholder values only.
- Output directory is created automatically if it does not exist.
- The specialist's five mandatory veteran flags (VA rating, CRDP/CRSC election,
  combat zone, SC §12-6-1171, SC property tax) are always produced, even when data
  is sparse or the journal is empty.
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
_DEFAULT_LLM_ENDPOINT_URL = "http://localhost:8088/v1"
_DEFAULT_LLM_MODEL = "anthropic/claude-sonnet-4.6"
_DEFAULT_LLM_API_KEY = "local-qwen"

# Default entity and workspace for Aaron Personal
_DEFAULT_ENTITY_ID = "a5dbe5e7-3c1f-5cb2-9f7c-95b5f3f1d4e2"
_DEFAULT_WORKSPACE_ID = "77753ea5-efe9-5539-a846-6b20ddc841b9"

_MODEL_MAP = {
    "sonnet": "claude-sonnet-4-6",
    "opus":   "claude-opus-4-7",
}
_SYSTEM_PROMPT_PATH = pathlib.Path(__file__).parent / "system_prompt.md"

# MCP tool names the Personal 1040 Specialist is allowed to call.
# Note: get_form_1120_data is NOT included — that is for the 1120 preparer only.
# The 1040 specialist uses get_period_journal as its primary data source.
_ALLOWED_TOOLS: list[str] = [
    "get_period_journal",
    "draft_memo",
    "flag_for_cpa_review",
    "get_current_rule",
]

# Maximum tool-call iterations in the OpenAI manual loop.
_MAX_LOOP_ITERATIONS = 40  # Personal returns can require more iterations for 5 mandatory flags

# JSON-mode system prompt suffix (fallback for models without native function calling).
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
# Helpers -- shared by both backends
# ---------------------------------------------------------------------------


def _load_system_prompt() -> str:
    """Read system_prompt.md from disk (same directory as this script)."""
    if not _SYSTEM_PROMPT_PATH.exists():
        raise FileNotFoundError(
            f"System prompt not found at {_SYSTEM_PROMPT_PATH}. "
            "Ensure the agents/tax_specialists/personal_1040_specialist/ directory is intact."
        )
    return _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


def _build_user_message(
    workspace_id: str,
    entity_id: str,
    tax_year: int,
    *,
    va_rating: int | None = None,
    crdp_crsc: str | None = None,
    combat_zone_year: bool = False,
) -> str:
    """Construct the user-turn message sent to the Personal 1040 Specialist.

    The personal-tax-specific user message includes:
    - Entity and tax year identifiers.
    - Any known veteran parameters (VA rating, CRDP/CRSC election, combat zone).
    - Explicit instruction to produce all five mandatory veteran flags.
    - Instruction to handle data gaps gracefully with placeholder values.

    Parameters
    ----------
    workspace_id:      Workspace UUID.
    entity_id:         Entity UUID (default: Aaron Personal).
    tax_year:          Four-digit tax year.
    va_rating:         Optional known VA disability rating percentage (e.g. 60).
    crdp_crsc:         Optional known election: "CRDP" or "CRSC".
    combat_zone_year:  True if the taxpayer had combat-zone deployment in this tax year.
    """
    veteran_context = ""
    if va_rating is not None:
        veteran_context += f"\n    known_va_rating    : {va_rating}%"
    if crdp_crsc is not None:
        veteran_context += f"\n    crdp_crsc_election : {crdp_crsc.upper()}"
    if combat_zone_year:
        veteran_context += "\n    combat_zone_months : [CONFIRM WITH CPA/EA]"

    if not veteran_context:
        veteran_context = "\n    veteran_context    : not pre-populated — specialist must flag all items for CPA/EA confirmation"

    return textwrap.dedent(f"""\
        Please prepare a DRAFT Form 1040 package for the following taxpayer.

        workspace_id   : {workspace_id}
        entity_id      : {entity_id}
        tax_year       : {tax_year}
        run_date       : {date.today().isoformat()}
        filing_status  : [CONFIRM WITH CPA/EA — not pre-set]
        state          : South Carolina{veteran_context}

        Follow the call sequence in your system prompt:
        1. get_period_journal — always first. Use {tax_year}-01-01 through {tax_year}-12-31.
        2. get_current_rule — verify SC §12-6-1171, VA disability exemption, CRSC/CRDP status
           before citing them.
        3. draft_memo — reviewer narrative (topic: "1040_reviewer_memo").
        4. flag_for_cpa_review — one call per flagged item.
           The FIVE MANDATORY VETERAN FLAGS (F-1 through F-5) MUST be written every run:
           F-1: Veteran Flag / VA Rating
           F-2: Veteran Flag / CRDP-CRSC Election
           F-3: Veteran Flag / Combat Zone §112
           F-4: SC 1040 / SC §12-6-1171
           F-5: SC Property Tax / §12-37-220(B)(8)
        5. Emit the full DRAFT output using the output schema from your system prompt.

        Data gap handling:
        - The personal entity journal may have sparse data. Handle gracefully.
        - If journal returns zero or few entries, write all mandatory veteran flags and
          flag every missing taxpayer document (DFAS 1099-R, VA award letter, etc.).
        - Use $0 or placeholder values with confidence="low" for any line with no data.
        - Do NOT invent numbers. Every placeholder must have a data-gap flag.

        Stamp all output DRAFT -- CPA/EA REVIEW REQUIRED at the top and bottom.
        Include all memo IDs and flag IDs in the footer.
        Include the UPL disclaimer in the footer.
    """)


# ---------------------------------------------------------------------------
# Helpers -- Anthropic path
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
        # Tool filter: restrict to personal 1040 tools only.
        # get_form_1120_data is intentionally excluded — this specialist uses
        # get_period_journal as its primary data source.
        "tool_filter": {"type": "include", "tool_names": _ALLOWED_TOOLS},
    }


# ---------------------------------------------------------------------------
# Helpers -- OpenAI / OpenRouter path
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
    va_rating: int | None,
    crdp_crsc: str | None,
    combat_zone_year: bool,
    *,
    verbose: bool = False,
) -> None:
    """Run the specialist using an OpenAI-compatible endpoint.

    Implements a manual MCP tool-call loop (no native MCP integration at the
    endpoint level).
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
    user_message = _build_user_message(
        workspace_id, entity_id, tax_year,
        va_rating=va_rating,
        crdp_crsc=crdp_crsc,
        combat_zone_year=combat_zone_year,
    )

    if verbose:
        print(f"[runner] Backend    : openai (endpoint: {llm_endpoint_url})")
        print(f"[runner] Model      : {llm_model}")
        print(f"[runner] MCP URL    : {mcp_url}")
        print(f"[runner] Entity     : {entity_id}")
        print(f"[runner] Tax year   : {tax_year}")
        print(f"[runner] Output     : {output_path}")
        print(f"[runner] VA rating  : {va_rating or 'not pre-set'}")
        print(f"[runner] CRDP/CRSC  : {crdp_crsc or 'not pre-set'}")
        print(f"[runner] Combat zone: {combat_zone_year}")
        print(f"[runner] Instr fmt  : {instruction_format}")
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
                    "token is valid, and the LLM endpoint is running.",
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

            # Surface queue IDs from the draft output.
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
    output_path: pathlib.Path,
    model_key: str,
    mcp_url: str,
    service_token: str,
    va_rating: int | None,
    crdp_crsc: str | None,
    combat_zone_year: bool,
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
    user_message = _build_user_message(
        workspace_id, entity_id, tax_year,
        va_rating=va_rating,
        crdp_crsc=crdp_crsc,
        combat_zone_year=combat_zone_year,
    )

    if verbose:
        print(f"[runner] Backend    : anthropic")
        print(f"[runner] Model      : {model_id}")
        print(f"[runner] MCP URL    : {mcp_url}")
        print(f"[runner] Entity     : {entity_id}")
        print(f"[runner] Tax year   : {tax_year}")
        print(f"[runner] Output     : {output_path}")
        print(f"[runner] VA rating  : {va_rating or 'not pre-set'}")
        print(f"[runner] CRDP/CRSC  : {crdp_crsc or 'not pre-set'}")
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
    va_rating: int | None,
    crdp_crsc: str | None,
    combat_zone_year: bool,
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
            va_rating=va_rating,
            crdp_crsc=crdp_crsc,
            combat_zone_year=combat_zone_year,
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
            va_rating=va_rating,
            crdp_crsc=crdp_crsc,
            combat_zone_year=combat_zone_year,
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
            "Run the Personal 1040 Specialist and write a DRAFT 1040 package "
            "to the specified output file. Defaults to Aaron Personal entity."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(f"""\
            Environment variables:
              ACCTOPS_SERVICE_TOKEN   Bearer token for the /mcp endpoint              [required]
              ACCTOPS_MCP_URL         MCP endpoint base URL                           [default: http://localhost:8000/mcp]

              -- Anthropic backend --
              ANTHROPIC_API_KEY       Anthropic API key                               [required for --backend anthropic]

              -- OpenAI / OpenRouter backend --
              LLM_ENDPOINT_URL        Base URL for OpenAI-compatible endpoint         [default: http://localhost:8088/v1]
              LLM_MODEL               Model name at the endpoint                      [default: anthropic/claude-sonnet-4.6]
              LLM_API_KEY             API key for the endpoint                        [default: local-qwen]
              OPENROUTER_API_KEY      Fleet-wide OpenRouter key (alternative to LLM_API_KEY)

            {HELP_TEXT_LLM_API_KEY}

            Default entity: Aaron Personal (a5dbe5e7-3c1f-5cb2-9f7c-95b5f3f1d4e2)
            Default workspace: 77753ea5-efe9-5539-a846-6b20ddc841b9
        """),
    )
    parser.add_argument(
        "--workspace-id",
        default=_DEFAULT_WORKSPACE_ID,
        help=f"Workspace UUID (default: Aaron workspace {_DEFAULT_WORKSPACE_ID})",
    )
    parser.add_argument(
        "--entity-id",
        default=_DEFAULT_ENTITY_ID,
        help=f"Entity UUID (default: Aaron Personal {_DEFAULT_ENTITY_ID})",
    )
    parser.add_argument(
        "--tax-year",
        required=True,
        type=int,
        help="Four-digit tax year (e.g. 2024)",
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
        help="Override MCP endpoint URL (default: $ACCTOPS_MCP_URL or http://localhost:8000/mcp)",
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
        help="Base URL for OpenAI-compatible endpoint. Default: $LLM_ENDPOINT_URL",
    )
    parser.add_argument(
        "--llm-model",
        default=None,
        help="Model name at the endpoint. Default: $LLM_MODEL or anthropic/claude-sonnet-4.6",
    )
    parser.add_argument(
        "--llm-api-key",
        default=None,
        help=(
            "API key for the endpoint. DEPRECATED — visible via `ps`. "
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
            "How to express tools to the model: "
            "'tools' (OpenAI function-calling, default) or "
            "'json' (structured text fallback). Ignored when --backend anthropic."
        ),
    )
    # Veteran-specific optional parameters
    parser.add_argument(
        "--va-rating",
        type=int,
        default=None,
        metavar="PCT",
        help=(
            "Known VA disability rating percentage (e.g. 60). "
            "If provided, pre-populates the user message. "
            "CPA/EA must still confirm from VA award letter."
        ),
    )
    parser.add_argument(
        "--crdp-crsc",
        choices=["CRDP", "CRSC", "crdp", "crsc"],
        default=None,
        metavar="ELECTION",
        help=(
            "Known CRDP/CRSC election ('CRDP' or 'CRSC'). "
            "If provided, pre-populates the user message. "
            "CPA/EA must still confirm from DFAS records."
        ),
    )
    parser.add_argument(
        "--combat-zone-year",
        action="store_true",
        help=(
            "Flag that the taxpayer had combat-zone deployment months in the tax year. "
            "Triggers IRC §112 combat zone exclusion analysis."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Stream tool-use events and config details to stdout",
    )
    return parser.parse_args(argv)


def _resolve_backend(args: argparse.Namespace) -> str:
    """Determine the backend based on flags and environment variables."""
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

    crdp_crsc = args.crdp_crsc.upper() if args.crdp_crsc else None

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
        va_rating=args.va_rating,
        crdp_crsc=crdp_crsc,
        combat_zone_year=args.combat_zone_year,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    main()
