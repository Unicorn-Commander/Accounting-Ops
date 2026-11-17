"""Offline tests for the manual "resolved elsewhere" Stripe unblock."""
from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from app.api.stripe_blocked_item import (
    ResolveBlockedItemRequest,
    require_owner_or_admin_human,
)
from app.auth import AuthContext
from app.connectors.stripe import blocked_resolution as br
from app.tenancy import WorkspaceContext
from scripts import stripe_sync
from tests.smoke.test_stripe_sync_unit import FIXED_NOW, _install_worker, _transactions

WID = uuid4()
EID = uuid4()
BT = "txn_blocked_fictional"


def _ctx(role: str = "admin", **changes: Any) -> WorkspaceContext:
    values = dict(
        workspace_id=WID,
        workspace_slug="fixture",
        role=role,
        user_uc_uid="fixture-human",
        agent_id=None,
        entity_scope=None,
        hipaa_protected=False,
    )
    values.update(changes)
    return WorkspaceContext(**values)


def _auth(*, human: bool = True, agent_id: str | None = None) -> AuthContext:
    claims = (
        {
            "azp": "accounting-ops",
            "auth_time": 1,
            "sid": "fixture-session",
            "preferred_username": "fixture-human",
        }
        if human
        else {}
    )
    return AuthContext("fixture-human", agent_id, (), claims, may_autoprovision=human)


class _Result:
    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows

    def mappings(self) -> "_Result":
        return self

    def all(self) -> list[dict[str, Any]]:
        return self._rows

    def first(self) -> Any:
        return self._rows[0] if self._rows else None


class _Session:
    """Just enough of the books for resolve_blocked_item."""

    def __init__(self, *, state: dict[str, Any] | None, journal_ids: set[str] | None = None):
        self.state = state
        self.journal_ids = journal_ids or set()
        self.approval = {
            "id": str(uuid4()),
            "status": "pending",
            "reasoning": f"(blocked) Stripe {BT} 1000: RuntimeError: fictional",
            "payload": {"stripe_balance_txn_id": BT, "blocked": True},
        }
        self.audits: list[dict[str, Any]] = []
        self.journal_inserts = 0

    async def execute(self, query: Any, params: dict[str, Any] | None = None) -> _Result:
        sql = str(query)
        params = params or {}
        if "INSERT INTO journal_entry" in sql or "INSERT INTO posting_log" in sql:
            self.journal_inserts += 1
            return _Result([])
        if "FROM connector_state" in sql and "FOR UPDATE" in sql:
            if self.state is None:
                return _Result([])
            return _Result(
                [{"id": "cs-1", "entity_id": str(EID), "state": json.dumps(self.state)}]
            )
        if "FROM journal_entry" in sql:
            assert params["wid"] == str(WID)
            if params["jid"] in self.journal_ids:
                return _Result([{"id": params["jid"], "entity_id": str(EID)}])
            return _Result([])
        if "FROM approval_queue" in sql:
            assert params["stripe_id"] == BT
            if self.approval["status"] == "pending":
                return _Result([dict(self.approval)])
            return _Result([])
        if "INSERT INTO decision_audit" in sql:
            self.audits.append(dict(params))
            return _Result([])
        if "UPDATE approval_queue" in sql:
            self.approval["status"] = "rejected"
            self.approval["decided_by_uc_uid"] = params["uid"]
            self.approval["reasoning"] += " | " + params["note"]
            self.approval["payload"].update(json.loads(params["payload"]))
            return _Result([])
        if "UPDATE connector_state" in sql:
            self.state = json.loads(params["state"])
            return _Result([])
        raise AssertionError(f"unexpected SQL: {sql}")


def _blocked_state() -> dict[str, Any]:
    return {
        "last_outcome": "blocked_item",
        "blocked_item": {
            "id": BT,
            "amount": 1000,
            "currency": "usd",
            "error": "RuntimeError: fictional",
        },
        "_stripe_continuation_cursor": "txn_before",
        "_stripe_failure_counts": {BT: 3, "txn_other": 1},
        "window_gte": 100,
        "window_lt": 200,
    }


async def _resolve(session: _Session, **changes: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = dict(
        ctx=_ctx(),
        bt_id=BT,
        entity_id=None,
        journal_entry_id=None,
        not_books_relevant_reason="Fictional test charge refunded in the processor",
        scope_check=lambda ctx, entity: None,
    )
    kwargs.update(changes)
    return await br.resolve_blocked_item(session, **kwargs)


@pytest.mark.parametrize("role", ["viewer", "bookkeeper", "accountant", "cpa"])
def test_non_admin_roles_are_refused(role: str) -> None:
    with pytest.raises(HTTPException) as exc:
        require_owner_or_admin_human(_auth(), _ctx(role))
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "auth,ctx",
    [
        (_auth(agent_id="fixture-agent"), _ctx(agent_id="fixture-agent")),
        (_auth(), _ctx(pat_scopes=("write",))),
        (_auth(human=False), _ctx()),
    ],
)
def test_non_human_owner_admin_callers_are_refused(auth: AuthContext, ctx: WorkspaceContext) -> None:
    with pytest.raises(HTTPException) as exc:
        require_owner_or_admin_human(auth, ctx)
    assert exc.value.status_code == 403


@pytest.mark.parametrize("role", ["owner", "admin"])
def test_owner_and_admin_humans_pass(role: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.api.approvals.get_settings",
        lambda: SimpleNamespace(keycloak_client_id="accounting-ops"),
    )
    require_owner_or_admin_human(_auth(), _ctx(role))


def test_route_is_registered() -> None:
    from app.main import app
    from tests.route_introspection import methods_for

    path = "/api/workspaces/{workspace_id}/stripe/blocked-item/resolve"
    assert methods_for(app, path) == {"POST"}


def test_request_model_accepts_both_shapes() -> None:
    assert ResolveBlockedItemRequest(bt_id=BT, journal_entry_id=uuid4()).journal_entry_id
    assert ResolveBlockedItemRequest(bt_id=BT, not_books_relevant_reason="x" * 12)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"not_books_relevant_reason": None},
        {"not_books_relevant_reason": "   "},
        {"journal_entry_id": uuid4()},  # both given
        {"not_books_relevant_reason": "too short"},
    ],
)
async def test_exactly_one_justification_required(changes: dict[str, Any]) -> None:
    session = _Session(state=_blocked_state())
    with pytest.raises(br.BlockedResolutionError) as exc:
        await _resolve(session, **changes)
    assert exc.value.status_code == 422
    assert session.state == _blocked_state()
    assert session.audits == []


@pytest.mark.asyncio
async def test_wrong_bt_id_is_409() -> None:
    session = _Session(state=_blocked_state())
    with pytest.raises(br.BlockedResolutionError) as exc:
        await _resolve(session, bt_id="txn_not_blocked")
    assert exc.value.status_code == 409
    assert session.state == _blocked_state()


@pytest.mark.asyncio
async def test_nothing_blocked_is_409() -> None:
    session = _Session(state={"last_outcome": "ok"})
    with pytest.raises(br.BlockedResolutionError) as exc:
        await _resolve(session)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_journal_entry_outside_this_workspace_is_rejected() -> None:
    session = _Session(state=_blocked_state(), journal_ids=set())
    with pytest.raises(br.BlockedResolutionError) as exc:
        await _resolve(session, journal_entry_id=uuid4(), not_books_relevant_reason=None)
    assert exc.value.status_code == 422
    assert session.state == _blocked_state()
    assert session.audits == []


@pytest.mark.asyncio
async def test_resolve_with_journal_entry_clears_block_and_records_everything() -> None:
    je = str(uuid4())
    session = _Session(state=_blocked_state(), journal_ids={je})
    response = await _resolve(session, journal_entry_id=UUID(je), not_books_relevant_reason=None)

    assert response["posted"] is False
    assert session.journal_inserts == 0
    # audit trail: who, why, which journal entry
    assert len(session.audits) == 1
    audit = session.audits[0]
    assert audit["tool_name"] == "stripe_blocked_item_resolve"
    assert audit["outcome"] == "resolved_elsewhere"
    assert je in audit["reasoning"] and "fixture-human" in audit["reasoning"]
    assert json.loads(audit["request"])["journal_entry_id"] == je
    # the (blocked) approval row is decided with the note
    assert session.approval["status"] == "rejected"
    assert session.approval["decided_by_uc_uid"] == "fixture-human"
    note = session.approval["payload"]["blocked_resolution"]
    assert note["journal_entry_id"] == je
    assert note["resolved_by_uc_uid"] == "fixture-human"
    assert note["resolved_at"]
    assert note["audit_id"] == audit["id"]
    # state: block and its failure count cleared, window kept, cursor on item
    state = session.state
    assert "blocked_item" not in state
    assert state["_stripe_failure_counts"] == {"txn_other": 1}
    assert state["_stripe_continuation_cursor"] == BT
    assert (state["window_gte"], state["window_lt"]) == (100, 200)
    assert state["resolved_blocked_items"][BT]["journal_entry_id"] == je

    with pytest.raises(br.BlockedResolutionError) as exc:
        await _resolve(session, journal_entry_id=UUID(je), not_books_relevant_reason=None)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_resolve_with_reason_records_reason() -> None:
    session = _Session(state=_blocked_state())
    reason = "Fictional test charge refunded in the processor"
    response = await _resolve(session)
    assert response["resolution"]["kind"] == "not_books_relevant"
    assert session.approval["payload"]["blocked_resolution"]["not_books_relevant_reason"] == reason
    assert reason in session.audits[0]["reasoning"]
    assert "_stripe_failure_counts" in session.state  # txn_other still counted
    assert session.state["resolved_blocked_items"][BT]["not_books_relevant_reason"] == reason


@pytest.mark.asyncio
async def test_entity_scope_is_enforced() -> None:
    session = _Session(state=_blocked_state())

    def deny(ctx: WorkspaceContext, entity: UUID) -> None:
        raise HTTPException(status_code=403, detail="out of scope")

    with pytest.raises(HTTPException) as exc:
        await _resolve(session, scope_check=deny)
    assert exc.value.status_code == 403
    assert session.audits == []


@pytest.mark.asyncio
async def test_load_resolved_ids_reads_state_key() -> None:
    class _One:
        async def execute(self, query: Any, params: Any = None) -> Any:
            return SimpleNamespace(
                first=lambda: SimpleNamespace(resolved=json.dumps({BT: {}, "": {}}))
            )

    assert await br.load_resolved_blocked_ids(_One(), EID) == frozenset({BT})


@pytest.mark.asyncio
async def test_sync_skips_resolved_item_and_advances(monkeypatch: pytest.MonkeyPatch) -> None:
    """After a resolution the next pass continues; a re-read never posts it."""
    created = int((FIXED_NOW - timedelta(days=2)).timestamp())
    seen: list[str] = []

    async def mapper(session: Any, *, txn: dict[str, Any], **kwargs: Any):
        seen.append(txn["id"])
        return SimpleNamespace(outcome="posted", fee_journal_entry_id=None)

    async def resolved(session: Any, entity_id: Any) -> frozenset[str]:
        return frozenset({"txn_00001"})

    workspace_id, _, _, position, _ = _install_worker(
        monkeypatch, _transactions(3, created), mapper=mapper
    )
    monkeypatch.setattr(stripe_sync, "load_resolved_blocked_ids", resolved)

    summary = await stripe_sync._sync_one_workspace(workspace_id)

    assert summary["last_outcome"] == "ok"
    assert seen == ["txn_00000", "txn_00002"]
    assert summary["skipped"] == 1
    assert position[1] is None  # chain exhausted and advanced
    assert position[5] is None
