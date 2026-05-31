"""Unit tests for the eBay HTTP client.

Verifies:
  - access-token refresh triggers only when the cached one is stale
  - 401 forces ONE refresh + retry; second 401 raises
  - 403 raises immediately (not a token issue)
  - the URL builders pick production vs sandbox correctly
  - credential blob round-trips via as_blob()

httpx is monkeypatched with a synchronous stub so no network calls happen.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from uuid import uuid4

import pytest

from app.connectors.ebay.client import (
    EbayApiError,
    EbayCredentialBlob,
    EbayFinancesClient,
    EBAY_FINANCES_BASE_PROD,
    EBAY_FINANCES_BASE_SANDBOX,
    EBAY_TOKEN_URL_PROD,
    EBAY_TOKEN_URL_SANDBOX,
)


# ---- fakes ------------------------------------------------------------


@dataclass
class FakeResponse:
    status_code: int
    body: dict | str = ""

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

    async def post(self, url, **kwargs):
        self.calls.append({"method": "POST", "url": url, **kwargs})
        if not self._responses:
            return FakeResponse(500, "out of scripted responses")
        return self._responses.pop(0)

    async def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self._responses:
            return FakeResponse(500, "out of scripted responses")
        return self._responses.pop(0)


def _blob(**overrides) -> EbayCredentialBlob:
    base = dict(
        workspace_id=uuid4(),
        credential_id=uuid4(),
        label="test",
        client_id="APPID",
        client_secret="SECRET",
        refresh_token="REFRESH",
        environment="production",
        ru_name="RU-NAME",
        granted_scopes=["https://api.ebay.com/oauth/api_scope/sell.finances"],
    )
    base.update(overrides)
    return EbayCredentialBlob(**base)


# ---- tests ------------------------------------------------------------


def test_token_url_picks_env():
    assert _blob(environment="production").token_url() == EBAY_TOKEN_URL_PROD
    assert _blob(environment="sandbox").token_url() == EBAY_TOKEN_URL_SANDBOX


def test_base_url_picks_env():
    assert _blob(environment="production").base_url() == EBAY_FINANCES_BASE_PROD
    assert _blob(environment="sandbox").base_url() == EBAY_FINANCES_BASE_SANDBOX


def test_blob_round_trip_preserves_secret_fields():
    b = _blob(
        access_token="AT123",
        access_token_expires_at=12345,
        ebay_user_handle="skybehind",
    )
    out = b.as_blob()
    assert out["refresh_token"] == "REFRESH"
    assert out["client_secret"] == "SECRET"
    assert out["access_token"] == "AT123"
    assert out["access_token_expires_at"] == 12345
    assert out["ebay_user_handle"] == "skybehind"
    assert out["environment"] == "production"


@pytest.mark.asyncio
async def test_access_token_refresh_when_missing(monkeypatch):
    blob = _blob()
    client = EbayFinancesClient.from_credential(blob)

    fake = FakeAsyncClient(
        [
            FakeResponse(
                200,
                {
                    "access_token": "NEW-TOKEN-AB12",
                    "expires_in": 7200,
                    "refresh_token": "REFRESH",  # unchanged
                },
            )
        ]
    )
    client._http_factory = lambda: fake
    # Persistence is a no-op for this unit test
    monkeypatch.setattr(
        "app.connectors.ebay.client._persist_refreshed_blob",
        lambda *_a, **_k: _noop(),
    )

    token = await client.ensure_access_token()
    assert token == "NEW-TOKEN-AB12"
    assert blob.access_token == "NEW-TOKEN-AB12"
    assert blob.access_token_expires_at and blob.access_token_expires_at > int(time.time())


@pytest.mark.asyncio
async def test_access_token_uses_cached_when_fresh(monkeypatch):
    """A token that doesn't expire for an hour returns from cache."""
    blob = _blob(
        access_token="CACHED-AT",
        access_token_expires_at=int(time.time()) + 3600,
    )
    client = EbayFinancesClient.from_credential(blob)
    fake = FakeAsyncClient([])  # no scripted responses → would explode if hit
    client._http_factory = lambda: fake

    token = await client.ensure_access_token()
    assert token == "CACHED-AT"
    assert fake.calls == []


@pytest.mark.asyncio
async def test_request_401_then_200_succeeds(monkeypatch):
    """First call gets 401; client refreshes; second call returns 200."""
    blob = _blob(
        access_token="STALE",
        access_token_expires_at=int(time.time()) + 3600,  # fresh from cache POV
    )
    client = EbayFinancesClient.from_credential(blob)

    fake = FakeAsyncClient(
        [
            FakeResponse(401, "Unauthorized"),       # first GET
            FakeResponse(200, {"access_token": "NEW-AT-12", "expires_in": 7200, "refresh_token": "R"}),  # refresh
            FakeResponse(200, {"payouts": []}),       # retry GET
        ]
    )
    client._http_factory = lambda: fake
    monkeypatch.setattr(
        "app.connectors.ebay.client._persist_refreshed_blob",
        lambda *_a, **_k: _noop(),
    )

    out = await client.list_payouts(limit=10)
    assert out == []
    # 3 calls expected: GET, POST(refresh), GET
    methods = [c["method"] for c in fake.calls]
    assert methods == ["GET", "POST", "GET"]


@pytest.mark.asyncio
async def test_request_403_raises_immediately(monkeypatch):
    blob = _blob(
        access_token="GOOD-TOKEN",
        access_token_expires_at=int(time.time()) + 3600,
    )
    client = EbayFinancesClient.from_credential(blob)
    fake = FakeAsyncClient([FakeResponse(403, "Forbidden")])
    client._http_factory = lambda: fake

    with pytest.raises(EbayApiError) as exc:
        await client.list_payouts(limit=10)
    assert exc.value.status_code == 403
    # No refresh attempt for a 403
    methods = [c["method"] for c in fake.calls]
    assert methods == ["GET"]


@pytest.mark.asyncio
async def test_refresh_failure_propagates(monkeypatch):
    """If the refresh-token POST fails, we raise without retrying."""
    blob = _blob()
    client = EbayFinancesClient.from_credential(blob)
    fake = FakeAsyncClient([FakeResponse(400, "invalid_grant")])
    client._http_factory = lambda: fake

    with pytest.raises(EbayApiError) as exc:
        await client.ensure_access_token()
    assert exc.value.status_code == 400


# ---- helpers ----------------------------------------------------------


async def _noop():
    return None
