"""Unit tests for the write-side projection BUILDERS (workstream B, S2).

The builders are the glue each authoritative write site calls AFTER commit:

  * app.journal.projection.project_journal_entry_posted
  * app.journal.projection.project_recategorization
  * app.tools._audit.project_decision_audit

They must schedule the right graph coroutine(s) via falkordb.project and NEVER
raise. Here falkordb.project is replaced with a recorder so we can assert what
each builder schedules without a live graph + without running the coroutine.
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest

from app import falkordb
from app.journal.posting import CorrectionOutcome, PostingOutcome
from app.journal.materiality import MaterialityVerdict
from app.journal.projection import (
    project_journal_entry_posted,
    project_recategorization,
)
from app.tools._audit import project_decision_audit


class _Ctx:
    """Minimal stand-in for WorkspaceContext (only fields the hook reads)."""

    def __init__(self, agent_id=None, user_uc_uid="human-1"):
        self.agent_id = agent_id
        self.user_uc_uid = user_uc_uid


@pytest.fixture
def recorder(monkeypatch):
    """Replace falkordb.project with a recorder; close coros to avoid warnings."""
    calls: list[dict] = []

    def _rec(coro, *, label="projection"):
        calls.append({"label": label, "coro_name": getattr(coro, "__qualname__", str(coro))})
        # We are not executing the projection; close the coroutine so Python
        # doesn't warn "coroutine was never awaited".
        try:
            coro.close()
        except Exception:
            pass

    monkeypatch.setattr(falkordb, "project", _rec)
    return calls


WID = uuid4()
EID = uuid4()


def test_project_journal_entry_posted_schedules_node(recorder):
    outcome = PostingOutcome(
        journal_entry_id=uuid4(),
        materiality=MaterialityVerdict(outcome="auto_post", tier="L", reason="ok"),
    )
    project_journal_entry_posted(
        workspace_id=WID, entity_id=EID, outcome=outcome,
        category="SaaS · AI", amount_cents=999, effective_date=date(2025, 3, 31),
        source="simplefin", source_external_id="ext-1", payee="Anthropic",
    )
    # One projection: the JournalEntry node. No source hash -> no SUPPORTED_BY.
    assert len(recorder) == 1
    assert recorder[0]["label"].startswith("journal_entry:")


def test_project_journal_entry_posted_with_source_hash_adds_link(recorder):
    outcome = PostingOutcome(
        journal_entry_id=uuid4(),
        materiality=MaterialityVerdict(outcome="auto_post", tier="L", reason="ok"),
    )
    project_journal_entry_posted(
        workspace_id=WID, entity_id=EID, outcome=outcome,
        category="Meals", amount_cents=100, effective_date=date(2025, 1, 1),
        source="simplefin", source_external_id="ext-2", payee="x",
        source_document_hash="abc123",
    )
    labels = [c["label"] for c in recorder]
    assert any(l.startswith("journal_entry:") for l in labels)
    assert any(l.startswith("je_supported_by:") for l in labels)


def test_project_recategorization_schedules_reversal_and_restatement(recorder):
    outcome = CorrectionOutcome(
        reversal_entry_id=uuid4(),
        restated_entry_id=uuid4(),
        materiality_tier="L",
        from_category="Meals",
        to_category="Office · Supplies",
    )
    project_recategorization(
        workspace_id=WID, entity_id=EID, outcome=outcome,
        orig_entry_id=uuid4(), amount_cents=500,
        effective_date=date(2025, 2, 2), payee="p",
    )
    labels = [c["label"] for c in recorder]
    assert any(l.startswith("recat_reversal:") for l in labels)
    assert any(l.startswith("recat_restated:") for l in labels)
    assert len(recorder) == 2


def test_project_decision_audit_schedules_decision(recorder):
    project_decision_audit(
        workspace_id=WID, entity_id=EID, audit_id=uuid4(), ctx=_Ctx(agent_id="cat-agent"),
        tool_name="categorize", outcome="applied", reasoning="because",
        subject_id=uuid4(), overrides_subject_id=uuid4(),
    )
    assert len(recorder) == 1
    assert recorder[0]["label"].startswith("decision:categorize:")


def test_project_decision_audit_never_raises_without_falkordb(monkeypatch):
    # With the REAL project (no recorder) + no falkordb-py, the builder must be a
    # safe no-op rather than raising into the (already-committed) request path.
    monkeypatch.setattr(falkordb, "FalkorDB", None)
    project_decision_audit(
        workspace_id=WID, entity_id=EID, audit_id=uuid4(), ctx=_Ctx(),
        tool_name="link_receipt", outcome="linked", reasoning="r",
    )  # must not raise
