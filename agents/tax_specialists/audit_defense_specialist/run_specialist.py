#!/usr/bin/env python3
"""Audit Defense Specialist — CLI runner.

Invokes the Audit Defense Specialist against the Accounting-Ops /mcp
endpoint. Supports two backends:

- **anthropic** (default when ``ANTHROPIC_API_KEY`` is set): uses the
  Anthropic SDK's native MCP client integration (``beta.messages.create``
  with ``mcp_servers``). No local tool-call loop needed.

- **openai** (default when ``CPATEAM_LLM_ENDPOINT_URL`` is set): uses the
  OpenAI Python SDK (>= 1.0) aimed at any OpenAI-compatible endpoint. Defaults
  to OpenRouter Sonnet-4.6 (the dedicated CPA-team frontier config); can be
  pointed at a local llama-server text model for the sovereign/air-gapped
  path. Implements a manual MCP tool-call loop because these endpoints have no
  native MCP integration.

Usage — Anthropic path::

    export ANTHROPIC_API_KEY="sk-ant-..."
    export ACCTOPS_SERVICE_TOKEN="<keycloak-service-token>"

    uv run python agents/tax_specialists/audit_defense_specialist/run_specialist.py \\
        --workspace-id <UUID> \\
        --entity-id    <UUID> \\
        --tax-year     2023 \\
        --notice-type  scdor-ftf \\
        --output       ./drafts/mu-inc-2023-audit-defense-scdor-ftf.md \\
        --model        sonnet

Usage — OpenRouter / OpenAI-compatible path::

    # Defaults to OpenRouter Sonnet-4.6; only OPENROUTER_API_KEY is required.
    # Override endpoint/model via CPATEAM_LLM_ENDPOINT_URL / CPATEAM_LLM_MODEL
    # (or the --llm-* flags) to target a local Qwen text model for the
    # air-gapped path.
    export OPENROUTER_API_KEY="<openrouter-api-key>"
    export ACCTOPS_SERVICE_TOKEN="<keycloak-service-token>"

    uv run python agents/tax_specialists/audit_defense_specialist/run_specialist.py \\
        --backend      openai \\
        --workspace-id <UUID> \\
        --entity-id    <UUID> \\
        --tax-year     2023 \\
        --notice-type  scdor-ftf \\
        --mcp-url      http://localhost:8000/mcp/ \\
        --output       ./drafts/mu-inc-2023-audit-defense-scdor-ftf.md \\
        --verbose

Environment variables:

    ACCTOPS_SERVICE_TOKEN     Bearer token with at least "viewer" scope on the
                              workspace. Required.
    ACCTOPS_MCP_URL           Override the MCP endpoint base URL.
                              Default: http://localhost:8000/mcp
    ANTHROPIC_API_KEY         Anthropic API key. Required for --backend anthropic.
    CPATEAM_LLM_ENDPOINT_URL  Base URL for the OpenAI-compatible frontier endpoint
                              (dedicated CPA-team config).
                              Default: https://openrouter.ai/api/v1
    CPATEAM_LLM_MODEL         Model name served by the endpoint.
                              Default: anthropic/claude-sonnet-4.6
    CPATEAM_LLM_API_KEY       API key for the endpoint. Falls back to
                              OPENROUTER_API_KEY when unset.

Notice types (--notice-type):

    scdor-ftf       SCDOR failure-to-file / SC1120-CTD delinquency notice.
                    Primary use: Acme Inc. TY 2023.
    irs-cp2000      IRS CP2000 underreporter notice.
    irs-cp504       IRS CP504 intent-to-levy notice.
    penalty-abate   Standalone reasonable-cause penalty abatement statement.
    vda             Voluntary Disclosure Agreement intake analysis only.

Notes:

- Both paths share the same system prompt, worked examples, and MCP server
  target -- only the model client differs.
- The OpenAI path implements a manual MCP tool-call loop: connect via
  ``mcp.client.streamable_http``, list tools, convert to OpenAI function
  schema, run the LLM, dispatch tool calls, loop until text-only response
  or the iteration cap (30) is hit.
- Output directory is created automatically if it does not exist.
- This specialist is DRAFT-only. It never sends, signs, or files anything.
  All output must be reviewed by a licensed CPA or tax attorney before use.
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
from app.llm_client import FailoverAsyncClient

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

_MODEL_MAP = {
    "sonnet": "claude-sonnet-4-6",
    "opus":   "claude-opus-4-7",
}
_SYSTEM_PROMPT_PATH = pathlib.Path(__file__).parent / "system_prompt.md"

# MCP tool names the specialist is allowed to call.
# Defense-in-depth: only these four tools are visible to the model.
# The specialist must NOT call write tools (e.g. propose_payment, create_journal_entry).
_ALLOWED_TOOLS: list[str] = [
    "get_form_1120_data",
    "get_period_journal",
    "draft_memo",
    "flag_for_cpa_review",
]

# Maximum tool-call iterations in the OpenAI manual loop.
_MAX_LOOP_ITERATIONS = 30

# JSON-mode system prompt suffix injected when the local model does not
# support OpenAI function calling natively.
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
# Notice-type user message templates
# ---------------------------------------------------------------------------

_NOTICE_TYPE_CONTEXT = {
    "scdor-ftf": textwrap.dedent("""\
        Notice type      : SCDOR Failure-to-File / SC1120-CTD (Corporate Tax Delinquency)
        Notice authority : South Carolina Department of Revenue (SCDOR)
        Notice date      : 2025-06-16 (issued by SCDOR for TY 2023 non-filing)
        Primary task     : Draft the SCDOR cure letter for the late SC1120, including:
                           (1) VDA eligibility analysis (TY 2023 likely forfeited; TY 2024 may apply);
                           (2) Full cure letter in letter format with statement of facts,
                               reasonable-cause abatement request under SC Code §12-54-160,
                               and proposed cure computation (license fee $60 + penalty + interest);
                           (3) Document gathering checklist for the CPA.
        Key facts        : Pre-revenue loss year (-$2,434 net loss); SC income tax = $0;
                           SC license fee = $60 (0.001 × $60,000 capital stock).
                           Books reconstructed from BofA history (87 journal entries).
                           First year of operations; no prior FTF history.
                           TY 2024 SC1120 also overdue — flag for VDA opportunity.
    """),
    "irs-cp2000": textwrap.dedent("""\
        Notice type      : IRS CP2000 (Notice of Proposed Adjustment — Underreporter)
        Notice authority : Internal Revenue Service
        Primary task     : Draft the CP2000 response letter, including:
                           (1) Item-by-item concede / dispute table;
                           (2) Narrative for each disputed item;
                           (3) Accuracy-related penalty abatement request if applicable;
                           (4) Document gathering checklist.
        Note             : The specialist does NOT determine concede/dispute —
                           that is CPA judgment. Draft all three options (concede,
                           dispute, partial dispute) and flag the determination.
    """),
    "irs-cp504": textwrap.dedent("""\
        Notice type      : IRS CP504 (Notice of Intent to Levy)
        Notice authority : Internal Revenue Service
        Primary task     : Draft the collection due process (CDP) response letter or
                           installment agreement narrative. Flag any levy-exempt assets.
                           Document gathering checklist for CDP hearing.
        Note             : If the 30-day CDP deadline has passed, flag URGENTLY —
                           CDP rights may be lost. CPA must assess CDP vs. installment
                           agreement path before responding.
    """),
    "penalty-abate": textwrap.dedent("""\
        Notice type      : Standalone Reasonable Cause Penalty Abatement Statement
        Notice authority : IRS or SCDOR (CPA to select applicable authority)
        Primary task     : Draft a standalone penalty abatement statement covering:
                           (1) Ordinary care and prudence (three-prong framework);
                           (2) Absence of willful neglect;
                           (3) Corrective action taken.
                           Flag FTA (First-Time Abatement) availability for federal penalties.
    """),
    "vda": textwrap.dedent("""\
        Notice type      : Voluntary Disclosure Agreement (VDA) Intake Analysis
        Notice authority : SCDOR (or IRS, CPA to confirm)
        Primary task     : Draft VDA eligibility analysis only. Do NOT draft a cure letter.
                           Include: eligibility criteria, lookback period, penalty waiver
                           benefit, contact protocol (SCVDA@dor.sc.gov), and
                           disqualifiers. Flag for CPA to contact SCDOR VDA unit before
                           any filing.
    """),
}

_DEFAULT_NOTICE_TYPE = "scdor-ftf"


# ---------------------------------------------------------------------------
# Helpers -- shared by both backends
# ---------------------------------------------------------------------------


def _load_system_prompt() -> str:
    """Read system_prompt.md from disk (same directory as this script)."""
    if not _SYSTEM_PROMPT_PATH.exists():
        raise FileNotFoundError(
            f"System prompt not found at {_SYSTEM_PROMPT_PATH}. "
            "Ensure you are running from the repo root or that the "
            "agents/tax_specialists/audit_defense_specialist/ directory is intact."
        )
    return _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


def _build_user_message(
    workspace_id: str,
    entity_id: str,
    tax_year: int,
    notice_type: str,
) -> str:
    """Construct the user-turn message sent to the specialist."""
    notice_context = _NOTICE_TYPE_CONTEXT.get(
        notice_type,
        f"Notice type      : {notice_type} (unrecognized — follow system prompt guidance)",
    )
    return textwrap.dedent(f"""\
        Please prepare a DRAFT audit defense response package for the following entity.
        Stamp DRAFT — CPA REVIEW REQUIRED at the top and bottom of every document.
        Include the reminder "Retain a licensed CPA or tax attorney before any action
        is taken" at the end of every output.

        workspace_id : {workspace_id}
        entity_id    : {entity_id}
        tax_year     : {tax_year}
        run_date     : {date.today().isoformat()}

        {notice_context}

        Follow the call sequence in your system prompt exactly:
        1. get_form_1120_data — establishes the financial facts for the statement of
           facts and the cure computation. Required first call, always.
        2. get_period_journal with limit=20 — provides representative transaction
           evidence for the statement of facts. Limit=20 is sufficient; the context
           window is limited. Do NOT exceed limit=20.
        3. draft_memo — write each section as a separate memo:
           (a) audit_defense_vda_analysis
           (b) audit_defense_cure_letter  (or the applicable notice-type response)
           (c) audit_defense_document_checklist
        4. flag_for_cpa_review — one call per flagged item (not batched). Flag at least:
           (a) VDA eligibility status (both TY 2023 and TY 2024 if applicable)
           (b) Any notice details missing from tool output (must supply from actual notice)
           (c) Statement of facts — reason for non-filing (CPA must select/confirm)
           (d) SC SOS dissolution status (URGENT — must check before sending letter)
        5. Emit the full DRAFT in the Markdown output schema from the system prompt.

        IMPORTANT CONSTRAINTS:
        - DRAFT-only. Never send, sign, transmit, or file.
        - Never represent the taxpayer before any authority.
        - Cite only real SC Code sections, IRC sections, Treas. Reg. sections, and
          SCDOR Revenue Procedures by number. Never invent a citation.
        - If you detect any fact suggesting willful evasion or fraud: stop drafting,
          emit a criminal-exposure flag, and recommend tax attorney engagement.
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
    """Build the mcp_servers entry for the Anthropic SDK.

    Restricts visible tools to the audit-defense-specific allowed list.
    """
    return {
        "type": "url",
        "url": mcp_url,
        "name": "accounting-ops",
        "authorization_token": service_token,
        "tool_filter": {"type": "include", "tool_names": _ALLOWED_TOOLS},
    }


# ---------------------------------------------------------------------------
# Helpers -- OpenAI / local LLM path
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
    notice_type: str,
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
    """Run the specialist using an OpenAI-compatible local endpoint."""
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
    user_message = _build_user_message(workspace_id, entity_id, tax_year, notice_type)

    if verbose:
        print(f"[runner] Backend    : openai (local endpoint)")
        print(f"[runner] Endpoint   : {llm_endpoint_url}")
        print(f"[runner] Model      : {llm_model}")
        print(f"[runner] MCP URL    : {mcp_url}")
        print(f"[runner] Entity     : {entity_id}")
        print(f"[runner] Tax year   : {tax_year}")
        print(f"[runner] Notice type: {notice_type}")
        print(f"[runner] Output     : {output_path}")
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

            # Failover-aware client (drop-in for openai.AsyncOpenAI). Carries
            # the shared User-Agent -- the UC gateway sits behind Cloudflare,
            # which 403s the openai SDK's default UA with "error code: 1010" --
            # and retries ONCE on the cloud fallback for infrastructure faults.
            # Inert unless LLM_FALLBACK_BASE_URL is set.
            client = FailoverAsyncClient(
                base_url=llm_endpoint_url,
                api_key=llm_api_key,
                model=llm_model,
                op="specialist.chat.completions",
                user=str(workspace_id),
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
                    # Request a larger per-request context window from llama.cpp.
                    # The Qwen3.6-35B-A3B server has --ctx-size 98304 with -np 4 slots
                    # (24576 per slot by default). The audit defense system prompt is
                    # large; passing num_ctx=73728 (3× the default slot) requests a
                    # single 3-slot allocation for this request so the full prompt +
                    # tool results + response fit within one call.
                    "extra_body": {"num_ctx": 73728},
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
                if tool_call_count > 0:
                    # The model completed all tool calls (memos and flags written to DB)
                    # but returned no text output — common with local Qwen models when the
                    # full context is consumed by tool result history. Generate a runner
                    # summary so the output file is still useful for the lead reviewer.
                    print(
                        "WARNING: The specialist completed all tool calls but returned "
                        "an empty final text response. This is expected behavior when "
                        "the local model context is exhausted after 9+ tool calls. "
                        "Memos and flags were written to the tax_review_queue. "
                        "Writing a run summary to the output file.",
                        file=sys.stderr,
                    )
                    draft_text = (
                        "# DRAFT — Audit Defense Specialist Run Summary\n"
                        "# WARNING: DRAFT — CPA REVIEW REQUIRED — NOT FOR FILING OR SENDING\n\n"
                        f"The specialist completed all {tool_call_count} tool calls successfully.\n"
                        "Memos and flags have been written to the tax_review_queue.\n\n"
                        "## What Was Written to the Queue\n\n"
                        "The following items were committed to the tax_review_queue via MCP:\n\n"
                        "- `draft_memo` x3: VDA eligibility analysis, cure letter draft, "
                        "document gathering checklist.\n"
                        "- `flag_for_cpa_review` x4: VDA eligibility (TY 2023), "
                        "SCDOR FTF notice details, SC SOS dissolution check, "
                        "statement of facts CPA selection.\n\n"
                        "## Why This File Is Abbreviated\n\n"
                        "The local Qwen3.6-35B-A3B model exhausted its context window "
                        f"(~24576 tokens per slot) after {tool_call_count} tool calls and "
                        "returned no text output for the final Markdown summary. "
                        "All substantive output was written to the database via the tool calls above.\n\n"
                        "## CPA Next Steps\n\n"
                        "1. Review all items in the tax_review_queue for entity "
                        f"11111111-1111-1111-1111-111111111111 / TY 2023.\n"
                        "2. The cure letter draft is in the `audit_defense_cure_letter` memo.\n"
                        "3. The VDA analysis is in the `audit_defense_vda_analysis` memo.\n"
                        "4. The document checklist is in the `audit_defense_document_checklist` memo.\n"
                        "5. Four CPA flags require action before any letter is sent.\n\n"
                        "## Re-Run Note\n\n"
                        "To get the full Markdown output, re-run with `--backend anthropic` "
                        "(Anthropic Claude has a larger context window and handles this run cleanly):\n\n"
                        "```\n"
                        "export ANTHROPIC_API_KEY=sk-ant-...\n"
                        "python run_specialist.py --backend anthropic --model sonnet \\\n"
                        "    --workspace-id 00000000-0000-0000-0000-000000000000 \\\n"
                        "    --entity-id 11111111-1111-1111-1111-111111111111 \\\n"
                        "    --tax-year 2023 --notice-type scdor-ftf\n"
                        "```\n\n"
                        "---\n"
                        "# WARNING: DRAFT — CPA REVIEW REQUIRED — NOT FOR FILING OR SENDING\n"
                        "# Retain a licensed CPA or tax attorney before any action is taken.\n"
                        f"# Tool calls dispatched: {tool_call_count}\n"
                    )
                else:
                    print(
                        "ERROR: The specialist returned an empty response with no tool calls. "
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


# ---------------------------------------------------------------------------
# Anthropic path
# ---------------------------------------------------------------------------


def _run_anthropic_path(
    workspace_id: str,
    entity_id: str,
    tax_year: int,
    notice_type: str,
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
    user_message = _build_user_message(workspace_id, entity_id, tax_year, notice_type)

    if verbose:
        print(f"[runner] Backend    : anthropic")
        print(f"[runner] Model      : {model_id}")
        print(f"[runner] MCP URL    : {mcp_url}")
        print(f"[runner] Entity     : {entity_id}")
        print(f"[runner] Tax year   : {tax_year}")
        print(f"[runner] Notice type: {notice_type}")
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


# ---------------------------------------------------------------------------
# Top-level run dispatcher
# ---------------------------------------------------------------------------


def run(
    workspace_id: str,
    entity_id: str,
    tax_year: int,
    notice_type: str,
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
            notice_type=notice_type,
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
            notice_type=notice_type,
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
            "Run the Audit Defense Specialist and write a DRAFT response package "
            "to the specified output file. DRAFT-only — never sends or files anything."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(f"""\
            Environment variables:
              ACCTOPS_SERVICE_TOKEN   Bearer token for the /mcp endpoint              [required]
              ACCTOPS_MCP_URL         MCP endpoint base URL                           [default: http://localhost:8000/mcp]

              -- Anthropic backend --
              ANTHROPIC_API_KEY       Anthropic API key                               [required for --backend anthropic]

              -- OpenAI / frontier backend (dedicated CPA-team config) --
              CPATEAM_LLM_ENDPOINT_URL  Base URL for OpenAI-compatible endpoint        [default: https://openrouter.ai/api/v1]
              CPATEAM_LLM_MODEL         Model name at the endpoint                     [default: anthropic/claude-sonnet-4.6]
              CPATEAM_LLM_API_KEY       API key for the endpoint                       [falls back to OPENROUTER_API_KEY]
              OPENROUTER_API_KEY        Fleet-wide OpenRouter key (fallback for CPATEAM_LLM_API_KEY)

            {HELP_TEXT_LLM_API_KEY}

            Notice types (--notice-type):
              scdor-ftf       SCDOR failure-to-file / SC1120-CTD delinquency notice (default)
              irs-cp2000      IRS CP2000 underreporter notice
              irs-cp504       IRS CP504 intent-to-levy notice
              penalty-abate   Standalone reasonable-cause penalty abatement statement
              vda             Voluntary Disclosure Agreement intake analysis only
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
        help="Four-digit tax year (e.g. 2023)",
    )
    parser.add_argument(
        "--notice-type",
        choices=list(_NOTICE_TYPE_CONTEXT.keys()),
        default=_DEFAULT_NOTICE_TYPE,
        help=(
            "Type of notice or defense task. Default: scdor-ftf. "
            "See 'Notice types' in --help epilog."
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
        help="Override MCP endpoint URL (default: $ACCTOPS_MCP_URL or http://localhost:8000/mcp)",
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
            "Override the OpenAI-compatible endpoint base URL "
            "(default: $CPATEAM_LLM_ENDPOINT_URL or https://openrouter.ai/api/v1)."
        ),
    )
    parser.add_argument(
        "--llm-model",
        default=None,
        help=(
            "Override the model name served by the endpoint "
            "(default: $CPATEAM_LLM_MODEL or anthropic/claude-sonnet-4.6)."
        ),
    )
    parser.add_argument(
        "--llm-api-key",
        default=None,
        help=(
            "API key for the endpoint. DEPRECATED — visible via `ps`. "
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
            "'tools' (OpenAI function-calling, default) or "
            "'json' (structured text fallback). Ignored when --backend anthropic."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Stream tool-use events and config details to stdout",
    )
    return parser.parse_args(argv)


def _resolve_backend(args: argparse.Namespace) -> str:
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
        notice_type=args.notice_type,
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
