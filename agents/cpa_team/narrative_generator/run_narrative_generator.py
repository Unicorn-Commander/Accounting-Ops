#!/usr/bin/env python3
"""CPA Narrative Generator — single-shot runner per (workspace, entity, tax_year).

Synthesises all tax_review_queue items + specialist drafts into a single coherent
CPA client memo (2-5 pages). This is what Morgan sees FIRST — a professional review
letter covering all 11 sections: Cover, Executive Summary, Year at a Glance, Federal
Return Position, 1099-NEC, State Return, Strategy, Sign-Off Items, Judgment Items,
Next Steps, and Appendix.

Usage — docker exec pattern (production)::

    TOKEN=$(cat /home/muut/.tax-1120-preparer.token)
    OPENROUTER_KEY=$(cat /home/muut/.openrouter.key)
    docker exec \\
      -e ACCTOPS_SERVICE_TOKEN="$TOKEN" \\
      accounting_ops-app-1 \\
      python /app/agents/cpa_team/narrative_generator/run_narrative_generator.py \\
        --workspace-id 77753ea5-efe9-5539-a846-6b20ddc841b9 \\
        --entity-id    1ab938df-43f2-567a-8566-800f8a14a466 \\
        --tax-year     2023 \\
        --mcp-url      http://localhost:8000/mcp/ \\
        --output       /drafts/mu-inc-2023-cpa-narrative.md \\
        --backend      openai \\
        --llm-endpoint-url https://openrouter.ai/api/v1 \\
        --llm-model    anthropic/claude-sonnet-4.6 \\
        --llm-api-key  "$OPENROUTER_KEY" \\
        --verbose

Usage — direct (development)::

    export ACCTOPS_SERVICE_TOKEN="..."
    uv run python agents/cpa_team/narrative_generator/run_narrative_generator.py \\
        --workspace-id <UUID> \\
        --entity-id    <UUID> \\
        --tax-year     2023 \\
        --output       ./drafts/mu-inc-2023-cpa-narrative.md \\
        --verbose

Environment variables:

    ACCTOPS_SERVICE_TOKEN   Bearer token with accountant role on the workspace.
                            Required. The existing ``tax-1120-preparer`` service
                            token (role_floor=accountant) satisfies this.
    ACCTOPS_MCP_URL         Override the MCP endpoint URL.
                            Default: http://localhost:8000/mcp

Backend:

    --backend anthropic     Use Anthropic SDK directly (native MCP integration).
    --backend openai        Use OpenAI-compatible endpoint (e.g., OpenRouter).
                            Default when LLM_ENDPOINT_URL is set.

Flow:
    1. Load system_prompt.md from this directory.
    2. Build the user message requesting the narrative.
    3. Call the LLM with MCP tool access (get_form_1120_data, get_tax_review_queue,
       get_period_journal, draft_memo).
    4. The LLM fetches data, composes the narrative, calls draft_memo to persist it.
    5. Write the final Markdown text to --output.
    6. Print a summary to stdout (section count, word count, queue items covered).

Idempotent: a new narrative memo is always created (no deduplication in the runner —
the API endpoint handles force_regenerate logic).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
import textwrap
from datetime import date
from typing import Any

try:
    import anthropic as _anthropic_module
except ImportError:
    _anthropic_module = None  # type: ignore[assignment]

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
_DEFAULT_LLM_API_KEY = "local-key"

_SYSTEM_PROMPT_PATH = pathlib.Path(__file__).parent / "system_prompt.md"

# Tools the narrative generator is allowed to call.
#
# Note: ``draft_memo`` is deliberately omitted.  The runner persists the
# generated narrative directly after the LLM loop (see ``_persist_narrative``);
# this avoids the LLM having to emit the full 5K–10K-token narrative *inside*
# a tool-call arguments JSON, which previously blew the ``max_tokens`` budget
# on OpenRouter and resulted in an empty ``function.arguments`` payload.
_ALLOWED_TOOLS: list[str] = [
    "get_form_1120_data",
    "get_tax_review_queue",
    "get_period_journal",
]

_MAX_LOOP_ITERATIONS = 20

# Output token budget for the LLM.  The narrative itself is ~5K–10K tokens;
# 16 384 leaves headroom for the structured Markdown plus any per-iteration
# tool-call boilerplate.  Both Claude Sonnet 4.6 and Opus 4.7 support this.
_MAX_OUTPUT_TOKENS = 16384


# ---------------------------------------------------------------------------
# Helpers — shared
# ---------------------------------------------------------------------------


def _load_system_prompt() -> str:
    if not _SYSTEM_PROMPT_PATH.exists():
        raise FileNotFoundError(
            f"System prompt not found at {_SYSTEM_PROMPT_PATH}. "
            "Ensure you are running from the repo root or that "
            "agents/cpa_team/narrative_generator/ is intact."
        )
    return _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


def _build_user_message(workspace_id: str, entity_id: str, tax_year: int) -> str:
    return textwrap.dedent(f"""\
        Please generate the CPA Narrative Memo for the following entity-year.

        workspace_id : {workspace_id}
        entity_id    : {entity_id}
        tax_year     : {tax_year}
        run_date     : {date.today().isoformat()}
        requesting_cpa : Morgan Qureshi

        Follow the call sequence in your system prompt exactly:
        1. get_form_1120_data — get the federal package
        2. get_tax_review_queue — get ALL queue items (include_resolved=true)
        3. get_period_journal — only if a specific figure needs verification
        4. Compose the full 11-section Markdown narrative
        5. draft_memo — persist the narrative with topic="cpa_narrative_{tax_year}"
        6. Return the full Markdown text as your final output

        The narrative must begin with the DRAFT watermark and end with the standard
        DRAFT footer block from the system prompt.
    """)


def _extract_text(response: Any) -> str:
    """Extract final text from an Anthropic Messages response."""
    parts: list[str] = []
    for block in response.content:
        if hasattr(block, "text"):
            parts.append(block.text)
    return "\n".join(parts)


def _mcp_server_config(mcp_url: str, service_token: str) -> dict[str, Any]:
    return {
        "type": "url",
        "url": mcp_url,
        "name": "accounting-ops",
        "authorization_token": service_token,
        "tool_filter": {"type": "include", "tool_names": _ALLOWED_TOOLS},
    }


# ---------------------------------------------------------------------------
# Anthropic backend
# ---------------------------------------------------------------------------


def _run_anthropic(
    workspace_id: str,
    entity_id: str,
    tax_year: int,
    service_token: str,
    mcp_url: str,
    model: str,
    output_path: pathlib.Path,
    *,
    verbose: bool = False,
) -> str:
    """Run the narrative generator using the Anthropic SDK with native MCP support."""
    if _anthropic_module is None:
        print("ERROR: 'anthropic' package not found.", file=sys.stderr)
        sys.exit(1)

    import anthropic

    system_prompt = _load_system_prompt()
    user_message = _build_user_message(workspace_id, entity_id, tax_year)

    # Map shorthand model names
    _model_map = {
        "sonnet": "claude-sonnet-4-6",
        "opus": "claude-opus-4-7",
        "haiku": "claude-haiku-4-5",
    }
    resolved_model = _model_map.get(model, model)

    if verbose:
        print(f"[narrative] Anthropic backend: model={resolved_model}")
        print(f"[narrative] MCP URL: {mcp_url}")

    client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=resolved_model,
        max_tokens=_MAX_OUTPUT_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
        mcp_servers=[_mcp_server_config(mcp_url, service_token)],
        betas=["mcp-client-2025-04-04"],
    )

    narrative_text = _extract_text(response)

    if verbose:
        print(
            f"[narrative] Anthropic response: "
            f"input_tokens={getattr(response.usage, 'input_tokens', '?')}, "
            f"output_tokens={getattr(response.usage, 'output_tokens', '?')}"
        )

    return narrative_text


# ---------------------------------------------------------------------------
# OpenAI / OpenRouter backend (manual MCP tool loop)
# ---------------------------------------------------------------------------


def _mcp_tool_to_openai_function(tool: Any) -> dict[str, Any]:
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


def _run_openai(
    workspace_id: str,
    entity_id: str,
    tax_year: int,
    service_token: str,
    mcp_url: str,
    llm_endpoint_url: str,
    llm_model: str,
    llm_api_key: str,
    output_path: pathlib.Path,
    *,
    verbose: bool = False,
) -> str:
    """Run the narrative generator using an OpenAI-compatible endpoint with manual MCP loop."""
    if _openai_module is None:
        print("ERROR: 'openai' package not found.", file=sys.stderr)
        sys.exit(1)

    try:
        import mcp.client.streamable_http as _mcp_transport
        from mcp.client.session import ClientSession as _MCPSession
    except ImportError:
        print(
            "ERROR: 'mcp' package not found. Install it: uv pip install mcp",
            file=sys.stderr,
        )
        sys.exit(1)

    import httpx  # noqa: F401  (transitive dependency used via openai)
    import openai

    system_prompt = _load_system_prompt()
    user_message = _build_user_message(workspace_id, entity_id, tax_year)

    if verbose:
        print(f"[narrative] OpenAI backend: model={llm_model}")
        print(f"[narrative] MCP URL: {mcp_url}")
        print(f"[narrative] LLM endpoint: {llm_endpoint_url}")

    async def _async_run() -> str:
        # Connect to the MCP server via StreamableHTTP.
        transport = _mcp_transport.streamablehttp_client(
            url=mcp_url,
            headers={"Authorization": f"Bearer {service_token}"},
        )
        async with transport as (read, write, _):
            async with _MCPSession(read, write) as mcp_session:
                await mcp_session.initialize()

                # Fetch available tools and filter to allowed set.
                tools_response = await mcp_session.list_tools()
                all_tools = tools_response.tools
                tools = [
                    t for t in all_tools if t.name in _ALLOWED_TOOLS
                ]
                if verbose:
                    print(
                        f"[narrative] MCP tools available: "
                        f"{[t.name for t in tools]}"
                    )

                openai_tools = [_mcp_tool_to_openai_function(t) for t in tools]
                llm = openai.OpenAI(base_url=llm_endpoint_url, api_key=llm_api_key)

                messages: list[dict[str, Any]] = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ]

                narrative_text = ""

                for iteration in range(_MAX_LOOP_ITERATIONS):
                    if verbose:
                        print(f"[narrative] LLM iteration {iteration + 1}")

                    response = llm.chat.completions.create(
                        model=llm_model,
                        messages=messages,
                        tools=openai_tools if openai_tools else None,
                        tool_choice="auto" if openai_tools else None,
                        max_tokens=_MAX_OUTPUT_TOKENS,
                        temperature=0.1,
                    )

                    choice = response.choices[0]
                    msg = choice.message
                    messages.append(msg.model_dump(exclude_none=True))

                    # Check for tool calls.
                    tool_calls = getattr(msg, "tool_calls", None) or []

                    if not tool_calls:
                        # Final text response.
                        narrative_text = msg.content or ""
                        if verbose:
                            print(
                                f"[narrative] Final response received "
                                f"({len(narrative_text)} chars)"
                            )
                        break

                    # Dispatch tool calls to MCP server.
                    for tc in tool_calls:
                        fn_name = tc.function.name
                        try:
                            fn_args = json.loads(tc.function.arguments or "{}")
                        except json.JSONDecodeError:
                            fn_args = {}

                        if verbose:
                            print(
                                f"[narrative]   tool_call: {fn_name}("
                                f"{list(fn_args.keys())})"
                            )

                        try:
                            result = await mcp_session.call_tool(fn_name, fn_args)
                            result_content = result.content
                            if isinstance(result_content, list):
                                # FastMCP returns list of TextContent objects
                                result_text = json.dumps([
                                    c.text if hasattr(c, "text") else str(c)
                                    for c in result_content
                                ])
                            else:
                                result_text = json.dumps(result_content)
                        except Exception as exc:
                            result_text = json.dumps({"error": str(exc)})
                            if verbose:
                                print(
                                    f"[narrative]   tool_call {fn_name} FAILED: {exc}",
                                    file=sys.stderr,
                                )

                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": result_text,
                        })

                return narrative_text

    return asyncio.run(_async_run())


# ---------------------------------------------------------------------------
# Post-run Section-1 (Cover) injection guard
# ---------------------------------------------------------------------------

import re as _re
from datetime import datetime as _dt, timezone as _tz

# Match the literal opening of Section 1 — anything else in the first
# non-blank line means the LLM skipped the cover.
_COVER_OPENING_RX = _re.compile(r"^\s*#\s+CPA Review Memo", _re.MULTILINE)

# Look for the entity name in the body — narratives typically mention it
# repeatedly.  Match a capitalized phrase ending in Inc., LLC, Corp., or Co.
_ENTITY_NAME_RX = _re.compile(
    r"\b([A-Z][\w&.,'\- ]{2,80}?(?:Inc|LLC|Corp|Co|Ltd|L\.L\.C|L\.P)\.?)\b"
)


def _extract_entity_name_from_body(body: str) -> str | None:
    """Best-effort entity-name extraction from narrative body, for cover
    injection.  Returns the most frequent match (>=3 occurrences) or None."""
    matches = _ENTITY_NAME_RX.findall(body[:20000])  # first 20K chars is enough
    if not matches:
        return None
    counts: dict[str, int] = {}
    for m in matches:
        counts[m] = counts.get(m, 0) + 1
    best = max(counts.items(), key=lambda kv: kv[1])
    return best[0] if best[1] >= 2 else None


def _build_cover(
    entity_name: str | None,
    entity_id: str,
    tax_year: int,
    cpa_name: str = "Morgan Qureshi",
) -> str:
    """Build a Section 1 cover block as a fallback when the LLM skipped it."""
    name = entity_name or f"Entity {entity_id[:8]}"
    short_id = entity_id[:8]
    today_iso = _dt.now(tz=_tz.utc).date().isoformat()
    return (
        f"# CPA Review Memo — {name}\n"
        f"### Tax Year {tax_year} · Form 1120 (Federal) + SC1120 (State)\n"
        f"\n"
        f"> **DRAFT — AI-PREPARED — FOR CPA REVIEW AND SIGNATURE ONLY**\n"
        f"> Prepared by: Accounting-Ops Narrative Generator (AI, no PTIN)\n"
        f"> Prepared for: {cpa_name}, CPA\n"
        f"> Entity: {name} ({short_id})\n"
        f"> Tax Year: {tax_year}\n"
        f"> Generated: {today_iso}\n"
        f">\n"
        f"> This document synthesises AI-prepared specialist drafts. It has NOT been\n"
        f"> reviewed by a licensed tax professional. Every position must be reviewed\n"
        f"> before filing.\n"
        f">\n"
        f"> *Cover auto-injected by runner — the LLM skipped Section 1.*\n"
        f"\n"
        f"---\n"
        f"\n"
    )


def _ensure_cover_present(
    narrative_text: str,
    entity_id: str,
    tax_year: int,
    *,
    verbose: bool = False,
) -> str:
    """If narrative_text already starts with a Section 1 cover, return it
    unchanged.  Otherwise prepend a generated cover and log a warning."""
    if not narrative_text.strip():
        return narrative_text
    if _COVER_OPENING_RX.search(narrative_text[:2000]):
        return narrative_text  # cover already present

    entity_name = _extract_entity_name_from_body(narrative_text)
    cover = _build_cover(entity_name, entity_id, tax_year)
    if verbose:
        print(
            f"[narrative] cover-guard: Section 1 missing — injecting "
            f"(entity_name={entity_name!r})",
            file=sys.stderr,
        )
    return cover + narrative_text


# ---------------------------------------------------------------------------
# Post-run persistence (runner-side, no LLM tool call required)
# ---------------------------------------------------------------------------


async def _persist_narrative_via_mcp(
    mcp_url: str,
    service_token: str,
    workspace_id: str,
    entity_id: str,
    tax_year: int,
    narrative_text: str,
    *,
    verbose: bool = False,
) -> str | None:
    """Persist the generated narrative as a ``memo`` row via the MCP draft_memo
    tool.  Returns the item_id on success, ``None`` on failure (printed to
    stderr — non-fatal because the Markdown file on disk is the primary
    deliverable).

    Why this lives outside the LLM loop: emitting a 5K–10K-token narrative
    *inside* a tool-call ``arguments`` JSON exceeds the model's ``max_tokens``
    budget on many endpoints and silently truncates to an empty payload.  By
    persisting from the runner we make the write deterministic and decoupled
    from output-token limits.
    """
    try:
        import mcp.client.streamable_http as _mcp_transport
        from mcp.client.session import ClientSession as _MCPSession
    except ImportError:
        print(
            "[narrative] persist: mcp package not available — skipping DB insert",
            file=sys.stderr,
        )
        return None

    transport = _mcp_transport.streamablehttp_client(
        url=mcp_url,
        headers={"Authorization": f"Bearer {service_token}"},
    )
    try:
        async with transport as (read, write, _):
            async with _MCPSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "draft_memo",
                    {
                        "workspace_id": workspace_id,
                        "entity_id": entity_id,
                        "tax_year": tax_year,
                        "topic": f"cpa_narrative_{tax_year}",
                        "content": narrative_text,
                        "confidence": "high",
                    },
                )
    except Exception as exc:  # noqa: BLE001
        print(
            f"[narrative] persist: draft_memo MCP call FAILED: {exc}",
            file=sys.stderr,
        )
        return None

    # Result payload from FastMCP is a list of TextContent; the tool returns
    # ``{"ok": True, "item_id": "<uuid>", ...}``.
    try:
        contents = result.content if hasattr(result, "content") else []
        for c in contents:
            text_payload = getattr(c, "text", None) or str(c)
            if "item_id" in text_payload:
                import json as _json
                try:
                    data = _json.loads(text_payload)
                    item_id = data.get("item_id")
                    if verbose:
                        print(f"[narrative] persist: memo row id = {item_id}")
                    return item_id
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass
    if verbose:
        print("[narrative] persist: draft_memo returned without item_id")
    return None


# ---------------------------------------------------------------------------
# Post-run analysis helpers
# ---------------------------------------------------------------------------


def _analyze_narrative(text: str) -> dict[str, Any]:
    """Extract basic metrics from the generated narrative."""
    word_count = len(text.split())
    sections = [
        line for line in text.splitlines()
        if line.startswith("## ")
    ]
    h3_sections = [
        line for line in text.splitlines()
        if line.startswith("### ")
    ]
    has_draft_watermark = "DRAFT" in text[:500]
    has_draft_footer = "DRAFT" in text[-500:]

    return {
        "word_count": word_count,
        "char_count": len(text),
        "section_count": len(sections),
        "sub_section_count": len(h3_sections),
        "has_draft_watermark": has_draft_watermark,
        "has_draft_footer": has_draft_footer,
        "sections": sections[:15],  # first 15 section headings
    }


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------


def run(
    workspace_id: str,
    entity_id: str,
    tax_year: int,
    service_token: str,
    mcp_url: str,
    output_path: pathlib.Path,
    backend: str,
    llm_endpoint_url: str,
    llm_model: str,
    llm_api_key: str,
    *,
    verbose: bool = False,
) -> None:
    """Run the CPA Narrative Generator end-to-end for one entity-year."""

    if verbose:
        print(f"[narrative] === CPA Narrative Generator ===")
        print(f"[narrative] Entity:    {entity_id}")
        print(f"[narrative] Tax year:  {tax_year}")
        print(f"[narrative] Workspace: {workspace_id}")
        print(f"[narrative] Backend:   {backend}")
        print(f"[narrative] MCP URL:   {mcp_url}")
        print()

    # ------------------------------------------------------------------
    # Run the LLM
    # ------------------------------------------------------------------
    if backend == "anthropic":
        narrative_text = _run_anthropic(
            workspace_id=workspace_id,
            entity_id=entity_id,
            tax_year=tax_year,
            service_token=service_token,
            mcp_url=mcp_url,
            model=llm_model,
            output_path=output_path,
            verbose=verbose,
        )
    else:
        narrative_text = _run_openai(
            workspace_id=workspace_id,
            entity_id=entity_id,
            tax_year=tax_year,
            service_token=service_token,
            mcp_url=mcp_url,
            llm_endpoint_url=llm_endpoint_url,
            llm_model=llm_model,
            llm_api_key=llm_api_key,
            output_path=output_path,
            verbose=verbose,
        )

    if not narrative_text.strip():
        print(
            "[narrative] WARNING: LLM returned an empty response. "
            "Check MCP connectivity and model availability.",
            file=sys.stderr,
        )
        narrative_text = (
            "# CPA Narrative — Generation Failed\n\n"
            f"The narrative generator returned an empty response for "
            f"entity {entity_id}, year {tax_year}. "
            "Re-run with --verbose to diagnose.\n\n"
            "DRAFT — not for distribution."
        )

    # ------------------------------------------------------------------
    # Cover-injection guard: prepend Section 1 if the LLM skipped it.
    # The LLM has been observed to start at Section 2 (Executive Summary)
    # ~50% of runs despite the prompt's explicit instructions.  The cover
    # holds the DRAFT watermark that downstream tooling looks for.
    # ------------------------------------------------------------------
    narrative_text = _ensure_cover_present(
        narrative_text, entity_id, tax_year, verbose=verbose,
    )

    # ------------------------------------------------------------------
    # Write output file
    # ------------------------------------------------------------------
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(narrative_text, encoding="utf-8")
    print(f"[narrative] Narrative written to: {output_path}")

    # ------------------------------------------------------------------
    # Persist narrative as a memo via MCP (runner-side; the LLM does not
    # call draft_memo — see _ALLOWED_TOOLS comment for the rationale).
    # ------------------------------------------------------------------
    if narrative_text and "Generation Failed" not in narrative_text[:200]:
        item_id = asyncio.run(
            _persist_narrative_via_mcp(
                mcp_url=mcp_url,
                service_token=service_token,
                workspace_id=workspace_id,
                entity_id=entity_id,
                tax_year=tax_year,
                narrative_text=narrative_text,
                verbose=verbose,
            )
        )
        if item_id:
            print(f"[narrative] Persisted to tax_review_queue item_id={item_id}")
        else:
            print(
                "[narrative] WARNING: persist step did not return an item_id "
                "— check MCP / draft_memo logs. The Markdown file is still on disk.",
                file=sys.stderr,
            )

    # ------------------------------------------------------------------
    # Print analysis
    # ------------------------------------------------------------------
    analysis = _analyze_narrative(narrative_text)
    print()
    print("[narrative] === Narrative Summary ===")
    print(f"[narrative] Word count:       {analysis['word_count']}")
    print(f"[narrative] Character count:  {analysis['char_count']}")
    print(f"[narrative] ## Sections:      {analysis['section_count']}")
    print(f"[narrative] ### Sub-sections: {analysis['sub_section_count']}")
    print(
        f"[narrative] DRAFT watermark:  "
        f"{'YES' if analysis['has_draft_watermark'] else 'MISSING — re-check prompt'}"
    )
    print(
        f"[narrative] DRAFT footer:     "
        f"{'YES' if analysis['has_draft_footer'] else 'MISSING — re-check prompt'}"
    )
    if analysis["sections"]:
        print("[narrative] Top-level sections found:")
        for s in analysis["sections"]:
            print(f"  {s}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "CPA Narrative Generator — synthesise all queue items + specialist "
            "drafts into a single CPA review memo."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Environment variables:
              ACCTOPS_SERVICE_TOKEN    Bearer token (accountant role floor)   [required]
              ACCTOPS_MCP_URL          MCP server URL                          [default: http://localhost:8000/mcp]
              LLM_ENDPOINT_URL         OpenAI-compatible endpoint URL          [default: http://localhost:8088/v1]
              LLM_MODEL                Model name                              [default: anthropic/claude-sonnet-4.6]
              LLM_API_KEY              API key                                 [default: local-key]
        """),
    )
    parser.add_argument("--workspace-id", required=True, help="Workspace UUID")
    parser.add_argument("--entity-id", required=True, help="Entity UUID")
    parser.add_argument("--tax-year", required=True, type=int, help="Four-digit tax year")
    parser.add_argument(
        "--output", required=True, type=pathlib.Path,
        help="Output path for the Markdown narrative",
    )
    parser.add_argument(
        "--mcp-url", default=None,
        help="MCP server URL (default: $ACCTOPS_MCP_URL or http://localhost:8000/mcp)",
    )
    parser.add_argument(
        "--backend",
        choices=["anthropic", "openai"],
        default=None,
        help="LLM backend (default: auto-detect from env)",
    )
    parser.add_argument(
        "--llm-endpoint-url", default=None,
        help="OpenAI-compatible endpoint URL",
    )
    parser.add_argument(
        "--llm-model", default=None,
        help="Model name at the endpoint",
    )
    parser.add_argument(
        "--llm-api-key", default=None,
        help="API key for the endpoint",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Stream progress events to stdout",
    )
    return parser.parse_args(argv)


def _resolve_backend(args: argparse.Namespace) -> str:
    if args.backend:
        return args.backend
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("LLM_ENDPOINT_URL"):
        return "openai"
    return "openai"


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    service_token = os.environ.get("ACCTOPS_SERVICE_TOKEN", "")
    if not service_token:
        print(
            "ERROR: ACCTOPS_SERVICE_TOKEN environment variable is required.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not (1990 <= args.tax_year <= date.today().year + 1):
        print(
            f"ERROR: --tax-year {args.tax_year} is not a plausible tax year.",
            file=sys.stderr,
        )
        sys.exit(1)

    mcp_url = (
        args.mcp_url
        or os.environ.get("ACCTOPS_MCP_URL")
        or _DEFAULT_MCP_URL
    )
    backend = _resolve_backend(args)
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
    llm_api_key = (
        args.llm_api_key
        or os.environ.get("LLM_API_KEY")
        or os.environ.get("OPENROUTER_KEY")
        or _DEFAULT_LLM_API_KEY
    )

    run(
        workspace_id=args.workspace_id,
        entity_id=args.entity_id,
        tax_year=args.tax_year,
        service_token=service_token,
        mcp_url=mcp_url,
        output_path=args.output,
        backend=backend,
        llm_endpoint_url=llm_endpoint_url,
        llm_model=llm_model,
        llm_api_key=llm_api_key,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    main()
