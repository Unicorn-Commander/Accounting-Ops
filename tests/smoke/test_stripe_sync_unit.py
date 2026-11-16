"""Offline tests for the Stripe sync worker's durable cursor behavior."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.connectors.stripe.client import (
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
    """Fake HTTP layer that honors Stripe's created filter and cursor."""

    def __init__(self, transactions: list[dict[str, Any]], created: int):
        self.transactions = transactions
        self.created = created
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self) -> "_StripeHttp":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def request(self, method: str, url: str, **kwargs: Any) -> _Response:
        self.calls.append({"method": method, "url": url, **kwargs})
        params = kwargs.get("params", {})
        if int(params.get("created[gte]", 0)) > self.created:
            return _Response({"data": [], "has_more": False})

        starting_after = params.get("starting_after")
        start = int(starting_after.removeprefix("txn_")) + 1 if starting_after else 0
        page = self.transactions[start : start + 100]
        return _Response(
            {
                "data": page,
                "has_more": start + len(page) < len(self.transactions),
            }
        )


def _transactions(count: int, created: int) -> list[dict[str, Any]]:
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
        for index in range(count)
    ]


def _install_worker(
    monkeypatch: pytest.MonkeyPatch,
    transactions: list[dict[str, Any]],
    created: int,
    *,
    mapper: Any = None,
):
    workspace_id = uuid4()
    entity_id = uuid4()
    http = _StripeHttp(transactions, created)
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
    session = _StubSession(accounts=_ACCOUNTS, existing_posts=31)
    position: list[Any] = [None, None]
    records: list[dict[str, Any]] = []

    async def load_credential(*args: Any, **kwargs: Any) -> StripeCredentialBlob:
        return client.blob

    async def load_position(*args: Any, **kwargs: Any) -> tuple[datetime | None, str | None]:
        return position[0], position[1]

    async def record_cursor(*args: Any, **kwargs: Any) -> None:
        position[0] = kwargs["last_sync_at"]
        position[1] = kwargs["continuation_cursor"]
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
    return workspace_id, http, session, position, records


@pytest.mark.asyncio
async def test_sync_recovers_transactions_after_the_50_page_cap(monkeypatch):
    """5,100 old rows must survive two capped passes without a cursor skip."""
    created = int((datetime.now(tz=timezone.utc) - timedelta(days=2)).timestamp())
    transactions = _transactions(5100, created)
    workspace_id, http, session, position, _ = _install_worker(
        monkeypatch, transactions, created
    )

    await stripe_sync._sync_one_workspace(workspace_id)
    await stripe_sync._sync_one_workspace(workspace_id)

    assert len(http.calls) == 51
    assert len(session.posted_entries) == 5100
    assert {entry["source_external_id"] for entry in session.posted_entries} == {
        f"txn_{index:05d}" for index in range(5100)
    }
    assert position[1] is None


@pytest.mark.asyncio
async def test_sync_does_not_advance_cursor_past_mapping_failure(monkeypatch):
    created = int((datetime.now(tz=timezone.utc) - timedelta(days=2)).timestamp())
    transactions = _transactions(4, created)
    seen: list[str] = []

    async def failing_mapper(session: Any, *, txn: dict[str, Any], **kwargs: Any):
        seen.append(txn["id"])
        if txn["id"] == "txn_00002":
            raise RuntimeError("fictional mapper failure")
        return SimpleNamespace(outcome="posted", fee_journal_entry_id=None)

    workspace_id, _, _, position, records = _install_worker(
        monkeypatch, transactions, created, mapper=failing_mapper
    )
    summary = await stripe_sync._sync_one_workspace(workspace_id)

    assert seen == ["txn_00000", "txn_00001", "txn_00002"]
    assert summary["last_outcome"] == "processing_error"
    assert position[1] == "txn_00001"
    assert position[0] == datetime.fromtimestamp(created, tz=timezone.utc)
    assert records[-1]["errors"]


@pytest.mark.asyncio
async def test_sync_below_cap_advances_to_now_and_clears_continuation(monkeypatch):
    created = int((datetime.now(tz=timezone.utc) - timedelta(days=2)).timestamp())
    transactions = _transactions(3, created)
    workspace_id, http, session, position, _ = _install_worker(
        monkeypatch, transactions, created
    )

    before = datetime.now(tz=timezone.utc)
    summary = await stripe_sync._sync_one_workspace(workspace_id)
    after = datetime.now(tz=timezone.utc)

    assert summary["last_outcome"] == "ok"
    assert len(http.calls) == 1
    assert len(session.posted_entries) == 3
    assert before <= position[0] <= after
    assert position[1] is None
