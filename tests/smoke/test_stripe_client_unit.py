"""Unit tests for the Stripe HTTP client.

Verifies:
  - Bearer auth + Stripe-Version header on every call
  - 429 triggers retry with backoff; eventual success
  - 429 past MAX_RETRIES raises StripeApiError
  - 5xx triggers retry once
  - 401 raises immediately (not retryable)
  - 403 raises immediately
  - pagination via starting_after cursor
  - paginate_balance_transactions stops when has_more=False
  - credential blob round-trips via as_blob()
  - safe_key_display never leaks full key

httpx is monkeypatched with a synchronous stub so no network calls happen.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from uuid import uuid4

import pytest

from app.connectors.stripe.client import (
    StripeApiError,
    StripeClient,
    StripeCredentialBlob,
    STRIPE_API_BASE,
    STRIPE_API_VERSION,
    paginate_balance_transactions,
)


# ---- fakes ------------------------------------------------------------


@dataclass
class FakeResponse:
    status_code: int
    body: dict | str = ""
    headers: dict | None = None

    def json(self):
        if isinstance(self.body, dict):
            return self.body
        raise ValueError("not json")

    @property
    def text(self):
        return json.dumps(self.body) if isinstance(self.body, dict) else str(self.body)


class FakeAsyncClient:
    """httpx.AsyncClient stand-in with scripted responses."""

    def __init__(self, responses: list[FakeResponse]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self._responses:
            return FakeResponse(500, "out of scripted responses")
        resp = self._responses.pop(0)
        if resp.headers is None:
            resp.headers = {}
        return resp

    async def get(self, url, **kwargs):
        return await self.request("GET", url, **kwargs)


def _blob(**overrides) -> StripeCredentialBlob:
    base = dict(
        workspace_id=uuid4(),
        credential_id=uuid4(),
        label="test",
        api_key="rk_live_000000000000000000000000",
        account_id="acct_test_123",
        environment="live",
    )
    base.update(overrides)
    return StripeCredentialBlob(**base)


# ---- tests ------------------------------------------------------------


def test_safe_key_display_never_leaks_full_key():
    blob = _blob(api_key="rk_live_000000000000000000000000")
    display = blob.safe_key_display()
    assert "VERYSECRETKEY" not in display
    assert "2345" in display  # last 4
    assert display.startswith("rk_live_***")


def test_safe_key_display_handles_test_key():
    blob = _blob(api_key="rk_test_000000000000000000000000")
    display = blob.safe_key_display()
    assert "rk_test_***" in display
    assert "1234" in display


def test_safe_key_display_handles_short_key():
    blob = _blob(api_key="rk_x")
    display = blob.safe_key_display()
    assert "rk_***" == display


def test_blob_round_trip_preserves_fields():
    b = _blob(
        webhook_secret="whsec_SECRETSECRETSECRET",
        default_revenue_account="4030",
        business_name="Magic Unicorn LLC",
    )
    out = b.as_blob()
    assert out["api_key"] == "rk_live_000000000000000000000000"
    assert out["webhook_secret"] == "whsec_SECRETSECRETSECRET"
    assert out["default_revenue_account"] == "4030"
    assert out["business_name"] == "Magic Unicorn LLC"
    assert out["environment"] == "live"


@pytest.mark.asyncio
async def test_bearer_and_stripe_version_headers_set():
    """Every request carries Authorization Bearer + Stripe-Version."""
    blob = _blob()
    client = StripeClient.from_credential(blob)
    fake = FakeAsyncClient(
        [FakeResponse(200, {"id": "acct_1", "data": []})]
    )
    client._http_factory = lambda: fake

    await client.get_account()
    assert len(fake.calls) == 1
    call = fake.calls[0]
    headers = call["headers"]
    assert headers["Authorization"] == f"Bearer {blob.api_key}"
    assert headers["Stripe-Version"] == STRIPE_API_VERSION
    assert headers["Accept"] == "application/json"
    assert call["url"] == f"{STRIPE_API_BASE}/account"


@pytest.mark.asyncio
async def test_429_triggers_retry_with_backoff(monkeypatch):
    """429 once → retry → 200 returns successfully."""
    # Skip sleep
    monkeypatch.setattr(asyncio, "sleep", lambda _: _noop())

    blob = _blob()
    client = StripeClient.from_credential(blob)
    fake = FakeAsyncClient(
        [
            FakeResponse(429, "rate_limited"),
            FakeResponse(200, {"data": [], "has_more": False}),
        ]
    )
    client._http_factory = lambda: fake

    out = await client.list_balance_transactions(limit=10)
    assert out == {"data": [], "has_more": False}
    methods = [c["method"] for c in fake.calls]
    assert methods == ["GET", "GET"]


@pytest.mark.asyncio
async def test_429_exhausts_retries_raises(monkeypatch):
    """If 429 keeps coming back past MAX_RETRIES, raise."""
    monkeypatch.setattr(asyncio, "sleep", lambda _: _noop())

    blob = _blob()
    client = StripeClient.from_credential(blob)
    # 6 x 429 (MAX_RETRIES=5 + initial attempt)
    fake = FakeAsyncClient([FakeResponse(429, "rate_limited") for _ in range(10)])
    client._http_factory = lambda: fake

    with pytest.raises(StripeApiError) as exc:
        await client.list_balance_transactions(limit=10)
    assert exc.value.status_code == 429


@pytest.mark.asyncio
async def test_500_triggers_one_retry(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", lambda _: _noop())

    blob = _blob()
    client = StripeClient.from_credential(blob)
    fake = FakeAsyncClient(
        [
            FakeResponse(500, "server_error"),
            FakeResponse(200, {"data": [], "has_more": False}),
        ]
    )
    client._http_factory = lambda: fake

    out = await client.list_balance_transactions(limit=10)
    assert out == {"data": [], "has_more": False}


@pytest.mark.asyncio
async def test_401_raises_immediately(monkeypatch):
    """401 is non-retryable — bail immediately."""
    monkeypatch.setattr(asyncio, "sleep", lambda _: _noop())

    blob = _blob()
    client = StripeClient.from_credential(blob)
    fake = FakeAsyncClient([FakeResponse(401, "unauthorized")])
    client._http_factory = lambda: fake

    with pytest.raises(StripeApiError) as exc:
        await client.get_account()
    assert exc.value.status_code == 401
    # Only one call (no retry)
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_403_raises_immediately(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", lambda _: _noop())

    blob = _blob()
    client = StripeClient.from_credential(blob)
    fake = FakeAsyncClient([FakeResponse(403, "forbidden")])
    client._http_factory = lambda: fake

    with pytest.raises(StripeApiError) as exc:
        await client.get_account()
    assert exc.value.status_code == 403
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_starting_after_pagination_passed_through():
    blob = _blob()
    client = StripeClient.from_credential(blob)
    fake = FakeAsyncClient([FakeResponse(200, {"data": [], "has_more": False})])
    client._http_factory = lambda: fake

    await client.list_balance_transactions(starting_after="txn_abc", limit=50)
    call = fake.calls[0]
    assert call["params"]["starting_after"] == "txn_abc"
    assert call["params"]["limit"] == 50


@pytest.mark.asyncio
async def test_paginate_balance_transactions_stops_on_has_more_false():
    blob = _blob()
    client = StripeClient.from_credential(blob)
    fake = FakeAsyncClient(
        [
            FakeResponse(
                200,
                {
                    "data": [{"id": "txn_1"}, {"id": "txn_2"}],
                    "has_more": True,
                },
            ),
            FakeResponse(
                200,
                {
                    "data": [{"id": "txn_3"}],
                    "has_more": False,
                },
            ),
        ]
    )
    client._http_factory = lambda: fake

    out = await paginate_balance_transactions(client, max_pages=10)
    assert len(out) == 3
    assert [t["id"] for t in out] == ["txn_1", "txn_2", "txn_3"]
    # Second page request should have starting_after=txn_2
    assert fake.calls[1]["params"]["starting_after"] == "txn_2"


@pytest.mark.asyncio
async def test_paginate_respects_max_pages():
    blob = _blob()
    client = StripeClient.from_credential(blob)
    # Always returns has_more=True; max_pages caps it
    fake = FakeAsyncClient(
        [
            FakeResponse(
                200, {"data": [{"id": f"txn_{i}"}], "has_more": True}
            )
            for i in range(20)
        ]
    )
    client._http_factory = lambda: fake

    out = await paginate_balance_transactions(client, max_pages=3)
    assert len(out) == 3
    assert len(fake.calls) == 3


# ---- helpers ----------------------------------------------------------


async def _noop():
    return None
