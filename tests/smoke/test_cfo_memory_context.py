"""Unit tests for the CFO chat grounding context (workstream C).

``agents.chat.cfo_context.build_cfo_memory_context`` assembles a read-only
markdown block (FalkorDB agent-memory + Sentinel findings) appended to the CFO
persona's system prompt. The hard contract:

  * It is BEST-EFFORT: any failure (graph down, falkordb-py absent, Sentinel
    read error) returns "" and never raises — the chat behaves as it does today.
  * Empty memory + empty findings -> "" (no block injected).
  * When there IS content, the block contains the decisions / rules / patterns /
    Sentinel sections and the framing header.
  * Workspace + entity scoped: the Sentinel SQL filters this entity (or
    workspace-wide) within an RLS-pinned session.

Everything runs against fakes — no live FalkorDB, no live Postgres.
"""
from __future__ import annotations

import pytest

from agents.chat import cfo_context

WID = "11111111-1111-1111-1111-111111111111"
EID = "22222222-2222-2222-2222-222222222222"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeMappingResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    """Minimal stand-in for an open workspace_session for the Sentinel read."""

    def __init__(self, rows=None, raise_exc=None):
        self._rows = rows or []
        self._raise = raise_exc
        self.executed: list[tuple[str, dict]] = []

    async def execute(self, stmt, params=None):
        self.executed.append((str(stmt), params or {}))
        if self._raise is not None:
            raise self._raise
        return _FakeMappingResult(self._rows)


def _patch_memory(monkeypatch, *, decisions=None, rules=None, patterns=None):
    import app.falkordb as fdb

    async def _decisions(workspace_id, entity_id, limit=8):
        return decisions or []

    async def _rules(workspace_id, entity_id, at_time=None):
        return rules or []

    async def _patterns(workspace_id, entity_id, limit=8):
        return patterns or []

    monkeypatch.setattr(fdb, "recent_decisions", _decisions)
    monkeypatch.setattr(fdb, "get_active_compliance_rules", _rules)
    monkeypatch.setattr(fdb, "notable_learned_patterns", _patterns)


# ---------------------------------------------------------------------------
# Empty / degrade paths
# ---------------------------------------------------------------------------


async def test_empty_everything_returns_blank(monkeypatch):
    _patch_memory(monkeypatch)  # all empty
    session = _FakeSession(rows=[])
    out = await cfo_context.build_cfo_memory_context(
        session=session, workspace_id=WID, entity_id=EID
    )
    assert out == ""


async def test_graph_failure_degrades_to_blank(monkeypatch):
    import app.falkordb as fdb

    async def _boom(*a, **k):
        raise RuntimeError("graph unreachable")

    monkeypatch.setattr(fdb, "recent_decisions", _boom)
    monkeypatch.setattr(fdb, "get_active_compliance_rules", _boom)
    monkeypatch.setattr(fdb, "notable_learned_patterns", _boom)
    # Sentinel also empty -> nothing to show -> "".
    session = _FakeSession(rows=[])
    out = await cfo_context.build_cfo_memory_context(
        session=session, workspace_id=WID, entity_id=EID
    )
    assert out == ""


async def test_sentinel_read_error_does_not_raise(monkeypatch):
    _patch_memory(monkeypatch)  # graph empty
    session = _FakeSession(raise_exc=RuntimeError("table missing"))
    # Must not raise; with both sides empty -> "".
    out = await cfo_context.build_cfo_memory_context(
        session=session, workspace_id=WID, entity_id=EID
    )
    assert out == ""


# ---------------------------------------------------------------------------
# Populated paths
# ---------------------------------------------------------------------------


async def test_decisions_block_renders_with_overrides(monkeypatch):
    _patch_memory(
        monkeypatch,
        decisions=[
            {
                "id": "d-2",
                "agent_id": "sc-state-specialist",
                "verdict": "recategorized to R&D",
                "reasoning": "qualifies under §174",
                "subject_id": "je-9",
                "rule_version": "v3",
                "created_at": 1735689600000,  # 2025-01-01
                "overrides": {
                    "id": "d-1",
                    "verdict": "Office Supplies",
                    "reasoning": "initial guess",
                },
                "superseded_by_id": None,
            }
        ],
    )
    session = _FakeSession(rows=[])
    out = await cfo_context.build_cfo_memory_context(
        session=session, workspace_id=WID, entity_id=EID
    )
    assert "Agent Memory & Sentinel Context" in out
    assert "Recent decisions" in out
    assert "recategorized to R&D" in out
    assert "qualifies under §174" in out
    assert "2025-01-01" in out
    # OVERRIDES lineage surfaced.
    assert "supersedes a prior decision" in out
    assert "Office Supplies" in out


async def test_superseded_decision_marked_historical(monkeypatch):
    _patch_memory(
        monkeypatch,
        decisions=[
            {
                "id": "d-1",
                "agent_id": "cat-agent",
                "verdict": "Office Supplies",
                "reasoning": "first pass",
                "subject_id": "je-9",
                "rule_version": None,
                "created_at": 1735689600000,
                "overrides": None,
                "superseded_by_id": "d-2",
            }
        ],
    )
    session = _FakeSession(rows=[])
    out = await cfo_context.build_cfo_memory_context(
        session=session, workspace_id=WID, entity_id=EID
    )
    assert "later overridden" in out
    assert "historical" in out


async def test_rules_and_patterns_render(monkeypatch):
    _patch_memory(
        monkeypatch,
        rules=[{"kind": "1099_nec", "trigger": "vendor paid > $600", "action": "flag for 1099"}],
        patterns=[{"vendor": "Anthropic", "category": "SaaS · AI", "confirmations": 4}],
    )
    session = _FakeSession(rows=[])
    out = await cfo_context.build_cfo_memory_context(
        session=session, workspace_id=WID, entity_id=EID
    )
    assert "Active compliance rules" in out
    assert "1099_nec" in out
    assert "Learned vendor → category patterns" in out
    assert "Anthropic" in out
    assert "SaaS · AI" in out
    assert "4 human confirmations" in out


async def test_sentinel_findings_render_and_scope(monkeypatch):
    _patch_memory(monkeypatch)  # graph empty; only Sentinel has data
    rows = [
        {
            "check_type": "filing_deadlines",
            "severity": "critical",
            "title": "1120 due in 14 days",
            "summary": "Federal C-corp return deadline approaching",
            "created_at": "2026-05-31",
        },
        {
            "check_type": "approval_aging",
            "severity": "low",
            "title": "3 approvals aging",
            "summary": "Pending > 7 days",
            "created_at": "2026-05-30",
        },
    ]
    session = _FakeSession(rows=rows)
    out = await cfo_context.build_cfo_memory_context(
        session=session, workspace_id=WID, entity_id=EID
    )
    assert "Open Sentinel findings" in out
    assert "1120 due in 14 days" in out
    # Most urgent (critical) is ordered before low.
    assert out.index("1120 due in 14 days") < out.index("3 approvals aging")
    assert "[CRITICAL]" in out
    # The Sentinel query is entity-scoped (entity OR workspace-wide) + RLS.
    sql, params = session.executed[0]
    assert "FROM sentinel_finding" in sql
    assert "status = 'open'" in sql
    assert "entity_id = :eid OR entity_id IS NULL" in sql
    assert params["eid"] == EID


async def test_field_truncation(monkeypatch):
    long_reason = "x" * 5000
    _patch_memory(
        monkeypatch,
        decisions=[
            {
                "id": "d-1",
                "agent_id": "a",
                "verdict": "v",
                "reasoning": long_reason,
                "subject_id": None,
                "rule_version": None,
                "created_at": 1735689600000,
                "overrides": None,
                "superseded_by_id": None,
            }
        ],
    )
    session = _FakeSession(rows=[])
    out = await cfo_context.build_cfo_memory_context(
        session=session, workspace_id=WID, entity_id=EID
    )
    # Truncated well below the raw 5000 chars (single field cap is ~300).
    assert long_reason not in out
    assert "…" in out


def test_ms_to_date_handles_bad_input():
    # Pure helper: never raises on junk.
    assert cfo_context._ms_to_date(None) == ""
    assert cfo_context._ms_to_date(1735689600000) == "2025-01-01"
    assert isinstance(cfo_context._ms_to_date("not-a-number"), str)
