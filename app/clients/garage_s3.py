"""Minimal, dependency-free S3 (Garage) client for the document-upload path.

Why not boto3? The app's dependency set is intentionally lean (see pyproject)
and adding boto3/aioboto3 would force a uv.lock regeneration — a known deploy
hazard for this project. We only need ONE S3 verb (PutObject) plus a HEAD for
existence checks, so we sign requests ourselves with AWS Signature V4 over
``httpx`` (already a dependency) and ``hashlib``/``hmac`` (stdlib). Garage is
S3-compatible and validates SigV4 exactly like AWS.

Security posture:
  * Credentials come ONLY from Settings (env-only — never hard-coded). When
    either key is blank, ``is_configured()`` is False and the caller surfaces a
    clear 503 instead of attempting an unauthenticated/partial write.
  * Path-style addressing (``{endpoint}/{bucket}/{key}``) — Garage's default,
    and avoids DNS/vhost-style bucket resolution.
  * The object key is built by the caller from the workspace id + a
    content-hash, so one workspace can never address another's namespace via
    this client (the bucket itself is per-deploy; row-level scoping lives in the
    ``receipt`` table + RLS).
  * The full SHA-256 of the body is sent as ``x-amz-content-sha256`` (no
    UNSIGNED-PAYLOAD shortcut), so the gateway verifies integrity end-to-end.

This module performs NO authorization and reads NO request context; it is a
pure transport helper. All tenant/role checks happen in the API layer before a
call lands here.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from app.config import Settings, get_settings

_SERVICE = "s3"
_ALGORITHM = "AWS4-HMAC-SHA256"
# Default per-call timeout. Uploads are bounded by document_upload_max_bytes and
# Garage is on the same docker network in production, so a 60s ceiling comfortably
# covers a max-size (100 MB) object while still keeping a stuck object store from
# hanging the request worker indefinitely.
_DEFAULT_TIMEOUT_S = 60.0


class GarageNotConfigured(RuntimeError):
    """Raised/signalled when S3 credentials are absent. The API maps this to a
    503 so the user learns uploads aren't wired up — never a silent failure."""


class GarageUploadError(RuntimeError):
    """Raised when the object store rejects or fails a PUT. Carries the HTTP
    status (when known) so the caller can log it without leaking the body."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class PutResult:
    bucket: str
    object_key: str
    etag: str | None
    size_bytes: int


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret: str, date_stamp: str, region: str, service: str) -> bytes:
    """Derive the SigV4 signing key (AWS4 → date → region → service → request)."""
    k_date = _hmac(("AWS4" + secret).encode("utf-8"), date_stamp)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, service)
    return _hmac(k_service, "aws4_request")


def _encode_key(object_key: str) -> str:
    """Percent-encode an object key for the request path.

    S3 canonical URIs encode each path segment but keep ``/`` as a separator.
    quote(safe="/") matches what AWS/Garage expect for path-style keys.
    """
    return quote(object_key, safe="/~")


class GarageS3Client:
    """One-verb-plus-HEAD S3 client (PutObject + HeadObject) for Garage.

    Stateless; safe to construct per-request. All config is captured at
    construction from ``Settings``.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._s = settings or get_settings()

    # ------------------------------------------------------------------ #
    def is_configured(self) -> bool:
        return bool(
            self._s.garage_s3_endpoint
            and self._s.garage_s3_access_key
            and self._s.garage_s3_secret_key
        )

    def _endpoint(self) -> str:
        return self._s.garage_s3_endpoint.rstrip("/")

    def _host(self) -> str:
        # Host header for the canonical request: scheme-less authority of the
        # endpoint (httpx fills the real Host, but SigV4 must sign the value we
        # expect the server to see).
        ep = self._endpoint()
        return ep.split("://", 1)[-1]

    def _signed_headers(
        self,
        *,
        method: str,
        object_key: str,
        bucket: str,
        payload: bytes,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Build SigV4-signed headers for a path-style request on one object."""
        region = self._s.garage_s3_region
        access_key = self._s.garage_s3_access_key
        secret_key = self._s.garage_s3_secret_key

        now = dt.datetime.now(dt.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")

        content_sha = _sha256_hex(payload)
        canonical_uri = f"/{bucket}/{_encode_key(object_key)}"

        headers: dict[str, str] = {
            "host": self._host(),
            "x-amz-content-sha256": content_sha,
            "x-amz-date": amz_date,
        }
        if extra_headers:
            # Lower-case header names for canonicalization consistency.
            for k, v in extra_headers.items():
                headers[k.lower()] = v

        signed_header_names = ";".join(sorted(headers.keys()))
        canonical_headers = "".join(
            f"{k}:{headers[k].strip()}\n" for k in sorted(headers.keys())
        )

        canonical_request = "\n".join(
            [
                method,
                canonical_uri,
                "",  # canonical query string (none)
                canonical_headers,
                signed_header_names,
                content_sha,
            ]
        )

        credential_scope = f"{date_stamp}/{region}/{_SERVICE}/aws4_request"
        string_to_sign = "\n".join(
            [
                _ALGORITHM,
                amz_date,
                credential_scope,
                _sha256_hex(canonical_request.encode("utf-8")),
            ]
        )

        signing_key = _signing_key(secret_key, date_stamp, region, _SERVICE)
        signature = hmac.new(
            signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        authorization = (
            f"{_ALGORITHM} Credential={access_key}/{credential_scope}, "
            f"SignedHeaders={signed_header_names}, Signature={signature}"
        )
        # Return real-case header dict for httpx; Authorization added last.
        out = {
            "x-amz-content-sha256": content_sha,
            "x-amz-date": amz_date,
            "Authorization": authorization,
        }
        if extra_headers:
            out.update(extra_headers)
        return out

    # ------------------------------------------------------------------ #
    async def put_object(
        self,
        *,
        bucket: str,
        object_key: str,
        body: bytes,
        content_type: str,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> PutResult:
        """Upload a single object. Raises GarageNotConfigured / GarageUploadError.

        The body is held in memory (bounded by document_upload_max_bytes at the
        API layer), hashed for the SigV4 signature, and PUT path-style. We do
        NOT retry — the caller owns the user-facing failure surface and a retry
        of a non-idempotent PUT into a content-hashed key is pointless (the key
        is deterministic; a re-PUT would just overwrite with identical bytes).
        """
        if not self.is_configured():
            raise GarageNotConfigured(
                "Garage S3 credentials are not configured (GARAGE_S3_ACCESS_KEY / "
                "GARAGE_S3_SECRET_KEY)."
            )

        extra = {"content-type": content_type}
        signed = self._signed_headers(
            method="PUT",
            object_key=object_key,
            bucket=bucket,
            payload=body,
            extra_headers=extra,
        )
        url = f"{self._endpoint()}/{bucket}/{_encode_key(object_key)}"
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                resp = await client.put(url, content=body, headers=signed)
        except httpx.HTTPError as exc:  # network/timeout — never leak internals
            raise GarageUploadError(
                f"Object store unreachable: {type(exc).__name__}"
            ) from exc

        if resp.status_code >= 400:
            raise GarageUploadError(
                f"Object store rejected upload (HTTP {resp.status_code}).",
                status_code=resp.status_code,
            )
        return PutResult(
            bucket=bucket,
            object_key=object_key,
            etag=resp.headers.get("ETag"),
            size_bytes=len(body),
        )

    # ------------------------------------------------------------------ #
    async def get_object(
        self,
        *,
        bucket: str,
        object_key: str,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> bytes:
        """Fetch a single object's bytes (GetObject), path-style + SigV4-signed.

        The upload path never needs this — it holds the bytes in memory already.
        It exists for reprocess/backfill paths (e.g. re-running the tax-doc
        extractor over already-stored documents): those have only the
        bucket/object_key and must read the bytes back. SigV4 for a bodyless GET
        signs the empty-payload hash, which ``_signed_headers`` computes from the
        empty ``payload``. Raises GarageNotConfigured / GarageUploadError.
        """
        if not self.is_configured():
            raise GarageNotConfigured(
                "Garage S3 credentials are not configured (GARAGE_S3_ACCESS_KEY / "
                "GARAGE_S3_SECRET_KEY)."
            )
        signed = self._signed_headers(
            method="GET",
            object_key=object_key,
            bucket=bucket,
            payload=b"",
        )
        url = f"{self._endpoint()}/{bucket}/{_encode_key(object_key)}"
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                resp = await client.get(url, headers=signed)
        except httpx.HTTPError as exc:  # network/timeout — never leak internals
            raise GarageUploadError(
                f"Object store unreachable: {type(exc).__name__}"
            ) from exc
        if resp.status_code >= 400:
            raise GarageUploadError(
                f"Object store rejected fetch (HTTP {resp.status_code}).",
                status_code=resp.status_code,
            )
        return resp.content


__all__ = (
    "GarageS3Client",
    "GarageNotConfigured",
    "GarageUploadError",
    "PutResult",
)
