#!/usr/bin/env python3
"""Tax Operations Coordinator — CLI runner.

Dispatches user requests to the appropriate tax specialists, runs them as
subprocesses (in parallel where the dependency graph allows), collects their
DRAFT outputs, and writes a unified coordinator report.

Usage::

    uv run python agents/tax_specialists/coordinator/run_coordinator.py \\
        --workspace-id 550e8400-e29b-41d4-a716-446655440000 \\
        --entity-id    aabbccdd-1234-5678-9abc-000000000001 \\
        --tax-year     2023 \\
        --request      "prepare 2023 returns for Acme Inc" \\
        --output-dir   ./drafts/ \\
        --model        sonnet

    # Full federal + 1099 + SC trio with custom LLM backend:
    uv run python agents/tax_specialists/coordinator/run_coordinator.py \\
        --workspace-id 550e8400-e29b-41d4-a716-446655440000 \\
        --entity-id    aabbccdd-1234-5678-9abc-000000000001 \\
        --tax-year     2023 \\
        --request      "prepare 2023 returns for Acme Inc" \\
        --backend      openai \\
        --llm-endpoint-url http://<host>:8088/v1 \\
        --output-dir   ./drafts/

Environment variables:

    ACCTOPS_SERVICE_TOKEN   Bearer token with at least "viewer" scope.  Required.
    ACCTOPS_MCP_URL         Override the MCP endpoint base URL.
                            Default: http://localhost:8000/mcp
    ANTHROPIC_API_KEY       Anthropic API key.  Required unless --backend is set.

Notes:

- The coordinator uses the Anthropic Python SDK's native MCP client integration
  (``anthropic.beta.messages.create`` with ``mcp_servers`` parameter), which
  requires ``anthropic >= 0.44.0`` and the ``mcp`` extra.
- Specialist subprocesses are launched via ``uv run python <runner_path>`` using
  the same Python environment. The service token, MCP URL, and LLM backend flags
  are propagated to every subprocess.
- SC State Specialist is always in parallel_group=2 — it starts only after all
  group-1 specialists (1120 Preparer + 1099 Specialist) have completed.
- Unavailable specialists (not yet built) produce gap notices in the report but
  do not abort the run.
- Use --dry-run to print the dispatch plan without invoking any specialist.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import subprocess
import sys
import textwrap
import time
import uuid
from concurrent.futures import ProcessPoolExecutor, Future, as_completed
from datetime import date, datetime, timezone
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

# The dispatch_plan module lives in the same package directory.
_HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(_HERE.parent.parent.parent))  # repo root

from agents.tax_specialists.coordinator.dispatch_plan import (
    DispatchPlan,
    DispatchStep,
    build_dispatch_plan,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_MCP_URL = "http://localhost:8000/mcp"
_MODEL_MAP = {
    "sonnet": "claude-sonnet-4-6",
    "opus":   "claude-opus-4-7",
}
_SYSTEM_PROMPT_PATH = _HERE / "system_prompt.md"

# The Coordinator itself only calls two MCP tools.
_ALLOWED_TOOLS: tuple[str, ...] = (
    "draft_memo",
    "get_form_1120_data",
)

# Slug → short output file prefix (used in draft file names).
_SLUG_PREFIX: dict[str, str] = {
    "form_1120_preparer":   "form-1120",
    "1099_specialist":      "1099-spec",
    "sc_state_specialist":  "sc-state",
    "audit_defense":        "audit-def",
    "payroll_advisor":      "payroll",
}


# ---------------------------------------------------------------------------
# Dataclass for one specialist run result
# ---------------------------------------------------------------------------

class SpecialistResult:
    """Tracks the outcome of one specialist subprocess invocation."""

    def __init__(self, step: DispatchStep, output_path: pathlib.Path | None = None):
        self.step = step
        self.output_path: pathlib.Path | None = output_path
        self.status: str = "PENDING"   # PENDING | COMPLETE | ERROR | SKIPPED | GAP
        self.error_msg: str = ""
        self.elapsed_seconds: float = 0.0
        self.memo_id: str = ""
        self.flag_ids: list[str] = []

    @property
    def slug(self) -> str:
        return self.step.specialist_slug

    @property
    def display_name(self) -> str:
        return self.step.display_name


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_system_prompt() -> str:
    if not _SYSTEM_PROMPT_PATH.exists():
        raise FileNotFoundError(
            f"System prompt not found at {_SYSTEM_PROMPT_PATH}."
        )
    return _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


def _mcp_server_config(mcp_url: str, service_token: str) -> dict[str, Any]:
    """Build the mcp_servers entry for the Coordinator's own Anthropic SDK call."""
    return {
        "type": "url",
        "url": mcp_url,
        "name": "accounting-ops",
        "authorization_token": service_token,
        "tool_filter": {"type": "include", "tool_names": list(_ALLOWED_TOOLS)},
    }


def _extract_text(response: Any) -> str:
    parts: list[str] = []
    for block in response.content:
        if hasattr(block, "text"):
            parts.append(block.text)
    return "\n".join(parts)


def _collect_tool_summaries(response: Any) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for block in response.content:
        if getattr(block, "type", None) == "tool_use":
            summaries.append({
                "tool": block.name,
                "id": block.id,
                "input_keys": list(block.input.keys()) if isinstance(block.input, dict) else [],
            })
    return summaries


def _draft_output_path(
    output_dir: pathlib.Path,
    slug: str,
    entity_id: str,
    tax_year: int,
    timestamp: str,
) -> pathlib.Path:
    prefix = _SLUG_PREFIX.get(slug, slug.replace("_", "-"))
    safe_entity = entity_id[:8]  # Use first 8 chars of UUID to keep names readable
    return output_dir / f"{prefix}-{safe_entity}-{tax_year}-{timestamp}.md"


def _parse_ids_from_draft(draft_text: str) -> tuple[str, list[str]]:
    """Extract the specialist's memo_id and flag_ids from its output footer."""
    memo_id = ""
    flag_ids: list[str] = []

    for line in draft_text.splitlines():
        m = re.search(r"Memo\s+IDs?:\s*\[?([^\]]+)\]?", line, re.IGNORECASE)
        if m and not memo_id:
            memo_id = m.group(1).strip().split(",")[0].strip()
        m2 = re.search(r"Flag\s+IDs?:\s*\[?([^\]]+)\]?", line, re.IGNORECASE)
        if m2:
            raw = m2.group(1).strip()
            flag_ids.extend(
                f.strip() for f in raw.split(",") if f.strip() and f.strip() != "..."
            )

    return memo_id, flag_ids


# ---------------------------------------------------------------------------
# Specialist subprocess runner (called in worker process)
# ---------------------------------------------------------------------------

def _run_specialist_subprocess(
    *,
    step: DispatchStep,
    output_path: pathlib.Path,
    repo_root: pathlib.Path,
    mcp_url: str,
    service_token: str,
    model_key: str,
    backend: str | None,
    llm_endpoint_url: str | None,
    verbose: bool,
) -> tuple[bool, str]:
    """Launch a specialist runner as a subprocess.

    Returns (success: bool, error_message: str).
    On success, error_message is "".
    """
    cmd: list[str] = [
        "uv", "run", "python",
        str(repo_root / step.runner_path),
        "--workspace-id", step.workspace_id,
        "--entity-id",    step.entity_id,
        "--tax-year",     str(step.tax_year),
        "--output",       str(output_path),
        "--model",        model_key,
        "--mcp-url",      mcp_url,
    ]
    if verbose:
        cmd.append("--verbose")
    if backend:
        cmd += ["--backend", backend]
    if llm_endpoint_url:
        cmd += ["--llm-endpoint-url", llm_endpoint_url]

    env = os.environ.copy()
    env["ACCTOPS_SERVICE_TOKEN"] = service_token
    env["ACCTOPS_MCP_URL"] = mcp_url

    if verbose:
        print(f"[coordinator] Launching: {' '.join(cmd)}", flush=True)

    result = subprocess.run(
        cmd,
        env=env,
        cwd=str(repo_root),
        capture_output=not verbose,
        text=True,
        timeout=600,  # 10 min max per specialist
    )

    if result.returncode != 0:
        err = (
            (result.stderr or "").strip()
            or (result.stdout or "").strip()
            or f"Exit code {result.returncode}"
        )
        return False, err[:2000]

    return True, ""


# ---------------------------------------------------------------------------
# Coordinator MCP call — sanity check + draft_memo
# ---------------------------------------------------------------------------

def _sanity_check_entity(
    client: anthropic.Anthropic,
    entity_id: str,
    tax_year: int,
    mcp_url: str,
    service_token: str,
) -> bool:
    """Call get_form_1120_data to confirm the entity + year exist before dispatching.

    Returns True if data is available, False otherwise.
    """
    system = (
        "You are a data verification agent. Call get_form_1120_data with the provided "
        "entity_id and tax_year. If the call succeeds and returns data, respond with "
        "exactly: ENTITY_OK. If the call fails, the entity is not found, or there is "
        "no data for the year, respond with exactly: ENTITY_NOT_FOUND. No other output."
    )
    user_msg = f"entity_id={entity_id} tax_year={tax_year}"

    try:
        response = client.beta.messages.create(
            model=_MODEL_MAP["sonnet"],  # Always use sonnet for sanity check
            max_tokens=64,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
            mcp_servers=[_mcp_server_config(mcp_url, service_token)],
            betas=["mcp-client-2025-04-04"],
        )
        text = _extract_text(response).strip()
        return "ENTITY_OK" in text
    except Exception as exc:
        print(f"[coordinator] WARNING: sanity check failed: {exc}", file=sys.stderr)
        return False  # Let the run proceed; specialists will surface data errors


def _write_coordinator_memo(
    client: anthropic.Anthropic,
    entity_id: str,
    tax_year: int,
    workspace_id: str,
    user_request: str,
    run_id: str,
    run_date: str,
    results: list[SpecialistResult],
    plan: DispatchPlan,
    mcp_url: str,
    service_token: str,
) -> str:
    """Call draft_memo to write the coordinator summary. Returns the memo_id."""

    # Build the dispatch plan table
    plan_rows: list[str] = []
    for r in results:
        out_str = str(r.output_path) if r.output_path else "n/a"
        plan_rows.append(
            f"| {r.display_name:<22} | {r.status:<9} | {out_str} |"
        )
    plan_table = "\n".join(plan_rows) if plan_rows else "| (none) | n/a | n/a |"

    # Build aggregated flag table
    flag_rows: list[str] = []
    for r in results:
        for fid in r.flag_ids:
            flag_rows.append(
                f"| {r.display_name:<22} | {fid:<36} | [see specialist draft] |"
            )
    flag_table = (
        "\n".join(flag_rows)
        if flag_rows
        else "| (none) | n/a | No flags raised or specialists not yet available |"
    )

    # Build gap section
    gap_rows: list[str] = []
    for g in plan.gaps:
        gap_rows.append(f"| GAP | {g[:200]} |")
    for r in results:
        if r.status == "ERROR":
            gap_rows.append(f"| ERROR | {r.display_name}: {r.error_msg[:200]} |")
    gap_table = (
        "\n".join(gap_rows)
        if gap_rows
        else "| (none) | All dispatched specialists completed successfully |"
    )

    # Next steps
    next_steps = [
        "Review each specialist DRAFT at the file paths listed in the dispatch plan above.",
        "Resolve all CPA-flagged items (see flag IDs above) before return preparation.",
    ]
    if any(s.slug == "sc_state_specialist" for s in plan.available_steps()):
        next_steps.append(
            "SC State Specialist output depends on federal 1120 data — confirm federal "
            "positions before finalizing SC1120."
        )
    if plan.gap_steps():
        gap_names = ", ".join(s.display_name for s in plan.gap_steps())
        next_steps.append(
            f"Gap specialists not yet built: {gap_names}. "
            f"CPA must cover these areas manually until the specialists are implemented."
        )
    next_steps.append(
        "All DRAFTs are for CPA preparation only — none are ready for filing."
    )
    next_steps_text = "\n".join(f"{i+1}. {s}" for i, s in enumerate(next_steps))

    memo_content = textwrap.dedent(f"""\
        ## Coordinator Summary — {entity_id} / {tax_year}

        Run ID      : {run_id}
        Run date    : {run_date}
        Requested by: {user_request[:200]}

        ### Dispatch Plan

        | Specialist             | Status    | Output File                                  |
        |---|---|---|
        {plan_table}

        ### CPA-Flagged Items (All Specialists)

        | Specialist             | Flag ID                              | Form Reference        |
        |---|---|---|
        {flag_table}

        Full flag details: query tax_review_queue by flag_id.

        ### Gaps / Errors

        | Type   | Description                                                      |
        |---|---|
        {gap_table}

        ### Next Steps for CPA

        {next_steps_text}

        DRAFT — CPA REVIEW REQUIRED — NOT FOR FILING
    """)

    # Citations = list of output file paths
    citations = [str(r.output_path) for r in results if r.output_path]

    system = _load_system_prompt()
    user_msg = textwrap.dedent(f"""\
        Write the coordinator summary memo for this run.

        entity_id    : {entity_id}
        tax_year     : {tax_year}
        workspace_id : {workspace_id}
        run_id       : {run_id}

        Use draft_memo with:
          - topic      : "coordinator_summary"
          - content    : the memo text below (reproduce verbatim)
          - citations  : {citations}
          - confidence : "high"

        Memo text:

        {memo_content}

        After calling draft_memo, output ONLY the returned memo_id UUID. No other text.
    """)

    response = client.beta.messages.create(
        model=_MODEL_MAP["sonnet"],
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
        mcp_servers=[_mcp_server_config(mcp_url, service_token)],
        betas=["mcp-client-2025-04-04"],
    )

    text = _extract_text(response).strip()
    # Extract UUID from response
    m = re.search(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        text,
        re.IGNORECASE,
    )
    return m.group(0) if m else text[:64]


# ---------------------------------------------------------------------------
# Coordinator report writer
# ---------------------------------------------------------------------------

def _write_coordinator_report(
    output_dir: pathlib.Path,
    entity_id: str,
    workspace_id: str,
    tax_year: int,
    user_request: str,
    run_id: str,
    run_date: str,
    plan: DispatchPlan,
    results: list[SpecialistResult],
    coordinator_memo_id: str,
    timestamp: str,
) -> pathlib.Path:
    """Build and write the unified coordinator report. Returns the report path."""

    safe_entity = entity_id[:8]
    report_path = output_dir / f"coordinator-{safe_entity}-{tax_year}-{timestamp}.md"

    # Dispatch table
    dispatch_rows: list[str] = []
    for r in results:
        elapsed = f"{r.elapsed_seconds:.1f}s"
        out_str = str(r.output_path.name) if r.output_path else "n/a"
        dispatch_rows.append(
            f"| {r.display_name:<22} | {r.slug:<22} | {r.status:<8} | {elapsed:<9} | {out_str} |"
        )
    dispatch_table = "\n".join(dispatch_rows) if dispatch_rows else "| (none dispatched) |"

    # Specialist output sections
    output_sections: list[str] = []
    for r in results:
        memo_ref = f"`{r.memo_id}`" if r.memo_id else "n/a"
        flag_count = len(r.flag_ids)
        flag_ref = ", ".join(f"`{f}`" for f in r.flag_ids[:5])
        if len(r.flag_ids) > 5:
            flag_ref += f" + {len(r.flag_ids) - 5} more"
        output_path_ref = f"[{r.output_path.name}](./{r.output_path.name})" if r.output_path else "n/a"
        gap_block = f"\n> **GAP:** {r.step.gap_notice}" if r.step.gap_notice else ""
        error_block = f"\n> **ERROR:** {r.error_msg[:400]}" if r.error_msg else ""

        output_sections.append(
            textwrap.dedent(f"""\
                ### {r.display_name}

                - **Status:** {r.status}
                - **Output:** {output_path_ref}
                - **CPA-Flagged Items:** {flag_count} flag(s) — Flag IDs: {flag_ref or "none"}
                - **Memo ID:** {memo_ref} (in tax_review_queue){gap_block}{error_block}
            """)
        )

    # Aggregated flag table
    flag_rows: list[str] = []
    for i, r in enumerate(results):
        for fid in r.flag_ids:
            flag_rows.append(f"| {i+1} | {r.display_name:<22} | `{fid}` | [see specialist draft] |")
    flag_table = (
        "\n".join(flag_rows)
        if flag_rows
        else "| — | No flags raised or all specialists had gaps | — | — |"
    )

    # Gap table
    gap_rows: list[str] = []
    for g in plan.gaps:
        gap_rows.append(f"| Gap  | {g[:300]} |")
    for r in results:
        if r.status == "ERROR":
            gap_rows.append(f"| Error | {r.display_name}: {r.error_msg[:300]} |")
    gap_table = (
        "\n".join(gap_rows)
        if gap_rows
        else "| — | All dispatched specialists completed successfully |"
    )

    # All flag IDs flat list for footer
    all_flag_ids = [fid for r in results for fid in r.flag_ids]
    all_memo_ids = [r.memo_id for r in results if r.memo_id]

    report = textwrap.dedent(f"""\
        # DRAFT — Tax Operations Coordinator Report
        # ⚠ DRAFT — CPA REVIEW REQUIRED — NOT FOR FILING ⚠

        ## Header

        | Field          | Value                                    |
        |---|---|
        | Entity         | {entity_id}                              |
        | Entity ID      | {entity_id}                              |
        | Tax Year       | {tax_year}                               |
        | Workspace      | {workspace_id}                           |
        | Run ID         | {run_id}                                 |
        | Coordinated by | AI / Tax Operations Coordinator          |
        | Run date       | {run_date}                               |
        | Status         | DRAFT                                    |

        ## User Request

        > {user_request[:500]}

        ## Dispatch Plan Executed

        | Specialist             | Slug                    | Status   | Run time  | Output File                    |
        |---|---|---|---|---|
        {dispatch_table}

        ## Specialist Outputs

        {chr(10).join(output_sections)}

        ## Aggregated CPA-Flagged Items

        > These flags were raised by specialists. The Coordinator does NOT add, remove, or
        > re-interpret flags. See each specialist's DRAFT for full flag context.

        | # | Specialist             | Flag ID | Form Reference               |
        |---|---|---|---|
        {flag_table}

        ## Gaps and Errors

        | Type  | Description                                                          |
        |---|---|
        {gap_table}

        ## Coordinator Memo

        Memo written to `tax_review_queue` (topic: `coordinator_summary`).
        Query by Coordinator Memo ID to read full content.

        Coordinator Memo ID: `{coordinator_memo_id}`

        ---

        # ⚠ DRAFT — CPA REVIEW REQUIRED — NOT FOR FILING ⚠
        # Generated by: Accounting-Ops / Tax Operations Coordinator
        # Coordinator Memo ID: {coordinator_memo_id}
        # Specialist Memo IDs: [{", ".join(all_memo_ids)}]
        # All Specialist Flag IDs: [{", ".join(all_flag_ids)}]
    """)

    output_dir.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    return report_path


# ---------------------------------------------------------------------------
# Main coordinator run logic
# ---------------------------------------------------------------------------

def run(
    workspace_id: str,
    entity_id: str,
    tax_year: int,
    user_request: str,
    output_dir: pathlib.Path,
    model_key: str,
    mcp_url: str,
    service_token: str,
    backend: str | None,
    llm_endpoint_url: str | None,
    *,
    dry_run: bool = False,
    verbose: bool = False,
) -> pathlib.Path:
    """Run the Tax Operations Coordinator end-to-end.

    Returns the path to the coordinator report file.
    """
    run_id = str(uuid.uuid4())
    run_date = datetime.now(tz=timezone.utc).isoformat()
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
    repo_root = pathlib.Path(__file__).parent.parent.parent.parent  # repo root

    output_dir.mkdir(parents=True, exist_ok=True)

    # Step 1 — Build dispatch plan
    plan = build_dispatch_plan(
        user_request=user_request,
        entity_id=entity_id,
        tax_year=tax_year,
        workspace_id=workspace_id,
    )

    print(f"[coordinator] Run ID   : {run_id}")
    print(f"[coordinator] Intent   : {plan.intent}")
    print(f"[coordinator] Steps    : {len(plan.steps)} specialist(s) in plan")
    if plan.gaps:
        print(f"[coordinator] Gaps     : {len(plan.gaps)} gap(s) detected")

    if dry_run:
        import json
        print("[coordinator] DRY RUN — dispatch plan (no specialists launched):")
        print(json.dumps(plan.to_dict(), indent=2))
        # Write a stub report
        stub_path = output_dir / f"coordinator-{entity_id[:8]}-{tax_year}-{timestamp}-dryrun.md"
        stub_path.write_text(
            f"# DRY RUN — Tax Operations Coordinator\n\nDispatch plan for intent '{plan.intent}':\n\n"
            + "\n".join(
                f"- {s.display_name} (group {s.parallel_group}, available={s.available})"
                for s in plan.steps
            ),
            encoding="utf-8",
        )
        print(f"[coordinator] Dry-run stub written to: {stub_path}")
        return stub_path

    if not plan.steps:
        print(
            "[coordinator] ERROR: No specialists in dispatch plan. Check the user request.",
            file=sys.stderr,
        )
        # Still write a report with the gap notice
        results: list[SpecialistResult] = []
        coordinator_memo_id = "no-memo-gap-run"
        return _write_coordinator_report(
            output_dir=output_dir,
            entity_id=entity_id,
            workspace_id=workspace_id,
            tax_year=tax_year,
            user_request=user_request,
            run_id=run_id,
            run_date=run_date,
            plan=plan,
            results=results,
            coordinator_memo_id=coordinator_memo_id,
            timestamp=timestamp,
        )

    # Step 2 — Sanity check entity via get_form_1120_data (coordinator's own MCP call)
    client = anthropic.Anthropic()
    if verbose:
        print("[coordinator] Sanity-checking entity via get_form_1120_data...")

    entity_ok = _sanity_check_entity(
        client=client,
        entity_id=entity_id,
        tax_year=tax_year,
        mcp_url=mcp_url,
        service_token=service_token,
    )
    if not entity_ok:
        print(
            f"[coordinator] WARNING: get_form_1120_data returned no data for "
            f"entity={entity_id} year={tax_year}. Proceeding anyway — "
            f"specialists will surface data errors.",
            file=sys.stderr,
        )

    # Step 3 — Execute specialists group by group
    results: list[SpecialistResult] = []

    # Pre-create SpecialistResult objects (including gap stubs for unavailable specialists)
    all_results: dict[str, SpecialistResult] = {}
    for step in plan.steps:
        output_path = _draft_output_path(
            output_dir=output_dir,
            slug=step.specialist_slug,
            entity_id=entity_id,
            tax_year=tax_year,
            timestamp=timestamp,
        ) if step.available else None

        r = SpecialistResult(step=step, output_path=output_path)
        if not step.available:
            r.status = "GAP"
        all_results[step.specialist_slug] = r
        results.append(r)

    # Execute available specialists in parallel-group order
    groups = plan.parallel_groups()
    for group_num, steps_in_group in groups.items():
        available_in_group = [s for s in steps_in_group if s.available]
        if not available_in_group:
            if verbose:
                print(f"[coordinator] Group {group_num}: no available specialists, skipping.")
            continue

        print(
            f"[coordinator] Group {group_num}: launching "
            f"{len(available_in_group)} specialist(s) in parallel..."
        )

        # Launch all specialists in this group concurrently using threads
        # (ProcessPoolExecutor can't pickle local functions in all environments —
        # use ThreadPoolExecutor for subprocess dispatch, which is I/O-bound anyway)
        from concurrent.futures import ThreadPoolExecutor

        def _run_one(step: DispatchStep) -> tuple[str, bool, str, float]:
            """Returns (slug, success, error_msg, elapsed_seconds)."""
            t0 = time.monotonic()
            success, err = _run_specialist_subprocess(
                step=step,
                output_path=all_results[step.specialist_slug].output_path,
                repo_root=repo_root,
                mcp_url=mcp_url,
                service_token=service_token,
                model_key=model_key,
                backend=backend,
                llm_endpoint_url=llm_endpoint_url,
                verbose=verbose,
            )
            elapsed = time.monotonic() - t0
            return step.specialist_slug, success, err, elapsed

        with ThreadPoolExecutor(max_workers=len(available_in_group)) as executor:
            futures = {
                executor.submit(_run_one, step): step.specialist_slug
                for step in available_in_group
            }
            for future in as_completed(futures):
                slug, success, err, elapsed = future.result()
                r = all_results[slug]
                r.elapsed_seconds = elapsed

                if success and r.output_path and r.output_path.exists():
                    draft_text = r.output_path.read_text(encoding="utf-8")
                    r.memo_id, r.flag_ids = _parse_ids_from_draft(draft_text)
                    r.status = "COMPLETE"
                    print(
                        f"[coordinator]   {r.display_name}: COMPLETE "
                        f"({elapsed:.1f}s, {len(r.flag_ids)} flag(s))"
                    )
                else:
                    r.status = "ERROR"
                    r.error_msg = err or "Output file missing after subprocess completed"
                    print(
                        f"[coordinator]   {r.display_name}: ERROR — {r.error_msg[:120]}",
                        file=sys.stderr,
                    )

    # Step 4 — Write coordinator memo via draft_memo
    print("[coordinator] Writing coordinator memo...")
    try:
        coordinator_memo_id = _write_coordinator_memo(
            client=client,
            entity_id=entity_id,
            tax_year=tax_year,
            workspace_id=workspace_id,
            user_request=user_request,
            run_id=run_id,
            run_date=run_date,
            results=results,
            plan=plan,
            mcp_url=mcp_url,
            service_token=service_token,
        )
        print(f"[coordinator] Coordinator memo ID: {coordinator_memo_id}")
    except Exception as exc:
        print(
            f"[coordinator] WARNING: draft_memo call failed: {exc}. "
            f"Proceeding without memo.",
            file=sys.stderr,
        )
        coordinator_memo_id = "memo-failed"

    # Step 5 — Write coordinator report
    report_path = _write_coordinator_report(
        output_dir=output_dir,
        entity_id=entity_id,
        workspace_id=workspace_id,
        tax_year=tax_year,
        user_request=user_request,
        run_id=run_id,
        run_date=run_date,
        plan=plan,
        results=results,
        coordinator_memo_id=coordinator_memo_id,
        timestamp=timestamp,
    )

    print(f"[coordinator] Coordinator report: {report_path}")

    # Summary
    complete = [r for r in results if r.status == "COMPLETE"]
    errors   = [r for r in results if r.status == "ERROR"]
    gaps     = [r for r in results if r.status == "GAP"]
    total_flags = sum(len(r.flag_ids) for r in results)

    print()
    print(f"[coordinator] Summary: {len(complete)} complete, {len(errors)} error(s), "
          f"{len(gaps)} gap(s), {total_flags} total CPA flag(s)")
    if errors:
        for r in errors:
            print(f"  [ERROR] {r.display_name}: {r.error_msg[:120]}")
    if gaps:
        for r in gaps:
            print(f"  [GAP]   {r.display_name}: specialist not yet built")

    return report_path


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Tax Operations Coordinator — dispatches user requests to tax specialists "
            "and produces a unified coordinator report."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Environment variables:
              ACCTOPS_SERVICE_TOKEN   Bearer token for the /mcp endpoint  [required]
              ACCTOPS_MCP_URL         MCP endpoint base URL               [default: http://localhost:8000/mcp]
              ANTHROPIC_API_KEY       Anthropic API key                   [required]

            Examples:
              # Full trio (federal + 1099 + SC) for Acme Inc 2023:
              uv run python agents/tax_specialists/coordinator/run_coordinator.py \\
                --workspace-id 550e8400-e29b-41d4-a716-446655440000 \\
                --entity-id    aabbccdd-1234-5678-9abc-000000000001 \\
                --tax-year     2023 \\
                --request      "prepare 2023 returns for Acme Inc" \\
                --output-dir   ./drafts/

              # Dry run — print dispatch plan without launching specialists:
              uv run python agents/tax_specialists/coordinator/run_coordinator.py \\
                --workspace-id 550e8400-e29b-41d4-a716-446655440000 \\
                --entity-id    aabbccdd-1234-5678-9abc-000000000001 \\
                --tax-year     2023 \\
                --request      "prepare 2023 returns" \\
                --dry-run

              # Custom LLM backend (self-hosted):
              uv run python agents/tax_specialists/coordinator/run_coordinator.py \\
                --workspace-id 550e8400-e29b-41d4-a716-446655440000 \\
                --entity-id    aabbccdd-1234-5678-9abc-000000000001 \\
                --tax-year     2023 \\
                --request      "prepare 2023 returns" \\
                --backend      openai \\
                --llm-endpoint-url http://<host>:8088/v1
        """),
    )
    parser.add_argument("--workspace-id", required=True, help="Workspace UUID")
    parser.add_argument("--entity-id", required=True, help="Entity UUID")
    parser.add_argument(
        "--tax-year", required=True, type=int, help="Four-digit tax year (e.g. 2023)"
    )
    parser.add_argument(
        "--request",
        required=True,
        help='Natural-language user request (e.g. "prepare 2023 returns for Acme Inc")',
    )
    parser.add_argument(
        "--output-dir",
        type=pathlib.Path,
        default=pathlib.Path("./drafts"),
        help="Directory to write specialist drafts and the coordinator report (default: ./drafts)",
    )
    parser.add_argument(
        "--model",
        choices=list(_MODEL_MAP.keys()),
        default="sonnet",
        help="Model to use for specialist runs: 'sonnet' (default) or 'opus'",
    )
    parser.add_argument(
        "--mcp-url",
        default=None,
        help="Override MCP endpoint URL (default: $ACCTOPS_MCP_URL or http://localhost:8000/mcp)",
    )
    parser.add_argument(
        "--backend",
        default=None,
        help='LLM backend for specialist runners (e.g. "openai" for local vLLM)',
    )
    parser.add_argument(
        "--llm-endpoint-url",
        default=None,
        help="LLM endpoint URL for specialist runners (e.g. http://<host>:8088/v1)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the dispatch plan and exit without launching any specialist",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Stream subprocess output and config details to stdout",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    # Resolve env vars
    service_token = os.environ.get("ACCTOPS_SERVICE_TOKEN", "")
    if not service_token and not args.dry_run:
        print(
            "ERROR: ACCTOPS_SERVICE_TOKEN environment variable is required.\n"
            "Obtain a service token from Keycloak (uchub realm, accounting-ops-app client).",
            file=sys.stderr,
        )
        sys.exit(1)

    if not os.environ.get("ANTHROPIC_API_KEY") and not args.backend and not args.dry_run:
        print(
            "ERROR: ANTHROPIC_API_KEY environment variable is required "
            "(or use --backend with a self-hosted LLM).",
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

    run(
        workspace_id=args.workspace_id,
        entity_id=args.entity_id,
        tax_year=args.tax_year,
        user_request=args.request,
        output_dir=args.output_dir,
        model_key=args.model,
        mcp_url=mcp_url,
        service_token=service_token or "dry-run",
        backend=args.backend,
        llm_endpoint_url=args.llm_endpoint_url,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    main()
