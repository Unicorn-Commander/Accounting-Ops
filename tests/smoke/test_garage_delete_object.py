"""Smoke tests for GarageS3Client.delete_object (added for data-rights erasure).

Follows the same httpx.MockTransport strategy as test_garage_s3_client.py.

The idempotency case matters more than it looks: erasure deletes many objects in
a loop, and a partially-completed erasure must be safely re-runnable. If an
already-deleted key raised, the retry would abort partway through and leave
customer documents in the object store while the receipt said otherwise.
"""
from __future__ import annotations

import hashlib
from unittest.mock import patch

import httpx
import pytest

from app.clients.garage_s3 import (
    GarageNotConfigured,
    GarageS3Client,
    GarageUploadError,
)
from app.config import Settings

_EMPTY_SHA = hashlib.sha256(b"").hexdigest()


def _settings(*, access="AKIDEXAMPLE", secret="secretkey"):
    return Settings(
        garage_s3_endpoint="http://garage:3900",
        garage_s3_access_key=access,
        garage_s3_secret_key=secret,
        garage_s3_region="garage",
        garage_bucket_prefix="accounting-ops",
    )


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_delete_unconfigured_raises_and_makes_no_call():
    client = GarageS3Client(_settings(access="", secret=""))
    with patch("httpx.AsyncClient") as mock_ac:
        with pytest.raises(GarageNotConfigured):
            await client.delete_object(bucket="b", object_key="k")
    mock_ac.assert_not_called()


@pytest.mark.asyncio
async def test_delete_signs_a_bodyless_request():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["content_sha"] = request.headers.get("x-amz-content-sha256")
        return httpx.Response(204)

    client = GarageS3Client(_settings())
    with patch("httpx.AsyncClient", return_value=_mock_client(handler)):
        assert await client.delete_object(bucket="bkt", object_key="docs/a/x.pdf") is True

    assert captured["method"] == "DELETE"
    assert captured["url"].endswith("/bkt/docs/a/x.pdf")
    assert captured["auth"].startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
    # Bodyless request signs the empty-payload hash.
    assert captured["content_sha"] == _EMPTY_SHA


@pytest.mark.asyncio
async def test_delete_is_idempotent_404_counts_as_gone():
    """Re-running a partially-failed erasure must not abort on already-deleted keys."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    client = GarageS3Client(_settings())
    with patch("httpx.AsyncClient", return_value=_mock_client(handler)):
        assert await client.delete_object(bucket="bkt", object_key="missing.pdf") is True


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [403, 500, 503])
async def test_delete_surfaces_real_errors(code):
    """A purge that cannot reach the store must fail loudly, never report success."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(code)

    client = GarageS3Client(_settings())
    with patch("httpx.AsyncClient", return_value=_mock_client(handler)):
        with pytest.raises(GarageUploadError) as exc:
            await client.delete_object(bucket="bkt", object_key="x.pdf")
    assert exc.value.status_code == code


@pytest.mark.asyncio
async def test_delete_maps_transport_error_without_leaking_internals():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = GarageS3Client(_settings())
    with patch("httpx.AsyncClient", return_value=_mock_client(handler)):
        with pytest.raises(GarageUploadError) as exc:
            await client.delete_object(bucket="bkt", object_key="x.pdf")
    assert "unreachable" in str(exc.value)
    assert "boom" not in str(exc.value)
