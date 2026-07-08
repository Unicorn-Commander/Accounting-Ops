"""Sentinel finding de-duplication — the topic-key idempotency that
``run_sentinel`` always documented but ``_write_finding`` never enforced
(restored 2026-06-02 via tenant migration 0020 + execute_action guard).

Two layers, both covered here:
  * runtime  — execute_action refreshes an existing OPEN finding and SKIPS the
    side effect instead of inserting a duplicate (pure-python, mocked session).
  * schema   — string-level checks on migration 0020 (no DB; runs in `make test`).
"""
from __future__ import annotations

import pathlib
import re
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from agents.sentinel.actions import (
    ActionContext,
    _finding_dedupe_key,
    execute_action,
)
from agents.sentinel.config import ALL_CHECK_TYPES, AutonomyLevel, SentinelConfig


def _cfg(level: AutonomyLevel = AutonomyLevel.DRAFT_AND_FLAG) -> SentinelConfig:
    return SentinelConfig(
        workspace_id=uuid4(),
        autonomy_level=level,
        check_interval_minutes=360,
        max_actions_per_day=50,
        enabled_checks=ALL_CHECK_TYPES,
        notify_owners=True,
        notify_accountants=True,
    )


# --------------------------------------------------------------------------
# _finding_dedupe_key priority + bounds
# --------------------------------------------------------------------------


def test_dedupe_key_explicit_else_check_type_entity():
    eid = uuid4()
    # An explicit per-subject key wins (the genuine loopers: filing per form,
    # regulatory per item, tax_credit per program, approval per item, connector
    # per connector).
    assert (
        _finding_dedupe_key(
            check_type="connector_health", entity_id=eid,
            explicit="sentinel-connector-stripe-abcd1234",
        )
        == "sentinel-connector-stripe-abcd1234"
    )
    # Default: ONE open finding per check per entity. A period-stamped topic is
    # intentionally NOT used (that would resurrect per-month/quarter dups).
    assert (
        _finding_dedupe_key(check_type="cash_runway", entity_id=eid)
        == f"cash_runway:{eid}"
    )


def test_dedupe_key_handles_null_entity_and_caps_length():
    assert (
        _finding_dedupe_key(check_type="pending_invitations", entity_id=None)
        == "pending_invitations:-"
    )
    key = _finding_dedupe_key(check_type="x", entity_id=uuid4(), explicit="y" * 500)
    assert len(key) == 200


# --------------------------------------------------------------------------
# execute_action: existing OPEN finding -> refresh + skip side effect
# --------------------------------------------------------------------------


def _session_recording(*, existing_open_id=None):
    """Mocked AsyncSession that records every executed SQL string and optionally
    reports an existing OPEN finding for the dedupe lookup."""
    session = MagicMock()
    session.execute = AsyncMock()
    seen: list[str] = []

    async def _router(stmt, params=None):
        s = str(stmt).lower()
        seen.append(s)
        result = MagicMock()
        if "count(*)" in s and "decision_audit" in s:
            row = MagicMock()
            row.c = 0
            result.first = MagicMock(return_value=row)
            return result
        if "from closed_period" in s:
            result.first = MagicMock(return_value=None)
            return result
        if "from sentinel_finding" in s and "status = 'open'" in s:
            # the _existing_open_finding lookup
            result.first = MagicMock(
                return_value=(existing_open_id,) if existing_open_id else None
            )
            return result
        result.first = MagicMock(return_value=None)
        result.mappings = MagicMock(
            return_value=MagicMock(first=MagicMock(return_value=None))
        )
        return result

    session.execute.side_effect = _router
    return session, seen


def _ctx(session, *, check_type="cash_runway") -> ActionContext:
    cfg = _cfg()
    return ActionContext(
        session=session,
        config=cfg,
        check_run_id=uuid4(),
        check_type=check_type,
        workspace_id=cfg.workspace_id,
    )


@pytest.mark.asyncio
async def test_existing_open_finding_dedupes_and_skips_side_effect():
    existing = uuid4()
    session, seen = _session_recording(existing_open_id=existing)
    res = await execute_action(
        _ctx(session),
        action_type="draft_memo",
        entity_id=uuid4(),
        title="Cash runway 2.3 months",
        summary="Runway dipped below 3 months.",
        payload={"tax_year": 2026, "topic": "cash-runway-topic"},
    )
    assert res["ok"] is True
    assert res.get("deduped") is True
    assert res["finding_id"] == str(existing)
    # The side effect (tax_review_queue memo) must NOT have been created.
    assert not any("insert into tax_review_queue" in s for s in seen)
    # No NEW sentinel_finding row was inserted...
    assert not any("insert into sentinel_finding" in s for s in seen)
    # ...the existing finding was refreshed instead (seen_count bump).
    assert any(
        "update sentinel_finding" in s and "seen_count" in s for s in seen
    )


@pytest.mark.asyncio
async def test_no_existing_finding_proceeds_to_side_effect_and_inserts():
    session, seen = _session_recording(existing_open_id=None)
    res = await execute_action(
        _ctx(session),
        action_type="draft_memo",
        entity_id=uuid4(),
        title="Cash runway 2.3 months",
        summary="Runway dipped below 3 months.",
        payload={"tax_year": 2026, "topic": "cash-runway-topic"},
    )
    assert res["ok"] is True
    assert res.get("deduped") is not True
    # First time: the memo + a finding (ON CONFLICT INSERT) are written.
    assert any("insert into tax_review_queue" in s for s in seen)
    assert any(
        "insert into sentinel_finding" in s and "on conflict" in s for s in seen
    )


# --------------------------------------------------------------------------
# Migration 0020 — string-level schema checks (no DB)
# --------------------------------------------------------------------------

_MIG = (
    pathlib.Path(__file__).parent.parent.parent
    / "migrations" / "tenant" / "versions" / "0020_sentinel_finding_dedupe.py"
)


def _mig_src() -> str:
    return _MIG.read_text(encoding="utf-8")


def test_migration_0020_exists_and_chains_after_0019():
    assert _MIG.exists()
    src = _mig_src()
    assert 'revision: str = "0020_sentinel_finding_dedupe"' in src
    assert 'down_revision: str | None = "0019_ar_invoicing"' in src
    assert "requires -x tenant=" in src


def test_migration_0020_adds_dedupe_columns():
    src = _mig_src()
    for col in ("dedupe_key", "last_seen_at", "seen_count"):
        assert col in src, f"0020 missing column {col!r}"


def test_migration_0020_partial_unique_index_on_open():
    src = _mig_src()
    assert "sentinel_finding_open_dedupe_idx" in src
    assert re.search(
        r"CREATE\s+UNIQUE\s+INDEX[\s\S]*\(workspace_id,\s*dedupe_key\)[\s\S]*WHERE\s+status\s*=\s*'open'",
        src,
        re.IGNORECASE,
    )


def test_migration_0020_collapses_dups_auditably():
    src = _mig_src()
    # Losers are dismissed (not deleted) and stamped for audit.
    assert "auto_deduped" in src
    assert "superseded_by" in src
    assert "dismissed" in src
    # Backfill keys the looping per-subject checks distinctly.
    for ct in ("approval_aging", "connector_health", "filing_deadlines", "regulatory_updates"):
        assert ct in src, f"0020 backfill should reference {ct!r}"


def test_migration_0020_downgrade_drops_index_and_columns():
    src = _mig_src()
    assert "def downgrade" in src
    assert "DROP INDEX" in src
    assert "DROP COLUMN" in src
