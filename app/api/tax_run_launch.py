"""Launch the tax-specialist coordinator from the web app ("Run drafts").

Until now the tax specialists (agents/tax_specialists/*) only ran as scripts
somebody started by hand inside the app container (``docker exec``). The web tax
page could show run history but could not start a run. This module is the
smallest safe way to start one from the GUI:

  GET  /api/workspaces/{wid}/entities/{eid}/tax/{year}/specialist-runs/launch
       Preview: is launching enabled here, which model and provider will be
       used, what data leaves the building, and whether a run is already active.
       Role floor: accountant.

  POST /api/workspaces/{wid}/entities/{eid}/tax/{year}/specialist-runs/launch
       Create a ``specialist_run`` row with status 'queued' and hand it to an
       in-process background worker. 202 + the row. 409 if a run is already
       queued/running for this entity + year. Role floor: accountant.

  POST /api/workspaces/{wid}/specialist-runs/{run_id}/cancel
       Cancel a queued/running run that THIS launcher started. The worker sees
       the status flip within a few seconds and kills the coordinator's whole
       process group (coordinator + specialist subprocesses). Role floor:
       accountant.

Design constraints (P-00189 / task 10d3785e):

* No new credential. The worker runs the coordinator with the app's own
  configured identity (``ACCTOPS_SERVICE_TOKEN`` — the same one the narrative
  trigger already uses) and the app's own frontier route (``CPATEAM_LLM_*`` →
  OpenRouter by default, via ``agents._shared.cpateam_llm``). No key is ever
  put on a command line; the child gets an allow-listed environment.
* Off by default. ``AO_TAX_RUN_LAUNCH_ENABLED=1`` must be set: every click
  spends frontier tokens, so turning this on is a deliberate deploy decision.
* One active run per entity + year (409 otherwise), serialised with a
  transaction-scoped advisory lock so two clicks cannot both pass the check.
* Drafts only. The specialists' MCP tool allow-lists hold read tools plus
  ``draft_memo`` / ``flag_for_cpa_review``; nothing here posts to the ledger.
  Output files land in ``<DRAFTS_DIR>/<wid>/<eid>/runs/<run_id>/`` and, on a
  successful run, are copied to the canonical tenant-scoped names the draft
  viewer / PDF export read (``<DRAFTS_DIR>/<wid>/<eid>/<year>-<slug>-draft.md``).
  A canonical file that would be replaced is first renamed to
  ``...draft.superseded-<run8>.md`` so nothing reviewed is silently lost.
* C-corporations only: the coordinator's GUI intent is the full trio
  (Form 1120 + 1099 + SC1120), which is a C-corp package.

Single-process assumption: the worker lives in the uvicorn process (compose runs
one). A run whose worker died with the process (restart/redeploy) is detected
as orphaned on the next launch/preview for that entity + year once it is older
than the hard timeout, and is marked failed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import re
import shutil
import signal
import sys
import time
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text

from app.authz import assert_entity_in_scope
from app.db import SessionLocal
from app.tenancy import WorkspaceContext, require_role, resolve_workspace
from app.workspace_session import workspace_session

log = logging.getLogger(__name__)

router = APIRouter(tags=["specialist-runs"])

# ---------------------------------------------------------------------------
# Constants / configuration
# ---------------------------------------------------------------------------

#: agent_token_id stamped on rows this launcher creates. Cancel and orphan
#: reconciliation only ever touch rows carrying it — an externally started run
#: cannot be killed from here, so it must not be marked cancelled from here.
LAUNCHER_AGENT_ID = "app-launcher"
SPECIALIST_ID = "tax-coordinator"
ACTIVE_STATUSES = ("queued", "running")

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
_COORDINATOR = _REPO_ROOT / "agents" / "tax_specialists" / "coordinator" / "run_coordinator.py"

#: The specialists the GUI request dispatches (dispatch_plan intent 'full_trio').
_SPECIALISTS = ("1120 Preparer", "1099 Specialist", "SC State Specialist")

#: What the specialists read through MCP and therefore send to the model.
_DATA_SENT = (
    "Entity name, entity type and tax facts for this entity",
    "Form 1120 package for the year (income statement lines, balance sheet)",
    "The year's posted journal entries (dates, descriptions, accounts, amounts)",
    "1099 payee totals (payee names and amounts; no TINs)",
)

# Coordinator output-file prefix -> canonical draft slug (app/api/specialist_pdf).
_PUBLISH_MAP = (
    ("form-1120-", "form-1120"),
    ("1099-spec-", "1099-nec"),
    ("sc-state-", "sc1120"),
    ("coordinator-", "coordinator-summary"),
)

_ENV_ALLOWLIST = (
    "PATH", "HOME", "LANG", "LC_ALL", "TZ", "PYTHONPATH", "PYTHONUNBUFFERED",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    # The CPA-team frontier route (agents/_shared/cpateam_llm.py). The vision
    # LLM_* knobs and ANTHROPIC_API_KEY are deliberately NOT passed.
    "CPATEAM_LLM_ENDPOINT_URL", "CPATEAM_LLM_MODEL", "CPATEAM_LLM_API_KEY",
    "OPENROUTER_API_KEY",
    "ACCTOPS_MCP_URL", "ACCTOPS_SERVICE_TOKEN",
)


def _launch_enabled() -> bool:
    return os.getenv("AO_TAX_RUN_LAUNCH_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def _max_runtime_s() -> int:
    # Coordinator = 2 dependency groups x 600 s per-specialist subprocess cap,
    # plus the sanity check and memo write. 30 min is the hard ceiling.
    try:
        return max(60, int(os.getenv("AO_TAX_RUN_TIMEOUT_S", "1800")))
    except ValueError:
        return 1800


def _max_concurrent() -> int:
    try:
        return max(1, int(os.getenv("AO_TAX_RUN_MAX_CONCURRENT", "2")))
    except ValueError:
        return 2


_POLL_S = 5.0


def _drafts_root() -> pathlib.Path:
    return pathlib.Path(os.getenv("DRAFTS_DIR", "/drafts"))


# ---------------------------------------------------------------------------
# Launch configuration (route + identity) — never returns secret values
# ---------------------------------------------------------------------------


def _resolve_route() -> tuple[str, str, str]:
    """(endpoint_url, model, api_key) the specialists will use — same resolver."""
    from agents._shared.cpateam_llm import resolve_cpateam_llm

    return resolve_cpateam_llm()


def _provider_label(endpoint_url: str) -> tuple[str, bool]:
    """Human label for the model route + whether it is a third-party API.

    Only a known public provider's hostname is echoed; an internal gateway is
    described generically (endpoint URLs are redacted everywhere else too).
    """
    host = (urlparse(endpoint_url).hostname or "").lower()
    if host == "openrouter.ai" or host.endswith(".openrouter.ai"):
        return "OpenRouter (third-party API)", True
    if host == "api.anthropic.com":
        return "Anthropic API (third-party)", True
    if host == "api.openai.com":
        return "OpenAI API (third-party)", True
    return "the workspace's configured model gateway", False


def _service_token_blocker(workspace_id: UUID) -> str | None:
    """Reason the app's own runner identity cannot serve this workspace, or None."""
    token = os.getenv("ACCTOPS_SERVICE_TOKEN", "")
    if not token:
        return (
            "The server has no runner identity configured (ACCTOPS_SERVICE_TOKEN "
            "is unset)."
        )
    try:
        from app.auth import _match_service_token

        ident = _match_service_token(token)
    except Exception:  # noqa: BLE001 — a lookup failure must not open the gate
        ident = None
    if ident is None:
        return (
            "The runner identity (ACCTOPS_SERVICE_TOKEN) is not registered in "
            "ACCTOPS_SERVICE_TOKENS."
        )
    wsids = ident.get("workspace_ids")
    if wsids is not None and str(workspace_id) not in {str(w) for w in wsids}:
        return "The runner identity is not scoped to this workspace."
    return None


def launch_config(workspace_id: UUID) -> dict[str, Any]:
    """Everything the confirm dialog shows, plus why launching is unavailable."""
    endpoint, model, key = _resolve_route()
    provider, third_party = _provider_label(endpoint)
    reason: str | None = None
    if not _launch_enabled():
        reason = (
            "Running drafts from the app is turned off on this server "
            "(AO_TAX_RUN_LAUNCH_ENABLED)."
        )
    elif not key:
        reason = "No model API key is configured for the CPA-team route."
    elif not _COORDINATOR.exists():
        reason = "The tax coordinator is not installed in this build."
    else:
        reason = _service_token_blocker(workspace_id)
    return {
        "enabled": reason is None,
        "disabled_reason": reason,
        "backend": "openai",
        "model": model,
        "provider": provider,
        "third_party": third_party,
        "specialists": list(_SPECIALISTS),
        "data_sent": list(_DATA_SENT),
        "max_runtime_s": _max_runtime_s(),
        "cost_note": (
            "Each run makes several frontier-model calls (one per specialist, "
            "each with tool round-trips). Token spend is billed by the provider; "
            "this app does not yet meter it per run."
        ),
    }


# ---------------------------------------------------------------------------
# Secret scrubbing for anything stored in the run row
# ---------------------------------------------------------------------------


def _scrub(textval: str) -> str:
    out = textval
    for name in ("ACCTOPS_SERVICE_TOKEN", "CPATEAM_LLM_API_KEY", "OPENROUTER_API_KEY"):
        val = os.getenv(name, "")
        if val and len(val) >= 8:
            out = out.replace(val, f"<{name}>")
    # Belt and braces: bearer-shaped and sk- keys.
    out = re.sub(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}", "Bearer <redacted>", out)
    out = re.sub(r"\bsk-[A-Za-z0-9_\-]{16,}", "sk-<redacted>", out)
    return out


# ---------------------------------------------------------------------------
# DB helpers (module-level so tests can substitute them)
# ---------------------------------------------------------------------------

_ROW_COLUMNS = """
    sr.id::text, sr.workspace_id::text, sr.entity_id::text, sr.tax_year,
    sr.specialist_id, sr.agent_token_id, sr.invoker_uc_uid, sr.backend, sr.model,
    sr.started_at, sr.finished_at, sr.duration_ms, sr.status, sr.input_summary,
    sr.output_summary, sr.metrics, sr.error_summary, sr.trace_id
"""


async def db_get_entity_kind(workspace_id: UUID, entity_id: UUID) -> str | None:
    async with workspace_session(SessionLocal, workspace_id) as session:
        row = (
            await session.execute(
                text("SELECT kind::text AS kind FROM entity WHERE id = :id"),
                {"id": str(entity_id)},
            )
        ).mappings().first()
    return row["kind"] if row else None


async def db_reconcile_orphans(
    session: Any, entity_id: UUID, tax_year: int, live_ids: list[str]
) -> None:
    """Fail launcher rows whose worker is gone (process restarted)."""
    await session.execute(
        text(
            """
            UPDATE specialist_run
               SET status = 'failed',
                   finished_at = now(),
                   error_summary = 'Orphaned: the app process that ran this '
                                   'launch stopped before it finished.'
             WHERE entity_id = :eid
               AND tax_year = :year
               AND agent_token_id = :launcher
               AND status IN ('queued', 'running')
               AND started_at < now() - make_interval(secs => :max_s)
               AND NOT (id::text = ANY(:live))
            """
        ),
        {
            "eid": str(entity_id),
            "year": tax_year,
            "launcher": LAUNCHER_AGENT_ID,
            "max_s": _max_runtime_s(),
            "live": live_ids,
        },
    )


async def db_find_active(session: Any, entity_id: UUID, tax_year: int) -> dict | None:
    """An active run for entity+year. External runs count while plausibly alive."""
    row = (
        await session.execute(
            text(
                f"""
                SELECT {_ROW_COLUMNS}
                  FROM specialist_run sr
                 WHERE sr.entity_id = :eid
                   AND sr.tax_year = :year
                   AND sr.status IN ('queued', 'running')
                   AND (sr.agent_token_id = :launcher
                        OR sr.started_at > now() - make_interval(secs => :max_s))
                 ORDER BY sr.started_at DESC
                 LIMIT 1
                """
            ),
            {
                "eid": str(entity_id),
                "year": tax_year,
                "launcher": LAUNCHER_AGENT_ID,
                "max_s": _max_runtime_s(),
            },
        )
    ).mappings().first()
    return dict(row) if row else None


async def db_get_active(workspace_id: UUID, entity_id: UUID, tax_year: int) -> dict | None:
    async with workspace_session(SessionLocal, workspace_id) as session:
        await db_reconcile_orphans(session, entity_id, tax_year, sorted(_LIVE_RUNS))
        return await db_find_active(session, entity_id, tax_year)


async def db_create_queued_run(
    workspace: WorkspaceContext,
    entity_id: UUID,
    tax_year: int,
    *,
    model: str,
    endpoint_url: str,
) -> tuple[dict | None, dict | None]:
    """Atomically check-and-insert. Returns (new_row, None) or (None, active_row)."""
    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        # Serialise launches for this entity+year across concurrent requests.
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": f"tax-run-launch:{workspace.workspace_id}:{entity_id}:{tax_year}"},
        )
        await db_reconcile_orphans(session, entity_id, tax_year, sorted(_LIVE_RUNS))
        active = await db_find_active(session, entity_id, tax_year)
        if active is not None:
            return None, active
        row = (
            await session.execute(
                text(
                    f"""
                    WITH ins AS (
                        INSERT INTO specialist_run (
                            workspace_id, entity_id, tax_year, specialist_id,
                            agent_token_id, invoker_uc_uid, backend, model,
                            endpoint_url, status, input_summary
                        ) VALUES (
                            :wid, :eid, :year, :specialist_id,
                            :launcher, :invoker, 'openai', :model,
                            :endpoint_url, 'queued', CAST(:input_summary AS jsonb)
                        )
                        RETURNING *
                    )
                    SELECT {_ROW_COLUMNS} FROM ins sr
                    """
                ),
                {
                    "wid": str(workspace.workspace_id),
                    "eid": str(entity_id),
                    "year": tax_year,
                    "specialist_id": SPECIALIST_ID,
                    "launcher": LAUNCHER_AGENT_ID,
                    "invoker": workspace.user_uc_uid,
                    "model": model,
                    "endpoint_url": endpoint_url,
                    "input_summary": json.dumps(
                        {
                            "workspace_id": str(workspace.workspace_id),
                            "entity_id": str(entity_id),
                            "tax_year": tax_year,
                            "launched_from": "web",
                            "request": _request_text(tax_year),
                        }
                    ),
                },
            )
        ).mappings().one()
    return dict(row), None


async def db_get_run(workspace_id: UUID, run_id: UUID) -> dict | None:
    async with workspace_session(SessionLocal, workspace_id) as session:
        row = (
            await session.execute(
                text(f"SELECT {_ROW_COLUMNS} FROM specialist_run sr WHERE sr.id = :id"),
                {"id": str(run_id)},
            )
        ).mappings().first()
    return dict(row) if row else None


async def db_get_status(workspace_id: UUID, run_id: UUID) -> str | None:
    async with workspace_session(SessionLocal, workspace_id) as session:
        return (
            await session.execute(
                text("SELECT status FROM specialist_run WHERE id = :id"),
                {"id": str(run_id)},
            )
        ).scalar_one_or_none()


async def db_mark_running(workspace_id: UUID, run_id: UUID) -> bool:
    """queued -> running. False if the row was cancelled meanwhile."""
    async with workspace_session(SessionLocal, workspace_id) as session:
        got = (
            await session.execute(
                text(
                    """
                    UPDATE specialist_run
                       SET status = 'running', started_at = now()
                     WHERE id = :id AND status = 'queued'
                    RETURNING id
                    """
                ),
                {"id": str(run_id)},
            )
        ).first()
    return got is not None


async def db_finish(
    workspace_id: UUID,
    run_id: UUID,
    *,
    new_status: str,
    duration_ms: int | None,
    output_summary: dict | None,
    error_summary: str | None,
) -> bool:
    """running -> final. Guarded so a concurrent cancel always wins."""
    async with workspace_session(SessionLocal, workspace_id) as session:
        got = (
            await session.execute(
                text(
                    """
                    UPDATE specialist_run
                       SET status = :status,
                           finished_at = now(),
                           duration_ms = :duration_ms,
                           output_summary = CAST(:output_summary AS jsonb),
                           error_summary = :error_summary
                     WHERE id = :id AND status IN ('queued', 'running')
                    RETURNING id
                    """
                ),
                {
                    "status": new_status,
                    "duration_ms": duration_ms,
                    "output_summary": json.dumps(output_summary) if output_summary else None,
                    "error_summary": error_summary,
                    "id": str(run_id),
                },
            )
        ).first()
    return got is not None


async def db_cancel(workspace_id: UUID, run_id: UUID, by_uc_uid: str) -> bool:
    async with workspace_session(SessionLocal, workspace_id) as session:
        got = (
            await session.execute(
                text(
                    """
                    UPDATE specialist_run
                       SET status = 'cancelled',
                           finished_at = now(),
                           duration_ms = CAST(EXTRACT(EPOCH FROM (now() - started_at)) * 1000 AS bigint),
                           error_summary = :why
                     WHERE id = :id
                       AND agent_token_id = :launcher
                       AND status IN ('queued', 'running')
                    RETURNING id
                    """
                ),
                {
                    "id": str(run_id),
                    "launcher": LAUNCHER_AGENT_ID,
                    "why": f"Cancelled by {by_uc_uid}",
                },
            )
        ).first()
    return got is not None


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

_LIVE_RUNS: set[str] = set()
_TASKS: set[asyncio.Task] = set()
_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(_max_concurrent())
    return _semaphore


def _request_text(tax_year: int) -> str:
    # Classified by dispatch_plan as 'full_trio' (1120 + 1099 -> SC1120).
    return f"prepare {tax_year} returns"


def _child_env() -> dict[str, str]:
    env = {k: os.environ[k] for k in _ENV_ALLOWLIST if k in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _coordinator_cmd(
    workspace_id: UUID, entity_id: UUID, tax_year: int, out_dir: pathlib.Path
) -> list[str]:
    return [
        sys.executable,
        str(_COORDINATOR),
        "--workspace-id", str(workspace_id),
        "--entity-id", str(entity_id),
        "--tax-year", str(tax_year),
        "--request", _request_text(tax_year),
        "--output-dir", str(out_dir),
        "--backend", "openai",
    ]


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    except OSError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass


_SUMMARY_RE = re.compile(r"Summary:\s*(\d+)\s+complete,\s*(\d+)\s+error", re.I)


def publish_drafts(
    run_dir: pathlib.Path, entity_dir: pathlib.Path, tax_year: int, run_id: str
) -> dict[str, str]:
    """Copy the run's outputs to the canonical names the draft viewer reads.

    Returns {canonical_slug: run-relative source file}. An existing canonical
    file is renamed to ``*.superseded-<run8>.md`` first, never overwritten.
    """
    published: dict[str, str] = {}
    for prefix, slug in _PUBLISH_MAP:
        candidates = sorted(
            (p for p in run_dir.glob(f"{prefix}*.md") if not p.name.endswith("-dryrun.md")),
            key=lambda p: p.stat().st_mtime,
        )
        if not candidates:
            continue
        src = candidates[-1]
        dest = entity_dir / f"{tax_year}-{slug}-draft.md"
        if dest.exists():
            dest.rename(entity_dir / f"{tax_year}-{slug}-draft.superseded-{run_id[:8]}.md")
        shutil.copy2(src, dest)
        published[slug] = src.name
    return published


async def execute_run(
    workspace_id: UUID, entity_id: UUID, tax_year: int, run_id: str
) -> None:
    """Background worker: run the coordinator for one queued row."""
    rid = UUID(run_id)
    try:
        async with _get_semaphore():
            if not await db_mark_running(workspace_id, rid):
                return  # cancelled while queued
            entity_dir = _drafts_root() / str(workspace_id) / str(entity_id)
            run_dir = entity_dir / "runs" / run_id
            run_dir.mkdir(parents=True, exist_ok=True)

            t0 = time.monotonic()
            proc = await asyncio.create_subprocess_exec(
                *_coordinator_cmd(workspace_id, entity_id, tax_year, run_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_child_env(),
                cwd=str(_REPO_ROOT),
                start_new_session=True,  # own process group -> cancel kills specialists too
            )
            comm = asyncio.ensure_future(proc.communicate())
            outcome: str | None = None  # 'cancelled' | 'timeout' | None (exited)
            while not comm.done():
                done, _ = await asyncio.wait({comm}, timeout=_POLL_S)
                if done:
                    break
                if await db_get_status(workspace_id, rid) == "cancelled":
                    outcome = "cancelled"
                elif time.monotonic() - t0 > _max_runtime_s():
                    outcome = "timeout"
                if outcome:
                    _kill_group(proc)
                    break
            try:
                stdout_b, stderr_b = await asyncio.wait_for(comm, timeout=30)
            except asyncio.TimeoutError:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                stdout_b, stderr_b = b"", b""
            duration_ms = int((time.monotonic() - t0) * 1000)
            stdout = stdout_b.decode(errors="replace")
            stderr = stderr_b.decode(errors="replace")

            if outcome == "cancelled":
                return  # row already says cancelled; partial files are NOT published
            if outcome == "timeout":
                await db_finish(
                    workspace_id, rid, new_status="failed", duration_ms=duration_ms,
                    output_summary=None,
                    error_summary=f"Timed out after {_max_runtime_s()} s; the run was stopped.",
                )
                return

            m = _SUMMARY_RE.search(stdout)
            complete = int(m.group(1)) if m else 0
            errors = int(m.group(2)) if m else 0
            err_lines = [ln.strip() for ln in stdout.splitlines() if ln.strip().startswith("[ERROR]")]
            if proc.returncode == 0 and complete > 0:
                published = publish_drafts(run_dir, entity_dir, tax_year, run_id)
                report = published.get("coordinator-summary")
                await db_finish(
                    workspace_id, rid, new_status="completed", duration_ms=duration_ms,
                    output_summary={
                        "output_file": f"runs/{run_id}/{report}" if report else None,
                        "specialists_complete": complete,
                        "specialists_errored": errors,
                        "published_drafts": sorted(published),
                    },
                    error_summary=_scrub("\n".join(err_lines))[:4000] or None,
                )
                return
            tail = "\n".join(err_lines) or stderr.strip()[-2000:] or stdout.strip()[-2000:]
            await db_finish(
                workspace_id, rid, new_status="failed", duration_ms=duration_ms,
                output_summary={"specialists_complete": complete, "specialists_errored": errors},
                error_summary=_scrub(
                    f"Coordinator exit {proc.returncode}; no specialist completed.\n{tail}"
                )[:4000],
            )
    except Exception as exc:  # noqa: BLE001 — the row must never be left 'running'
        log.exception("[tax-run] worker crashed run_id=%s", run_id)
        try:
            await db_finish(
                workspace_id, rid, new_status="failed", duration_ms=None,
                output_summary=None,
                error_summary=_scrub(f"Launcher error: {type(exc).__name__}: {exc}")[:2000],
            )
        except Exception:  # noqa: BLE001
            log.exception("[tax-run] could not record failure run_id=%s", run_id)
    finally:
        _LIVE_RUNS.discard(run_id)


def spawn_worker(workspace_id: UUID, entity_id: UUID, tax_year: int, run_id: str) -> None:
    _LIVE_RUNS.add(run_id)
    task = asyncio.get_running_loop().create_task(
        execute_run(workspace_id, entity_id, tax_year, run_id)
    )
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

_LAUNCH_PATH = "/api/workspaces/{workspace_id}/entities/{entity_id}/tax/{tax_year}/specialist-runs/launch"


def _check_year(tax_year: int) -> None:
    import datetime as dt

    if not (1990 <= tax_year <= dt.date.today().year):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"tax_year {tax_year} is not a plausible completed tax year.",
        )


def _authorize(workspace: WorkspaceContext, entity_id: UUID) -> None:
    require_role(workspace, "accountant")
    assert_entity_in_scope(workspace, entity_id)
    if workspace.entity_scope is not None:
        # The runner acts as the app's service identity, whose reach is the
        # workspace, not this member's entity subset. A prompt-injected model
        # could then read entities the member cannot. Until runs carry a
        # per-member identity, launching needs workspace-wide membership.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Starting a run needs workspace-wide access; your membership is "
                "limited to specific entities. Ask a workspace admin to start it."
            ),
        )


def _public(row: dict) -> dict:
    row = dict(row)
    row.pop("endpoint_url", None)
    return row


@router.get(_LAUNCH_PATH)
async def preview_tax_run(
    workspace_id: UUID,
    entity_id: UUID,
    tax_year: int,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    _authorize(workspace, entity_id)
    _check_year(tax_year)
    cfg = launch_config(workspace.workspace_id)
    active = await db_get_active(workspace.workspace_id, entity_id, tax_year)
    cfg["active_run"] = _public(active) if active else None
    return cfg


@router.post(_LAUNCH_PATH, status_code=status.HTTP_202_ACCEPTED)
async def launch_tax_run(
    workspace_id: UUID,
    entity_id: UUID,
    tax_year: int,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    _authorize(workspace, entity_id)
    _check_year(tax_year)

    cfg = launch_config(workspace.workspace_id)
    if not cfg["enabled"]:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=cfg["disabled_reason"],
        )

    kind = await db_get_entity_kind(workspace.workspace_id, entity_id)
    if kind is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    if kind != "c_corp":
        raise HTTPException(
            status_code=422,
            detail=(
                "Run drafts currently prepares the C-corporation package "
                f"(Form 1120, 1099, SC1120); this entity is '{kind}'."
            ),
        )

    endpoint, model, _key = _resolve_route()
    row, active = await db_create_queued_run(
        workspace, entity_id, tax_year, model=model, endpoint_url=endpoint
    )
    if active is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": (
                    f"A run is already {active['status']} for this entity and "
                    f"{tax_year}. Wait for it to finish or cancel it."
                ),
                "active_run_id": active["id"],
            },
        )
    assert row is not None
    spawn_worker(workspace.workspace_id, entity_id, tax_year, row["id"])
    log.info(
        "[tax-run] queued run_id=%s entity=%s year=%s by=%s",
        row["id"], str(entity_id)[:8], tax_year, workspace.user_uc_uid,
    )
    return {"run": _public(row)}


@router.post("/api/workspaces/{workspace_id}/specialist-runs/{run_id}/cancel")
async def cancel_tax_run(
    workspace_id: UUID,
    run_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    require_role(workspace, "accountant")
    row = await db_get_run(workspace.workspace_id, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Specialist run not found")
    assert_entity_in_scope(workspace, UUID(row["entity_id"]))
    if row["agent_token_id"] != LAUNCHER_AGENT_ID:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This run was started outside the app and cannot be cancelled here.",
        )
    if row["status"] not in ACTIVE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Run is already '{row['status']}'.",
        )
    if not await db_cancel(workspace.workspace_id, run_id, workspace.user_uc_uid):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Run finished before it could be cancelled.",
        )
    return {"ok": True, "id": str(run_id), "status": "cancelled"}
