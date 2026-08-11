#!/usr/bin/env python3
"""Sentinel autonomous CPA daemon — main loop.

Usage::

    # Continuous loop (default 6h between cycles, configurable per workspace)
    python -m agents.sentinel.run_sentinel

    # One-shot mode (run one cycle and exit) — used by tests + manual runs
    python -m agents.sentinel.run_sentinel --once

    # Only one workspace
    python -m agents.sentinel.run_sentinel --once --workspace <UUID>

Cycle steps:

  1. Refresh the regulatory feed cache (public schema, workspace-shared).
  2. For each active workspace:
     a. Load (or create with defaults) sentinel_config.
     b. Skip if autonomy_level == 'off'.
     c. GATE 1 — skip unless sentinel_config.check_interval_minutes has
        elapsed since sentinel_config.last_run_at.
     d. Process due sentinel_scheduled_action rows.
     e. Open a check cycle (cycle_id uuid). GATE 2 — run only the enabled
        checks whose CHECK_CADENCE has elapsed since their own last run
        (agents/sentinel/cadence.py + tenant table sentinel_check_state).
     f. After all checks finish, stamp sentinel_config.last_run_at = now().

Cadence (fixed 2026-07-20)
=========================
This loop used to run EVERY enabled check on EVERY tick of one global env
interval (SENTINEL_DEFAULT_INTERVAL_MINUTES, default 360 = 6h), ignoring both
``CHECK_CADENCE`` and the per-workspace ``check_interval_minutes`` that the
settings UI lets users edit. A check labelled "monthly" therefore ran 4x a DAY.
Both gates are now enforced (see agents/sentinel/cadence.py for the arithmetic
and the first-cycle rule). The loop interval below is now only a POLLING
frequency — it decides how often Sentinel looks, never how often a check fires.

Kill switch:
  SENTINEL_DISABLED=true → log + sleep without running.

Determinism + idempotency:
  Each check writes via execute_action(), which de-dupes on the finding's
  dedupe_key (payload['topic'] or "{check_type}:{entity_id}"): if an OPEN
  finding for the same key already exists, it is refreshed (severity / summary /
  title / last_seen_at / seen_count++) and the side effect is SKIPPED — so a
  re-detected condition never spawns a duplicate finding, memo, or flag. A
  partial unique index on (workspace_id, dedupe_key) WHERE status='open'
  (tenant migration 0020) is the DB-enforced backstop. Re-running a cycle —
  same day or weeks later while the finding is still open — adds zero rows.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import os
import signal
import sys
from contextlib import suppress
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from agents.sentinel.actions import (
    ActionContext,
    kill_switch_active,
)
from agents.sentinel.cadence import (
    is_workspace_due,
    partition_due_checks,
)
from agents.sentinel.checks import CHECK_REGISTRY
from agents.sentinel.config import (
    AutonomyLevel,
    SentinelConfig,
    get_or_create_config,
    load_check_state,
    record_check_run,
    update_last_run,
)
from app.connectors.regulatory_feeds import poll_all as poll_regulatory_feeds
from app.db import SessionLocal
from app.workspace_session import workspace_session

_log = logging.getLogger("acctops.sentinel.runner")

DEFAULT_INTERVAL_MINUTES = int(os.getenv("SENTINEL_DEFAULT_INTERVAL_MINUTES", "360"))
SLEEP_SECONDS_BETWEEN_CHECKS = float(os.getenv("SENTINEL_LOOP_SLEEP_SECONDS", "300"))


# ---------------------------------------------------------------------------
# Workspace enumeration
# ---------------------------------------------------------------------------


async def list_active_workspaces(only_workspace: UUID | None = None) -> list[dict[str, Any]]:
    """All active workspaces; optionally filtered to one."""
    async with SessionLocal() as session:
        async with session.begin():
            if only_workspace:
                rows = (
                    await session.execute(
                        text(
                            """
                            SELECT id::text AS id, slug, display_name, postgres_schema
                            FROM public.workspace
                            WHERE status = 'active' AND id = :wid
                            """
                        ),
                        {"wid": str(only_workspace)},
                    )
                ).mappings().all()
            else:
                rows = (
                    await session.execute(
                        text(
                            """
                            SELECT id::text AS id, slug, display_name, postgres_schema
                            FROM public.workspace
                            WHERE status = 'active'
                            ORDER BY slug
                            """
                        )
                    )
                ).mappings().all()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Scheduled action processor
# ---------------------------------------------------------------------------


async def process_due_scheduled_actions(workspace_id: UUID) -> int:
    """Execute any sentinel_scheduled_action rows whose scheduled_for <= now().

    For now, all scheduled actions are 'send_email' stubs (already audit-trailed
    via the existing send_email handler). Future: dispatch by action_type.
    """
    executed = 0
    async with workspace_session(SessionLocal, workspace_id) as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT id::text, entity_id::text, action_type, payload,
                           scheduled_for, created_by_check_run_id::text
                    FROM sentinel_scheduled_action
                    WHERE workspace_id = :wid
                      AND status = 'pending'
                      AND scheduled_for <= now()
                    ORDER BY scheduled_for ASC
                    LIMIT 50
                    """
                ),
                {"wid": str(workspace_id)},
            )
        ).mappings().all()
        if not rows:
            return 0

        for r in rows:
            # For now just mark executed with a note. Future: dispatch by action_type.
            await session.execute(
                text(
                    """
                    UPDATE sentinel_scheduled_action
                    SET status = 'executed',
                        executed_at = now(),
                        execution_notes = :notes
                    WHERE id = :id
                    """
                ),
                {
                    "id": r["id"],
                    "notes": f"Executed by Sentinel at {dt.datetime.utcnow().isoformat()}Z (stub).",
                },
            )
            executed += 1
    return executed


# ---------------------------------------------------------------------------
# Check execution
# ---------------------------------------------------------------------------


async def _open_check_run(
    session, *, workspace_id: UUID, check_type: str, cycle_id: UUID
) -> UUID:
    run_id = uuid4()
    await session.execute(
        text(
            """
            INSERT INTO sentinel_check_run (
                id, workspace_id, entity_id, check_type, cycle_id, status
            ) VALUES (
                :id, :wid, NULL, :ct, :cycle, 'running'
            )
            """
        ),
        {
            "id": str(run_id),
            "wid": str(workspace_id),
            "ct": check_type,
            "cycle": str(cycle_id),
        },
    )
    return run_id


async def _close_check_run(
    session,
    *,
    run_id: UUID,
    started_at: dt.datetime,
    findings: int,
    actions: int,
    summary: str,
    status: str = "completed",
    error: str | None = None,
) -> None:
    finished = dt.datetime.now(tz=dt.timezone.utc)
    duration_ms = int((finished - started_at).total_seconds() * 1000)
    await session.execute(
        text(
            """
            UPDATE sentinel_check_run
            SET finished_at = :fin, duration_ms = :dur,
                status = :st, findings_count = :findings, actions_taken = :actions,
                summary = :sum, error_summary = :err
            WHERE id = :id
            """
        ),
        {
            "id": str(run_id),
            "fin": finished,
            "dur": duration_ms,
            "st": status,
            "findings": findings,
            "actions": actions,
            "sum": summary,
            "err": error,
        },
    )


async def run_one_check(
    workspace_id: UUID, check_type: str, cycle_id: UUID, config: SentinelConfig
) -> dict[str, Any]:
    """Run a single check inside its own workspace_session transaction.

    Each check gets its own session so a failure in one check doesn't poison
    the next. The cycle_id ties all check runs in this tick together for the
    UI rollup.
    """
    handler = CHECK_REGISTRY.get(check_type)
    if handler is None:
        _log.warning("Unknown check_type %r, skipping", check_type)
        return {"check_type": check_type, "skipped": True, "reason": "unknown"}

    started = dt.datetime.now(tz=dt.timezone.utc)
    try:
        async with workspace_session(SessionLocal, workspace_id) as session:
            run_id = await _open_check_run(
                session, workspace_id=workspace_id, check_type=check_type, cycle_id=cycle_id
            )
            ctx = ActionContext(
                session=session,
                config=config,
                check_run_id=run_id,
                check_type=check_type,
                workspace_id=workspace_id,
            )
            try:
                result = await handler(ctx)
                await _close_check_run(
                    session,
                    run_id=run_id,
                    started_at=started,
                    findings=int(result.get("findings", 0)),
                    actions=int(result.get("actions", 0)),
                    summary=str(result.get("summary", ""))[:1000],
                    status="completed",
                )
                # Cadence stamp, in the SAME transaction as the run record so
                # the two can never disagree about whether this check ran.
                await record_check_run(
                    session, workspace_id, check_type,
                    status="completed", cycle_id=cycle_id,
                    findings=int(result.get("findings", 0)),
                )
                return {"check_type": check_type, "run_id": str(run_id), **result}
            except Exception as e:  # noqa: BLE001
                _log.exception("Check %s crashed: %s", check_type, e)
                await _close_check_run(
                    session,
                    run_id=run_id,
                    started_at=started,
                    findings=0,
                    actions=0,
                    summary=f"crashed: {type(e).__name__}",
                    status="failed",
                    error=str(e)[:1000],
                )
                # A crashing check still counts as "ran" for cadence purposes —
                # otherwise a permanently broken check would retry at full loop
                # frequency forever. consecutive_failures carries the alarm.
                await record_check_run(
                    session, workspace_id, check_type,
                    status="failed", cycle_id=cycle_id, findings=0,
                )
                return {"check_type": check_type, "error": str(e)[:500]}
    except Exception as e:  # noqa: BLE001
        # Outer-session-level failure (e.g. workspace schema gone).
        _log.exception("Check %s outer-session failed: %s", check_type, e)
        return {"check_type": check_type, "error": str(e)[:500]}


# ---------------------------------------------------------------------------
# Workspace cycle
# ---------------------------------------------------------------------------


async def run_workspace_cycle(
    workspace_id: UUID,
    workspace_label: str,
    *,
    force: bool = False,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Run one Sentinel cycle for a single workspace.

    ``force=True`` bypasses BOTH cadence gates. Used by the manual admin
    trigger (POST /sentinel/run) and ``--once --force``: when a human
    explicitly asks for a run, "not due yet" is the wrong answer. Scheduled
    daemon ticks never force.

    ``now`` overrides the clock read. ``agents/sentinel/cadence.py`` is pure
    arithmetic over an explicit ``now``, but this runner read the wall clock
    directly, which left cadence behaviour untestable at a fixed instant —
    tests pinned to a hardcoded date silently changed meaning as real time
    moved past it. Production callers pass nothing and get the real clock.
    """
    if kill_switch_active():
        _log.warning("Sentinel kill switch active; skipping workspace %s", workspace_id)
        return {"workspace_id": str(workspace_id), "skipped": "kill_switch"}

    now = now or dt.datetime.now(tz=dt.timezone.utc)

    # Load config + per-check cadence state inside one session.
    async with workspace_session(SessionLocal, workspace_id) as session:
        try:
            config = await get_or_create_config(session, workspace_id)
        except Exception as e:  # noqa: BLE001
            _log.exception("Config load failed for %s: %s", workspace_id, e)
            return {"workspace_id": str(workspace_id), "error": f"config_load: {e}"}
        last_run_by_check = await load_check_state(session, workspace_id)

    if config.autonomy_level == AutonomyLevel.OFF:
        _log.info("Sentinel: %s autonomy=off, skipping", workspace_label)
        return {"workspace_id": str(workspace_id), "skipped": "autonomy_off"}

    # ---- GATE 1: the per-workspace interval the settings UI actually edits --
    # config.last_run_at is None on a brand-new workspace -> always due, so a
    # fresh tenant is never made to wait out its interval before its first run.
    if not force and not is_workspace_due(
        config.last_run_at, config.check_interval_minutes, now
    ):
        next_due = config.last_run_at + dt.timedelta(
            minutes=config.check_interval_minutes
        )
        _log.info(
            "Sentinel: %s not due (interval=%dm, last_run=%s, next=%s), skipping",
            workspace_label, config.check_interval_minutes,
            config.last_run_at.isoformat(), next_due.isoformat(),
        )
        return {
            "workspace_id": str(workspace_id),
            "workspace_label": workspace_label,
            "skipped": "workspace_not_due",
            "check_interval_minutes": config.check_interval_minutes,
            "last_run_at": config.last_run_at.isoformat(),
            "next_due_at": next_due.isoformat(),
        }

    # Process due scheduled actions first (so they aren't deferred by check work)
    try:
        sched_executed = await process_due_scheduled_actions(workspace_id)
    except Exception as e:  # noqa: BLE001
        _log.exception("Scheduled-action processor failed: %s", e)
        sched_executed = 0

    # ---- GATE 2: per-check CHECK_CADENCE (daily / weekly / monthly) ---------
    if force:
        due_checks, deferred = list(config.enabled_checks), []
    else:
        due_checks, deferred = partition_due_checks(
            config.enabled_checks, last_run_by_check, now
        )

    cycle_id = uuid4()
    cycle_started = dt.datetime.now(tz=dt.timezone.utc)
    check_results: list[dict[str, Any]] = []
    for check_type in due_checks:
        result = await run_one_check(workspace_id, check_type, cycle_id, config)
        check_results.append(result)

    # Stamp last_run_at
    async with workspace_session(SessionLocal, workspace_id) as session:
        with suppress(Exception):
            await update_last_run(session, workspace_id)

    total_findings = sum(int(r.get("findings", 0) or 0) for r in check_results)
    total_actions = sum(int(r.get("actions", 0) or 0) for r in check_results)

    cycle_dur = (dt.datetime.now(tz=dt.timezone.utc) - cycle_started).total_seconds()
    _log.info(
        "Sentinel: %s cycle=%s — %d/%d checks due (%d deferred by cadence), "
        "%d findings, %d actions, %d scheduled-executed, %.1fs",
        workspace_label, cycle_id, len(check_results), len(config.enabled_checks),
        len(deferred), total_findings, total_actions, sched_executed, cycle_dur,
    )
    if deferred:
        _log.debug(
            "Sentinel: %s deferred by cadence: %s",
            workspace_label,
            ", ".join(f"{d['check_type']}({d['cadence']})" for d in deferred),
        )

    return {
        "workspace_id": str(workspace_id),
        "workspace_label": workspace_label,
        "cycle_id": str(cycle_id),
        "autonomy_level": config.autonomy_level.value,
        "forced": force,
        "checks_enabled": len(config.enabled_checks),
        "checks_run": len(check_results),
        "checks_deferred": len(deferred),
        "deferred": deferred,
        "findings": total_findings,
        "actions": total_actions,
        "scheduled_executed": sched_executed,
        "duration_seconds": round(cycle_dur, 2),
        "check_results": check_results,
    }


# ---------------------------------------------------------------------------
# Main cycle (all workspaces)
# ---------------------------------------------------------------------------


async def run_full_cycle(
    only_workspace: UUID | None = None, *, force: bool = False
) -> dict[str, Any]:
    """Run one full Sentinel cycle: refresh feeds + every active workspace.

    ``force`` bypasses the cadence gates for every workspace visited (see
    ``run_workspace_cycle``). Never set on the daemon's scheduled ticks.
    """
    started = dt.datetime.now(tz=dt.timezone.utc)

    # 1. Refresh regulatory feeds (workspace-shared)
    try:
        async with SessionLocal() as session:
            async with session.begin():
                feed_stats = await poll_regulatory_feeds(session)
    except Exception as e:  # noqa: BLE001
        _log.exception("Regulatory feed refresh failed: %s", e)
        feed_stats = {"error": str(e)[:500]}

    # 2. Per-workspace cycles
    workspaces = await list_active_workspaces(only_workspace)
    _log.info("Sentinel: %d active workspaces, kill_switch=%s",
              len(workspaces), kill_switch_active())

    per_workspace: list[dict[str, Any]] = []
    for ws in workspaces:
        per_workspace.append(
            await run_workspace_cycle(
                UUID(ws["id"]), ws["display_name"], force=force
            )
        )

    dur = (dt.datetime.now(tz=dt.timezone.utc) - started).total_seconds()
    return {
        "started_at": started.isoformat(),
        "duration_seconds": round(dur, 2),
        "kill_switch": kill_switch_active(),
        "regulatory_feed_stats": feed_stats,
        "workspaces": per_workspace,
    }


# ---------------------------------------------------------------------------
# CLI loop
# ---------------------------------------------------------------------------


_shutdown = False


def _install_signal_handlers() -> None:
    def _handler(signum, _frame):
        global _shutdown
        _log.info("Sentinel: caught signal %s; will exit after current cycle", signum)
        _shutdown = True

    with suppress(Exception):
        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)


async def loop_forever(interval_seconds: int) -> None:
    _install_signal_handlers()
    _log.info("Sentinel: starting daemon, interval=%ds, kill_switch=%s",
              interval_seconds, kill_switch_active())
    while not _shutdown:
        try:
            await run_full_cycle()
        except Exception as e:  # noqa: BLE001
            _log.exception("Sentinel cycle crashed: %s", e)
        # Sleep in small chunks so SIGTERM is responsive.
        slept = 0
        while slept < interval_seconds and not _shutdown:
            await asyncio.sleep(min(30, interval_seconds - slept))
            slept += 30
    _log.info("Sentinel: clean shutdown")


def main() -> int:
    logging.basicConfig(
        level=os.getenv("SENTINEL_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Run one cycle then exit.")
    parser.add_argument("--workspace", type=str, default=None, help="Limit to one workspace UUID.")
    parser.add_argument("--interval", type=int, default=None,
                        help=f"Seconds between cycles (POLLING frequency only; per-check "
                             f"cadence still applies). Default {DEFAULT_INTERVAL_MINUTES*60}.")
    parser.add_argument("--force", action="store_true",
                        help="Bypass the workspace-interval and per-check cadence gates. "
                             "For manual/debug runs — the daemon loop never forces.")
    args = parser.parse_args()

    only_ws = UUID(args.workspace) if args.workspace else None
    if args.once:
        result = asyncio.run(run_full_cycle(only_ws, force=args.force))
        import json
        print(json.dumps(result, indent=2, default=str))
        return 0

    interval = args.interval if args.interval is not None else DEFAULT_INTERVAL_MINUTES * 60
    asyncio.run(loop_forever(interval))
    return 0


if __name__ == "__main__":
    sys.exit(main())
