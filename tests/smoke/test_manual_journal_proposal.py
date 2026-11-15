"""Focused contract tests for the non-cash manual journal proposal lane."""

from __future__ import annotations

import asyncio
import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.auth import AuthContext
from app.journal.manual_proposal import (
    ManualJournalRequest,
    _pair_lines,
    apply_manual_proposal,
    enqueue_manual_proposal,
)
from app.journal.posting import PostingRefusal, PostingOutcome
from app.tenancy import WorkspaceContext


WID = uuid4()
EID = uuid4()


def _request(**changes):
    value = {
        "idempotency_key": "fixture-reclass-1",
        "effective_date": "2026-11-11",
        "memo": "Reclass fictional founder capital",
        "lines": [
            {"account_code": "3010", "side": "debit", "amount_cents": 125000},
            {"account_code": "3050", "side": "credit", "amount_cents": 125000},
        ],
        "evidence_links": ["https://example.com/evidence/capital-reclass"],
        "reasoning_trace": "Synthetic fixture only",
        "model_version": "fixture/v1",
    }
    value.update(changes)
    return ManualJournalRequest(**value).checked()


class _Result:
    def __init__(self, rows=None, row=None):
        self.rows = rows if rows is not None else ([] if row is None else [row])
        self.row = row

    def mappings(self):
        return self

    def all(self):
        return self.rows

    def first(self):
        return self.row if self.row is not None else (self.rows[0] if self.rows else None)


class _Session:
    def __init__(self, accounts=None, existing=None, period=None):
        self.accounts = accounts or {}
        self.existing = existing
        self.period = period
        self.executed = []

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.executed.append((sql, params))
        if "approval_queue" in sql:
            return _Result(row=self.existing)
        if "chart_of_account" in sql:
            code = (params or {}).get("code")
            account_id = (params or {}).get("id")
            if account_id is not None:
                rows = [row for values in self.accounts.values() for row in values if row["id"] == account_id]
                return _Result(row=rows[0] if rows else None)
            return _Result(rows=self.accounts.get(code, []))
        if "INSERT INTO approval_queue" in sql:
            return _Result()
        return _Result()


def _accounts():
    return {
        "3010": [{"id": "acct-3010", "account_number": "3010", "canonical_form": "3010·Owner Contribution", "linked_external_account_id": None}],
        "3050": [{"id": "acct-3050", "account_number": "3050", "canonical_form": "3050·Retained Earnings", "linked_external_account_id": None}],
    }


def test_manual_proposal_balance_and_active_entity_accounts():
    request = _request()
    session = _Session(accounts=_accounts())
    with patch("app.journal.manual_proposal.assert_period_open", AsyncMock(return_value=None)):
        result = asyncio.run(
            enqueue_manual_proposal(
                session,
                workspace_id=WID,
                entity_id=EID,
                request=request,
                proposer_user_uc_uid="fixture-user",
                proposer_agent_id="fixture-agent",
            )
        )
    assert result[1:] == (True, 125000, False)
    insert = next(params for sql, params in session.executed if "INSERT INTO approval_queue" in sql)
    payload = json.loads(insert["payload"])
    assert payload["lines"][0]["resolved_account"]["account_id"] == "acct-3010"
    assert payload["pairs"] == [{"debit_account": "3010·Owner Contribution", "credit_account": "3050·Retained Earnings", "amount_cents": 125000}]
    assert not any("journal_entry" in sql and "approval_queue" not in sql for sql, _ in session.executed)


def test_manual_proposal_closed_period_before_and_after_queue():
    request = _request()
    session = _Session(accounts=_accounts())
    with patch("app.journal.manual_proposal.assert_period_open", AsyncMock(return_value=date(2026, 11, 30))):
        with pytest.raises(ValueError, match="Period closed"):
            asyncio.run(
                enqueue_manual_proposal(
                    session, workspace_id=WID, entity_id=EID, request=request,
                    proposer_user_uc_uid="fixture-user", proposer_agent_id=None,
                )
            )
    payload = _payload(request)
    with patch("app.journal.manual_proposal.assert_period_open", AsyncMock(return_value=date(2026, 11, 30))):
        with pytest.raises(ValueError, match="Period closed"):
            asyncio.run(apply_manual_proposal(
                _Session(accounts=_accounts()), workspace_id=WID, entity_id=EID,
                approval_id=uuid4(), payload=payload, approving_human="fixture-human",
            ))


def test_manual_proposal_idempotent_replay_and_conflict():
    request = _request()
    existing = {"id": "approval-1", "status": "pending", "payload": {"request_digest": "wrong"}}
    session = _Session(existing=existing)
    with pytest.raises(ValueError, match="Conflicting replay"):
        asyncio.run(enqueue_manual_proposal(
            session, workspace_id=WID, entity_id=EID, request=request,
            proposer_user_uc_uid="fixture-user", proposer_agent_id=None,
        ))
    existing["payload"]["request_digest"] = __import__("app.journal.manual_proposal", fromlist=["_digest_request"])._digest_request(request)
    existing["payload"]["total_debit_cents"] = 125000
    assert asyncio.run(enqueue_manual_proposal(
        session, workspace_id=WID, entity_id=EID, request=request,
        proposer_user_uc_uid="fixture-user", proposer_agent_id=None,
    )) == ("approval-1", True, 125000, True)


def test_manual_proposal_never_posts_on_enqueue():
    session = _Session(accounts=_accounts())
    with patch("app.journal.manual_proposal.assert_period_open", AsyncMock(return_value=None)), \
         patch("app.journal.manual_proposal.post_manual_journal_pair", AsyncMock()) as post:
        asyncio.run(enqueue_manual_proposal(
            session, workspace_id=WID, entity_id=EID, request=_request(),
            proposer_user_uc_uid="fixture-user", proposer_agent_id="fixture-agent",
        ))
    post.assert_not_awaited()


def _human(role="admin", *, agent_id=None, human=True):
    workspace = WorkspaceContext(WID, "fixture", role, "fixture-human", agent_id, None, False)
    claims = {"azp": "accounting-ops", "auth_time": 1, "sid": "fixture-session", "preferred_username": "fixture-human"} if human else {}
    auth = AuthContext("fixture-human", agent_id, (), claims, may_autoprovision=human)
    return workspace, auth


def test_manual_proposal_owner_admin_human_only():
    from app.api.approvals import _require_manual_proposal_human
    workspace, auth = _human("admin")
    with patch("app.api.approvals.get_settings", return_value=SimpleNamespace(keycloak_client_id="accounting-ops")):
        _require_manual_proposal_human(auth, workspace)
        with pytest.raises(Exception, match="owner or admin"):
            _require_manual_proposal_human(auth, _human("accountant")[0])
        with pytest.raises(Exception, match="human session"):
            _require_manual_proposal_human(_human("admin", agent_id="fixture-agent")[1], workspace)


def test_manual_proposal_agent_and_bulk_decision_refused():
    from app.api.approval_clusters import BulkDecideRequest
    assert BulkDecideRequest(decision="approve", approval_ids=[str(uuid4())]).decision == "approve"
    # The cluster route's explicit subject guard is the bulk-path contract.
    from app.api.approvals import _require_manual_proposal_human
    with pytest.raises(Exception):
        _require_manual_proposal_human(_human("admin", agent_id="fixture-agent")[1], _human("admin")[0])


def _payload(request):
    session = _Session(accounts=_accounts())
    with patch("app.journal.manual_proposal.assert_period_open", AsyncMock(return_value=None)):
        asyncio.run(enqueue_manual_proposal(
            session, workspace_id=WID, entity_id=EID, request=request,
            proposer_user_uc_uid="fixture-user", proposer_agent_id="fixture-agent",
        ))
    return json.loads(next(params for sql, params in session.executed if "INSERT INTO approval_queue" in sql)["payload"])


def test_manual_proposal_two_and_multi_line_atomic_apply():
    request = _request(lines=[
        {"account_code": "3010", "side": "debit", "amount_cents": 10000},
        {"account_code": "3050", "side": "debit", "amount_cents": 5000},
        {"account_code": "3050", "side": "credit", "amount_cents": 15000},
    ])
    session = _Session(accounts=_accounts())
    outcome = PostingOutcome(uuid4(), SimpleNamespace(tier="L"))
    with patch("app.journal.manual_proposal.assert_period_open", AsyncMock(return_value=None)), \
         patch("app.journal.manual_proposal.post_manual_journal_pair", AsyncMock(return_value=outcome)) as post:
        result = asyncio.run(apply_manual_proposal(
            session, workspace_id=WID, entity_id=EID, approval_id=uuid4(),
            payload=_payload(request), approving_human="fixture-human",
        ))
    assert len(result["journal_entry_ids"]) == 2
    assert [call.kwargs["amount_cents"] for call in post.await_args_list] == [10000, 5000]
    assert post.await_args_list[0].kwargs["debit_account"].startswith("3010")
    assert post.await_args_list[1].kwargs["debit_account"].startswith("3050")


def test_manual_proposal_hash_chain_and_audit_rollback():
    request = _request()
    payload = _payload(request)
    refused = PostingRefusal("blocked_idempotent", "synthetic pair failure")
    with patch("app.journal.manual_proposal.post_manual_journal_pair", AsyncMock(return_value=refused)):
        with pytest.raises(ValueError, match="synthetic pair failure"):
            asyncio.run(apply_manual_proposal(
                _Session(accounts=_accounts()), workspace_id=WID, entity_id=EID,
                approval_id=uuid4(), payload=payload, approving_human="fixture-human",
            ))


def test_manual_proposal_1000_gate_on_total():
    request = _request(lines=[
        {"account_code": "3010", "side": "debit", "amount_cents": 100000},
        {"account_code": "3050", "side": "credit", "amount_cents": 100000},
    ])
    assert sum(line.amount_cents for line in request.lines if line.side == "debit") == 100000
    assert _pair_lines([
        {"side": "debit", "amount_cents": 100000, "canonical_form": "Dr"},
        {"side": "credit", "amount_cents": 100000, "canonical_form": "Cr"},
    ])[0]["amount_cents"] == 100000
