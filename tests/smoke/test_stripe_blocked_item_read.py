"""Offline tests for GET /api/workspaces/{wid}/stripe/blocked-item (read-only)."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api import stripe_blocked_item as sbi
from app.tenancy import WorkspaceContext

WID = uuid4()
EID = uuid4()
OTHER_EID = uuid4()
BT = "txn_blocked_fictional"
SINCE = datetime(2026, 11, 10, 9, 30, tzinfo=timezone.utc)


def _ctx(role: str = "viewer", **changes: Any) -> WorkspaceContext:
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


class _Result:
    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows

    def mappings(self) -> "_Result":
        return self

    def all(self) -> list[dict[str, Any]]:
        return self._rows

    def first(self) -> Any:
        return self._rows[0] if self._rows else None


def _state() -> dict[str, Any]:
    return {
        "blocked_item": {
            "id": BT,
            "amount": 1250,
            "currency": "usd",
            "error": "RuntimeError: fictional mapping failure",
        },
        "_stripe_failure_counts": {BT: 4, "txn_other": 1},
        "_stripe_continuation_cursor": "txn_cursor_secret_ish",
        "window_gte": 100,
        "window_lt": 200,
        "block_revision": 7,
        "resolved_blocked_items": {
            "txn_old_a": {
                "kind": "not_books_relevant",
                "resolved_by_uc_uid": "fixture-admin",
                "resolved_at": "2026-11-01T10:00:00+00:00",
                "journal_entry_id": None,
                "not_books_relevant_reason": "Fictional duplicate test charge",
                "blocked_item": {"id": "txn_old_a", "amount": 500, "currency": "usd",
                                 "error": "fictional"},
                "approval_id": "appr-a",
                "audit_id": "audit-a",
                "position": {"window_gte": 1, "window_lt": 2},
            },
            "txn_old_b": {
                "kind": "journal_entry",
                "resolved_by_uc_uid": "fixture-owner",
                "resolved_at": "2026-11-05T10:00:00+00:00",
                "journal_entry_id": "je-b",
                "not_books_relevant_reason": None,
                "blocked_item": {"id": "txn_old_b", "amount": 900, "currency": "eur"},
                "approval_id": "appr-b",
                "audit_id": "audit-b",
            },
        },
    }


class _Session:
    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = rows
        self.sql: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, query: Any, params: dict[str, Any] | None = None) -> _Result:
        sql = str(query)
        params = params or {}
        self.sql.append((sql, params))
        assert "FOR UPDATE" not in sql, "the read must not take the Stripe state lock"
        assert "UPDATE " not in sql and "INSERT " not in sql, "the read must write nothing"
        if "FROM connector_state" in sql:
            assert params["wid"] == str(WID)
            rows = self.rows
            if "entity_scope" in params:
                rows = [r for r in rows if r["entity_id"] in params["entity_scope"]]
            return _Result(rows)
        if "FROM approval_queue" in sql:
            assert params["stripe_id"] == BT
            return _Result([{"id": "appr-blocked", "status": "pending", "created_at": SINCE}])
        raise AssertionError(f"unexpected SQL: {sql}")


def _rows() -> list[dict[str, Any]]:
    return [
        {"entity_id": str(EID), "entity_display_name": "Fictional Co", "state": json.dumps(_state())},
        {"entity_id": str(OTHER_EID), "entity_display_name": "Other Co", "state": {}},
    ]


async def test_view_shows_the_blocked_item_and_resolutions() -> None:
    view = await sbi.load_blocked_item_view(_Session(_rows()), _ctx())
    assert view["min_reason_chars"] == 10
    assert view["max_reason_chars"] == sbi.MAX_REASON_CHARS
    assert view["blocked"] == [{
        "bt_id": BT,
        "entity_id": str(EID),
        "entity_display_name": "Fictional Co",
        "amount": 1250,
        "currency": "usd",
        "error": "RuntimeError: fictional mapping failure",
        "failure_count": 4,
        "since": SINCE.isoformat(),
        "approval_id": "appr-blocked",
        "approval_status": "pending",
    }]
    # Newest resolution first, with who / when / why and the audit link.
    assert [r["bt_id"] for r in view["resolved"]] == ["txn_old_b", "txn_old_a"]
    b, a = view["resolved"]
    assert b["kind"] == "journal_entry" and b["journal_entry_id"] == "je-b"
    assert b["resolved_by_uc_uid"] == "fixture-owner" and b["currency"] == "eur"
    assert a["not_books_relevant_reason"] == "Fictional duplicate test charge"
    assert a["audit_id"] == "audit-a" and a["amount"] == 500
    # Internal sync bookkeeping never leaves the server.
    blob = json.dumps(view)
    for internal in ("txn_cursor_secret_ish", "window_gte", "block_revision", "position"):
        assert internal not in blob


async def test_nothing_blocked_is_an_empty_view() -> None:
    rows = [{"entity_id": str(EID), "entity_display_name": None, "state": {}}]
    view = await sbi.load_blocked_item_view(_Session(rows), _ctx())
    assert view["blocked"] == [] and view["resolved"] == []


async def test_malformed_state_is_ignored() -> None:
    state = {
        "blocked_item": {"id": BT, "amount": True, "currency": 5},
        "_stripe_failure_counts": {BT: "x"},
        "resolved_blocked_items": {"": {}, "txn_bad": "not-a-dict"},
    }
    rows = [{"entity_id": str(EID), "entity_display_name": None, "state": state}]
    view = await sbi.load_blocked_item_view(_Session(rows), _ctx())
    assert view["blocked"][0]["amount"] is None
    assert view["blocked"][0]["currency"] is None
    assert view["blocked"][0]["failure_count"] == 0
    assert view["resolved"] == []


async def test_entity_scope_restricts_rows() -> None:
    session = _Session(_rows())
    view = await sbi.load_blocked_item_view(session, _ctx(entity_scope=(OTHER_EID,)))
    assert view["blocked"] == [] and view["resolved"] == []
    assert session.sql[0][1]["entity_scope"] == [str(OTHER_EID)]


async def test_route_reads_through_the_workspace_session(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _Session(_rows())
    seen: list[Any] = []

    @asynccontextmanager
    async def fake_workspace_session(_factory: Any, workspace_id: Any):
        seen.append(workspace_id)
        yield session

    monkeypatch.setattr(sbi, "workspace_session", fake_workspace_session)
    view = await sbi.get_stripe_blocked_item(WID, workspace=_ctx("viewer"))
    assert seen == [WID]
    assert view["blocked"][0]["bt_id"] == BT


async def test_route_has_the_status_read_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sbi, "workspace_session", None)
    with pytest.raises(HTTPException) as exc:
        await sbi.get_stripe_blocked_item(WID, workspace=_ctx("no_such_role"))
    assert exc.value.status_code == 403


def test_get_route_is_registered() -> None:
    from app.main import app
    from tests.route_introspection import methods_for

    assert methods_for(app, "/api/workspaces/{workspace_id}/stripe/blocked-item") == {"GET"}
