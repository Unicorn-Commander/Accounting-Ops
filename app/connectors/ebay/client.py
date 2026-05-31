"""Async HTTP client for the eBay Sell Finances API.

Reads the encrypted credential blob from public.tenant_credentials (written
by app/api/ebay_oauth.py) and exposes high-level async methods:

  * list_payouts(start_date, end_date, limit)
  * list_transactions(payout_id, start_date, end_date, limit)
  * get_payout_summary(payout_id)

Auto-refreshes the ~2h access token using the long-lived refresh token. If
eBay returns 401 on a request we ALWAYS try a forced refresh + retry exactly
once; a second 401 propagates as EbayApiError (the refresh token is dead and
the user needs to reconnect).

OTEL: each high-level call is wrapped in a span tagged with the eBay method,
duration, and outcome. We never log the refresh token; access-token logging is
strictly first/last 4 chars.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import text

from app.db import SessionLocal
from app.security.credentials import get_credential_plaintext

_log = logging.getLogger("acctops.ebay_client")

EBAY_TOKEN_URL_PROD = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_TOKEN_URL_SANDBOX = "https://api.sandbox.ebay.com/identity/v1/oauth2/token"
EBAY_FINANCES_BASE_PROD = "https://apiz.ebay.com/sell/finances/v1"
EBAY_FINANCES_BASE_SANDBOX = "https://apiz.sandbox.ebay.com/sell/finances/v1"

# Marketplace ID lives in a header on most sell.* requests. US is default;
# override per-call only if/when we add multi-locale support.
DEFAULT_MARKETPLACE_ID = "EBAY_US"

# Buffer: refresh ~5 min before the actual expiry so a concurrent burst of
# calls doesn't all race the boundary.
ACCESS_TOKEN_REFRESH_BUFFER_S = 300


class EbayApiError(Exception):
    """Wrapping for any non-2xx response / network error from eBay."""

    def __init__(self, message: str, *, status_code: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        # Truncated for safety; body is for ops debugging, never logged in full.
        self.body = (body or "")[:500]


@dataclass
class EbayCredentialBlob:
    """In-memory representation of the JSON blob in tenant_credentials.

    Mutable in this dataclass form because we update `access_token` +
    `access_token_expires_at` on refresh; the changes are persisted back to
    the credential row by ``persist_token_refresh``.
    """

    workspace_id: UUID
    credential_id: UUID
    label: str
    client_id: str
    client_secret: str
    refresh_token: str
    environment: str  # "production" | "sandbox"
    ru_name: str | None = None
    entity_id: str | None = None
    access_token: str | None = None
    access_token_expires_at: int | None = None
    refresh_token_expires_at: int | None = None
    granted_scopes: list[str] = field(default_factory=list)
    granted_at: int | None = None
    ebay_user_handle: str | None = None
    last_refreshed_at: int | None = None

    def token_url(self) -> str:
        return EBAY_TOKEN_URL_SANDBOX if self.environment == "sandbox" else EBAY_TOKEN_URL_PROD

    def base_url(self) -> str:
        return (
            EBAY_FINANCES_BASE_SANDBOX
            if self.environment == "sandbox"
            else EBAY_FINANCES_BASE_PROD
        )

    def as_blob(self) -> dict[str, Any]:
        return {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
            "ru_name": self.ru_name,
            "environment": self.environment,
            "access_token": self.access_token,
            "access_token_expires_at": self.access_token_expires_at,
            "refresh_token_expires_at": self.refresh_token_expires_at,
            "granted_scopes": self.granted_scopes,
            "granted_at": self.granted_at,
            "ebay_user_handle": self.ebay_user_handle,
            "entity_id": self.entity_id,
            "last_refreshed_at": self.last_refreshed_at,
        }


async def load_ebay_credential(
    workspace_id: UUID,
    *,
    requesting_service: str,
    actor_uc_uid: str | None,
    actor_agent_id: str | None = None,
) -> EbayCredentialBlob | None:
    """Decrypt + parse the workspace's ebay_finances credential.

    Returns None if no credential exists. Raises CredentialDecryptionError if
    the blob is present but undecryptable (caller decides whether to surface
    or skip).
    """
    async with SessionLocal() as session:
        async with session.begin():
            # Fetch the id alongside the plaintext so we can write refreshes back.
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
                        WHERE workspace_id = :wid AND connector = 'ebay_finances'
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
                "ebay_finances",
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
        _log.warning("ebay_credential_blob_not_json workspace=%s", workspace_id)
        return None
    try:
        return EbayCredentialBlob(
            workspace_id=workspace_id,
            credential_id=UUID(row.id),
            label=row.label,
            client_id=blob["client_id"],
            client_secret=blob["client_secret"],
            refresh_token=blob["refresh_token"],
            environment=blob.get("environment", "production"),
            ru_name=blob.get("ru_name"),
            entity_id=blob.get("entity_id"),
            access_token=blob.get("access_token"),
            access_token_expires_at=blob.get("access_token_expires_at"),
            refresh_token_expires_at=blob.get("refresh_token_expires_at"),
            granted_scopes=blob.get("granted_scopes") or [],
            granted_at=blob.get("granted_at"),
            ebay_user_handle=blob.get("ebay_user_handle"),
            last_refreshed_at=blob.get("last_refreshed_at"),
        )
    except KeyError as exc:
        _log.warning("ebay_credential_blob_missing_field=%s workspace=%s", exc, workspace_id)
        return None


async def _persist_refreshed_blob(blob: EbayCredentialBlob) -> None:
    """Re-encrypt the updated blob into the same credential row.

    Called after an access-token refresh so future loads of this credential
    get a fresh, non-expired token without forcing another /token round trip.
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


def _redact(token: str | None) -> str:
    """Safe logging: first 4 + last 4 only, never the middle."""
    if not token:
        return "<none>"
    if len(token) <= 12:
        return "<short>"
    return f"{token[:4]}..{token[-4:]}"


@dataclass
class EbayFinancesClient:
    """Authenticated async client. Construct with `from_credential`."""

    blob: EbayCredentialBlob
    timeout_s: float = 20.0
    _http_factory: Any = None  # injected in tests; default = httpx.AsyncClient

    @classmethod
    def from_credential(cls, blob: EbayCredentialBlob) -> "EbayFinancesClient":
        return cls(blob=blob)

    # -------- token refresh --------------------------------------------------

    def _http_client(self) -> httpx.AsyncClient:
        if self._http_factory is None:
            return httpx.AsyncClient(timeout=self.timeout_s)
        return self._http_factory()

    async def ensure_access_token(self, *, force: bool = False) -> str:
        """Return a valid access token, refreshing if expired (or forced)."""
        now = int(time.time())
        token = self.blob.access_token
        exp = self.blob.access_token_expires_at or 0
        if (
            not force
            and token
            and exp - now > ACCESS_TOKEN_REFRESH_BUFFER_S
        ):
            return token
        # Refresh.
        basic = base64.b64encode(
            f"{self.blob.client_id}:{self.blob.client_secret}".encode()
        ).decode()
        async with self._http_client() as client:
            resp = await client.post(
                self.blob.token_url(),
                headers={
                    "Authorization": f"Basic {basic}",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self.blob.refresh_token,
                    # Re-request the SAME scopes the user originally granted.
                    "scope": " ".join(self.blob.granted_scopes) or
                             "https://api.ebay.com/oauth/api_scope/sell.finances",
                },
            )
        if resp.status_code != 200:
            _log.warning(
                "ebay_access_token_refresh_failed status=%s old_token=%s",
                resp.status_code,
                _redact(token),
            )
            raise EbayApiError(
                f"eBay token refresh returned HTTP {resp.status_code}",
                status_code=resp.status_code,
                body=resp.text,
            )
        data = resp.json()
        new_token = data["access_token"]
        new_exp = int(time.time()) + int(data.get("expires_in", 7200))
        self.blob.access_token = new_token
        self.blob.access_token_expires_at = new_exp
        self.blob.last_refreshed_at = int(time.time())
        # Persist back so subsequent calls don't redo the refresh.
        try:
            await _persist_refreshed_blob(self.blob)
        except Exception as exc:
            # Not fatal — we have a valid in-memory token. Log + carry on.
            _log.warning(
                "ebay_token_persist_failed: %s (token still valid in memory)",
                exc.__class__.__name__,
            )
        _log.info(
            "ebay_access_token_refreshed new_token=%s expires_in=%ds",
            _redact(new_token),
            new_exp - int(time.time()),
        )
        return new_token

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send one request, refreshing the token + retrying once on 401."""
        url = f"{self.blob.base_url()}{path}"
        token = await self.ensure_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "X-EBAY-C-MARKETPLACE-ID": DEFAULT_MARKETPLACE_ID,
        }
        async with self._http_client() as client:
            resp = await client.request(method, url, headers=headers, params=params or {})

        if resp.status_code == 401:
            _log.info("ebay_401_forcing_refresh path=%s", path)
            token = await self.ensure_access_token(force=True)
            headers["Authorization"] = f"Bearer {token}"
            async with self._http_client() as client:
                resp = await client.request(method, url, headers=headers, params=params or {})

        if resp.status_code == 403:
            raise EbayApiError(
                f"eBay returned 403 for {path}. "
                "The user is not authorized for this scope/resource.",
                status_code=403,
                body=resp.text,
            )
        if resp.status_code >= 400:
            raise EbayApiError(
                f"eBay returned HTTP {resp.status_code} for {path}",
                status_code=resp.status_code,
                body=resp.text,
            )
        try:
            return resp.json()
        except ValueError:
            return {}

    # -------- high-level methods --------------------------------------------

    async def list_payouts(
        self,
        *,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        limit: int = 200,
        max_pages: int = 50,
    ) -> list[dict[str, Any]]:
        """Page through GET /payout.

        eBay supports a `filter` query param: `payoutDate:[2026-01-01T00:00:00.000Z..]`.
        Returns the flat list of payout dicts in eBay's shape.
        """
        params: dict[str, Any] = {"limit": min(limit, 200), "offset": 0}
        if start_date or end_date:
            params["filter"] = _build_date_filter("payoutDate", start_date, end_date)
        out: list[dict[str, Any]] = []
        pages = 0
        while True:
            data = await self._request("GET", "/payout", params=params)
            page = data.get("payouts") or []
            if not page:
                break
            out.extend(page)
            pages += 1
            total = int(data.get("total") or 0)
            params["offset"] += len(page)
            if pages >= max_pages:
                break
            if total and params["offset"] >= total:
                break
            if len(out) >= limit:
                out = out[:limit]
                break
        return out

    async def list_transactions(
        self,
        *,
        payout_id: str | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        limit: int = 200,
        max_pages: int = 50,
    ) -> list[dict[str, Any]]:
        """Page through GET /transaction. Either by payout_id OR by date range."""
        params: dict[str, Any] = {"limit": min(limit, 200), "offset": 0}
        filters: list[str] = []
        if payout_id:
            filters.append(f"payoutId:{{{payout_id}}}")
        if start_date or end_date:
            filters.append(_build_date_filter("transactionDate", start_date, end_date))
        if filters:
            params["filter"] = ",".join(filters)
        out: list[dict[str, Any]] = []
        pages = 0
        while True:
            data = await self._request("GET", "/transaction", params=params)
            page = data.get("transactions") or []
            if not page:
                break
            out.extend(page)
            pages += 1
            total = int(data.get("total") or 0)
            params["offset"] += len(page)
            if pages >= max_pages:
                break
            if total and params["offset"] >= total:
                break
            if len(out) >= limit:
                out = out[:limit]
                break
        return out

    async def get_payout_summary(
        self,
        payout_id: str | None = None,
        *,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> dict[str, Any]:
        """GET /payout_summary — returns processing/funded totals for the seller.

        If neither payout_id nor a date range is given, eBay returns the
        summary for the current period.
        """
        params: dict[str, Any] = {}
        filters: list[str] = []
        if payout_id:
            filters.append(f"payoutId:{{{payout_id}}}")
        if start_date or end_date:
            filters.append(_build_date_filter("payoutDate", start_date, end_date))
        if filters:
            params["filter"] = ",".join(filters)
        return await self._request("GET", "/payout_summary", params=params)


def _build_date_filter(
    field_name: str, start: datetime | None, end: datetime | None
) -> str:
    """Build eBay's date-range filter syntax: `field:[start..end]`."""
    def iso(d: datetime) -> str:
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    start_s = iso(start) if start else ""
    end_s = iso(end) if end else ""
    return f"{field_name}:[{start_s}..{end_s}]"


__all__ = (
    "EbayApiError",
    "EbayCredentialBlob",
    "EbayFinancesClient",
    "load_ebay_credential",
)
