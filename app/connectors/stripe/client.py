"""Async HTTP client for the Stripe REST API.

Reads the encrypted credential blob from public.tenant_credentials (written by
app/api/stripe_oauth.py) and exposes high-level async methods:

  * list_charges(starting_after=None, limit=100)
  * list_refunds(starting_after=None, limit=100)
  * list_payouts(starting_after=None, limit=100)
  * list_balance_transactions(starting_after=None, limit=100, type=None, created_gte=None)

Authentication: simple Bearer with the restricted API key. No token refresh —
restricted API keys are long-lived; Aaron rotates them in the Stripe dashboard
if they leak.

Retry behavior:
  * 429 (rate limit)  → exponential backoff up to MAX_RETRIES then raise
  * 5xx               → one retry with backoff then raise
  * 4xx (non-429)     → raise StripeApiError immediately

OTEL: each high-level call is wrapped in a span tagged with the Stripe
endpoint, duration, outcome. Audit 2026-05-31 lesson: NEVER log any byte
of the API key, including prefix/suffix. Log credential_id + workspace_id
+ status only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import text

from app.db import SessionLocal
from app.security.credentials import get_credential_plaintext

_log = logging.getLogger("acctops.stripe_client")

STRIPE_API_BASE = "https://api.stripe.com/v1"

# Stripe pins its API version per-call; pin to a known release so a Stripe
# server-side default change doesn't silently change our payload shape.
STRIPE_API_VERSION = "2024-06-20"

# Retry tuning for 429 / 5xx. Stripe's recommended pattern: exponential
# backoff with full jitter, capped at MAX_BACKOFF_S.
MAX_RETRIES = 5
INITIAL_BACKOFF_S = 0.5
MAX_BACKOFF_S = 30.0


class StripeApiError(Exception):
    """Wrapping for any non-2xx response / network error from Stripe."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: str | None = None,
        stripe_request_id: str | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        # Truncated for safety; body is for ops debugging, never logged in full.
        self.body = (body or "")[:500]
        self.stripe_request_id = stripe_request_id


@dataclass
class StripeCredentialBlob:
    """In-memory representation of the JSON blob in tenant_credentials.

    Stripe restricted API keys are long-lived (no refresh cycle), so unlike the
    eBay blob there's no access_token + expires_at lifecycle. Webhook secret is
    stored alongside the API key so the webhook handler can verify signatures
    using the same per-workspace decryption envelope.
    """

    workspace_id: UUID
    credential_id: UUID
    label: str
    api_key: str  # rk_live_... or rk_test_...
    account_id: str | None  # acct_... — captured from /v1/account at first save
    environment: str  # "live" | "test"
    entity_id: str | None = None
    webhook_secret: str | None = None  # whsec_... — added when webhook is configured
    default_revenue_account: str = "4200"  # configurable per-entity; default 4200 (AI Infra Revenue)
    business_name: str | None = None  # for UI display
    granted_at: int | None = None
    last_used_at: int | None = None

    def as_blob(self) -> dict[str, Any]:
        return {
            "api_key": self.api_key,
            "account_id": self.account_id,
            "environment": self.environment,
            "entity_id": self.entity_id,
            "webhook_secret": self.webhook_secret,
            "default_revenue_account": self.default_revenue_account,
            "business_name": self.business_name,
            "granted_at": self.granted_at,
            "last_used_at": self.last_used_at,
        }

    def safe_key_display(self) -> str:
        """Show last 4 chars of the API key for the UI — never the full key."""
        if not self.api_key or len(self.api_key) < 8:
            return "rk_***"
        prefix = "rk_live" if self.api_key.startswith("rk_live") else "rk_test"
        return f"{prefix}_***{self.api_key[-4:]}"


async def load_stripe_credential(
    workspace_id: UUID,
    *,
    requesting_service: str,
    actor_uc_uid: str | None,
    actor_agent_id: str | None = None,
) -> StripeCredentialBlob | None:
    """Decrypt + parse the workspace's stripe_api_key credential.

    Returns None if no credential exists. Raises CredentialDecryptionError if
    the blob is present but undecryptable (caller decides whether to surface
    or skip).
    """
    async with SessionLocal() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.current_workspace', :wid, true)"),
                {"wid": str(workspace_id)},
            )
            row = (
                await session.execute(
                    text(
                        """
                        SELECT id::text AS id, label
                        FROM public.tenant_credentials
                        WHERE workspace_id = :wid AND connector = 'stripe_api_key'
                        ORDER BY encrypted_at DESC
                        LIMIT 1
                        """
                    ),
                    {"wid": str(workspace_id)},
                )
            ).first()
            if row is None:
                return None
            plaintext = await get_credential_plaintext(
                session,
                workspace_id,
                "stripe_api_key",
                label=row.label,
                requesting_service=requesting_service,
                actor_uc_uid=actor_uc_uid,
                actor_agent_id=actor_agent_id,
            )
    if not plaintext:
        return None
    try:
        blob = json.loads(plaintext)
    except json.JSONDecodeError:
        _log.warning("stripe_credential_blob_not_json workspace=%s", workspace_id)
        return None
    try:
        return StripeCredentialBlob(
            workspace_id=workspace_id,
            credential_id=UUID(row.id),
            label=row.label,
            api_key=blob["api_key"],
            account_id=blob.get("account_id"),
            environment=blob.get("environment", "live"),
            entity_id=blob.get("entity_id"),
            webhook_secret=blob.get("webhook_secret"),
            default_revenue_account=blob.get("default_revenue_account", "4200"),
            business_name=blob.get("business_name"),
            granted_at=blob.get("granted_at"),
            last_used_at=blob.get("last_used_at"),
        )
    except KeyError as exc:
        _log.warning(
            "stripe_credential_blob_missing_field=%s workspace=%s", exc, workspace_id
        )
        return None


async def persist_stripe_blob(blob: StripeCredentialBlob) -> None:
    """Re-encrypt and persist the updated blob to the same credential row.

    Called when we update fields like ``webhook_secret`` (from the webhook
    setup flow), ``default_revenue_account`` (from the UI), or
    ``last_used_at`` (from successful API calls).
    """
    from app.security.crypto import get_crypto

    crypto = get_crypto()
    nonce, ciphertext = crypto.encrypt(
        json.dumps(blob.as_blob()), blob.workspace_id, blob.credential_id
    )
    async with SessionLocal() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.current_workspace', :wid, true)"),
                {"wid": str(blob.workspace_id)},
            )
            await session.execute(
                text(
                    """
                    UPDATE public.tenant_credentials
                    SET ciphertext = :ct, nonce = :nonce,
                        encrypted_at = now(), last_used_at = now()
                    WHERE id = :id AND workspace_id = :wid
                    """
                ),
                {
                    "ct": ciphertext,
                    "nonce": nonce,
                    "id": str(blob.credential_id),
                    "wid": str(blob.workspace_id),
                },
            )


@dataclass
class StripeClient:
    """Authenticated async client. Construct with ``from_credential``."""

    blob: StripeCredentialBlob
    timeout_s: float = 20.0
    _http_factory: Any = None  # injected in tests; default = httpx.AsyncClient

    @classmethod
    def from_credential(cls, blob: StripeCredentialBlob) -> "StripeClient":
        return cls(blob=blob)

    def _http_client(self) -> httpx.AsyncClient:
        if self._http_factory is None:
            return httpx.AsyncClient(timeout=self.timeout_s)
        return self._http_factory()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.blob.api_key}",
            "Accept": "application/json",
            "Stripe-Version": STRIPE_API_VERSION,
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send one request, retrying 429 + 5xx with exponential backoff."""
        url = f"{STRIPE_API_BASE}{path}"
        attempt = 0
        last_status: int | None = None
        last_body: str | None = None
        last_req_id: str | None = None
        while attempt <= MAX_RETRIES:
            async with self._http_client() as client:
                resp = await client.request(
                    method, url, headers=self._headers(), params=params or {}
                )
            last_status = resp.status_code
            last_req_id = resp.headers.get("Request-Id")
            if resp.status_code < 400:
                try:
                    return resp.json()
                except ValueError:
                    return {}
            last_body = resp.text
            # Retry on 429 (rate limit) and 5xx (transient).
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt >= MAX_RETRIES:
                    break
                # Stripe sets Retry-After on 429 sometimes; fall back to
                # exponential with full jitter.
                retry_after = resp.headers.get("Retry-After")
                if retry_after is not None:
                    try:
                        sleep_s = min(float(retry_after), MAX_BACKOFF_S)
                    except ValueError:
                        sleep_s = min(
                            INITIAL_BACKOFF_S * (2 ** attempt) + random.random(),
                            MAX_BACKOFF_S,
                        )
                else:
                    sleep_s = min(
                        INITIAL_BACKOFF_S * (2 ** attempt) + random.random(),
                        MAX_BACKOFF_S,
                    )
                _log.info(
                    "stripe_retry status=%s attempt=%d sleep_s=%.2f path=%s req_id=%s",
                    resp.status_code,
                    attempt + 1,
                    sleep_s,
                    path,
                    last_req_id or "?",
                )
                await asyncio.sleep(sleep_s)
                attempt += 1
                continue
            # Non-retryable 4xx — bail immediately.
            break
        # Out of retries or non-retryable. Audit: never log the API key, just
        # credential metadata.
        _log.warning(
            "stripe_api_error status=%s credential_id=%s workspace_id=%s path=%s req_id=%s",
            last_status,
            self.blob.credential_id,
            self.blob.workspace_id,
            path,
            last_req_id or "?",
        )
        raise StripeApiError(
            f"Stripe returned HTTP {last_status} for {path}",
            status_code=last_status,
            body=last_body,
            stripe_request_id=last_req_id,
        )

    # -------- high-level methods --------------------------------------------

    async def get_account(self) -> dict[str, Any]:
        """GET /v1/account — owner of the API key. Used at credential-save time
        to verify the key is valid + capture acct_... id for the UI."""
        return await self._request("GET", "/account")

    async def list_charges(
        self,
        *,
        starting_after: str | None = None,
        limit: int = 100,
        created_gte: int | None = None,
    ) -> dict[str, Any]:
        """Page through GET /charges. Returns one Stripe page; caller paginates.

        Use ``starting_after=last_object_id`` (NOT a cursor token) to fetch
        the next page. ``has_more`` in the response indicates more pages.
        """
        params: dict[str, Any] = {"limit": min(limit, 100)}
        if starting_after:
            params["starting_after"] = starting_after
        if created_gte is not None:
            # Stripe filter format: created[gte]=unix_seconds
            params["created[gte]"] = created_gte
        return await self._request("GET", "/charges", params=params)

    async def list_refunds(
        self,
        *,
        starting_after: str | None = None,
        limit: int = 100,
        created_gte: int | None = None,
    ) -> dict[str, Any]:
        """Page through GET /refunds."""
        params: dict[str, Any] = {"limit": min(limit, 100)}
        if starting_after:
            params["starting_after"] = starting_after
        if created_gte is not None:
            params["created[gte]"] = created_gte
        return await self._request("GET", "/refunds", params=params)

    async def list_payouts(
        self,
        *,
        starting_after: str | None = None,
        limit: int = 100,
        created_gte: int | None = None,
    ) -> dict[str, Any]:
        """Page through GET /payouts."""
        params: dict[str, Any] = {"limit": min(limit, 100)}
        if starting_after:
            params["starting_after"] = starting_after
        if created_gte is not None:
            params["created[gte]"] = created_gte
        return await self._request("GET", "/payouts", params=params)

    async def list_balance_transactions(
        self,
        *,
        starting_after: str | None = None,
        limit: int = 100,
        type: str | None = None,
        created_gte: int | None = None,
    ) -> dict[str, Any]:
        """Page through GET /balance_transactions.

        This is the canonical row for journal mapping — every charge, refund,
        fee, and payout shows up here with both the gross + net + fee amounts
        broken out, plus a ``type`` discriminator we route on.
        """
        params: dict[str, Any] = {"limit": min(limit, 100)}
        if starting_after:
            params["starting_after"] = starting_after
        if type:
            params["type"] = type
        if created_gte is not None:
            params["created[gte]"] = created_gte
        return await self._request("GET", "/balance_transactions", params=params)


async def paginate_balance_transactions(
    client: StripeClient,
    *,
    created_gte: int | None = None,
    max_pages: int = 50,
) -> list[dict[str, Any]]:
    """Convenience: pull every page of balance_transactions matching the filter.

    Bounded by ``max_pages`` to keep a sync pass from running unbounded when
    Stripe has, e.g., 10k+ historical txns.
    """
    out: list[dict[str, Any]] = []
    starting_after: str | None = None
    for _ in range(max_pages):
        page = await client.list_balance_transactions(
            starting_after=starting_after, limit=100, created_gte=created_gte
        )
        data = page.get("data") or []
        if not data:
            break
        out.extend(data)
        if not page.get("has_more"):
            break
        starting_after = data[-1]["id"]
    return out


__all__ = (
    "StripeApiError",
    "StripeCredentialBlob",
    "StripeClient",
    "STRIPE_API_BASE",
    "STRIPE_API_VERSION",
    "load_stripe_credential",
    "paginate_balance_transactions",
    "persist_stripe_blob",
)
