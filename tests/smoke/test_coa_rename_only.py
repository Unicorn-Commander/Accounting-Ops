"""PATCH /api/workspaces/{wid}/coa/{id}: a rename-only update (no type change)
must succeed and leave the stored normal_balance untouched.

Kit F84 (PR #98 lane): update_coa_account built the UPDATE params from
updates["normal_balance"] unconditionally. normal_balance is only added to
updates when body.account_type is not None, so a rename-only request (the
common case, and the case test_coa_entity_scope.py exercises) raised KeyError
and the endpoint 500'd. normal_balance is derived from account_type
(normal_balance_for in app/journal/accounts.py), so when the type is not part
of the update the stored balance must be carried forward unchanged.

Offline: the workspace session is a recording fake; the route runs for real.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import pytest

import app.api.journal as journal_api
from app.api.journal import CoaUpdateRequest, update_coa_account
from app.tenancy import WorkspaceContext

WID = uuid4()
EID = uuid4()
ACCT_ID = uuid4()

_EXISTING = {
    "id": str(ACCT_ID),
    "entity_id": str(EID),
    "account_number": "6100",
    "account_name": "SaaS · AI",
    "account_type": "expense",
    "is_active": True,
    "is_system": False,
}


class _FakeResult:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def mappings(self) -> "_FakeResult":
        return self

    def first(self) -> dict | None:
        return self._rows[0] if self._rows else None


class _FakeSession:
    """SELECTs return the canned COA row; records every UPDATE and its params."""

    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _FakeResult:
        sql = str(stmt)
        if sql.strip().upper().startswith("SELECT"):
            return _FakeResult([dict(_EXISTING)])
        if "UPDATE" in sql.upper():
            assert params is not None
            self.updates.append(params)
        return _FakeResult([])


def _session_ctx(session: _FakeSession) -> Any:
    @asynccontextmanager
    async def _fake_workspace_session(session_factory: Any, workspace_id: Any):
        yield session

    return _fake_workspace_session


def _workspace() -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=WID,
        workspace_slug="fixture",
        role="accountant",
        user_uc_uid="fixture-human",
        agent_id=None,
        entity_scope=None,
        hipaa_protected=False,
    )


async def test_rename_only_update_keeps_normal_balance(monkeypatch) -> None:
    """The reported case: account_type is None, account_name changes.

    Must NOT raise (previously KeyError -> 500), must issue the rename UPDATE,
    and the UPDATE must bind the account's stored normal_balance ('debit' for
    an expense account), not drop or re-derive it from the missing type.
    """
    session = _FakeSession()
    monkeypatch.setattr(
        journal_api, "workspace_session", _session_ctx(session)
    )

    result = await update_coa_account(
        workspace_id=WID,
        account_id=ACCT_ID,
        body=CoaUpdateRequest(account_name="SaaS · AI (renamed)"),
        workspace=_workspace(),
    )

    assert result["account_name"] == "SaaS · AI (renamed)"
    assert result["account_type"] == "expense"
    assert len(session.updates) == 1
    update = session.updates[0]
    assert update["name"] == "SaaS · AI (renamed)"
    # Balance is the expense account's stored value, unchanged by the rename.
    assert update["nb"] == "debit"
    # The type itself must be re-bound unchanged, not dropped.
    assert update["atype"] == "expense"
    assert update["aid"] == str(ACCT_ID)


async def test_type_change_recomputes_normal_balance(monkeypatch) -> None:
    """Control: a reclassification recomputes the balance from the NEW type,
    so the carried-forward path is only used when the type is absent."""
    session = _FakeSession()
    monkeypatch.setattr(journal_api, "workspace_session", _session_ctx(session))

    result = await update_coa_account(
        workspace_id=WID,
        account_id=ACCT_ID,
        body=CoaUpdateRequest(account_type="revenue"),
        workspace=_workspace(),
    )

    assert result["account_type"] == "revenue"
    assert len(session.updates) == 1
    assert session.updates[0]["atype"] == "revenue"
    assert session.updates[0]["nb"] == "credit"
