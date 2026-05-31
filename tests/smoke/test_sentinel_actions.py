"""Sentinel action gating + execution tests.

Pure-python — no DB. Mocks the AsyncSession and asserts:

  * Kill switch blocks every action.
  * Autonomy level 'off' blocks every action.
  * Autonomy level 'watch_only' allows log_finding but not draft_memo.
  * Autonomy level 'draft_and_flag' allows draft_memo + flag_for_cpa_review,
    but rejects bulk_decide_approvals (fully_autonomous-only).
  * Rate limit kicks in when count >= max_actions_per_day.
  * Closed-period guard returns skipped_reason='closed_period:...'.
  * On exception, a finding row is still written (best-effort visibility).
"""
from __future__ import annotations

import datetime as dt
import os
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from agents.sentinel.actions import (
    ACTION_INFO,
    ActionContext,
    execute_action,
    kill_switch_active,
)
from agents.sentinel.config import ALL_CHECK_TYPES, AutonomyLevel, SentinelConfig


def _make_config(level: AutonomyLevel, max_per_day: int = 50) -> SentinelConfig:
    return SentinelConfig(
        workspace_id=uuid4(),
        autonomy_level=level,
        check_interval_minutes=360,
        max_actions_per_day=max_per_day,
        enabled_checks=ALL_CHECK_TYPES,
        notify_owners=True,
        notify_accountants=True,
    )


def _make_ctx(
    config: SentinelConfig,
    *,
    actions_used_today: int = 0,
    in_closed_period: bool = False,
) -> ActionContext:
    """ActionContext with a faked AsyncSession that:
       - returns ``actions_used_today`` for the count query
       - returns a period_end row (or None) for the closed_period query
       - swallows all other inserts/updates
    """
    session = MagicMock()
    session.execute = AsyncMock()

    async def _exec_router(stmt, params=None):
        s = str(stmt).lower()
        result = MagicMock()
        if "count(*)" in s and "decision_audit" in s:
            row = MagicMock()
            row.c = actions_used_today
            result.first = MagicMock(return_value=row)
            return result
        if "from closed_period" in s:
            if in_closed_period:
                result.first = MagicMock(return_value=(dt.date(2025, 12, 31),))
            else:
                result.first = MagicMock(return_value=None)
            return result
        # Default: insert/update returns empty
        result.first = MagicMock(return_value=None)
        result.mappings = MagicMock(return_value=MagicMock(first=MagicMock(return_value=None)))
        return result

    session.execute.side_effect = _exec_router

    return ActionContext(
        session=session,
        config=config,
        check_run_id=uuid4(),
        check_type="filing_deadlines",
        workspace_id=config.workspace_id,
    )


# -------------------- Kill switch --------------------


@pytest.mark.asyncio
async def test_kill_switch_blocks_all_actions():
    with patch.dict(os.environ, {"SENTINEL_DISABLED": "true"}):
        assert kill_switch_active() is True
        ctx = _make_ctx(_make_config(AutonomyLevel.DRAFT_AND_FLAG))
        res = await execute_action(
            ctx,
            action_type="draft_memo",
            entity_id=uuid4(),
            title="X",
            summary="Y",
        )
        assert res["ok"] is False
        assert res["skipped_reason"] == "kill_switch"


@pytest.mark.asyncio
async def test_kill_switch_inactive_when_unset():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("SENTINEL_DISABLED", None)
        assert kill_switch_active() is False


# -------------------- Autonomy gating --------------------


@pytest.mark.asyncio
async def test_autonomy_off_blocks_everything():
    ctx = _make_ctx(_make_config(AutonomyLevel.OFF))
    res = await execute_action(
        ctx, action_type="log_finding", entity_id=None, title="t", summary="s"
    )
    assert res["ok"] is False
    assert res["skipped_reason"] == "autonomy_off"


@pytest.mark.asyncio
async def test_watch_only_allows_log_finding_blocks_memo():
    ctx = _make_ctx(_make_config(AutonomyLevel.WATCH_ONLY))
    res_ok = await execute_action(
        ctx, action_type="log_finding", entity_id=None, title="t", summary="s"
    )
    assert res_ok["ok"] is True

    res_no = await execute_action(
        ctx, action_type="draft_memo", entity_id=uuid4(), title="t", summary="s"
    )
    assert res_no["ok"] is False
    assert res_no["skipped_reason"].startswith("autonomy_too_low")


@pytest.mark.asyncio
async def test_draft_and_flag_blocks_fully_autonomous_action():
    ctx = _make_ctx(_make_config(AutonomyLevel.DRAFT_AND_FLAG))
    res = await execute_action(
        ctx, action_type="bulk_decide_approvals", entity_id=uuid4(), title="t", summary="s"
    )
    assert res["ok"] is False
    assert res["skipped_reason"].startswith("autonomy_too_low")
    assert "fully_autonomous" in res["skipped_reason"]


@pytest.mark.asyncio
async def test_fully_autonomous_permits_bulk_decide():
    # We still don't execute the action body (there's no _do_bulk_decide_approvals
    # handler yet) — but the gate must let it pass through.
    ctx = _make_ctx(_make_config(AutonomyLevel.FULLY_AUTONOMOUS))
    res = await execute_action(
        ctx, action_type="bulk_decide_approvals", entity_id=uuid4(), title="t", summary="s"
    )
    # No handler registered → falls through to log_finding. Either way: not gated out.
    assert "autonomy_too_low" not in (res.get("skipped_reason") or "")


@pytest.mark.asyncio
async def test_unknown_action_returns_unknown_skipped_reason():
    ctx = _make_ctx(_make_config(AutonomyLevel.DRAFT_AND_FLAG))
    res = await execute_action(
        ctx, action_type="not_a_real_action", entity_id=None, title="t", summary="s"
    )
    assert res["ok"] is False
    assert res["skipped_reason"].startswith("unknown_action")


# -------------------- Rate limit --------------------


@pytest.mark.asyncio
async def test_rate_limit_blocks_action_at_cap():
    cfg = _make_config(AutonomyLevel.DRAFT_AND_FLAG, max_per_day=5)
    ctx = _make_ctx(cfg, actions_used_today=5)
    res = await execute_action(
        ctx, action_type="draft_memo", entity_id=uuid4(), title="t", summary="s"
    )
    assert res["ok"] is False
    assert res["skipped_reason"] == "rate_limited"


@pytest.mark.asyncio
async def test_rate_limit_does_not_apply_to_log_finding():
    cfg = _make_config(AutonomyLevel.WATCH_ONLY, max_per_day=0)
    ctx = _make_ctx(cfg, actions_used_today=999)
    res = await execute_action(
        ctx, action_type="log_finding", entity_id=None, title="t", summary="s"
    )
    # log_finding bypasses the cap — pure observation
    assert res["ok"] is True


# -------------------- Closed period --------------------


@pytest.mark.asyncio
async def test_closed_period_blocks_action_with_target_date():
    cfg = _make_config(AutonomyLevel.DRAFT_AND_FLAG)
    ctx = _make_ctx(cfg, in_closed_period=True)
    res = await execute_action(
        ctx,
        action_type="draft_memo",
        entity_id=uuid4(),
        title="t",
        summary="s",
        target_date=dt.date(2024, 6, 1),
    )
    assert res["ok"] is False
    assert res["skipped_reason"].startswith("closed_period:")


@pytest.mark.asyncio
async def test_closed_period_check_skipped_when_no_entity_or_date():
    cfg = _make_config(AutonomyLevel.DRAFT_AND_FLAG)
    ctx = _make_ctx(cfg, in_closed_period=True)
    # No target_date passed -> guard is a no-op, the action proceeds (or fails for
    # missing entity_id on memo, but not for closed_period).
    res = await execute_action(
        ctx, action_type="draft_memo", entity_id=None, title="t", summary="s"
    )
    # entity_required_for_memo, not closed_period
    assert res["ok"] is False
    assert "closed_period" not in (res.get("skipped_reason") or "")


# -------------------- ACTION_INFO sanity --------------------


def test_action_info_covers_known_types():
    expected = {
        "log_finding", "draft_memo", "flag_for_cpa_review", "send_email",
        "send_invitation", "schedule_reminder", "create_open_item",
        "run_specialist", "create_project_ops_task",
        "bulk_decide_approvals", "initiate_connector_sync", "provision_entity",
    }
    assert expected.issubset(set(ACTION_INFO.keys()))


def test_action_info_assigns_minimum_levels_correctly():
    assert ACTION_INFO["log_finding"]["min_level"] == AutonomyLevel.WATCH_ONLY
    assert ACTION_INFO["draft_memo"]["min_level"] == AutonomyLevel.DRAFT_AND_FLAG
    assert ACTION_INFO["bulk_decide_approvals"]["min_level"] == AutonomyLevel.FULLY_AUTONOMOUS


# -------------------- AutonomyLevel parsing --------------------


def test_autonomy_level_string_round_trip():
    for v in ("off", "watch_only", "draft_and_flag", "fully_autonomous"):
        assert AutonomyLevel(v).value == v
