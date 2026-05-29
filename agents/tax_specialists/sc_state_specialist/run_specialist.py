#!/usr/bin/env python3
"""SC State Tax Specialist — CLI runner.

Invokes the SC State Tax Specialist against the Accounting-Ops /mcp endpoint
using the Anthropic SDK with native MCP tool integration. Streams the model
response and writes the final DRAFT to an output file.

Usage::

    uv run python agents/tax_specialists/sc_state_specialist/run_specialist.py \\
        --workspace-id <UUID> \\
        --entity-id    <UUID> \\
        --tax-year     2023 \\
        --output       ./drafts/mu-inc-2023-sc1120-draft.md \\
        --model        sonnet   # or "opus"

Environment variables:

    ACCTOPS_SERVICE_TOKEN   Bearer token with at least "viewer" scope on the
                            workspace. Required.
    ACCTOPS_MCP_URL         Override the MCP endpoint base URL.
                            Default: http://localhost:8000/mcp
    ANTHROPIC_API_KEY       Anthropic API key. Required (standard SDK env var).

Notes:

- IMPORTANT: Run the Form 1120 Preparer first (same entity_id + tax_year) before
  invoking this runner. The SC specialist calls get_form_1120_data to read the
  federal package as its starting point.
- The runner uses the Anthropic Python SDK's native MCP client integration
  (``anthropic.beta.messages.create`` with ``mcp_servers`` parameter), which
  requires ``anthropic >= 0.44.0`` and the ``mcp`` extra.
- The /mcp endpoint must be running locally (``make run``) or reachable via the
  configured URL before the runner is invoked.
- Output directory is created automatically if it does not exist.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
import textwrap
from datetime import date
from typing import Any

# ---------------------------------------------------------------------------
# Dependency check
# ---------------------------------------------------------------------------

try:
    import anthropic
except ImportError:
    print(
        "ERROR: 'anthropic' package not found.\n"
        "Install the tax-agent extras:\n"
        "    uv pip install accounting-ops[tax-agents]\n"
        "or:\n"
        "    uv pip install 'anthropic>=0.44.0'",
        file=sys.stderr,
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_MCP_URL = "http://localhost:8000/mcp"
_MODEL_MAP = {
    "sonnet": "claude-sonnet-4-6",
    "opus":   "claude-opus-4-7",
}
_SYSTEM_PROMPT_PATH = pathlib.Path(__file__).parent / "system_prompt.md"

# SC State Tax Specialist allowed tools — principle of least privilege.
# Excludes get_1099_payees (1099 Specialist scope only).
_ALLOWED_TOOLS: list[str] = [
    "get_form_1120_data",
    "get_period_journal",
    "draft_memo",
    "flag_for_cpa_review",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_system_prompt() -> str:
    """Read system_prompt.md from disk (same directory as this script)."""
    if not _SYSTEM_PROMPT_PATH.exists():
        raise FileNotFoundError(
            f"System prompt not found at {_SYSTEM_PROMPT_PATH}. "
            "Ensure you are running from the repo root or that the "
            "agents/tax_specialists/sc_state_specialist/ directory is intact."
        )
    return _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


def _build_user_message(
    workspace_id: str,
    entity_id: str,
    tax_year: int,
) -> str:
    """Construct the user-turn message sent to the SC specialist."""
    return textwrap.dedent(f"""\
        Please prepare a DRAFT SC1120 state tax package for the following entity.

        workspace_id : {workspace_id}
        entity_id    : {entity_id}
        tax_year     : {tax_year}
        run_date     : {date.today().isoformat()}

        Follow the call sequence in your system prompt exactly:
        1. get_form_1120_data — always first; use the federal package as your starting point.
        2. get_period_journal — for the full tax year (sales/use tax sweep) and for any
           SC depreciation modification analysis. Document why each call was made.
        3. draft_memo — write the SC reviewer narrative, failure-to-file cure package
           (if applicable), and sales/use tax sweep memo.
        4. flag_for_cpa_review — one call per flagged item (SC depreciation modification,
           SC NOL carryforward, SC sales/use tax exposure, failure-to-file if applicable).
           Write flags during processing, not batched at the end.
        5. Emit the full DRAFT output in the Markdown schema from your system prompt.

        Stamp the output DRAFT — CPA REVIEW REQUIRED at the top and bottom.
        Use "SC1120, ..." as the form_reference prefix in all flag_for_cpa_review calls.
        Include all memo IDs and flag IDs in the footer.

        If the entity is non-compliant for this tax year (SC1120 not filed), produce the
        full failure-to-file cure package as specified in your system prompt.
    """)


def _extract_text_from_response(response: Any) -> str:
    """Pull the final text content from an Anthropic Messages response object."""
    parts: list[str] = []
    for block in response.content:
        if hasattr(block, "text"):
            parts.append(block.text)
    return "\n".join(parts)


def _collect_tool_use_summaries(response: Any) -> list[dict[str, Any]]:
    """Return a brief summary of each tool_use block in the response."""
    summaries: list[dict[str, Any]] = []
    for block in response.content:
        if getattr(block, "type", None) == "tool_use":
            summaries.append({
                "tool": block.name,
                "id": block.id,
                "input_keys": list(block.input.keys()) if isinstance(block.input, dict) else [],
            })
    return summaries


# ---------------------------------------------------------------------------
# MCP server config builder
# ---------------------------------------------------------------------------


def _mcp_server_config(mcp_url: str, service_token: str) -> dict[str, Any]:
    """Build the mcp_servers entry for the Anthropic SDK.

    ``tool_filter`` restricts the model to only the SC specialist's allowed
    tools — defense in depth against accidentally calling write tools or the
    get_1099_payees tool (which is 1099 Specialist scope only).
    """
    return {
        "type": "url",
        "url": mcp_url,
        "name": "accounting-ops",
        "authorization_token": service_token,
        "tool_filter": {"type": "include", "tool_names": _ALLOWED_TOOLS},
    }


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------


def run(
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
    """Run the SC State Tax Specialist end-to-end."""
    model_id = _MODEL_MAP[model_key]
    system_prompt = _load_system_prompt()
    user_message = _build_user_message(workspace_id, entity_id, tax_year)

    if verbose:
        print(f"[runner] Specialist : SC State Tax Specialist")
        print(f"[runner] Model      : {model_id}")
        print(f"[runner] MCP URL    : {mcp_url}")
        print(f"[runner] Entity     : {entity_id}")
        print(f"[runner] Tax year   : {tax_year}")
        print(f"[runner] Output     : {output_path}")
        print(f"[runner] Tools      : {_ALLOWED_TOOLS}")
        print()

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env

    response = client.beta.messages.create(
        model=model_id,
        max_tokens=8192,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
        mcp_servers=[_mcp_server_config(mcp_url, service_token)],
        betas=["mcp-client-2025-04-04"],
    )

    # Report tool calls made
    tool_summaries = _collect_tool_use_summaries(response)
    if tool_summaries:
        print(f"[runner] Tool calls made: {len(tool_summaries)}")
        for ts in tool_summaries:
            print(f"  [{ts['tool']}] id={ts['id']}  inputs={ts['input_keys']}")
        print()

    # Extract the final text
    draft_text = _extract_text_from_response(response)
    if not draft_text.strip():
        print(
            "ERROR: The specialist returned an empty response. "
            "Check that the /mcp endpoint is reachable and the service token is valid.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Write output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(draft_text, encoding="utf-8")
    print(f"[runner] Draft written to: {output_path}")

    # Report token usage
    usage = response.usage
    print(
        f"[runner] Token usage — input: {usage.input_tokens}  "
        f"output: {usage.output_tokens}"
    )

    # Surface memo/flag IDs from the footer line if present
    for line in draft_text.splitlines():
        if "Memo IDs:" in line or "Flag IDs:" in line:
            print(f"[runner] {line.strip()}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the SC State Tax Specialist and write a DRAFT SC1120 tax package "
            "to the specified output file."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Prerequisites:
              Run the Form 1120 Preparer first for the same entity_id + tax_year.
              The SC specialist reads the federal package via get_form_1120_data.

            Environment variables:
              ACCTOPS_SERVICE_TOKEN   Bearer token for the /mcp endpoint  [required]
              ACCTOPS_MCP_URL         MCP endpoint base URL               [default: http://localhost:8000/mcp]
              ANTHROPIC_API_KEY       Anthropic API key                   [required]
        """),
    )
    parser.add_argument("--workspace-id", required=True,
                        help="Workspace UUID")
    parser.add_argument("--entity-id", required=True,
                        help="Entity UUID within the workspace")
    parser.add_argument("--tax-year", required=True, type=int,
                        help="Four-digit tax year (e.g. 2023)")
    parser.add_argument("--output", required=True, type=pathlib.Path,
                        help="Output path for the DRAFT markdown file")
    parser.add_argument("--model", choices=list(_MODEL_MAP.keys()), default="sonnet",
                        help="Model to use: 'sonnet' (default) or 'opus' (higher quality)")
    parser.add_argument("--mcp-url", default=None,
                        help="Override MCP endpoint URL")
    parser.add_argument("--verbose", action="store_true",
                        help="Stream tool-use events and config details to stdout")
    return parser.parse_args(argv)


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

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY environment variable is required.", file=sys.stderr)
        sys.exit(1)

    mcp_url = args.mcp_url or os.environ.get("ACCTOPS_MCP_URL") or _DEFAULT_MCP_URL

    if not (1900 <= args.tax_year <= date.today().year):
        print(f"ERROR: --tax-year {args.tax_year} is not a plausible tax year.", file=sys.stderr)
        sys.exit(1)

    run(
        workspace_id=args.workspace_id,
        entity_id=args.entity_id,
        tax_year=args.tax_year,
        output_path=args.output,
        model_key=args.model,
        mcp_url=mcp_url,
        service_token=service_token,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    main()
