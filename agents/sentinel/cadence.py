"""Sentinel cadence gating — the "should this run right now?" arithmetic.

Background (the defect this module fixes)
=========================================
``config.CHECK_CADENCE`` has always declared a per-check cadence
(daily / weekly / monthly) and ``sentinel_config.check_interval_minutes`` has
always been editable in the settings UI — but NEITHER was enforced.
``run_workspace_cycle`` iterated ``config.enabled_checks`` with zero filtering
and the daemon slept on one global env interval
(``SENTINEL_DEFAULT_INTERVAL_MINUTES``, default 360 = 6h). Net effect: a check
labelled "monthly" ran 4x a DAY, and a workspace configured for a 7-day
interval ran on the same global tick as everyone else. The settings screen was
telling users something untrue.

Two independent gates, both now enforced
========================================
1. **Workspace gate** — ``sentinel_config.check_interval_minutes``. How often
   Sentinel does *anything* for this workspace. Evaluated against
   ``sentinel_config.last_run_at``.
2. **Check gate** — ``config.CHECK_CADENCE[check_type]``. How often each
   individual check runs *within* a due workspace cycle. Evaluated against
   ``sentinel_check_state.last_run_at`` for that (workspace, check_type).

A check therefore runs only when BOTH gates open. The daemon's own loop
interval is now just a polling frequency: it decides when to *look*, never how
often a check actually fires. Lowering ``SENTINEL_DEFAULT_INTERVAL_MINUTES``
increases responsiveness without increasing check volume.

First-cycle behaviour (explicit design requirement)
==================================================
``last_run_at is None`` means "never run" and is ALWAYS due. A freshly
provisioned workspace runs its full check set on the very first cycle rather
than waiting up to a month for the monthly checks to come around. The same is
true of a newly added check_type on an existing workspace.

The grace window
================
The daemon ticks on a coarse interval (6h by default). Without a grace window
a "daily" check whose last run was 23h30m ago would be deferred to the NEXT
tick — i.e. it would effectively become a 30-hour check, and the drift would
compound every cycle. ``GRACE_MINUTES`` (default 30, env-tunable) lets a check
fire slightly early so cadence stays anchored to a stable wall-clock rhythm
instead of ratcheting later on every run.

Everything here is pure arithmetic over explicit ``now``/``last_run`` values —
no clock reads hidden inside the predicates — so it is directly unit-testable.
"""
from __future__ import annotations

import datetime as dt
import os

from agents.sentinel.config import CHECK_CADENCE

# Cadence name -> minimum minutes between runs.
CADENCE_MINUTES: dict[str, int] = {
    "daily": 24 * 60,        # 1440
    "weekly": 7 * 24 * 60,   # 10080
    "monthly": 30 * 24 * 60,  # 43200
}

# Cadence applied to a check_type with no CHECK_CADENCE entry. "daily" is the
# conservative choice: a new check that someone forgot to classify keeps the
# old-ish behaviour (runs often) instead of silently going quiet for a month.
DEFAULT_CADENCE = "daily"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def grace_minutes() -> int:
    """Minutes a check may fire EARLY so cadence doesn't ratchet later.

    Read at call time (not import time) so tests and operators can retune it
    without reimporting the module.
    """
    return max(0, _env_int("SENTINEL_CADENCE_GRACE_MINUTES", 30))


def cadence_for(check_type: str) -> str:
    """Cadence NAME for a check_type ('daily' | 'weekly' | 'monthly')."""
    return CHECK_CADENCE.get(check_type, DEFAULT_CADENCE)


def cadence_minutes_for(check_type: str) -> int:
    """Minimum minutes between two runs of ``check_type``."""
    return CADENCE_MINUTES.get(cadence_for(check_type), CADENCE_MINUTES["daily"])


def _as_aware(value: dt.datetime | None) -> dt.datetime | None:
    """Normalize a possibly-naive DB timestamp to UTC-aware.

    asyncpg returns tz-aware values for TIMESTAMPTZ, but a naive value can
    reach us from a test fixture or a legacy row. Comparing naive to aware
    raises TypeError, which inside the runner would look like "cadence
    crashed" — so coerce rather than trust.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value


def is_due(
    last_run_at: dt.datetime | None,
    interval_minutes: int,
    now: dt.datetime,
    *,
    grace: int | None = None,
) -> bool:
    """Generic gate: has ``interval_minutes`` (minus grace) elapsed since last run?

    ``last_run_at is None`` -> True (never run == always due). A non-positive
    interval also means "always due" (an operator setting the interval to 0 is
    asking for every-cycle behaviour, which is the pre-fix status quo).
    """
    last = _as_aware(last_run_at)
    if last is None:
        return True
    if interval_minutes <= 0:
        return True
    g = grace_minutes() if grace is None else max(0, grace)
    threshold = max(0, interval_minutes - g)
    elapsed_min = (_as_aware(now) - last).total_seconds() / 60.0
    return elapsed_min >= threshold


def is_workspace_due(
    last_run_at: dt.datetime | None,
    check_interval_minutes: int,
    now: dt.datetime,
    *,
    grace: int | None = None,
) -> bool:
    """Gate 1 — has this workspace's own ``check_interval_minutes`` elapsed?"""
    return is_due(last_run_at, check_interval_minutes, now, grace=grace)


def is_check_due(
    check_type: str,
    last_run_at: dt.datetime | None,
    now: dt.datetime,
    *,
    grace: int | None = None,
) -> bool:
    """Gate 2 — has this check's declared CHECK_CADENCE elapsed?"""
    return is_due(last_run_at, cadence_minutes_for(check_type), now, grace=grace)


def partition_due_checks(
    enabled_checks: tuple[str, ...] | list[str],
    last_run_by_check: dict[str, dt.datetime | None],
    now: dt.datetime,
    *,
    grace: int | None = None,
) -> tuple[list[str], list[dict[str, object]]]:
    """Split ``enabled_checks`` into (due, deferred).

    ``deferred`` entries carry enough detail for the cycle log + the API
    response to EXPLAIN the skip — the whole point of this fix is that the
    scheduling behaviour is now legible instead of implicit.
    """
    due: list[str] = []
    deferred: list[dict[str, object]] = []
    for check_type in enabled_checks:
        last = last_run_by_check.get(check_type)
        if is_check_due(check_type, last, now, grace=grace):
            due.append(check_type)
            continue
        last_aware = _as_aware(last)
        next_due = last_aware + dt.timedelta(minutes=cadence_minutes_for(check_type))
        deferred.append(
            {
                "check_type": check_type,
                "cadence": cadence_for(check_type),
                "last_run_at": last_aware.isoformat(),
                "next_due_at": next_due.isoformat(),
            }
        )
    return due, deferred
