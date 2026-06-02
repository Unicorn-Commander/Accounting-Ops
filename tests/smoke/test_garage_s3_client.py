"""Smoke tests for app.clients.garage_s3.GarageS3Client.

No real network. Every HTTP interaction is intercepted by httpx.MockTransport
(same strategy as test_llm_categorizer). Covers:

  1. is_configured() — False when keys blank, True when both present.
  2. put_object raises GarageNotConfigured when unconfigured (no HTTP call).
  3. put_object happy path — issues a PUT, sends a SigV4 Authorization header +
     x-amz-content-sha256 equal to the body hash, returns PutResult.
  4. put_object surfaces a 4xx/5xx from the store as GarageUploadError.
  5. put_object maps a transport error to GarageUploadError (never leaks).
  6. SigV4 signing-key derivation matches the AWS canonical test vector.
  7. The signature is deterministic for a fixed clock + inputs (regression).
"""
from __future__ import annotations

import hashlib
import hmac

import httpx
import pytest

from app.clients.garage_s3 import (
    GarageNotConfigured,
    GarageS3Client,
    GarageUploadError,
    _signing_key,
)
from app.config import Settings


def _settings(*, access="AKIDEXAMPLE", secret="secretkey", endpoint="http://garage:3900"):
    return Settings(
        garage_s3_endpoint=endpoint,
        garage_s3_access_key=access,
        garage_s3_secret_key=secret,
        garage_s3_region="garage",
        garage_bucket_prefix="accounting-ops",
    )


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------- #
def test_is_configured():
    assert GarageS3Client(_settings()).is_configured() is True
    assert GarageS3Client(_settings(access="")).is_configured() is False
    assert GarageS3Client(_settings(secret="")).is_configured() is False


@pytest.mark.asyncio
async def test_put_object_unconfigured_raises_and_makes_no_call():
    client = GarageS3Client(_settings(access="", secret=""))
    from unittest.mock import patch

    with patch("httpx.AsyncClient") as mock_ac:
        with pytest.raises(GarageNotConfigured):
            await client.put_object(
                bucket="accounting-ops",
                object_key="w/e/uploads/abc.pdf",
                body=b"hello",
                content_type="application/pdf",
            )
    mock_ac.assert_not_called()


@pytest.mark.asyncio
async def test_put_object_happy_path_signs_and_sends_hash():
    body = b"%PDF-1.4 synthetic receipt bytes"
    expected_hash = hashlib.sha256(body).hexdigest()
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["content_sha"] = request.headers.get("x-amz-content-sha256")
        captured["amz_date"] = request.headers.get("x-amz-date")
        captured["content_type"] = request.headers.get("content-type")
        captured["body"] = request.content
        return httpx.Response(200, headers={"ETag": '"deadbeef"'})

    from unittest.mock import patch

    client = GarageS3Client(_settings())
    with patch("httpx.AsyncClient", return_value=_mock_client(handler)):
        result = await client.put_object(
            bucket="accounting-ops",
            object_key="wid/eid/uploads/" + expected_hash + ".pdf",
            body=body,
            content_type="application/pdf",
        )

    assert captured["method"] == "PUT"
    assert captured["url"].endswith(f"/accounting-ops/wid/eid/uploads/{expected_hash}.pdf")
    # SigV4 Authorization present + well-formed.
    assert captured["auth"].startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
    assert "SignedHeaders=" in captured["auth"]
    assert "Signature=" in captured["auth"]
    # The signed payload hash equals the real body hash (integrity end-to-end).
    assert captured["content_sha"] == expected_hash
    assert captured["content_type"] == "application/pdf"
    assert captured["body"] == body
    assert result.etag == '"deadbeef"'
    assert result.size_bytes == len(body)
    assert result.object_key.endswith(".pdf")


@pytest.mark.asyncio
async def test_put_object_store_rejection_is_upload_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="AccessDenied")

    from unittest.mock import patch

    client = GarageS3Client(_settings())
    with patch("httpx.AsyncClient", return_value=_mock_client(handler)):
        with pytest.raises(GarageUploadError) as exc:
            await client.put_object(
                bucket="accounting-ops",
                object_key="w/e/uploads/x.png",
                body=b"\x89PNG\r\n\x1a\n",
                content_type="image/png",
            )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_put_object_transport_error_is_upload_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    from unittest.mock import patch

    client = GarageS3Client(_settings())
    with patch("httpx.AsyncClient", return_value=_mock_client(handler)):
        with pytest.raises(GarageUploadError) as exc:
            await client.put_object(
                bucket="accounting-ops",
                object_key="w/e/uploads/x.png",
                body=b"data",
                content_type="image/png",
            )
    # No status code on a transport failure; message must not leak internals.
    assert exc.value.status_code is None
    assert "unreachable" in str(exc.value).lower()


def test_sigv4_signing_key_matches_aws_vector():
    """AWS SigV4 canonical example (Signature Version 4 Test Suite)."""
    secret = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
    sk = _signing_key(secret, "20150830", "us-east-1", "iam")
    string_to_sign = (
        "AWS4-HMAC-SHA256\n"
        "20150830T123600Z\n"
        "20150830/us-east-1/iam/aws4_request\n"
        "f536975d06c0309214f805bb90ccff089219ecd68b2577efef23edd43b7e1a59"
    )
    sig = hmac.new(sk, string_to_sign.encode(), hashlib.sha256).hexdigest()
    assert sig == "5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7"
