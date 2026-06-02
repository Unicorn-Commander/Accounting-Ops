"""Unit tests for scripts/backfill_falkordb.py (workstream B, S1).

No live Postgres + no live FalkorDB: a fake workspace-scoped session feeds
canned rows, and the falkordb projection helpers are replaced with AsyncMocks.
We assert:

  * each row type (journal_entry / decision_audit / compliance_rule) is replayed
    via the matching idempotent helper with correctly-mapped fields;
  * a SUPPORTED_BY link is created ONLY when a row has a real source_document_id
    (not the chain-hash placeholder);
  * per-workspace counts are accurate;
  * the backfill is idempotent (running twice issues the same MERGE calls — the
    helpers themselves MERGE, and the script adds no CREATE/dedup state);
  * a helper failure for one row type is recorded + the others still run.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

import scripts.backfill_falkordb as bf
from app import falkordb


WID = "11111111-1111-1111-1111-111111111111"
EID = "22222222-2222-2222-2222-222222222222"


# ---------------------------------------------------------------------------
# Fake session: routes execute() by inspecting the SQL for the table name.
# ---------------------------------------------------------------------------


class _Mappings:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return _Mappings(self._rows)


class _FakeSession:
    def __init__(self, *, journal=None, decisions=None, rules=None):
        self._journal = journal or []
        self._decisions = decisions or []
        self._rules = rules or []
        self.seen: list[str] = []

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.seen.append(sql)
        if "FROM journal_entry" in sql:
            return _Result(self._journal)
        if "FROM decision_audit" in sql:
            return _Result(self._decisions)
        if "FROM compliance_rule" in sql:
            return _Result(self._rules)
        return _Result([])


@asynccontextmanager
async def _fake_ws_session(sessionmaker, workspace_id):
    # Mirror the real workspace_session: call the sessionmaker to get a session.
    # In these tests `sessionmaker` is `_admin_sessionmaker()` == a 0-arg lambda
    # returning the _FakeSession.
    yield sessionmaker()


@pytest.fixture
def patched(monkeypatch):
    """Patch the graph helpers + ensure_workspace_subgraph with AsyncMocks."""
    mocks = {
        "ensure_workspace_subgraph": AsyncMock(return_value="workspace_x"),
        "upsert_journal_entry": AsyncMock(),
        "link_journal_entry_to_source": AsyncMock(),
        "record_decision": AsyncMock(),
        "upsert_compliance_rule": AsyncMock(),
    }
    for name, m in mocks.items():
        monkeypatch.setattr(falkordb, name, m)
    # Pretend falkordb-py is installed so backfill_workspace proceeds.
    if falkordb.FalkorDB is None:
        monkeypatch.setattr(falkordb, "FalkorDB", object)
    # Route the workspace_session used by the backfill to our fake.
    monkeypatch.setattr(bf, "workspace_session", _fake_ws_session)
    return mocks


async def test_backfill_replays_all_row_types(patched, monkeypatch):
    session = _FakeSession(
        journal=[
            {
                "id": "je-1", "entity_id": EID, "category": "SaaS · AI",
                "amount_cents": 1234, "effective_date": "2025-03-31",
                "source": "simplefin", "source_external_id": "ext-1",
                "payee": "Anthropic", "source_document_hash": "deadbeef",
                "source_document_id": "rcpt-1", "reverses_entry_id": None,
            },
            {
                "id": "je-2", "entity_id": EID, "category": "Meals",
                "amount_cents": 500, "effective_date": "2025-03-01",
                "source": "simplefin", "source_external_id": "ext-2",
                "payee": "Chipotle", "source_document_hash": "chainhashhex",
                "source_document_id": None, "reverses_entry_id": None,
            },
        ],
        decisions=[
            {
                "id": "d-1", "entity_id": EID, "tool_name": "categorize",
                "outcome": "applied", "reasoning_trace": "because",
                "model_version": "cat-agent", "subject_id": "je-1",
                "rule_version": None,
            },
        ],
        rules=[
            {
                "id": "r-1", "entity_id": EID, "kind": "fers",
                "effective_from": "2025-01-01T00:00:00+00:00",
                "effective_to": None, "trigger": "event", "action": "notify",
                "source": "ai_inferred", "active": True,
            },
        ],
    )
    # Route the admin sessionmaker call to return our fake session.
    monkeypatch.setattr(bf, "_admin_sessionmaker", lambda: (lambda: session))

    counts = await bf.backfill_workspace(WID)

    assert counts.errors == []
    assert counts.journal_entries == 2
    # Only je-1 has a real source_document_id -> exactly one SUPPORTED_BY link.
    assert counts.supported_by_links == 1
    assert counts.decisions == 1
    assert counts.compliance_rules == 1

    patched["ensure_workspace_subgraph"].assert_awaited_once_with(WID)
    # journal entries projected with mapped fields
    assert patched["upsert_journal_entry"].await_count == 2
    first_call = patched["upsert_journal_entry"].await_args_list[0]
    assert first_call.args[0] == WID and first_call.args[1] == EID
    assert first_call.kwargs["category"] == "SaaS · AI"
    assert first_call.kwargs["amount_cents"] == 1234
    # link only for je-1 / its real content hash
    patched["link_journal_entry_to_source"].assert_awaited_once_with(
        WID, EID, "je-1", "deadbeef"
    )
    # decision mapped: id, verdict=outcome, subject from request_payload->>'id'
    dcall = patched["record_decision"].await_args_list[0]
    assert dcall.kwargs["decision_id"] == "d-1"
    assert dcall.kwargs["verdict"] == "applied"
    assert dcall.kwargs["subject_id"] == "je-1"
    # compliance rule mapped: active mirrors profile.active
    rcall = patched["upsert_compliance_rule"].await_args_list[0]
    assert rcall.kwargs["active"] is True
    assert rcall.kwargs["kind"] == "fers"


async def test_backfill_idempotent_second_run_same_calls(patched, monkeypatch):
    rows = dict(
        journal=[{
            "id": "je-1", "entity_id": EID, "category": "Meals",
            "amount_cents": 500, "effective_date": "2025-03-01",
            "source": "simplefin", "source_external_id": "ext-2",
            "payee": "Chipotle", "source_document_hash": None,
            "source_document_id": None, "reverses_entry_id": None,
        }],
        decisions=[],
        rules=[],
    )
    # Fresh session per run (mirrors re-reading PG); same canned rows.
    monkeypatch.setattr(bf, "_admin_sessionmaker", lambda: (lambda: _FakeSession(**rows)))

    c1 = await bf.backfill_workspace(WID)
    n1 = patched["upsert_journal_entry"].await_count
    c2 = await bf.backfill_workspace(WID)
    n2 = patched["upsert_journal_entry"].await_count

    # The script issues the SAME number of (MERGE-based) calls each run; the
    # graph helpers converge — no growth in distinct work, no dedup state.
    assert c1.journal_entries == c2.journal_entries == 1
    assert n2 - n1 == 1
    # No SUPPORTED_BY link: row had no source_document_id.
    assert c1.supported_by_links == 0


async def test_backfill_one_row_type_failure_is_isolated(patched, monkeypatch):
    session = _FakeSession(
        journal=[{
            "id": "je-1", "entity_id": EID, "category": "Meals",
            "amount_cents": 1, "effective_date": "2025-03-01",
            "source": "s", "source_external_id": "e", "payee": "p",
            "source_document_hash": None, "source_document_id": None,
            "reverses_entry_id": None,
        }],
        decisions=[{
            "id": "d-1", "entity_id": EID, "tool_name": "categorize",
            "outcome": "applied", "reasoning_trace": "x",
            "model_version": "a", "subject_id": None, "rule_version": None,
        }],
        rules=[],
    )
    monkeypatch.setattr(bf, "_admin_sessionmaker", lambda: (lambda: session))

    # Make the decision projection blow up; journal must still be projected.
    patched["record_decision"].side_effect = RuntimeError("graph blip")

    counts = await bf.backfill_workspace(WID)

    assert counts.journal_entries == 1  # ran before the failing section
    assert any("decision_audit" in e for e in counts.errors)


async def test_backfill_without_falkordb_records_error(monkeypatch):
    monkeypatch.setattr(falkordb, "FalkorDB", None)
    counts = await bf.backfill_workspace(WID)
    assert counts.journal_entries == 0
    assert counts.errors and "not installed" in counts.errors[0]
