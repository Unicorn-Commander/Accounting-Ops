"""Sentinel cadence gating — the fix for "a monthly check ran 4x a day".

Two layers are covered:

  1. The pure arithmetic in ``agents/sentinel/cadence.py``.
  2. The REAL runner path (``run_workspace_cycle``), with only the DB seams
     faked. Testing the arithmetic alone would not prove the runner actually
     consults it — and "declared but never enforced" is exactly the defect
     being fixed here, so the wiring is the part worth pinning down.
"""
from __future__ import annotations

import datetime as dt
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from agents.sentinel import run_sentinel
from agents.sentinel.cadence import (
    CADENCE_MINUTES,
    cadence_minutes_for,
    is_check_due,
    is_workspace_due,
    partition_due_checks,
)
from agents.sentinel.config import ALL_CHECK_TYPES, CHECK_CADENCE, AutonomyLevel, SentinelConfig

NOW = dt.datetime(2026, 7, 20, 12, 0, tzinfo=dt.timezone.utc)


def _ago(**kw) -> dt.datetime:
    return NOW - dt.timedelta(**kw)


# ---------------------------------------------------------------------------
# Pure cadence arithmetic
# ---------------------------------------------------------------------------


def test_every_check_type_has_a_known_cadence():
    for ct in ALL_CHECK_TYPES:
        assert CHECK_CADENCE[ct] in CADENCE_MINUTES
        assert cadence_minutes_for(ct) == CADENCE_MINUTES[CHECK_CADENCE[ct]]


def test_never_run_is_always_due():
    """First-cycle rule: a workspace/check that has never run runs NOW."""
    for ct in ALL_CHECK_TYPES:
        assert is_check_due(ct, None, NOW) is True
    assert is_workspace_due(None, 10080, NOW) is True


def test_monthly_check_does_not_run_four_times_a_day():
    """The headline defect: cash_runway is monthly, the loop ticked every 6h."""
    assert CHECK_CADENCE["cash_runway"] == "monthly"
    for hours in (6, 12, 18, 24, 48, 24 * 20):
        assert is_check_due("cash_runway", _ago(hours=hours), NOW) is False
    # ...and it DOES run once the month has actually elapsed.
    assert is_check_due("cash_runway", _ago(days=30), NOW) is True


def test_weekly_check_gated_between_runs():
    assert CHECK_CADENCE["regulatory_updates"] == "weekly"
    assert is_check_due("regulatory_updates", _ago(days=1), NOW) is False
    assert is_check_due("regulatory_updates", _ago(days=6), NOW) is False
    assert is_check_due("regulatory_updates", _ago(days=7), NOW) is True


def test_daily_check_gated_between_runs():
    assert CHECK_CADENCE["filing_deadlines"] == "daily"
    assert is_check_due("filing_deadlines", _ago(hours=6), NOW) is False
    assert is_check_due("filing_deadlines", _ago(hours=24), NOW) is True


def test_grace_window_prevents_cadence_ratcheting():
    """A daily check 23h50m old fires now rather than slipping a whole tick.

    Without grace, a coarse 6h poll would push it to 30h, then 36h, ... —
    cadence would drift later on every single run.
    """
    assert is_check_due("filing_deadlines", _ago(hours=23, minutes=50), NOW, grace=30) is True
    assert is_check_due("filing_deadlines", _ago(hours=23, minutes=50), NOW, grace=0) is False


def test_workspace_interval_is_enforced():
    """sentinel_config.check_interval_minutes is editable in the UI — honor it."""
    weekly_ws = 7 * 24 * 60
    assert is_workspace_due(_ago(hours=6), weekly_ws, NOW) is False
    assert is_workspace_due(_ago(days=3), weekly_ws, NOW) is False
    assert is_workspace_due(_ago(days=7), weekly_ws, NOW) is True
    # A 6h workspace on a 6h tick still runs every tick.
    assert is_workspace_due(_ago(hours=6), 360, NOW) is True


def test_zero_interval_means_always_due():
    assert is_workspace_due(_ago(minutes=1), 0, NOW) is True


def test_naive_timestamps_do_not_explode():
    """A naive DB/fixture value must not raise TypeError inside the gate."""
    naive = NOW.replace(tzinfo=None) - dt.timedelta(days=40)
    assert is_check_due("cash_runway", naive, NOW) is True


def test_partition_splits_due_from_deferred_and_explains_why():
    last = {
        "filing_deadlines": _ago(hours=2),    # daily, not due
        "cash_runway": _ago(days=40),         # monthly, due
        "regulatory_updates": _ago(days=2),   # weekly, not due
        # "approval_aging" absent -> never run -> due
    }
    due, deferred = partition_due_checks(
        ["filing_deadlines", "cash_runway", "regulatory_updates", "approval_aging"],
        last, NOW,
    )
    assert set(due) == {"cash_runway", "approval_aging"}
    by_type = {d["check_type"]: d for d in deferred}
    assert set(by_type) == {"filing_deadlines", "regulatory_updates"}
    # The skip is explained, not silent.
    assert by_type["regulatory_updates"]["cadence"] == "weekly"
    assert by_type["regulatory_updates"]["next_due_at"]


# ---------------------------------------------------------------------------
# Runner wiring — does run_workspace_cycle ACTUALLY gate?
# ---------------------------------------------------------------------------


class _FakeSession:
    async def execute(self, *a, **kw):
        return None


def _install_runner_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    config: SentinelConfig,
    check_state: dict[str, dt.datetime | None],
) -> list[str]:
    """Fake every DB seam; return the list that records checks actually run."""
    ran: list[str] = []

    @asynccontextmanager
    async def _fake_ws_session(_maker, _wid):
        yield _FakeSession()

    async def _fake_config(_session, _wid):
        return config

    async def _fake_state(_session, _wid):
        return check_state

    async def _fake_run_one(_wid, check_type, _cycle, _cfg):
        ran.append(check_type)
        return {"check_type": check_type, "findings": 0, "actions": 0}

    async def _fake_sched(_wid):
        return 0

    async def _fake_last_run(_session, _wid):
        return None

    monkeypatch.setattr(run_sentinel, "workspace_session", _fake_ws_session)
    monkeypatch.setattr(run_sentinel, "get_or_create_config", _fake_config)
    monkeypatch.setattr(run_sentinel, "load_check_state", _fake_state)
    monkeypatch.setattr(run_sentinel, "run_one_check", _fake_run_one)
    monkeypatch.setattr(run_sentinel, "process_due_scheduled_actions", _fake_sched)
    monkeypatch.setattr(run_sentinel, "update_last_run", _fake_last_run)
    monkeypatch.setattr(run_sentinel, "kill_switch_active", lambda: False)
    return ran


def _config(**over) -> SentinelConfig:
    base = dict(
        workspace_id=uuid4(),
        autonomy_level=AutonomyLevel.DRAFT_AND_FLAG,
        check_interval_minutes=360,
        max_actions_per_day=50,
        enabled_checks=("filing_deadlines", "regulatory_updates", "cash_runway"),
        notify_owners=True,
        notify_accountants=True,
        last_run_at=None,
    )
    base.update(over)
    return SentinelConfig(**base)


async def test_fresh_workspace_runs_everything_on_first_cycle(monkeypatch):
    """A brand-new workspace must NOT wait a month for its monthly checks."""
    cfg = _config(last_run_at=None)
    ran = _install_runner_fakes(monkeypatch, config=cfg, check_state={})

    result = await run_sentinel.run_workspace_cycle(cfg.workspace_id, "New Co", now=NOW)

    assert set(ran) == set(cfg.enabled_checks)
    assert result["checks_run"] == 3
    assert result["checks_deferred"] == 0


async def test_cadence_gates_checks_between_runs(monkeypatch):
    """Everything ran an hour ago: only checks whose cadence elapsed re-run."""
    cfg = _config(last_run_at=_ago(hours=7))  # workspace gate open (360m)
    state = {
        "filing_deadlines": _ago(hours=1),   # daily -> NOT due
        "regulatory_updates": _ago(days=9),  # weekly -> due
        "cash_runway": _ago(days=1),         # monthly -> NOT due
    }
    ran = _install_runner_fakes(monkeypatch, config=cfg, check_state=state)

    result = await run_sentinel.run_workspace_cycle(cfg.workspace_id, "Acme", now=NOW)

    assert ran == ["regulatory_updates"]
    assert result["checks_run"] == 1
    assert result["checks_deferred"] == 2
    assert {d["check_type"] for d in result["deferred"]} == {
        "filing_deadlines", "cash_runway",
    }


async def test_workspace_interval_skips_the_whole_cycle(monkeypatch):
    """A workspace configured weekly does not run on the global 6h tick."""
    cfg = _config(check_interval_minutes=7 * 24 * 60, last_run_at=_ago(hours=6))
    ran = _install_runner_fakes(monkeypatch, config=cfg, check_state={})

    result = await run_sentinel.run_workspace_cycle(cfg.workspace_id, "Weekly Co", now=NOW)

    assert ran == []
    assert result["skipped"] == "workspace_not_due"
    assert result["next_due_at"]


async def test_force_bypasses_both_gates(monkeypatch):
    """The manual 'Run now' admin trigger must never be a silent no-op."""
    cfg = _config(check_interval_minutes=7 * 24 * 60, last_run_at=_ago(minutes=5))
    state = {ct: _ago(minutes=5) for ct in cfg.enabled_checks}
    ran = _install_runner_fakes(monkeypatch, config=cfg, check_state=state)

    result = await run_sentinel.run_workspace_cycle(
        cfg.workspace_id, "Acme", force=True, now=NOW
    )

    assert set(ran) == set(cfg.enabled_checks)
    assert result["forced"] is True
    assert result["checks_deferred"] == 0


async def test_autonomy_off_still_short_circuits(monkeypatch):
    cfg = _config(autonomy_level=AutonomyLevel.OFF)
    ran = _install_runner_fakes(monkeypatch, config=cfg, check_state={})

    result = await run_sentinel.run_workspace_cycle(cfg.workspace_id, "Off Co", now=NOW)

    assert ran == []
    assert result["skipped"] == "autonomy_off"


async def test_missing_check_state_table_falls_back_to_running_everything(monkeypatch):
    """Code deployed before migration 0027 degrades to the OLD behaviour.

    That is the safe direction: run too often (status quo ante) rather than
    crash the daemon or silently stop checking.
    """
    cfg = _config(last_run_at=_ago(hours=7))
    ran = _install_runner_fakes(monkeypatch, config=cfg, check_state={})

    result = await run_sentinel.run_workspace_cycle(cfg.workspace_id, "Acme", now=NOW)

    assert set(ran) == set(cfg.enabled_checks)
    assert result["checks_deferred"] == 0
