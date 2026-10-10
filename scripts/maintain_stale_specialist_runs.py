"""List and optionally fail specialist runs that have become stale.

Usage::

    python -m scripts.maintain_stale_specialist_runs
    python -m scripts.maintain_stale_specialist_runs --hours 48 --apply

The default is a read-only report. ``--apply`` marks only rows that are still
``running`` and older than the cutoff as ``failed``. Each update records a
dated ``error_summary`` note so the maintenance action is visible in the run
audit trail.

The shared ``books`` schema uses FORCE RLS, including for the owner role. The
command therefore discovers workspaces through the public registry and runs
the candidate query/update in a workspace-scoped session rather than trying
to bypass tenant isolation with one unscoped query.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import math
from collections.abc import Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, AsyncEngine, async_sessionmaker, create_async_engine

from app.config import get_admin_database_url
from app.workspace_session import workspace_session

DEFAULT_STALE_HOURS = 24.0


def _positive_hours(raw: str) -> float:
    try:
        hours = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("hours must be a positive number") from exc
    if not math.isfinite(hours) or hours <= 0:
        raise argparse.ArgumentTypeError("hours must be a positive number")
    return hours


def maintenance_note(now: dt.datetime) -> str:
    """Return the stable audit note used for stale-run updates."""
    return f"marked stale by maintenance {now.date().isoformat()}"


async def list_stale_runs(
    session: AsyncSession, *, cutoff: dt.datetime
) -> list[dict[str, Any]]:
    """Return running specialist runs older than ``cutoff`` for one workspace."""
    result = await session.execute(
        text(
            """
            SELECT id, workspace_id, specialist_id, model, started_at
              FROM specialist_run
             WHERE status = 'running'
               AND started_at < :cutoff
             ORDER BY started_at ASC, id
            """
        ),
        {"cutoff": cutoff},
    )
    return [dict(row) for row in result.mappings().all()]


async def mark_stale_runs(
    session: AsyncSession,
    *,
    cutoff: dt.datetime,
    finished_at: dt.datetime,
    note: str,
) -> int:
    """Mark currently-stale running rows failed and return the row count."""
    result = await session.execute(
        text(
            """
            UPDATE specialist_run
               SET status = 'failed',
                   finished_at = :finished_at,
                   error_summary = :note
             WHERE status = 'running'
               AND started_at < :cutoff
            """
        ),
        {"cutoff": cutoff, "finished_at": finished_at, "note": note},
    )
    return int(result.rowcount or 0)


async def maintain_workspace(
    session: AsyncSession,
    *,
    cutoff: dt.datetime,
    now: dt.datetime,
    apply: bool,
) -> dict[str, Any]:
    """Inspect one workspace, optionally applying the stale-run transition."""
    candidates = await list_stale_runs(session, cutoff=cutoff)
    marked = 0
    if apply and candidates:
        marked = await mark_stale_runs(
            session,
            cutoff=cutoff,
            finished_at=now,
            note=maintenance_note(now),
        )
    return {"candidates": candidates, "marked": marked}


async def _workspace_ids(sessionmaker: async_sessionmaker[AsyncSession]) -> list[str]:
    """Read the workspace registry with no tenant-scoped table access."""
    async with sessionmaker() as session:
        async with session.begin():
            result = await session.execute(
                text("SELECT id FROM public.workspace ORDER BY id")
            )
            return [str(row[0]) for row in result.all()]


async def run_maintenance(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    hours: float = DEFAULT_STALE_HOURS,
    apply: bool = False,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Run one dry-run or apply pass across all registered workspaces."""
    if hours <= 0 or not math.isfinite(hours):
        raise ValueError("hours must be a positive finite number")
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(hours=hours)
    candidates: list[dict[str, Any]] = []
    marked = 0

    for workspace_id in await _workspace_ids(sessionmaker):
        async with workspace_session(sessionmaker, workspace_id) as session:
            result = await maintain_workspace(
                session,
                cutoff=cutoff,
                now=now,
                apply=apply,
            )
            candidates.extend(result["candidates"])
            marked += result["marked"]

    return {
        "hours": hours,
        "cutoff": cutoff,
        "candidates": candidates,
        "marked": marked,
        "apply": apply,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Report specialist runs stuck in running state, optionally fail them"
    )
    parser.add_argument(
        "--hours",
        type=_positive_hours,
        default=DEFAULT_STALE_HOURS,
        help="Only runs older than this many hours (default: 24)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Mark matching running runs failed; default is read-only",
    )
    return parser


def _print_summary(summary: dict[str, Any]) -> None:
    candidates = summary["candidates"]
    print(
        f"stale specialist runs older than {summary['hours']:g}h "
        f"(cutoff {summary['cutoff'].isoformat()}): {len(candidates)}"
    )
    for row in candidates:
        print(
            f"  id={row['id']} workspace={row['workspace_id']} "
            f"specialist={row['specialist_id']} model={row['model']} "
            f"started_at={row['started_at'].isoformat()}"
        )
    if summary["apply"]:
        print(f"marked failed: {summary['marked']}")
    else:
        print("dry-run only; pass --apply to mark these rows failed")


async def _async_main(args: argparse.Namespace) -> None:
    engine: AsyncEngine = create_async_engine(
        get_admin_database_url(),
        pool_pre_ping=True,
    )
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        summary = await run_maintenance(
            sessionmaker,
            hours=args.hours,
            apply=args.apply,
        )
    finally:
        await engine.dispose()
    _print_summary(summary)


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
