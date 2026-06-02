"""End-to-end wiring test for the categorize MCP tool's projection (S2/S3).

Proves the REAL ``app.tools.categorization.categorize_transaction`` call site
fires the FalkorDB projection AFTER its entity_session commits — without a live
DB or graph. We mock:

  * authorize_workspace  -> a WorkspaceContext
  * entity_session       -> yields a fake session that returns the original
                            posted row and accepts write_decision_audit
  * post_correction      -> returns a CorrectionOutcome (applied)
  * write_decision_audit -> returns a fixed audit id
  * project_decision_audit / project_recategorization -> recorders

and assert BOTH projections are invoked with the right ids — the decision (with
OVERRIDES via subject) and the reversal+restatement entries. A second assertion
covers the queued (no ledger change) path: the decision is still projected but
NO recat-entry projection happens.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

import app.tools.categorization as catmod
from app.journal.posting import CorrectionOutcome, CorrectionQueued


class _Ctx:
    def __init__(self):
        self.user_uc_uid = "human-1"
        self.agent_id = "cat-agent"


_ORIGINAL_ROW = {
    "id": str(uuid4()),
    "amount": "5.00",
    "debit_account": "6520·Meals",
    "credit_account": "1010·Cash",
    "category": "Meals",
    "effective_date": "2025-03-31",
    "payee": "Chipotle",
    "description": "lunch",
    "memo": None,
    "status": "posted",
    "receipt_link_id": None,
}


class _FakeSession:
    """Returns the original posted row for the SELECT; swallows the audit INSERT."""

    async def execute(self, statement, params=None):
        class _R:
            def mappings(self_inner):
                return self_inner

            def first(self_inner):
                return dict(_ORIGINAL_ROW)

        return _R()


@pytest.fixture
def wired(monkeypatch):
    rec = {"decisions": [], "recats": []}

    async def _authorize(workspace_id, entity_id, min_role="accountant"):
        return _Ctx()

    @asynccontextmanager
    async def _entity_session(sessionmaker, workspace_id, entity_id):
        yield _FakeSession(), entity_id

    async def _write_audit(*a, **k):
        return uuid4()

    def _proj_decision(**k):
        rec["decisions"].append(k)

    def _proj_recat(**k):
        rec["recats"].append(k)

    monkeypatch.setattr(catmod, "authorize_workspace", _authorize)
    monkeypatch.setattr(catmod, "entity_session", _entity_session)
    monkeypatch.setattr(catmod, "write_decision_audit", _write_audit)
    monkeypatch.setattr(catmod, "project_decision_audit", _proj_decision)
    # project_recategorization is imported lazily inside the function from
    # app.journal.projection, so patch it at the source module.
    import app.journal.projection as projmod
    monkeypatch.setattr(projmod, "project_recategorization", _proj_recat)
    return rec


async def test_applied_recat_projects_decision_and_entries(wired, monkeypatch):
    tx_id = uuid4()

    async def _post_correction(session, **k):
        return CorrectionOutcome(
            reversal_entry_id=uuid4(),
            restated_entry_id=uuid4(),
            materiality_tier="L",
            from_category="Meals",
            to_category="Office · Supplies",
        )

    monkeypatch.setattr(catmod, "post_correction", _post_correction)

    out = await catmod.categorize_transaction(
        uuid4(), uuid4(), tx_id, "Office · Supplies", "reclassify"
    )
    assert out["outcome"] == "applied"
    # Decision projected with OVERRIDES via subject = the txn id.
    assert len(wired["decisions"]) == 1
    d = wired["decisions"][0]
    assert d["tool_name"] == "categorize"
    assert str(d["subject_id"]) == str(tx_id)
    assert str(d["overrides_subject_id"]) == str(tx_id)
    # Recat entries projected.
    assert len(wired["recats"]) == 1
    assert str(wired["recats"][0]["orig_entry_id"]) == str(tx_id)


async def test_queued_recat_projects_decision_but_no_entries(wired, monkeypatch):
    async def _post_correction(session, **k):
        return CorrectionQueued(
            approval_id=uuid4(), materiality_tier="H",
            reason="material", from_category="Meals", to_category="Travel · Lodging",
        )

    monkeypatch.setattr(catmod, "post_correction", _post_correction)

    out = await catmod.categorize_transaction(
        uuid4(), uuid4(), uuid4(), "Travel · Lodging", "reclassify"
    )
    assert out["outcome"] == "queued_for_approval"
    assert len(wired["decisions"]) == 1     # decision still projected
    assert len(wired["recats"]) == 0        # no ledger change -> no entry projection
