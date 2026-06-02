"""Unit tests for the FalkorDB projection layer (workstream B).

The graph is a best-effort PROJECTION of authoritative Postgres. These tests
pin the contract WITHOUT a live FalkorDB:

  * project() never raises + adds zero latency (fire-and-forget) and swallows
    projection errors; it's a clean no-op when falkordb-py is absent or there's
    no running loop.
  * the new graph helpers (upsert_journal_entry, upsert_compliance_rule,
    record_decision w/ OVERRIDES, link_journal_entry_to_source,
    record_learned_pattern, get_learned_category_for_vendor) emit the expected
    MERGE-based Cypher + params against a FAKE graph.
  * the read-side apply_learned_pattern degrades gracefully (no entity / no
    pattern / graph raises / already-auto) and surfaces a learned pattern as a
    review-grade `suggest` otherwise.

A live graph is never required; every graph call goes through a fake client.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from app import falkordb
from app.categorization import CategorySuggestion, apply_learned_pattern


# ---------------------------------------------------------------------------
# Fake FalkorDB graph: records (query, params) and returns canned result sets.
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, result_set):
        self.result_set = result_set


class _FakeGraph:
    def __init__(self, responses=None):
        # responses: list of result_sets returned in call order; default [].
        self.queries: list[tuple[str, dict]] = []
        self._responses = list(responses or [])

    async def query(self, q, params=None):
        self.queries.append((q, params or {}))
        if self._responses:
            return _FakeResult(self._responses.pop(0))
        return _FakeResult([])


def _install_fake_graph(monkeypatch, graph) -> None:
    """Patch falkordb_session to yield `graph`, and pretend falkordb-py exists."""

    @asynccontextmanager
    async def _session(workspace_id):
        yield graph

    monkeypatch.setattr(falkordb, "falkordb_session", _session)
    # Pretend the client is installed so project() schedules instead of no-op.
    if falkordb.FalkorDB is None:
        monkeypatch.setattr(falkordb, "FalkorDB", object)


WID = "11111111-1111-1111-1111-111111111111"
EID = "22222222-2222-2222-2222-222222222222"


# ---------------------------------------------------------------------------
# project(): the fire-and-forget projection guard
# ---------------------------------------------------------------------------


async def test_project_runs_coro_and_swallows_success(monkeypatch):
    monkeypatch.setattr(falkordb, "FalkorDB", object)
    ran = asyncio.Event()

    async def _coro():
        ran.set()

    falkordb.project(_coro(), label="t")
    # project() returns immediately; the task runs on the loop.
    await asyncio.wait_for(ran.wait(), timeout=1.0)


async def test_project_swallows_exception(monkeypatch, caplog):
    monkeypatch.setattr(falkordb, "FalkorDB", object)
    done = asyncio.Event()

    async def _boom():
        try:
            raise RuntimeError("graph down")
        finally:
            done.set()

    # Must NOT raise into the caller.
    falkordb.project(_boom(), label="boom")
    await asyncio.wait_for(done.wait(), timeout=1.0)
    # let the done_callback + logging settle
    await asyncio.sleep(0)


def test_project_noop_without_falkordb(monkeypatch):
    """No falkordb-py installed -> project() closes the coro + no-ops, no raise."""
    from inspect import getcoroutinestate, CORO_CLOSED

    monkeypatch.setattr(falkordb, "FalkorDB", None)

    async def _coro():
        return 1

    c = _coro()
    falkordb.project(c, label="x")  # must not raise
    # The coroutine was closed (never awaited) so it doesn't leak / warn.
    assert getcoroutinestate(c) == CORO_CLOSED


def test_project_noop_without_running_loop(monkeypatch):
    """No running loop (sync context) -> project() no-ops cleanly."""
    monkeypatch.setattr(falkordb, "FalkorDB", object)

    async def _coro():
        return 1

    c = _coro()
    # Called from sync context (no running loop): no raise, coro closed.
    falkordb.project(c, label="x")
    c.close()  # idempotent close is fine


# ---------------------------------------------------------------------------
# Write-side graph helpers emit the right Cypher + params
# ---------------------------------------------------------------------------


async def test_upsert_journal_entry_merges_node_and_for_entity(monkeypatch):
    graph = _FakeGraph()
    _install_fake_graph(monkeypatch, graph)

    await falkordb.upsert_journal_entry(
        WID, EID, "je-1", category="SaaS · AI", amount_cents=1234,
        effective_date="2025-03-31", source="simplefin",
        source_external_id="ext-1", payee="Anthropic",
    )
    assert len(graph.queries) == 1
    q, params = graph.queries[0]
    assert "MERGE (j:JournalEntry {id: $je_id})" in q
    assert "MERGE (j)-[:FOR_ENTITY]->(e)" in q
    assert params["je_id"] == "je-1"
    assert params["entity_id"] == EID
    assert params["category"] == "SaaS · AI"
    assert params["amount_cents"] == 1234


async def test_upsert_journal_entry_reversal_adds_reverses_edge(monkeypatch):
    graph = _FakeGraph()
    _install_fake_graph(monkeypatch, graph)

    await falkordb.upsert_journal_entry(
        WID, EID, "rev-1", category="Meals", amount_cents=500,
        effective_date="2025-01-01", source="reversal",
        source_external_id="reversal:orig-1", payee="x",
        reverses_entry_id="orig-1",
    )
    assert len(graph.queries) == 2
    rev_q, rev_params = graph.queries[1]
    assert "MERGE (j)-[:REVERSES]->(orig)" in rev_q
    assert rev_params["orig_id"] == "orig-1"


async def test_record_decision_merges_decision_node(monkeypatch):
    graph = _FakeGraph()
    _install_fake_graph(monkeypatch, graph)

    await falkordb.record_decision(
        WID, EID, decision_id="d-1", agent_id="cat-agent",
        verdict="applied", reasoning="because", subject_id="je-9",
    )
    assert len(graph.queries) == 1
    q, params = graph.queries[0]
    assert "MERGE (d:Decision {id: $id})" in q
    assert params["id"] == "d-1"
    assert params["subject_id"] == "je-9"


async def test_record_decision_explicit_overrides_edge(monkeypatch):
    graph = _FakeGraph()
    _install_fake_graph(monkeypatch, graph)

    await falkordb.record_decision(
        WID, EID, decision_id="d-2", agent_id="a", verdict="applied",
        reasoning="r", overrides_decision_id="d-1",
    )
    assert len(graph.queries) == 2
    q, params = graph.queries[1]
    assert "MERGE (d)-[:OVERRIDES]->(prev)" in q
    assert params["prev_id"] == "d-1"


async def test_record_decision_subject_derived_overrides_edge(monkeypatch):
    graph = _FakeGraph()
    _install_fake_graph(monkeypatch, graph)

    await falkordb.record_decision(
        WID, EID, decision_id="d-3", agent_id="a", verdict="applied",
        reasoning="r", subject_id="je-1", overrides_subject_id="je-1",
    )
    assert len(graph.queries) == 2
    q, params = graph.queries[1]
    # Derives the prior decision for the same subject in-graph.
    assert "prev.subject_id = $subject_id" in q
    assert "MERGE (d)-[:OVERRIDES]->(prev)" in q
    assert params["subject_id"] == "je-1"


async def test_explicit_override_takes_precedence_over_subject(monkeypatch):
    graph = _FakeGraph()
    _install_fake_graph(monkeypatch, graph)
    await falkordb.record_decision(
        WID, EID, decision_id="d-4", agent_id="a", verdict="v", reasoning="r",
        overrides_decision_id="d-prev", overrides_subject_id="je-1",
    )
    # Only the explicit-id branch runs (elif), so no subject-derived query.
    assert len(graph.queries) == 2
    _, params = graph.queries[1]
    assert params["prev_id"] == "d-prev"


async def test_upsert_compliance_rule_temporal_fields(monkeypatch):
    graph = _FakeGraph()
    _install_fake_graph(monkeypatch, graph)

    await falkordb.upsert_compliance_rule(
        WID, EID, "rule-1", kind="fers", effective_from="2025-01-01T00:00:00+00:00",
        effective_to=None, trigger="event", action="notify",
        source="ai_inferred", active=False,
    )
    q, params = graph.queries[0]
    assert "MERGE (r:ComplianceRule {id: $id})" in q
    assert "datetime($effective_from)" in q
    assert params["active"] is False
    assert params["kind"] == "fers"


async def test_link_journal_entry_to_source_edge(monkeypatch):
    graph = _FakeGraph()
    _install_fake_graph(monkeypatch, graph)
    await falkordb.link_journal_entry_to_source(WID, EID, "je-1", "abc123")
    q, params = graph.queries[0]
    assert "MERGE (j)-[:SUPPORTED_BY]->(s)" in q
    assert params["hash"] == "abc123"


# ---------------------------------------------------------------------------
# Learned-pattern LEARN + READ
# ---------------------------------------------------------------------------


async def test_record_learned_pattern_merges_vendor_and_rule(monkeypatch):
    graph = _FakeGraph()
    _install_fake_graph(monkeypatch, graph)

    await falkordb.record_learned_pattern(
        WID, EID, "Anthropic", "SaaS · AI", confirmed_by="aaron",
    )
    q, params = graph.queries[0]
    assert "MERGE (v:Vendor {entity_id: $entity_id, name: $name})" in q
    assert "MERGE (v)-[:LEARNED_PATTERN]->(r)" in q
    assert "r.confirmations" in q  # confirmation counter advances
    assert params["name"] == "Anthropic"
    assert params["category"] == "SaaS · AI"


async def test_record_learned_pattern_ignores_blank(monkeypatch):
    graph = _FakeGraph()
    _install_fake_graph(monkeypatch, graph)
    await falkordb.record_learned_pattern(WID, EID, "  ", "SaaS · AI")
    await falkordb.record_learned_pattern(WID, EID, "x", "  ")
    assert graph.queries == []  # nothing projected for blank vendor/category


async def test_get_learned_category_returns_pattern(monkeypatch):
    graph = _FakeGraph(responses=[[["SaaS · AI", 3]]])
    _install_fake_graph(monkeypatch, graph)

    out = await falkordb.get_learned_category_for_vendor(WID, EID, "Anthropic")
    assert out == {
        "category": "SaaS · AI",
        "confirmations": 3,
        "source": "falkordb:learned_pattern",
    }
    q, params = graph.queries[0]
    assert "MATCH (v:Vendor)-[p:LEARNED_PATTERN]->(r:CategorizationRule)" in q
    assert "toLower(v.name) = toLower($name)" in q
    assert params["entity_id"] == EID


async def test_get_learned_category_none_when_empty(monkeypatch):
    graph = _FakeGraph(responses=[[]])
    _install_fake_graph(monkeypatch, graph)
    out = await falkordb.get_learned_category_for_vendor(WID, EID, "Unknown Co")
    assert out is None


async def test_get_learned_category_blank_vendor_short_circuits(monkeypatch):
    graph = _FakeGraph()
    _install_fake_graph(monkeypatch, graph)
    out = await falkordb.get_learned_category_for_vendor(WID, EID, "   ")
    assert out is None
    assert graph.queries == []  # no query issued for a blank vendor


# ---------------------------------------------------------------------------
# Read-side integration: apply_learned_pattern degrades gracefully
# ---------------------------------------------------------------------------


def _base_suggestion(status="unknown", category="Uncategorized"):
    return CategorySuggestion(
        category=category, status=status, source="default",
        kind="unknown", reason="x", sign_matched=False,
    )


async def test_apply_learned_pattern_no_entity_returns_original():
    s = _base_suggestion()
    out = await apply_learned_pattern(s, workspace_id=WID, entity_id=None, vendor_name="X")
    assert out is s


async def test_apply_learned_pattern_auto_not_overridden(monkeypatch):
    # An auto (deterministic) suggestion must NOT be replaced by the graph.
    called = False

    async def _never(*a, **k):
        nonlocal called
        called = True
        return {"category": "Other", "confirmations": 9}

    monkeypatch.setattr(falkordb, "get_learned_category_for_vendor", _never)
    s = _base_suggestion(status="auto", category="SaaS · AI")
    out = await apply_learned_pattern(s, workspace_id=WID, entity_id=EID, vendor_name="Anthropic")
    assert out is s
    assert called is False


async def test_apply_learned_pattern_graph_down_degrades(monkeypatch):
    async def _raise(*a, **k):
        raise RuntimeError("graph unreachable")

    monkeypatch.setattr(falkordb, "get_learned_category_for_vendor", _raise)
    s = _base_suggestion()
    out = await apply_learned_pattern(s, workspace_id=WID, entity_id=EID, vendor_name="Anthropic")
    assert out is s  # unchanged — exactly as before this hook existed


async def test_apply_learned_pattern_surfaces_suggest(monkeypatch):
    async def _hit(*a, **k):
        return {"category": "Office · Supplies", "confirmations": 2, "source": "falkordb:learned_pattern"}

    monkeypatch.setattr(falkordb, "get_learned_category_for_vendor", _hit)
    s = _base_suggestion()  # unknown
    out = await apply_learned_pattern(s, workspace_id=WID, entity_id=EID, vendor_name="Some Vendor")
    assert out is not s
    assert out.category == "Office · Supplies"
    assert out.status == "suggest"  # review-grade, never auto-post
    assert out.source == "falkordb:learned_pattern"


async def test_apply_learned_pattern_no_pattern_returns_original(monkeypatch):
    async def _none(*a, **k):
        return None

    monkeypatch.setattr(falkordb, "get_learned_category_for_vendor", _none)
    s = _base_suggestion()
    out = await apply_learned_pattern(s, workspace_id=WID, entity_id=EID, vendor_name="Vendor")
    assert out is s
