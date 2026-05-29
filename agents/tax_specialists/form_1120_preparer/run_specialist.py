#!/usr/bin/env python3
"""Form 1120 Preparer — CLI runner.

Invokes the Form 1120 Preparer specialist against the Accounting-Ops /mcp
endpoint using the Anthropic SDK with native MCP tool integration. Streams the
model response and writes the final DRAFT to an output file.

Usage::

    uv run python agents/tax_specialists/form_1120_preparer/run_specialist.py \\
        --workspace-id <UUID> \\
        --entity-id    <UUID> \\
        --tax-year     2023 \\
        --output       ./drafts/mu-inc-2023-1120-draft.md \\
        --model        sonnet   # or "opus"

Environment variables:

    ACCTOPS_SERVICE_TOKEN   Bearer token with at least "viewer" scope on the
                            workspace. Required.
    ACCTOPS_MCP_URL         Override the MCP endpoint base URL.
                            Default: http://localhost:8000/mcp
    ANTHROPIC_API_KEY       Anthropic API key. Required (standard SDK env var).

Notes:

- The runner does NOT require a running Anthropic SDK test server; it connects
  to the REAL Anthropic API. Set ANTHROPIC_API_KEY before running.
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

from agents.tax_specialists._shared.run_telemetry import RunTelemetry

# ---------------------------------------------------------------------------
# Dependency check — give a clear error before the import explosion
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

# These are the MCP tool names the specialist is allowed to call.
# Matches the tools documented in system_prompt.md.
_ALLOWED_TOOLS: list[str] = [
    "get_form_1120_data",
    "get_period_journal",
    "get_1099_payees",
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
            "agents/tax_specialists/form_1120_preparer/ directory is intact."
        )
    return _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


def _build_user_message(
    workspace_id: str,
    entity_id: str,
    tax_year: int,
) -> str:
    """Construct the user-turn message sent to the specialist."""
    return textwrap.dedent(f"""\
        Please prepare a DRAFT Form 1120 for the following entity.

        workspace_id : {workspace_id}
        entity_id    : {entity_id}
        tax_year     : {tax_year}
        run_date     : {date.today().isoformat()}

        Follow the call sequence in your system prompt exactly:
        1. get_form_1120_data
        2. get_1099_payees
        3. get_period_journal (only if needed — document why)
        4. draft_memo (reviewer narrative)
        5. flag_for_cpa_review (one call per flagged item)
        6. Emit the full DRAFT output in the Markdown schema from your system prompt.

        Stamp the output DRAFT — CPA REVIEW REQUIRED at the top and bottom.
        Include all memo IDs and flag IDs in the footer.
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

    The Anthropic SDK (>= 0.44.0) accepts an ``mcp_servers`` list in
    ``messages.create``. Each entry is a dict describing a remote MCP server
    reachable via HTTP (Streamable HTTP transport, i.e. the /mcp endpoint
    served by FastMCP).

    The service token is passed as an Authorization header so the /mcp auth
    middleware can validate it against the uchub Keycloak realm exactly as it
    would for any other bearer-token caller.
    """
    return {
        "type": "url",
        "url": mcp_url,
        "name": "accounting-ops",
        "authorization_token": service_token,
        # Restrict the model to only the tools this specialist is permitted to use.
        # Any tool registered on the MCP server that is NOT in this list will not
        # be visible to the model — defense in depth against the specialist
        # accidentally calling a write tool (e.g. propose_payment).
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
    """Run the Form 1120 Preparer specialist end-to-end.

    Args:
        workspace_id:   Workspace UUID.
        entity_id:      Entity UUID within the workspace.
        tax_year:       Four-digit tax year (e.g. 2023).
        output_path:    Path to write the DRAFT markdown to.
        model_key:      "sonnet" or "opus".
        mcp_url:        Base URL of the /mcp endpoint.
        service_token:  Bearer token for the MCP endpoint.
        verbose:        If True, stream tool-use events to stdout.
    """
    model_id = _MODEL_MAP[model_key]
    system_prompt = _load_system_prompt()
    user_message = _build_user_message(workspace_id, entity_id, tax_year)

    # Derive the backend API base URL from the MCP URL (strip /mcp suffix).
    base_url = mcp_url.rstrip("/").removesuffix("/mcp") or "http://localhost:8000"

    if verbose:
        print(f"[runner] Model     : {model_id}")
        print(f"[runner] MCP URL   : {mcp_url}")
        print(f"[runner] Entity    : {entity_id}")
        print(f"[runner] Tax year  : {tax_year}")
        print(f"[runner] Output    : {output_path}")
        print()

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env

    telemetry = RunTelemetry(
        base_url=base_url,
        service_token=service_token,
        workspace_id=workspace_id,
        entity_id=entity_id,
        tax_year=tax_year,
        specialist_id="tax-1120-preparer",
        backend="anthropic",
        model=model_id,
        endpoint_url=mcp_url,
        input_summary={
            "workspace_id": workspace_id,
            "entity_id": entity_id,
            "tax_year": tax_year,
            "args": {"model_key": model_key, "output": str(output_path)},
        },
    )

    with telemetry:
        # The Anthropic SDK's MCP integration lets the model call remote MCP tools
        # natively — no local tool-call loop required on our side.
        # ``betas=["mcp-client-2025-04-04"]`` enables the feature.
        # The model will make tool calls, receive results, and continue reasoning
        # until it produces a final text response.
        response = client.beta.messages.create(
            model=model_id,
            max_tokens=8192,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
            mcp_servers=[_mcp_server_config(mcp_url, service_token)],
            betas=["mcp-client-2025-04-04"],
        )

        # Ingest response into telemetry (tool calls + token counts)
        telemetry.ingest_anthropic_response(response)

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

        # Record output summary for telemetry
        telemetry.set_output_summary({
            "output_file": str(output_path),
            "output_bytes": len(draft_text.encode("utf-8")),
        })

    if telemetry.run_id:
        print(f"[runner] Telemetry run id: {telemetry.run_id}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the Form 1120 Preparer specialist and write a DRAFT tax package "
            "to the specified output file."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Environment variables:
              ACCTOPS_SERVICE_TOKEN   Bearer token for the /mcp endpoint  [required]
              ACCTOPS_MCP_URL         MCP endpoint base URL               [default: http://localhost:8000/mcp]
              ANTHROPIC_API_KEY       Anthropic API key                   [required]
        """),
    )
    parser.add_argument(
        "--workspace-id",
        required=True,
        help="Workspace UUID (e.g. 550e8400-e29b-41d4-a716-446655440000)",
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
        "--output",
        required=True,
        type=pathlib.Path,
        help="Output path for the DRAFT markdown file",
    )
    parser.add_argument(
        "--model",
        choices=list(_MODEL_MAP.keys()),
        default="sonnet",
        help="Model to use: 'sonnet' (claude-sonnet-4-6, default) or 'opus' (claude-opus-4-7, higher quality, higher cost)",
    )
    parser.add_argument(
        "--mcp-url",
        default=None,
        help="Override MCP endpoint URL (default: $ACCTOPS_MCP_URL or http://localhost:8000/mcp)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Stream tool-use events and config details to stdout",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    # Resolve env vars
    service_token = os.environ.get("ACCTOPS_SERVICE_TOKEN", "")
    if not service_token:
        print(
            "ERROR: ACCTOPS_SERVICE_TOKEN environment variable is required.\n"
            "Obtain a service token from Keycloak (uchub realm, accounting-ops-app client).",
            file=sys.stderr,
        )
        sys.exit(1)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "ERROR: ANTHROPIC_API_KEY environment variable is required.",
            file=sys.stderr,
        )
        sys.exit(1)

    mcp_url = (
        args.mcp_url
        or os.environ.get("ACCTOPS_MCP_URL")
        or _DEFAULT_MCP_URL
    )

    # Validate tax year
    if not (1900 <= args.tax_year <= date.today().year):
        print(
            f"ERROR: --tax-year {args.tax_year} is not a plausible tax year.",
            file=sys.stderr,
        )
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
