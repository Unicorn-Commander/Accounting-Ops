"""Unit tests for the CFO-facing FalkorDB read helpers (workstream C).

``recent_decisions`` and ``notable_learned_patterns`` are read-side companions
the CFO chat persona consumes. Like the rest of the graph layer they are
best-effort: they run against a FAKE graph here (no live FalkorDB), and the
caller (cfo_context) catches + degrades on any failure.

Pins:
  * recent_decisions emits the expected MATCH + OVERRIDES lineage query, is
    entity-scoped, caps the limit, and maps the (verbose) row tuple into the
    decision dict shape — including BOTH OVERRIDES directions.
  * notable_learned_patterns ranks by confirmations, is entity-scoped, and
    drops rows with a null vendor/category.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from app import falkordb


class _FakeResult:
    def __init__(self, result_set):
        self.result_set = result_set


class _FakeGraph:
    def __init__(self, responses=None):
        self.queries: list[tuple[str, dict]] = []
        self._responses = list(responses or [])

    async def query(self, q, params=None):
        self.queries.append((q, params or {}))
        if self._responses:
            return _FakeResult(self._responses.pop(0))
        return _FakeResult([])


def _install_fake_graph(monkeypatch, graph) -> None:
    @asynccontextmanager
    async def _session(workspace_id):
        yield graph

    monkeypatch.setattr(falkordb, "falkordb_session", _session)
    if falkordb.FalkorDB is None:
        monkeypatch.setattr(falkordb, "FalkorDB", object)


WID = "11111111-1111-1111-1111-111111111111"
EID = "22222222-2222-2222-2222-222222222222"


# ---------------------------------------------------------------------------
# recent_decisions
# ---------------------------------------------------------------------------


async def test_recent_decisions_query_shape_and_scope(monkeypatch):
    graph = _FakeGraph(responses=[[]])
    _install_fake_graph(monkeypatch, graph)

    out = await falkordb.recent_decisions(WID, EID, limit=5)
    assert out == []
    q, params = graph.queries[0]
    assert "MATCH (d:Decision)" in q
    assert "d.entity_id = $entity_id" in q
    # Both OVERRIDES directions are walked.
    assert "(d)-[:OVERRIDES]->(prev:Decision)" in q
    assert "(newer:Decision)-[:OVERRIDES]->(d)" in q
    assert "ORDER BY d.created_at DESC" in q
    assert params["entity_id"] == EID
    assert params["limit"] == 5


async def test_recent_decisions_maps_row_with_overrides(monkeypatch):
    # One decision that supersedes a prior one (overrides_* populated) and is
    # NOT itself superseded (superseded_by_id is None).
    row = [
        "d-2", "sc-state-specialist", "recategorized", "moved to R&D",
        "je-9", "v3", 1735689600000,
        "d-1", "categorized", "was Office Supplies",  # prev (overrides)
        None,  # superseded_by_id
    ]
    graph = _FakeGraph(responses=[[row]])
    _install_fake_graph(monkeypatch, graph)

    out = await falkordb.recent_decisions(WID, EID, limit=10)
    assert len(out) == 1
    d = out[0]
    assert d["id"] == "d-2"
    assert d["agent_id"] == "sc-state-specialist"
    assert d["verdict"] == "recategorized"
    assert d["reasoning"] == "moved to R&D"
    assert d["subject_id"] == "je-9"
    assert d["created_at"] == 1735689600000
    assert d["overrides"] == {
        "id": "d-1",
        "verdict": "categorized",
        "reasoning": "was Office Supplies",
    }
    assert d["superseded_by_id"] is None


async def test_recent_decisions_marks_superseded(monkeypatch):
    # A decision with no prior (overrides None) but which a newer one overrides.
    row = [
        "d-1", "cat-agent", "categorized", "Office Supplies",
        "je-9", None, 1735689600000,
        None, None, None,        # no prev
        "d-2",                    # superseded_by_id present
    ]
    graph = _FakeGraph(responses=[[row]])
    _install_fake_graph(monkeypatch, graph)

    out = await falkordb.recent_decisions(WID, EID)
    assert out[0]["overrides"] is None
    assert out[0]["superseded_by_id"] == "d-2"


async def test_recent_decisions_caps_limit(monkeypatch):
    graph = _FakeGraph(responses=[[]])
    _install_fake_graph(monkeypatch, graph)
    await falkordb.recent_decisions(WID, EID, limit=10_000)
    _, params = graph.queries[0]
    assert params["limit"] == 50  # capped


async def test_recent_decisions_dedupes_fanout(monkeypatch):
    # Same decision id appears twice (OPTIONAL MATCH fan-out). We keep the
    # first row only so LIMIT counts distinct decisions.
    base = ["d-1", "a", "v", "r", "je-1", None, 1735689600000]
    row_a = base + ["d-0", "prev-v", "prev-r", None]
    row_b = base + ["d-0", "prev-v", "prev-r", "d-2"]  # duplicate d-1
    graph = _FakeGraph(responses=[[row_a, row_b]])
    _install_fake_graph(monkeypatch, graph)

    out = await falkordb.recent_decisions(WID, EID)
    assert len(out) == 1
    assert out[0]["id"] == "d-1"


# ---------------------------------------------------------------------------
# notable_learned_patterns
# ---------------------------------------------------------------------------


async def test_notable_learned_patterns_ranks_and_scopes(monkeypatch):
    graph = _FakeGraph(
        responses=[[["Anthropic", "SaaS · AI", 4], ["AWS", "Hosting", 2]]]
    )
    _install_fake_graph(monkeypatch, graph)

    out = await falkordb.notable_learned_patterns(WID, EID, limit=10)
    assert out == [
        {"vendor": "Anthropic", "category": "SaaS · AI", "confirmations": 4},
        {"vendor": "AWS", "category": "Hosting", "confirmations": 2},
    ]
    q, params = graph.queries[0]
    assert "MATCH (v:Vendor)-[:LEARNED_PATTERN]->(r:CategorizationRule)" in q
    assert "v.entity_id = $entity_id" in q
    assert "r.entity_id = $entity_id" in q
    assert "ORDER BY confirmations DESC" in q
    assert params["entity_id"] == EID


async def test_notable_learned_patterns_drops_null_rows(monkeypatch):
    graph = _FakeGraph(
        responses=[[[None, "Cat", 3], ["Vendor", None, 2], ["Good", "Cat", 1]]]
    )
    _install_fake_graph(monkeypatch, graph)
    out = await falkordb.notable_learned_patterns(WID, EID)
    assert out == [{"vendor": "Good", "category": "Cat", "confirmations": 1}]


async def test_notable_learned_patterns_empty(monkeypatch):
    graph = _FakeGraph(responses=[[]])
    _install_fake_graph(monkeypatch, graph)
    assert await falkordb.notable_learned_patterns(WID, EID) == []
