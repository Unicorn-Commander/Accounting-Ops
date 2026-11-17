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
        self.stripe_posts: set[str] = set()
        self.lock_timeouts: list[str] = []

    async def execute(self, query: Any, params: dict[str, Any] | None = None) -> _Result:
        sql = str(query)
        params = params or {}
        if sql.startswith("SET LOCAL lock_timeout"):
            self.lock_timeouts.append(sql)
            return _Result([])
        if "INSERT INTO journal_entry" in sql or "INSERT INTO posting_log" in sql:
            self.journal_inserts += 1
            return _Result([])
        if "FROM connector_state" in sql and "FOR UPDATE" in sql:
            if self.state is None:
                return _Result([])
            return _Result(
                [{"id": "cs-1", "entity_id": str(EID), "state": json.dumps(self.state)}]
            )
        if "FROM journal_entry" in sql and "source_external_id = :ext" in sql:
            return _Result([{"x": 1}] if params["ext"] in self.stripe_posts else [])
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
        if "UPDATE approval_queue" in sql and "RETURNING" in sql:
            # reopen: mark every not-yet-superseded rejection of the item
            assert params["stripe_id"] == BT
            if (
                self.approval["status"] == "rejected"
                and params["marker"] not in self.approval["payload"]
            ):
                self.approval["payload"].update(json.loads(params["payload"]))
                return _Result([{"id": self.approval["id"]}])
            return _Result([])
        if "UPDATE approval_queue" in sql:
            if "decided_by_uc_uid" in sql:
                self.approval["status"] = "rejected"
                self.approval["decided_by_uc_uid"] = params["uid"]
            self.approval["reasoning"] += " | " + params["note"]
            self.approval["payload"].update(json.loads(params["payload"]))
            return _Result([])
        if "UPDATE connector_state" in sql:
            # MERGE_STATE_SQL: drop keys, then jsonb || patch. Never a replace.
            assert "CAST(:state AS jsonb)" not in sql
            merged = {
                key: value
                for key, value in (self.state or {}).items()
                if key not in params["drop_keys"]
            }
            merged.update(json.loads(params["patch"]))
            self.state = merged
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
async def test_load_resolved_ids_locks_the_state_row_first() -> None:
    queries: list[str] = []

    class _One:
        async def execute(self, query: Any, params: Any = None) -> Any:
            queries.append(str(query))
            return SimpleNamespace(
                first=lambda: SimpleNamespace(id="row-1", state=json.dumps({br.RESOLVED_STATE_KEY: {BT: {}, "": {}}}))
            )

    assert await br.load_resolved_blocked_ids(_One(), EID) == frozenset({BT})
    assert len(queries) == 1 and "FOR UPDATE" in queries[0]


@pytest.mark.asyncio
async def test_resolve_refuses_an_item_the_sync_already_posted() -> None:
    session = _Session(state=_blocked_state())
    session.stripe_posts.add(BT)
    with pytest.raises(br.BlockedResolutionError) as exc:
        await _resolve(session)
    assert exc.value.status_code == 409
    assert session.state == _blocked_state()
    assert session.audits == []


@pytest.mark.asyncio
async def test_resolve_waits_briefly_then_409s_while_a_sync_holds_the_lock() -> None:
    class _LockNotAvailable(Exception):
        sqlstate = "55P03"

    class _Busy(_Session):
        async def execute(self, query: Any, params: dict[str, Any] | None = None) -> _Result:
            if "FOR UPDATE" in str(query) and "connector_state" in str(query):
                raise RuntimeError("wrapped") from _LockNotAvailable()
            return await super().execute(query, params)

    session = _Busy(state=_blocked_state())
    with pytest.raises(br.BlockedResolutionError) as exc:
        await _resolve(session)
    assert exc.value.status_code == 409
    assert "retry" in exc.value.detail
    assert session.lock_timeouts == [f"SET LOCAL lock_timeout = '{br.LOCK_TIMEOUT_MS}ms'"]


def test_merge_never_drops_resolutions() -> None:
    with pytest.raises(ValueError):
        br.merge_params("cs-1", {}, {br.RESOLVED_STATE_KEY})


async def _resolved_session(**changes: Any) -> _Session:
    session = _Session(state=_blocked_state(), **changes)
    await _resolve(session)
    session.audits.clear()
    return session


async def _reopen(session: _Session, **changes: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = dict(
        ctx=_ctx(role="owner", user_uc_uid="fixture-owner"),
        bt_id=BT,
        entity_id=None,
        reason="Fictional refund was reversed; book it normally",
        scope_check=lambda ctx, entity: None,
    )
    kwargs.update(changes)
    return await br.reopen_resolved_item(session, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [None, "  ", "too short"])
async def test_reopen_requires_a_reason(reason: str | None) -> None:
    session = await _resolved_session()
    before = json.loads(json.dumps(session.state))
    with pytest.raises(br.BlockedResolutionError) as exc:
        await _reopen(session, reason=reason)
    assert exc.value.status_code == 422
    assert session.state == before and session.audits == []


@pytest.mark.asyncio
async def test_reopen_of_an_unresolved_id_is_409() -> None:
    session = _Session(state=_blocked_state())
    with pytest.raises(br.BlockedResolutionError) as exc:
        await _reopen(session)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_reopen_refused_while_another_item_is_blocked() -> None:
    session = await _resolved_session()
    session.state["blocked_item"] = {"id": "txn_later_block", "amount": 5}
    with pytest.raises(br.BlockedResolutionError) as exc:
        await _reopen(session)
    assert exc.value.status_code == 409
    assert BT in session.state[br.RESOLVED_STATE_KEY]


@pytest.mark.asyncio
async def test_reopen_rearms_the_item_with_an_audit_trail() -> None:
    session = await _resolved_session()
    # The chain moved on after the resolution.
    session.state.update(
        {
            "_stripe_continuation_cursor": "txn_later",
            "window_gte": 300,
            "window_lt": 400,
            "awaiting_review_item": {"id": "txn_review"},
        }
    )
    response = await _reopen(session)

    assert response["posted"] is False and session.journal_inserts == 0
    assert "warning" not in response  # reason-kind resolution cites no entry
    state = session.state
    assert BT not in state[br.RESOLVED_STATE_KEY]
    assert state["blocked_item"]["id"] == BT
    assert state["blocked_item"]["amount"] == 1000
    assert state["blocked_item"]["reopened"]["reopened_by_uc_uid"] == "fixture-owner"
    # back in the window it was blocked in, read from that window's newest end
    assert (state["window_gte"], state["window_lt"]) == (100, 200)
    assert "_stripe_continuation_cursor" not in state
    assert "awaiting_review_item" not in state
    assert BT not in state.get("_stripe_failure_counts", {})  # fresh count
    history = state[br.REOPENED_STATE_KEY][BT]
    assert len(history) == 1 and history[0]["reason"].startswith("Fictional refund")
    # audited who / why
    assert len(session.audits) == 1
    audit = session.audits[0]
    assert audit["tool_name"] == "stripe_blocked_item_reopen"
    assert "fixture-owner" in audit["reasoning"] and "Fictional refund" in audit["reasoning"]
    # the decided (blocked) row keeps its history and gains the reopen note
    assert session.approval["status"] == "rejected"
    assert session.approval["payload"]["blocked_reopen"]["audit_id"] == audit["id"]
    assert "blocked_resolution" in session.approval["payload"]
    # the resolution's rejection no longer holds the item for the worker
    marker = session.approval["payload"][br.SUPERSEDED_BY_REOPEN_KEY]
    assert marker["audit_id"] == audit["id"]
    assert response["superseded_rejection_ids"] == [session.approval["id"]]
    # resolve bumped the revision once, reopen once more
    assert state[br.BLOCK_REVISION_KEY] == 2

    # reopened means blocked again: it can be resolved again, and not reopened twice
    with pytest.raises(br.BlockedResolutionError) as exc:
        await _reopen(session)
    assert exc.value.status_code == 409
    await _resolve(session)
    assert BT in session.state[br.RESOLVED_STATE_KEY]


@pytest.mark.asyncio
async def test_reopen_of_a_journal_entry_resolution_warns_about_the_manual_entry() -> None:
    je = str(uuid4())
    session = _Session(state=_blocked_state(), journal_ids={je})
    await _resolve(session, journal_entry_id=UUID(je), not_books_relevant_reason=None)
    response = await _reopen(session)
    assert je in response["warning"]


def test_reopen_route_is_registered() -> None:
    from app.main import app
    from tests.route_introspection import methods_for

    path = "/api/workspaces/{workspace_id}/stripe/blocked-item/reopen"
    assert methods_for(app, path) == {"POST"}


@pytest.mark.asyncio
async def test_sync_skips_resolved_item_and_advances(monkeypatch: pytest.MonkeyPatch) -> None:
    """After a resolution the next pass continues; a re-read never posts it."""
    created = int((FIXED_NOW - timedelta(days=2)).timestamp())
    seen: list[str] = []

    async def mapper(session: Any, *, txn: dict[str, Any], **kwargs: Any):
        seen.append(txn["id"])
        return SimpleNamespace(outcome="posted", fee_journal_entry_id=None)

    async def resolved(session: Any, entity_id: Any) -> br.LockedBlockState:
        return br.LockedBlockState(True, frozenset({"txn_00001"}), 0, None)

    workspace_id, _, _, position, _ = _install_worker(
        monkeypatch, _transactions(3, created), mapper=mapper
    )
    monkeypatch.setattr(stripe_sync, "lock_block_state", resolved)

    summary = await stripe_sync._sync_one_workspace(workspace_id)

    assert summary["last_outcome"] == "ok"
    assert seen == ["txn_00000", "txn_00002"]
    assert summary["skipped"] == 1
    assert position[1] is None  # chain exhausted and advanced
    assert position[5] is None


def _blocked_position(created: int) -> tuple[Any, ...]:
    return (
        None,
        "txn_00000",
        created - 10,
        created + 1,
        {"txn_00001": 3},
        {"id": "txn_00001", "amount": 1000, "currency": "usd", "error": "fictional"},
    )


@pytest.mark.asyncio
async def test_rejected_blocked_item_is_never_success_on_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed (review 79c78f14 #2): human_rejected never clears a block.

    Even when only the awaiting-review rejection check fires (so the result
    reaches the post-mapper outcome handling as human_rejected), a retried
    blocked item keeps the block, its failure count and the cursor.
    """
    created = int((FIXED_NOW - timedelta(days=2)).timestamp())
    seen: list[str] = []

    async def mapper(session: Any, *, txn: dict[str, Any], **kwargs: Any):
        seen.append(txn["id"])
        return SimpleNamespace(outcome="posted", fee_journal_entry_id=None)

    async def rejected(session: Any, *, awaiting_review_only: bool = False, **kw: Any) -> bool:
        return awaiting_review_only

    workspace_id, _, session, position, records = _install_worker(
        monkeypatch,
        _transactions(3, created),
        mapper=mapper,
        position=_blocked_position(created),
    )
    monkeypatch.setattr(stripe_sync, "_stripe_item_was_rejected", rejected)

    summary = await stripe_sync._sync_one_workspace(workspace_id)

    assert summary["last_outcome"] == "blocked_item"
    assert seen == []
    assert not session.posted_entries
    assert position[1] == "txn_00000"
    assert position[4] == {"txn_00001": 3}
    assert position[5]["id"] == "txn_00001"
    assert "rejected" in " ".join(records[-1]["errors"])


@pytest.mark.asyncio
async def test_block_changed_after_load_maps_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review 79c78f14 #1: decisions come from the state read under the lock."""
    created = int((FIXED_NOW - timedelta(days=2)).timestamp())
    seen: list[str] = []

    async def mapper(session: Any, *, txn: dict[str, Any], **kwargs: Any):
        seen.append(txn["id"])
        return SimpleNamespace(outcome="posted", fee_journal_entry_id=None)

    async def locked(session: Any, entity_id: Any) -> br.LockedBlockState:
        # A resolve then a reopen landed between the load and the lock.
        return br.LockedBlockState(True, frozenset(), 2, "txn_00001")

    workspace_id, _, session, position, records = _install_worker(
        monkeypatch,
        _transactions(3, created),
        mapper=mapper,
        position=_blocked_position(created),
    )
    monkeypatch.setattr(stripe_sync, "lock_block_state", locked)
    writes_before = len(records)

    summary = await stripe_sync._sync_one_workspace(workspace_id)

    assert summary["last_outcome"] == "superseded_by_resolution"
    assert seen == []
    assert not session.posted_entries
    assert len(records) == writes_before
    assert position[1] == "txn_00000"
    assert position[5]["id"] == "txn_00001"
