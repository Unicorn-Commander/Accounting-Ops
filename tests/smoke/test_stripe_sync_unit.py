"""Offline tests for the Stripe sync worker's durable cursor behavior."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
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


@pytest.mark.asyncio
async def test_sync_recovers_transactions_after_the_50_page_cap():
    """5,100 old rows must survive two capped passes without a cursor skip."""
    workspace_id = uuid4()
    entity_id = uuid4()
    created = int((datetime.now(tz=timezone.utc) - timedelta(days=2)).timestamp())
    transactions = [
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
        for index in range(5100)
    ]
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
    last_sync: datetime | None = None

    async def load_credential(*args: Any, **kwargs: Any) -> StripeCredentialBlob:
        return client.blob

    async def load_cursor(*args: Any, **kwargs: Any) -> datetime | None:
        return last_sync

    async def record_cursor(*args: Any, **kwargs: Any) -> None:
        nonlocal last_sync
        last_sync = datetime.now(tz=timezone.utc)

    @asynccontextmanager
    async def fake_workspace_session(*args: Any, **kwargs: Any):
        yield session

    async def fake_reconcile(*args: Any, **kwargs: Any) -> dict[str, int]:
        return {"checked_payouts": 0, "matched": 0, "unmatched": 0}

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(stripe_sync, "load_stripe_credential", load_credential)
    monkeypatch.setattr(stripe_sync, "_load_last_sync", load_cursor)
    monkeypatch.setattr(stripe_sync, "_record_sync_outcome", record_cursor)
    monkeypatch.setattr(stripe_sync, "workspace_session", fake_workspace_session)
    monkeypatch.setattr(stripe_sync, "reconcile_one_entity", fake_reconcile)
    monkeypatch.setattr(
        stripe_sync.StripeClient,
        "from_credential",
        classmethod(lambda cls, blob: client),
    )
    try:
        await stripe_sync._sync_one_workspace(workspace_id)
        await stripe_sync._sync_one_workspace(workspace_id)
    finally:
        monkeypatch.undo()

    assert len(http.calls) == 51
    assert len(session.posted_entries) == 5100
    assert {entry["source_external_id"] for entry in session.posted_entries} == {
        f"txn_{index:05d}" for index in range(5100)
    }
