"""Offline repros for eBay pagination and durable sync positions."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.exc import DBAPIError

from app.connectors.ebay.client import EbayCredentialBlob, EbayFinancesClient
from scripts import ebay_sync

from tests.smoke.test_ebay_mapper_unit import _ACCOUNTS, _StubSession


RUN_END = datetime(2026, 10, 22, 0, 0, tzinfo=timezone.utc)
PAYOUT_DATE = "2026-10-20T00:00:00.000Z"


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


class _EbayHttp:
    """Dynamic eBay response fake that honors offset and request limit."""

    def __init__(self, payouts: list[dict[str, Any]], transactions: dict[str, list[dict[str, Any]]]):
        self.payouts = payouts
        self.transactions = transactions
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self) -> "_EbayHttp":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def request(self, method: str, url: str, **kwargs: Any) -> _Response:
        params = kwargs.get("params", {})
        self.calls.append({"method": method, "url": url, "params": dict(params)})
        offset = int(params.get("offset", 0))
        limit = int(params.get("limit", 200))
        if url.endswith("/payout"):
            rows = self.payouts[offset : offset + limit]
            return _Response({"payouts": rows, "total": len(self.payouts)})
        if url.endswith("/transaction"):
            payout_id = None
            for part in str(params.get("filter", "")).split(","):
                if part.startswith("payoutId:{"):
                    payout_id = part.removeprefix("payoutId:{").removesuffix("}")
            rows = self.transactions.get(payout_id or "", [])
            return _Response({"transactions": rows[offset : offset + limit], "total": len(rows)})
        raise AssertionError(f"unexpected URL: {url}")


def _blob(workspace_id, entity_id) -> EbayCredentialBlob:
    return EbayCredentialBlob(
        workspace_id=workspace_id,
        credential_id=uuid4(),
        label="test",
        client_id="fictional-app",
        client_secret="fictional-secret",
        refresh_token="fictional-refresh",
        environment="production",
        ru_name="fictional-ru",
        granted_scopes=["https://api.ebay.com/oauth/api_scope/sell.finances"],
        access_token="fictional-access",
        access_token_expires_at=4_000_000_000,
        entity_id=str(entity_id),
    )


def _payout(index: int) -> dict[str, Any]:
    return {
        "payoutId": f"PAY-{index:03d}",
        "amount": {"value": "10.00", "currency": "USD"},
        "payoutDate": PAYOUT_DATE,
        "payoutStatus": "SUCCEEDED",
    }


def _txn(index: int, payout_id: str) -> dict[str, Any]:
    return {
        "transactionId": f"TXN-{payout_id}-{index:03d}",
        "transactionType": "SALE",
        "amount": {"value": "1.00", "currency": "USD"},
        "transactionDate": PAYOUT_DATE,
        "orderId": f"ORDER-{index:03d}",
    }


def _install_sync(
    monkeypatch: pytest.MonkeyPatch,
    *,
    client: EbayFinancesClient,
    session: Any,
    position: dict[str, Any],
    mapper: Any,
    fail_final_once: bool = False,
):
    workspace_id = client.blob.workspace_id
    entity_id = uuid4()
    client.blob.entity_id = str(entity_id)
    records: list[dict[str, Any]] = []

    async def load_credential(*args: Any, **kwargs: Any):
        return client.blob

    async def load_position(*args: Any, **kwargs: Any):
        return position["last_sync_at"], {
            "cursor": position["continuation_cursor"],
            "failure_counts": position.get("failure_counts", {}),
            "blocked_item": position.get("blocked_item"),
        }

    async def entity_for_credential(*args: Any, **kwargs: Any):
        return entity_id

    async def record_outcome(*args: Any, **kwargs: Any):
        if (
            fail_final_once
            and "finished_at" in kwargs["outcome_summary"]
            and not position.get("_crashed")
        ):
            position["_crashed"] = True
            raise RuntimeError("fictional crash between page and state write")
        position["last_sync_at"] = kwargs["last_sync_at"]
        position["continuation_cursor"] = kwargs["continuation_cursor"]
        position["failure_counts"] = dict(kwargs.get("failure_counts") or {})
        if kwargs.get("blocked_item") is not None:
            position["blocked_item"] = kwargs["blocked_item"]
        elif kwargs.get("clear_blocked_item"):
            position["blocked_item"] = None
        records.append(dict(kwargs["outcome_summary"]))

    @asynccontextmanager
    async def fake_workspace_session(*args: Any, **kwargs: Any):
        yield session

    async def fake_reconcile(*args: Any, **kwargs: Any) -> dict[str, int]:
        return {"checked_payouts": 0, "matched": 0, "unmatched": 0}

    monkeypatch.setattr(ebay_sync, "load_ebay_credential", load_credential)
    monkeypatch.setattr(ebay_sync, "_entity_for_credential", entity_for_credential)
    monkeypatch.setattr(ebay_sync, "_load_sync_position", load_position)
    monkeypatch.setattr(ebay_sync, "_record_sync_outcome", record_outcome)
    monkeypatch.setattr(ebay_sync, "workspace_session", fake_workspace_session)
    monkeypatch.setattr(ebay_sync, "reconcile_one_entity", fake_reconcile)
    monkeypatch.setattr(ebay_sync, "map_and_post_transaction", mapper)
    monkeypatch.setattr(
        ebay_sync.EbayFinancesClient,
        "from_credential",
        classmethod(lambda cls, blob: client),
    )
    monkeypatch.setattr(ebay_sync, "_utcnow", lambda: RUN_END)
    return workspace_id, entity_id, records


@pytest.mark.asyncio
async def test_ebay_paginators_report_offset_continuation_without_skipping_rows():
    workspace_id = uuid4()
    entity_id = uuid4()
    payouts = [_payout(index) for index in range(201)]
    transactions = [_txn(index, "PAY-000") for index in range(501)]
    http = _EbayHttp(payouts, {"PAY-000": transactions})
    client = EbayFinancesClient(blob=_blob(workspace_id, entity_id), _http_factory=lambda: http)

    payout_page = await client.list_payouts(limit=200)
    transaction_page = await client.list_transactions(payout_id="PAY-000", limit=500)

    assert len(payout_page) == 200
    assert payout_page.exhausted is False
    assert payout_page.continuation_offset == 200
    assert len(transaction_page) == 500
    assert transaction_page.exhausted is False
    assert transaction_page.continuation_offset == 500
    assert transaction_page.items[-1]["transactionId"] == "TXN-PAY-000-499"


@pytest.mark.asyncio
async def test_payout_transaction_pull_ignores_payout_date_window():
    """A late-settling sale linked to a payout must survive the date window."""
    workspace_id = uuid4()
    entity_id = uuid4()
    late_txn = _txn(0, "PAY-000")
    late_txn["transactionDate"] = "2026-11-05T00:00:00.000Z"
    http = _EbayHttp([_payout(0)], {"PAY-000": [late_txn]})
    client = EbayFinancesClient(
        blob=_blob(workspace_id, entity_id), _http_factory=lambda: http
    )

    page = await client.list_transactions(
        payout_id="PAY-000",
        start_date=datetime(2026, 11, 9, tzinfo=timezone.utc),
        end_date=datetime(2026, 11, 13, tzinfo=timezone.utc),
        limit=500,
    )

    assert [row["transactionId"] for row in page] == [late_txn["transactionId"]]
    transaction_call = next(
        call for call in http.calls if call["url"].endswith("/transaction")
    )
    assert transaction_call["params"]["filter"] == "payoutId:{PAY-000}"


@pytest.mark.asyncio
async def test_sync_recovers_above_cap_payouts_and_transactions(monkeypatch):
    """Two capped passes post every row once and finish both continuations."""
    workspace_id = uuid4()
    entity_id = uuid4()
    payouts = [_payout(index) for index in range(201)]
    transactions = {"PAY-000": [_txn(index, "PAY-000") for index in range(501)]}
    http = _EbayHttp(payouts, transactions)
    client = EbayFinancesClient(blob=_blob(workspace_id, entity_id), _http_factory=lambda: http)
    session = _StubSession(accounts=_ACCOUNTS, existing_posts=31)
    position: dict[str, Any] = {"last_sync_at": None, "continuation_cursor": None}
    records: list[dict[str, Any]] = []

    async def load_credential(*args: Any, **kwargs: Any) -> EbayCredentialBlob:
        return client.blob

    async def load_position(*args: Any, **kwargs: Any):
        return position["last_sync_at"], position["continuation_cursor"]

    async def record_outcome(*args: Any, **kwargs: Any) -> None:
        position["last_sync_at"] = kwargs["last_sync_at"]
        position["continuation_cursor"] = kwargs["continuation_cursor"]
        records.append(dict(kwargs["outcome_summary"]))

    @asynccontextmanager
    async def fake_workspace_session(*args: Any, **kwargs: Any):
        yield session

    async def fake_reconcile(*args: Any, **kwargs: Any) -> dict[str, int]:
        return {"checked_payouts": 0, "matched": 0, "unmatched": 0}

    async def fake_entity(*args: Any, **kwargs: Any):
        return entity_id

    monkeypatch.setattr(ebay_sync, "load_ebay_credential", load_credential)
    monkeypatch.setattr(ebay_sync, "_entity_for_credential", fake_entity)
    monkeypatch.setattr(ebay_sync, "_load_sync_position", load_position)
    monkeypatch.setattr(ebay_sync, "_record_sync_outcome", record_outcome)
    monkeypatch.setattr(ebay_sync, "workspace_session", fake_workspace_session)
    monkeypatch.setattr(ebay_sync, "reconcile_one_entity", fake_reconcile)
    monkeypatch.setattr(ebay_sync.EbayFinancesClient, "from_credential", classmethod(lambda cls, blob: client))
    monkeypatch.setattr(ebay_sync, "_utcnow", lambda: RUN_END)

    await ebay_sync._sync_one_workspace(workspace_id)
    await ebay_sync._sync_one_workspace(workspace_id)

    external_ids = [entry["source_external_id"] for entry in session.posted_entries]
    expected = {f"PAY-{index:03d}" for index in range(201)} | {
        f"TXN-PAY-000-{index:03d}" for index in range(501)
    }
    assert set(external_ids) == expected
    assert len(external_ids) == len(expected)
    assert position["continuation_cursor"] is None
    assert position["last_sync_at"] == datetime(
        2026, 10, 20, 0, 0, 1, tzinfo=timezone.utc
    )
    assert records[-1]["last_outcome"] == "ok"


@pytest.mark.asyncio
async def test_sync_stops_at_failed_mapping_and_keeps_last_good_position(monkeypatch):
    workspace_id = uuid4()
    entity_id = uuid4()
    payouts = [_payout(index) for index in range(2)]
    http = _EbayHttp(payouts, {})
    client = EbayFinancesClient(blob=_blob(workspace_id, entity_id), _http_factory=lambda: http)
    session = _StubSession(accounts=_ACCOUNTS, existing_posts=31)
    position: dict[str, Any] = {"last_sync_at": None, "continuation_cursor": None}

    async def load_credential(*args: Any, **kwargs: Any) -> EbayCredentialBlob:
        return client.blob

    async def load_position(*args: Any, **kwargs: Any):
        return position["last_sync_at"], position["continuation_cursor"]

    async def record_outcome(*args: Any, **kwargs: Any) -> None:
        position["last_sync_at"] = kwargs["last_sync_at"]
        position["continuation_cursor"] = kwargs["continuation_cursor"]

    @asynccontextmanager
    async def fake_workspace_session(*args: Any, **kwargs: Any):
        yield session

    seen: list[str] = []

    async def failing_mapper(session: Any, *, txn: dict[str, Any], **kwargs: Any):
        seen.append(txn["transactionId"])
        if txn["transactionId"] == "PAY-001":
            raise RuntimeError("fictional mapper failure")
        return SimpleNamespace(outcome="posted", fee_journal_entry_id=None)

    async def fake_entity(*args: Any, **kwargs: Any):
        return entity_id

    async def fake_reconcile(*args: Any, **kwargs: Any) -> dict[str, int]:
        return {"checked_payouts": 0, "matched": 0, "unmatched": 0}

    monkeypatch.setattr(ebay_sync, "load_ebay_credential", load_credential)
    monkeypatch.setattr(ebay_sync, "_entity_for_credential", fake_entity)
    monkeypatch.setattr(ebay_sync, "_load_sync_position", load_position)
    monkeypatch.setattr(ebay_sync, "_record_sync_outcome", record_outcome)
    monkeypatch.setattr(ebay_sync, "workspace_session", fake_workspace_session)
    monkeypatch.setattr(ebay_sync, "reconcile_one_entity", fake_reconcile)
    monkeypatch.setattr(ebay_sync, "map_and_post_transaction", failing_mapper)
    monkeypatch.setattr(ebay_sync.EbayFinancesClient, "from_credential", classmethod(lambda cls, blob: client))
    monkeypatch.setattr(ebay_sync, "_utcnow", lambda: RUN_END)

    summary = await ebay_sync._sync_one_workspace(workspace_id)

    assert seen == ["PAY-000", "PAY-001"]
    assert summary["last_outcome"] == "processing_error"
    assert position["last_sync_at"] is None
    assert position["continuation_cursor"]["payouts"]["offset"] == 1


@pytest.mark.asyncio
async def test_sync_below_cap_advances_time_cursor_and_clears_continuation(monkeypatch):
    workspace_id = uuid4()
    entity_id = uuid4()
    payouts = [_payout(0)]
    http = _EbayHttp(payouts, {})
    client = EbayFinancesClient(blob=_blob(workspace_id, entity_id), _http_factory=lambda: http)
    session = _StubSession(accounts=_ACCOUNTS, existing_posts=31)
    position: dict[str, Any] = {"last_sync_at": None, "continuation_cursor": None}

    async def load_credential(*args: Any, **kwargs: Any):
        return client.blob

    async def load_position(*args: Any, **kwargs: Any):
        return position["last_sync_at"], position["continuation_cursor"]

    async def record_outcome(*args: Any, **kwargs: Any):
        position["last_sync_at"] = kwargs["last_sync_at"]
        position["continuation_cursor"] = kwargs["continuation_cursor"]

    @asynccontextmanager
    async def fake_workspace_session(*args: Any, **kwargs: Any):
        yield session

    async def fake_entity(*args: Any, **kwargs: Any):
        return entity_id

    async def fake_reconcile(*args: Any, **kwargs: Any) -> dict[str, int]:
        return {"checked_payouts": 0, "matched": 0, "unmatched": 0}

    monkeypatch.setattr(ebay_sync, "load_ebay_credential", load_credential)
    monkeypatch.setattr(ebay_sync, "_entity_for_credential", fake_entity)
    monkeypatch.setattr(ebay_sync, "_load_sync_position", load_position)
    monkeypatch.setattr(ebay_sync, "_record_sync_outcome", record_outcome)
    monkeypatch.setattr(ebay_sync, "workspace_session", fake_workspace_session)
    monkeypatch.setattr(ebay_sync, "reconcile_one_entity", fake_reconcile)
    monkeypatch.setattr(ebay_sync.EbayFinancesClient, "from_credential", classmethod(lambda cls, blob: client))
    monkeypatch.setattr(ebay_sync, "_utcnow", lambda: RUN_END)

    summary = await ebay_sync._sync_one_workspace(workspace_id)

    assert summary["last_outcome"] == "ok"
    assert position["last_sync_at"] == datetime(
        2026, 10, 20, 0, 0, 1, tzinfo=timezone.utc
    )
    assert position["continuation_cursor"] is None


@pytest.mark.asyncio
async def test_stale_offset_restarts_the_same_pinned_window(monkeypatch):
    workspace_id = uuid4()
    entity_id = uuid4()
    payouts = [_payout(0), _payout(1)]
    http = _EbayHttp(payouts, {})
    client = EbayFinancesClient(blob=_blob(workspace_id, entity_id), _http_factory=lambda: http)
    session = _StubSession(accounts=_ACCOUNTS, existing_posts=31)
    start = datetime(2026, 10, 19, tzinfo=timezone.utc)
    end = datetime(2026, 10, 21, tzinfo=timezone.utc)
    position = {
        "last_sync_at": start,
        "continuation_cursor": {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "payouts": {"offset": 99},
            "transactions": {},
        },
    }

    async def mapper(*args: Any, **kwargs: Any):
        return SimpleNamespace(outcome="posted", fee_journal_entry_id=None)

    _install_sync(
        monkeypatch,
        client=client,
        session=session,
        position=position,
        mapper=mapper,
    )
    summary = await ebay_sync._sync_one_workspace(workspace_id)

    assert summary["last_outcome"] == "ok"
    payout_calls = [call for call in http.calls if call["url"].endswith("/payout")]
    assert payout_calls[0]["params"]["offset"] == 99
    assert payout_calls[1]["params"]["offset"] == 0
    assert position["continuation_cursor"] is None


@pytest.mark.asyncio
async def test_rows_arriving_mid_chain_wait_for_the_next_chain(monkeypatch):
    workspace_id = uuid4()
    entity_id = uuid4()
    initial = [_payout(0), _payout(1)]

    class _LatePayoutHttp(_EbayHttp):
        def __init__(self):
            super().__init__(initial, {})
            self.first = True

        async def request(self, method: str, url: str, **kwargs: Any) -> _Response:
            response = await super().request(method, url, **kwargs)
            if self.first and url.endswith("/payout") and kwargs.get("params", {}).get("offset", 0) == 0:
                self.first = False
                self.payouts.append(_payout(99))
            return response

    http = _LatePayoutHttp()
    client = EbayFinancesClient(blob=_blob(workspace_id, entity_id), _http_factory=lambda: http)
    session = _StubSession(accounts=_ACCOUNTS, existing_posts=31)
    seen: set[str] = set()

    async def mapper(*args: Any, **kwargs: Any):
        txn = kwargs["txn"]
        external_id = txn.get("transactionId") or txn.get("payoutId")
        if external_id in seen:
            session.idempotent_hit = True
            return SimpleNamespace(outcome="blocked_idempotent", reason="duplicate")
        seen.add(external_id)
        return SimpleNamespace(outcome="posted", fee_journal_entry_id=None)

    position = {"last_sync_at": None, "continuation_cursor": None}
    _install_sync(
        monkeypatch,
        client=client,
        session=session,
        position=position,
        mapper=mapper,
    )
    await ebay_sync._sync_one_workspace(workspace_id)
    assert "PAY-099" not in seen
    await ebay_sync._sync_one_workspace(workspace_id)
    assert "PAY-099" in seen


@pytest.mark.asyncio
async def test_refusal_blocks_on_third_attempt_and_keeps_cursor_before_item(monkeypatch):
    workspace_id = uuid4()
    entity_id = uuid4()
    payouts = [_payout(0), _payout(1), _payout(2)]
    http = _EbayHttp(payouts, {})
    client = EbayFinancesClient(blob=_blob(workspace_id, entity_id), _http_factory=lambda: http)
    session = _StubSession(accounts=_ACCOUNTS, existing_posts=31)
    position = {"last_sync_at": None, "continuation_cursor": None}
    seen: list[str] = []

    async def mapper(*args: Any, **kwargs: Any):
        external_id = kwargs["txn"].get("transactionId")
        seen.append(external_id)
        if external_id == "PAY-001":
            return SimpleNamespace(
                outcome="blocked_unknown_account",
                reason="fictional missing account",
                fee_journal_entry_id=None,
            )
        return SimpleNamespace(outcome="posted", fee_journal_entry_id=None)

    _install_sync(
        monkeypatch,
        client=client,
        session=session,
        position=position,
        mapper=mapper,
    )
    first = await ebay_sync._sync_one_workspace(workspace_id)
    second = await ebay_sync._sync_one_workspace(workspace_id)
    third = await ebay_sync._sync_one_workspace(workspace_id)

    assert first["last_outcome"] == "processing_error"
    assert second["last_outcome"] == "processing_error"
    assert third["last_outcome"] == "blocked_item"
    assert position["continuation_cursor"]["payouts"]["offset"] == 1
    assert position["failure_counts"] == {"PAY-001": 3}
    assert len(session.approvals) == 1
    assert "PAY-002" not in seen


@pytest.mark.asyncio
async def test_db_error_savepoint_preserves_prior_payout(monkeypatch):
    workspace_id = uuid4()
    entity_id = uuid4()
    http = _EbayHttp([_payout(0), _payout(1)], {})
    client = EbayFinancesClient(blob=_blob(workspace_id, entity_id), _http_factory=lambda: http)
    session = _StubSession(accounts=_ACCOUNTS, existing_posts=31)
    position = {"last_sync_at": None, "continuation_cursor": None}
    real_mapper = ebay_sync.map_and_post_transaction

    async def mapper(session: Any, *, txn: dict[str, Any], **kwargs: Any):
        if txn.get("transactionId") == "PAY-001":
            raise DBAPIError("SELECT fictional", {}, RuntimeError("db failure"))
        return await real_mapper(session, txn=txn, **kwargs)

    _install_sync(
        monkeypatch,
        client=client,
        session=session,
        position=position,
        mapper=mapper,
    )
    summary = await ebay_sync._sync_one_workspace(workspace_id)

    assert summary["last_outcome"] == "processing_error"
    assert [entry["source_external_id"] for entry in session.posted_entries] == ["PAY-000"]
    assert position["continuation_cursor"]["payouts"]["offset"] == 1
    assert position["failure_counts"] == {"PAY-001": 1}


@pytest.mark.asyncio
async def test_crash_before_final_state_write_replays_without_double_post(monkeypatch):
    workspace_id = uuid4()
    entity_id = uuid4()
    http = _EbayHttp([_payout(0)], {})
    client = EbayFinancesClient(blob=_blob(workspace_id, entity_id), _http_factory=lambda: http)
    session = _StubSession(accounts=_ACCOUNTS, existing_posts=31)
    position = {"last_sync_at": None, "continuation_cursor": None}
    posted_ids: list[str] = []

    async def mapper(*args: Any, **kwargs: Any):
        external_id = kwargs["txn"].get("transactionId")
        if external_id in posted_ids:
            session.idempotent_hit = True
            return SimpleNamespace(outcome="blocked_idempotent", reason="duplicate")
        posted_ids.append(external_id)
        return SimpleNamespace(outcome="posted", fee_journal_entry_id=None)

    _install_sync(
        monkeypatch,
        client=client,
        session=session,
        position=position,
        mapper=mapper,
        fail_final_once=True,
    )
    with pytest.raises(RuntimeError, match="crash between page"):
        await ebay_sync._sync_one_workspace(workspace_id)
    await ebay_sync._sync_one_workspace(workspace_id)

    assert posted_ids == ["PAY-000"]
    assert position["continuation_cursor"] is None
