"""Offline tests for the Stripe sync worker's fixed-window cursor behavior."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.exc import DBAPIError

from app.connectors.stripe.client import (
    StripeApiError,
    StripeClient,
    StripeCredentialBlob,
)
from scripts import stripe_sync
from tests.smoke.test_stripe_mapper_unit import _ACCOUNTS, _StubSession

FIXED_NOW = datetime(2026, 11, 10, tzinfo=timezone.utc)


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
                    "fictional invalid starting_after",
                    status_code=400,
                    body='{"error":{"code":"resource_missing","param":"starting_after"}}',
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
    position: tuple[Any, ...]
    | None = None,
    reconcile: Any = None,
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
    initial = list(position or (None, None, None, None))
    while len(initial) < 5:
        initial.append({})
    if len(initial) < 6:
        initial.append(None)
    current: list[Any] = initial
    records: list[dict[str, Any]] = []

    async def load_credential(*args: Any, **kwargs: Any) -> StripeCredentialBlob:
        return client.blob

    async def load_position(*args: Any, **kwargs: Any):
        return tuple(current)

    async def record_cursor(*args: Any, **kwargs: Any) -> None:
        if kwargs.get("blocked_item") is not None:
            next_blocked = dict(kwargs["blocked_item"])
        elif kwargs.get("clear_blocked_item"):
            next_blocked = None
        else:
            next_blocked = current[5]
        current[:] = [
            kwargs["last_sync_at"],
            kwargs["continuation_cursor"],
            kwargs["window_gte"],
            kwargs["window_lt"],
            dict(kwargs["failure_counts"]),
            next_blocked,
        ]
        records.append(dict(kwargs["outcome_summary"]))

    @asynccontextmanager
    async def fake_workspace_session(*args: Any, **kwargs: Any):
        yield session

    async def fake_reconcile(*args: Any, **kwargs: Any) -> dict[str, int]:
        if reconcile is not None:
            return await reconcile(*args, **kwargs)
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
async def test_sync_recovers_transactions_after_the_50_page_cap(monkeypatch):
    """5,000 rows at two days plus 100 at ten days are all recorded."""
    two_days_ago = int((FIXED_NOW - timedelta(days=2)).timestamp())
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
    created = int((FIXED_NOW - timedelta(days=2)).timestamp())
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
        (FIXED_NOW - timedelta(days=3)).timestamp()
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
    created = int((FIXED_NOW - timedelta(days=2)).timestamp())
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
async def test_fixed_blocked_item_beyond_cap_reached_after_missing_cursor(monkeypatch):
    created = int((FIXED_NOW - timedelta(days=2)).timestamp())
    window_gte = created - 10
    window_lt = created + 1
    blocked_id = "txn_05001"
    blocked = {
        "id": blocked_id,
        "amount": 1000,
        "currency": "usd",
        "error": "RuntimeError: fictional failure",
    }
    workspace_id, _, session, position, _ = _install_worker(
        monkeypatch,
        _transactions(5002, created),
        position=(
            datetime.fromtimestamp(window_gte, tz=timezone.utc),
            "txn_00000",
            window_gte,
            window_lt,
            {blocked_id: 3},
            blocked,
        ),
    )

    first = await stripe_sync._sync_one_workspace(workspace_id)

    assert first["last_outcome"] == "blocked_item"
    assert not session.posted_entries
    assert position[1] == "txn_05000"
    assert position[2] == window_gte
    assert position[3] == window_lt

    second = await stripe_sync._sync_one_workspace(workspace_id)

    assert second["last_outcome"] == "ok"
    assert [entry["source_external_id"] for entry in session.posted_entries] == [
        blocked_id
    ]
    assert position[1] == blocked_id
    assert position[2] == window_gte
    assert position[3] == window_lt
    assert position[5] is None


@pytest.mark.asyncio
async def test_unposted_review_item_holds_cursor_until_journal_exists(monkeypatch):
    created = int((FIXED_NOW - timedelta(days=2)).timestamp())
    seen: list[str] = []

    async def review_mapper(session: Any, *, txn: dict[str, Any], **kwargs: Any):
        seen.append(txn["id"])
        return SimpleNamespace(
            outcome="queued_first_run",
            fee_journal_entry_id=None,
            reason="fictional human review",
        )

    workspace_id, _, session, position, records = _install_worker(
        monkeypatch,
        _transactions(3, created),
        mapper=review_mapper,
    )

    summary = await stripe_sync._sync_one_workspace(workspace_id)

    assert summary["last_outcome"] == "awaiting_review"
    assert summary["awaiting_review_item"] == {
        "id": "txn_00000",
        "amount": 1000,
        "currency": "usd",
    }
    assert seen == ["txn_00000"]
    assert not session.posted_entries
    assert position[0] is None
    assert position[1] is None
    assert position[2] is not None
    assert position[3] == created + 1
    assert records[-1]["last_outcome"] == "awaiting_review"


@pytest.mark.asyncio
async def test_persisted_block_survives_deleted_cursor_and_missing_item(monkeypatch):
    created = int((FIXED_NOW - timedelta(days=2)).timestamp())
    blocked = {
        "id": "txn_missing",
        "amount": 1000,
        "currency": "usd",
        "error": "RuntimeError: fictional failure",
    }
    workspace_id, _, session, position, records = _install_worker(
        monkeypatch,
        _transactions(2, created),
        position=(
            None,
            "txn_deleted_cursor",
            created - 10,
            created + 1,
            {"txn_missing": 3},
            blocked,
        ),
    )

    summary = await stripe_sync._sync_one_workspace(workspace_id)

    assert summary["last_outcome"] == "blocked_item"
    assert not session.posted_entries
    assert position[0] is None
    assert position[1] is None
    assert position[2] == created - 10
    assert position[3] == created + 1
    assert position[4] == {"txn_missing": 3}
    assert position[5] == blocked
    assert records[-1]["blocked_item"] == blocked


@pytest.mark.asyncio
async def test_unconfirmed_idempotent_result_stops_chain(monkeypatch):
    created = int((FIXED_NOW - timedelta(days=2)).timestamp())

    async def mapper(session: Any, *, txn: dict[str, Any], **kwargs: Any):
        return SimpleNamespace(
            outcome="blocked_idempotent",
            fee_journal_entry_id=None,
            reason="Concurrent post / duplicate",
        )

    workspace_id, _, session, position, records = _install_worker(
        monkeypatch,
        _transactions(2, created),
        mapper=mapper,
    )

    summary = await stripe_sync._sync_one_workspace(workspace_id)

    assert summary["last_outcome"] == "processing_error"
    assert not session.posted_entries
    assert position[1] is None
    assert position[4] == {"txn_00000": 1}
    assert records[-1]["errors"]


@pytest.mark.asyncio
async def test_reread_from_the_overlap_does_not_double_post(monkeypatch):
    created = int((FIXED_NOW - timedelta(days=2)).timestamp())
    workspace_id, _, session, _, _ = _install_worker(
        monkeypatch, _transactions(3, created)
    )

    await stripe_sync._sync_one_workspace(workspace_id)
    await stripe_sync._sync_one_workspace(workspace_id)

    assert len(session.posted_entries) == 3


@pytest.mark.asyncio
async def test_sync_does_not_advance_window_past_mapping_failure(monkeypatch):
    created = int((FIXED_NOW - timedelta(days=2)).timestamp())
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
async def test_mapper_refusal_stops_chain_without_advancing_past_item(monkeypatch):
    created = int((FIXED_NOW - timedelta(days=2)).timestamp())
    seen: list[str] = []

    async def mapper(session: Any, *, txn: dict[str, Any], **kwargs: Any):
        seen.append(txn["id"])
        if txn["id"] == "txn_00001":
            return SimpleNamespace(
                outcome="blocked_unknown_account",
                reason="missing fictional revenue account",
                fee_journal_entry_id=None,
            )
        return SimpleNamespace(outcome="posted", fee_journal_entry_id=None)

    workspace_id, _, _, position, records = _install_worker(
        monkeypatch, _transactions(3, created), mapper=mapper
    )

    summary = await stripe_sync._sync_one_workspace(workspace_id)

    assert seen == ["txn_00000", "txn_00001"]
    assert summary["last_outcome"] == "processing_error"
    assert position[1] == "txn_00000"
    assert position[0] is None
    assert position[0] != datetime.fromtimestamp(created + 1, tz=timezone.utc)
    assert position[4] == {"txn_00001": 1}
    assert records[-1]["errors"]


@pytest.mark.asyncio
async def test_zero_amount_mapper_outcome_advances_without_failure(monkeypatch):
    created = int((FIXED_NOW - timedelta(days=2)).timestamp())
    seen: list[str] = []

    async def mapper(session: Any, *, txn: dict[str, Any], **kwargs: Any):
        seen.append(txn["id"])
        if txn["id"] == "txn_00001":
            return SimpleNamespace(
                outcome="skipped_zero_amount",
                reason="Amount is zero",
                fee_journal_entry_id=None,
            )
        return SimpleNamespace(outcome="posted", fee_journal_entry_id=None)

    workspace_id, _, _, position, records = _install_worker(
        monkeypatch, _transactions(3, created), mapper=mapper
    )

    summary = await stripe_sync._sync_one_workspace(workspace_id)

    assert seen == ["txn_00000", "txn_00001", "txn_00002"]
    assert summary["last_outcome"] == "ok"
    assert summary["posted"] == 2
    assert summary["skipped"] == 1
    assert position[1] is None
    assert position[4] == {}
    assert not records[-1]["errors"]


@pytest.mark.asyncio
async def test_mapper_refusal_does_not_clear_existing_three_failure_count(monkeypatch):
    created = int((FIXED_NOW - timedelta(days=2)).timestamp())

    async def mapper(session: Any, *, txn: dict[str, Any], **kwargs: Any):
        if txn["id"] == "txn_00001":
            return SimpleNamespace(
                outcome="blocked_unknown_account",
                reason="missing fictional revenue account",
                fee_journal_entry_id=None,
            )
        return SimpleNamespace(outcome="posted", fee_journal_entry_id=None)

    workspace_id, _, session, position, _ = _install_worker(
        monkeypatch,
        _transactions(3, created),
        mapper=mapper,
        position=(None, None, None, None, {"txn_00001": 3}),
    )

    summary = await stripe_sync._sync_one_workspace(workspace_id)

    assert summary["last_outcome"] == "blocked_item"
    assert position[1] == "txn_00000"
    assert position[4] == {"txn_00001": 3}
    assert len(session.approvals) == 1
    assert "blocked_unknown_account" in session.approvals[0]["reasoning"]


@pytest.mark.asyncio
async def test_db_error_rolls_back_only_current_item_savepoint(monkeypatch):
    created = int((FIXED_NOW - timedelta(days=2)).timestamp())
    seen: list[str] = []
    real_mapper = stripe_sync.map_and_post_balance_transaction

    async def mapper(session: Any, *, txn: dict[str, Any], **kwargs: Any):
        seen.append(txn["id"])
        if txn["id"] == "txn_00002":
            raise DBAPIError("SELECT fictional", {}, RuntimeError("db failure"))
        return await real_mapper(session, txn=txn, **kwargs)

    workspace_id, _, session, position, records = _install_worker(
        monkeypatch, _transactions(4, created), mapper=mapper
    )

    summary = await stripe_sync._sync_one_workspace(workspace_id)

    assert seen == ["txn_00000", "txn_00001", "txn_00002"]
    assert [entry["source_external_id"] for entry in session.posted_entries] == [
        "txn_00000",
        "txn_00001",
    ]
    assert summary["last_outcome"] == "processing_error"
    assert position[1] == "txn_00001"
    assert position[4] == {"txn_00002": 1}
    assert records[-1]["errors"]


@pytest.mark.asyncio
async def test_sync_below_cap_uses_stripe_window_end_and_clears_continuation(monkeypatch):
    created = int((FIXED_NOW - timedelta(days=2)).timestamp())
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


@pytest.mark.asyncio
async def test_poison_item_blocks_on_third_pass_and_retries_before_advancing(monkeypatch):
    created = int((FIXED_NOW - timedelta(days=2)).timestamp())
    seen: list[str] = []
    attempts = 0

    async def mapper(session: Any, *, txn: dict[str, Any], **kwargs: Any):
        nonlocal attempts
        seen.append(txn["id"])
        if txn["id"] == "txn_00002" and attempts < 3:
            attempts += 1
            raise RuntimeError("fictional deterministic mapper failure")
        return SimpleNamespace(outcome="posted", fee_journal_entry_id=None)

    workspace_id, _, session, position, records = _install_worker(
        monkeypatch,
        _transactions(4, created),
        mapper=mapper,
    )

    first = await stripe_sync._sync_one_workspace(workspace_id)
    second = await stripe_sync._sync_one_workspace(workspace_id)
    third = await stripe_sync._sync_one_workspace(workspace_id)
    assert third["last_outcome"] == "blocked_item"
    assert third["blocked_item"] == {
        "id": "txn_00002",
        "amount": 1000,
        "currency": "usd",
        "error": "RuntimeError: fictional deterministic mapper failure",
    }
    assert position[1] == "txn_00001"
    assert position[4] == {"txn_00002": 3}
    assert len(session.approvals) == 1
    assert session.approvals[0]["reasoning"] == (
        "(blocked) Stripe txn_00002 1000: "
        "RuntimeError: fictional deterministic mapper failure"
    )

    fourth = await stripe_sync._sync_one_workspace(workspace_id)
    fifth = await stripe_sync._sync_one_workspace(workspace_id)

    assert first["last_outcome"] == "processing_error"
    assert second["last_outcome"] == "processing_error"
    assert fourth["last_outcome"] == "ok"
    assert fifth["last_outcome"] == "ok"
    assert seen.count("txn_00002") == 4
    assert {txn_id for txn_id in seen} == {
        "txn_00000",
        "txn_00001",
        "txn_00002",
        "txn_00003",
    }
    assert seen[-1] == "txn_00003"
    assert position[1] is None
    assert position[4] == {}
    assert len(session.approvals) == 1
    assert any(record.get("last_outcome") == "blocked_item" for record in records)


@pytest.mark.asyncio
async def test_non_missing_400_does_not_restart_continuation(monkeypatch):
    created = int((FIXED_NOW - timedelta(days=2)).timestamp())
    window_gte = created - 10
    window_lt = created + 1
    workspace_id, _, _, position, records = _install_worker(
        monkeypatch,
        _transactions(2, created),
        position=(
            datetime.fromtimestamp(window_gte, tz=timezone.utc),
            "txn_cursor",
            window_gte,
            window_lt,
        ),
    )
    calls = 0

    async def bad_request(*args: Any, **kwargs: Any):
        nonlocal calls
        calls += 1
        raise StripeApiError(
            "fictional invalid limit",
            status_code=400,
            body='{"error":{"code":"invalid_request_error","param":"limit"}}',
        )

    monkeypatch.setattr(stripe_sync, "paginate_balance_transactions", bad_request)

    summary = await stripe_sync._sync_one_workspace(workspace_id)

    assert summary["last_outcome"] == "api_error_balance_transactions"
    assert calls == 1
    assert position[1] == "txn_cursor"
    assert records[-1]["errors"]


@pytest.mark.asyncio
async def test_capped_progress_is_saved_before_reconcile_failure(monkeypatch):
    created = int((FIXED_NOW - timedelta(days=2)).timestamp())

    async def mapper(session: Any, *, txn: dict[str, Any], **kwargs: Any):
        return SimpleNamespace(outcome="posted", fee_journal_entry_id=None)

    workspace_id, _, _, position, records = _install_worker(
        monkeypatch,
        _transactions(5001, created),
        mapper=mapper,
    )
    observed: list[Any] = []

    async def failing_reconcile(*args: Any, **kwargs: Any):
        observed.append(tuple(position))
        raise RuntimeError("fictional reconcile failure")

    monkeypatch.setattr(stripe_sync, "reconcile_one_entity", failing_reconcile)

    summary = await stripe_sync._sync_one_workspace(workspace_id)

    assert summary["last_outcome"] == "ok"
    assert observed[0][1] == "txn_04999"
    assert observed[0][3] == created + 1
    assert position[1] == "txn_04999"
    assert position[3] == created + 1
    assert "reconcile: RuntimeError" in records[-1]["errors"][-1]
