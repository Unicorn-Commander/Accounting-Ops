"""Fast unit tests for the ai_categorize_pending tool's logic.

No database: ``workspace_session``, ``authorize_workspace`` and the LLM engine
call are mocked at the seams so we exercise the tool's decision logic — which
rows it touches, what it writes, that it audits, and that ``dry_run`` writes
nothing — without an integration Postgres. The real engine-on-real-data path is
verified live on dogfood.
"""
from __future__ import annotations

import contextlib
import types
from uuid import uuid4

import pytest

from app.categorization.engine import CategorySuggestion
from app.journal.posting import CATEGORY_TO_ACCOUNT
import app.tools.ai_categorize as mod

# A real COA category so the engine-output passes the in-allow-list guard.
_VALID_CATEGORY = next(iter(CATEGORY_TO_ACCOUNT))


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    """Records UPDATE / INSERT calls; serves the SELECT its seeded rows."""

    def __init__(self, rows):
        self._rows = rows
        self.updates: list[dict] = []
        self.audits: list[dict] = []

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        if "FROM approval_queue" in sql and sql.strip().upper().startswith("SELECT"):
            return _FakeResult(self._rows)
        if "UPDATE approval_queue" in sql:
            self.updates.append(params)
            return _FakeResult([])
        if "INSERT INTO decision_audit" in sql:
            self.audits.append(params)
            return _FakeResult([])
        return _FakeResult([])


def _install(monkeypatch, rows, suggestions):
    """Wire mocks; return the fake session so tests can inspect writes."""
    session = _FakeSession(rows)

    @contextlib.asynccontextmanager
    async def _fake_ws(_sessionmaker, _wid):
        yield session

    async def _fake_authorize(workspace_id, entity_id, min_role="accountant"):
        return types.SimpleNamespace(
            agent_id=None, user_uc_uid="u1", workspace_id=workspace_id, role="accountant"
        )

    # categorize_transaction_async returns the next queued suggestion per call.
    calls = iter(suggestions)

    async def _fake_categorize(**kwargs):
        return next(calls)

    monkeypatch.setattr(mod, "workspace_session", _fake_ws)
    monkeypatch.setattr(mod, "authorize_workspace", _fake_authorize)
    monkeypatch.setattr(mod, "categorize_transaction_async", _fake_categorize)
    return session


def _suggest(category, status="suggest", source="llm:qwen3.6-35b-a3b"):
    return CategorySuggestion(
        category=category, status=status, source=source,
        kind="expense", reason="test", sign_matched=False,
    )


@pytest.mark.asyncio
async def test_writes_one_suggestion_and_audits(monkeypatch):
    rows = [
        {"approval_id": str(uuid4()), "payload": {"description": "ANTHROPIC", "amount_cents": -2000}},
        {"approval_id": str(uuid4()), "payload": {"description": "MYSTERY", "amount_cents": -500}},
    ]
    # Row 1 → confident category; row 2 → model leaves it Uncategorized.
    session = _install(monkeypatch, rows, [_suggest(_VALID_CATEGORY), _suggest("Uncategorized")])

    out = await mod.ai_categorize_pending(uuid4(), uuid4(), limit=50)

    assert out["scanned"] == 2
    assert out["suggested"] == 1
    assert out["unchanged"] == 1
    assert out["dry_run"] is False
    # Exactly one row updated, with the valid category; one audit row written.
    assert len(session.updates) == 1
    assert session.updates[0]["cat"] == _VALID_CATEGORY
    assert session.updates[0]["cstatus"] == "suggest"
    assert len(session.audits) == 1
    # The result echoes the suggestion for the human/agent.
    assert out["results"][0]["suggested_category"] == _VALID_CATEGORY


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(monkeypatch):
    rows = [{"approval_id": str(uuid4()), "payload": {"description": "ANTHROPIC", "amount_cents": -2000}}]
    session = _install(monkeypatch, rows, [_suggest(_VALID_CATEGORY)])

    out = await mod.ai_categorize_pending(uuid4(), uuid4(), limit=50, dry_run=True)

    assert out["suggested"] == 1
    assert out["dry_run"] is True
    # Nothing persisted, but the suggestion is still returned for preview.
    assert session.updates == []
    assert session.audits == []
    assert out["results"][0]["suggested_category"] == _VALID_CATEGORY


@pytest.mark.asyncio
async def test_hallucinated_category_is_not_written(monkeypatch):
    rows = [{"approval_id": str(uuid4()), "payload": {"description": "X", "amount_cents": -100}}]
    # Engine would normally reject this, but defense-in-depth: even if a category
    # outside CATEGORY_TO_ACCOUNT reaches the tool, it must not be written.
    session = _install(monkeypatch, rows, [_suggest("Totally Made Up Category")])

    out = await mod.ai_categorize_pending(uuid4(), uuid4(), limit=50)

    assert out["suggested"] == 0
    assert out["unchanged"] == 1
    assert session.updates == []
    assert session.audits == []
