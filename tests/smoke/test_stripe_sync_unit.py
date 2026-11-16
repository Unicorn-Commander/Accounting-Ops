"""Offline tests for the Stripe sync worker's fixed-window cursor behavior."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from app.connectors.stripe.client import (
    StripeApiError,
    StripeClient,
    StripeCredentialBlob,
)
from scripts import stripe_sync
from tests.smoke.test_stripe_mapper_unit import _ACCOUNTS, _StubSession


class _Response:
    status_code = 200
    headers: dict[str, str] = {}

    def __init__(self, body: dict[str, Any]):
        self.body = body

    def json(self) -> dict[str, Any]:
        return self.body

    @property
    def text(self) -> str:
        return "ok"


class _StripeHttp:
    """Newest-first fake that honors both created filters and object cursors."""

    def __init__(self, transactions: list[dict[str, Any]]):
        self.transactions = transactions
        self.calls: list[dict[str, Any]] = []
        self.on_first_request: Any = None

    async def __aenter__(self) -> "_StripeHttp":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def request(self, method: str, url: str, **kwargs: Any) -> _Response:
        self.calls.append({"method": method, "url": url, **kwargs})
        params = kwargs.get("params", {})
        created_gte = int(params.get("created[gte]", 0))
        created_lt = params.get("created[lt]")
        filtered = [
            txn
            for txn in self.transactions
            if txn["created"] >= created_gte
            and (created_lt is None or txn["created"] < int(created_lt))
        ]
        starting_after = params.get("starting_after")
        if starting_after:
            ids = [txn["id"] for txn in filtered]
            if starting_after not in ids:
                raise StripeApiError(
                    "fictional invalid starting_after", status_code=400
                )
            start = ids.index(starting_after) + 1
        else:
            start = 0
        page = filtered[start : start + 100]
        if len(self.calls) == 1 and self.on_first_request:
            self.on_first_request()
        return _Response(
            {
                "data": page,
                "has_more": start + len(page) < len(filtered),
            }
        )


def _transactions(
    count: int,
    created: int,
    *,
    id_start: int = 0,
) -> list[dict[str, Any]]:
    return [
        {
            "id": f"txn_{index:05d}",
            "object": "balance_transaction",
            "type": "charge",
            "amount": 1000,
            "currency": "usd",
            "fee": 0,
            "net": 1000,
            "created": created,
            "available_on": created,
            "reporting_category": "charge",
        }
        for index in range(id_start, id_start + count)
    ]


class _IdempotentSession(_StubSession):
    """Mapper session double that behaves like journal_entry's unique key."""

    def __init__(self) -> None:
        super().__init__(accounts=_ACCOUNTS, existing_posts=31)
        self.external_ids: set[str] = set()

    async def execute(self, query: Any, params: dict[str, Any] | None = None):
        sql = str(query)
        params = params or {}
        if "FROM journal_entry" in sql and "source_external_id = :ext" in sql:
            ext = params.get("ext", "")
            if ext in self.external_ids:
                return SimpleNamespace(
                    first=lambda: SimpleNamespace(
                        id="00000000-0000-0000-0000-000000000001"
                    )
                )
        result = await super().execute(query, params)
        if "INSERT INTO journal_entry" in sql:
            external_id = params.get("source_external_id")
            if external_id:
                self.external_ids.add(external_id)
        return result


def _install_worker(
    monkeypatch: pytest.MonkeyPatch,
    transactions: list[dict[str, Any]],
    *,
    mapper: Any = None,
    position: tuple[datetime | None, str | None, int | None, int | None]
    | None = None,
):
    workspace_id = uuid4()
    entity_id = uuid4()
    http = _StripeHttp(transactions)
    client = StripeClient(
        blob=StripeCredentialBlob(
            workspace_id=workspace_id,
            credential_id=uuid4(),
            label="test",
            api_key="rk_test fictional",
            account_id="acct_test",
            environment="test",
            entity_id=str(entity_id),
        ),
        _http_factory=lambda: http,
    )
    session = _IdempotentSession()
    current: list[Any] = list(position or (None, None, None, None))
    records: list[dict[str, Any]] = []

    async def load_credential(*args: Any, **kwargs: Any) -> StripeCredentialBlob:
        return client.blob

    async def load_position(*args: Any, **kwargs: Any):
        return tuple(current)

    async def record_cursor(*args: Any, **kwargs: Any) -> None:
        current[:] = [
            kwargs["last_sync_at"],
            kwargs["continuation_cursor"],
            kwargs["window_gte"],
            kwargs["window_lt"],
        ]
        records.append(dict(kwargs["outcome_summary"]))

    @asynccontextmanager
    async def fake_workspace_session(*args: Any, **kwargs: Any):
        yield session

    async def fake_reconcile(*args: Any, **kwargs: Any) -> dict[str, int]:
        return {"checked_payouts": 0, "matched": 0, "unmatched": 0}

    monkeypatch.setattr(stripe_sync, "load_stripe_credential", load_credential)
    monkeypatch.setattr(stripe_sync, "_load_sync_position", load_position)
    monkeypatch.setattr(stripe_sync, "_record_sync_outcome", record_cursor)
    monkeypatch.setattr(stripe_sync, "workspace_session", fake_workspace_session)
    monkeypatch.setattr(stripe_sync, "reconcile_one_entity", fake_reconcile)
    monkeypatch.setattr(
        stripe_sync.StripeClient,
        "from_credential",
        classmethod(lambda cls, blob: client),
    )
    if mapper is not None:
        monkeypatch.setattr(stripe_sync, "map_and_post_balance_transaction", mapper)
    return workspace_id, http, session, current, records


@pytest.mark.asyncio
async def test_sync_records_the_worst_case_old_rows_after_the_50_page_cap(monkeypatch):
    """5,000 rows at two days plus 100 at ten days are all recorded."""
    two_days_ago = int((datetime.now(tz=timezone.utc) - timedelta(days=2)).timestamp())
    ten_days_ago = two_days_ago - 8 * 24 * 60 * 60
    transactions = _transactions(5000, two_days_ago) + _transactions(
        100, ten_days_ago, id_start=5000
    )
    workspace_id, http, session, position, _ = _install_worker(
        monkeypatch, transactions
    )

    await stripe_sync._sync_one_workspace(workspace_id)
    await stripe_sync._sync_one_workspace(workspace_id)

    assert len(session.posted_entries) == 5100
    assert len({entry["source_external_id"] for entry in session.posted_entries}) == 5100
    assert position[1] is None
    assert position[2] is None
    assert position[3] is None
    assert http.calls[0]["params"]["created[gte]"] < ten_days_ago
    assert http.calls[50]["params"]["created[lt]"] == two_days_ago + 1


@pytest.mark.asyncio
async def test_rows_arriving_mid_chain_are_recorded_by_the_next_chain(monkeypatch):
    created = int((datetime.now(tz=timezone.utc) - timedelta(days=2)).timestamp())
    new_created = created + 2 * 24 * 60 * 60
    old = _transactions(2, created)
    workspace_id, http, session, position, _ = _install_worker(monkeypatch, old)

    def add_new_row() -> None:
        http.transactions.append(_transactions(1, new_created, id_start=2)[0])

    http.on_first_request = add_new_row
    await stripe_sync._sync_one_workspace(workspace_id)
    assert len(session.posted_entries) == 2
    assert position[0] == datetime.fromtimestamp(created + 1, tz=timezone.utc)

    await stripe_sync._sync_one_workspace(workspace_id)
    assert len(session.posted_entries) == 3
    assert {entry["source_external_id"] for entry in session.posted_entries} == {
        "txn_00000",
        "txn_00001",
        "txn_00002",
    }


@pytest.mark.asyncio
async def test_chain_window_does_not_use_worker_clock_after_spanning_time(monkeypatch):
    first_created = int(
        (datetime.now(tz=timezone.utc) - timedelta(days=3)).timestamp()
    )
    later_created = first_created + 2 * 24 * 60 * 60
    transactions = _transactions(2, later_created) + _transactions(
        2, first_created, id_start=2
    )
    workspace_id, _, _, position, _ = _install_worker(monkeypatch, transactions)

    await stripe_sync._sync_one_workspace(workspace_id)

    assert position[0] == datetime.fromtimestamp(later_created + 1, tz=timezone.utc)


@pytest.mark.asyncio
async def test_deleted_continuation_restarts_the_same_pinned_window(monkeypatch):
    created = int((datetime.now(tz=timezone.utc) - timedelta(days=2)).timestamp())
    window_gte = created - 10
    window_lt = created + 1
    workspace_id, http, session, position, records = _install_worker(
        monkeypatch,
        _transactions(3, created),
        position=(
            datetime.fromtimestamp(window_gte, tz=timezone.utc),
            "txn_deleted",
            window_gte,
            window_lt,
        ),
    )

    summary = await stripe_sync._sync_one_workspace(workspace_id)

    assert summary["last_outcome"] == "ok"
    assert len(session.posted_entries) == 3
    assert http.calls[0]["params"]["starting_after"] == "txn_deleted"
    assert "starting_after" not in http.calls[1]["params"]
    assert position[1] is None
    assert records[-1]["last_outcome"] == "ok"


@pytest.mark.asyncio
async def test_reread_from_the_overlap_does_not_double_post(monkeypatch):
    created = int((datetime.now(tz=timezone.utc) - timedelta(days=2)).timestamp())
    workspace_id, _, session, _, _ = _install_worker(
        monkeypatch, _transactions(3, created)
    )

    await stripe_sync._sync_one_workspace(workspace_id)
    await stripe_sync._sync_one_workspace(workspace_id)

    assert len(session.posted_entries) == 3


@pytest.mark.asyncio
async def test_sync_does_not_advance_window_past_mapping_failure(monkeypatch):
    created = int((datetime.now(tz=timezone.utc) - timedelta(days=2)).timestamp())
    seen: list[str] = []

    async def failing_mapper(session: Any, *, txn: dict[str, Any], **kwargs: Any):
        seen.append(txn["id"])
        if txn["id"] == "txn_00002":
            raise RuntimeError("fictional mapper failure")
        return SimpleNamespace(outcome="posted", fee_journal_entry_id=None)

    workspace_id, _, _, position, records = _install_worker(
        monkeypatch, _transactions(4, created), mapper=failing_mapper
    )
    summary = await stripe_sync._sync_one_workspace(workspace_id)

    assert seen == ["txn_00000", "txn_00001", "txn_00002"]
    assert summary["last_outcome"] == "processing_error"
    assert position[1] == "txn_00001"
    assert position[0] is None
    assert position[2] is not None
    assert position[3] == created + 1
    assert records[-1]["errors"]


@pytest.mark.asyncio
async def test_sync_below_cap_uses_stripe_window_end_and_clears_continuation(monkeypatch):
    created = int((datetime.now(tz=timezone.utc) - timedelta(days=2)).timestamp())
    workspace_id, http, session, position, _ = _install_worker(
        monkeypatch, _transactions(3, created)
    )

    summary = await stripe_sync._sync_one_workspace(workspace_id)

    assert summary["last_outcome"] == "ok"
    assert len(http.calls) == 1
    assert len(session.posted_entries) == 3
    assert position[0] == datetime.fromtimestamp(created + 1, tz=timezone.utc)
    assert position[1] is None
    assert position[2] is None
    assert position[3] is None
