"""eBay OAuth authorization-code flow.

This module implements the eBay sell.finances user-consent OAuth flow
end-to-end (configure your own eBay developer app keys via the connector
credentials):

  GET  /api/connectors/ebay/oauth/start?workspace_id=X&entity_id=Y
       -> 302 to eBay's authorize URL with a signed state token

  GET  /api/connectors/ebay/oauth/callback?code=Z&state=W
       -> exchange code for {access_token, refresh_token, ...}, encrypt + store
          in public.tenant_credentials (kind='ebay_finances'), then 302 to
          /connectors?ebay=connected

The state token carries (workspace_id, entity_id, nonce, expires_at) signed
with HMAC-SHA256(secret = the master encryption key, since it's already a
high-entropy server-only value). Single-use enforced via an in-process set so
the same state can't be replayed.

App credentials come from env (shared with Listing-Ops):
  EBAY_APP_ID         — eBay App ID (client_id)
  EBAY_CERT_ID        — eBay Cert ID (client_secret)
  EBAY_DEV_ID         — eBay Dev ID (informational; not used in OAuth)
  EBAY_REDIRECT_URI   — eBay RuName for the redirect (e.g. "Magic_Unicorn-...-xfngkob")

The actual https://accounting.magicunicorn.dev/api/connectors/ebay/oauth/callback
URL is what eBay redirects to AFTER the RuName→URL lookup on eBay's side, so
the RuName must be configured in the eBay developer dashboard to point at THIS
URL exactly.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from typing import Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from sqlalchemy import text

from app.auth import AuthContext, validate_bearer_token
from app.config import get_settings
from app.db import SessionLocal
from app.security.credentials import store_credential
from app.security.crypto import MASTER_KEY_PATH
from app.tenancy import WorkspaceContext, require_role, resolve_workspace


_log = logging.getLogger("acctops.ebay_oauth")

router = APIRouter(tags=["connectors:ebay"])

EBAY_AUTHORIZE_URL = "https://auth.ebay.com/oauth2/authorize"
EBAY_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_SANDBOX_AUTHORIZE_URL = "https://auth.sandbox.ebay.com/oauth2/authorize"
EBAY_SANDBOX_TOKEN_URL = "https://api.sandbox.ebay.com/identity/v1/oauth2/token"

DEFAULT_SCOPES = [
    "https://api.ebay.com/oauth/api_scope/sell.finances",
    "https://api.ebay.com/oauth/api_scope/sell.account.readonly",
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly",
]

STATE_TTL_SECONDS = 600  # 10 minutes
# In-memory single-use enforcement. A redeemed state is added here so a
# replay loses. Worker / multi-pod deployments only run a single instance of
# the app right now; if that changes we move this to the DB.
_REDEEMED_STATES: set[str] = set()
_REDEEMED_STATES_MAX = 1024


def _ebay_env() -> dict[str, str]:
    """Read eBay app config from env. Raises 503 with a clear message if missing.

    Listing-Ops already uses these env vars in production — we deliberately
    reuse the same app keys (same redirect URI, same RuName) so we don't have
    to register a new app and re-walk eBay's Application Growth Check.
    """
    app_id = os.getenv("EBAY_APP_ID", "").strip()
    cert_id = os.getenv("EBAY_CERT_ID", "").strip()
    ru_name = os.getenv("EBAY_REDIRECT_URI", "").strip()
    environment = os.getenv("EBAY_ENVIRONMENT", "production").strip().lower()
    if not app_id or not cert_id or not ru_name:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "eBay OAuth not configured. Set EBAY_APP_ID, EBAY_CERT_ID, "
                "EBAY_REDIRECT_URI in the app environment (see agents/_shared/"
                "ebay_connector.md)."
            ),
        )
    return {
        "app_id": app_id,
        "cert_id": cert_id,
        "ru_name": ru_name,
        "dev_id": os.getenv("EBAY_DEV_ID", "").strip(),
        "environment": environment if environment in ("production", "sandbox") else "production",
    }


def _state_secret() -> bytes:
    """Per-process state-signing key.

    Re-uses the master encryption key (already a high-entropy server-only
    secret; never leaves the host). Falls back to a per-process random secret
    only when the master key file is missing (dev mode) so unit tests that
    don't mount the secret still work.
    """
    try:
        return MASTER_KEY_PATH.read_bytes().strip(b"\n")[:32]
    except Exception:
        # Fall back to a per-process secret. State signed with it dies if the
        # process restarts — acceptable for a 10-minute TTL.
        global _STATE_FALLBACK_SECRET
        try:
            return _STATE_FALLBACK_SECRET  # type: ignore[name-defined]
        except NameError:
            _STATE_FALLBACK_SECRET = secrets.token_bytes(32)
            return _STATE_FALLBACK_SECRET


def _sign_state(payload: dict[str, Any]) -> str:
    """Build a URL-safe signed state token.

    Encoding: base64url(payload_json) + '.' + base64url(hmac_sha256(payload_json))
    """
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    sig = hmac.new(_state_secret(), payload_bytes, hashlib.sha256).digest()
    p64 = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode()
    s64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
    return f"{p64}.{s64}"


def _verify_state(token: str) -> dict[str, Any]:
    """Verify + parse a state token. Raises 400 on any malformity / forgery."""
    try:
        p64, s64 = token.split(".", 1)
        # Re-pad for urlsafe decode
        payload_bytes = base64.urlsafe_b64decode(p64 + "=" * ((4 - len(p64) % 4) % 4))
        sig = base64.urlsafe_b64decode(s64 + "=" * ((4 - len(s64) % 4) % 4))
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed OAuth state token",
        )
    expected = hmac.new(_state_secret(), payload_bytes, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid OAuth state signature",
        )
    try:
        payload = json.loads(payload_bytes.decode())
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OAuth state payload not JSON",
        )
    if int(payload.get("exp", 0)) < int(time.time()):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OAuth state expired (>10 min old). Restart the connect flow.",
        )
    if token in _REDEEMED_STATES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OAuth state already redeemed (single-use enforcement).",
        )
    return payload


def _mark_state_redeemed(token: str) -> None:
    if len(_REDEEMED_STATES) >= _REDEEMED_STATES_MAX:
        # Bounded set: drop the oldest by clearing half. The TTL covers
        # correctness; this only matters for memory under sustained pressure.
        for _ in range(_REDEEMED_STATES_MAX // 2):
            _REDEEMED_STATES.pop()
    _REDEEMED_STATES.add(token)


# ---------------------------------------------------------------------------
# /start — kick off consent
# ---------------------------------------------------------------------------


@router.get("/api/connectors/ebay/oauth/start")
async def ebay_oauth_start(
    workspace_id: UUID = Query(..., description="Workspace UUID to attach the credential to"),
    entity_id: UUID = Query(..., description="Entity UUID that will own this eBay revenue stream"),
    auth: AuthContext = Depends(validate_bearer_token),
) -> RedirectResponse:
    """Redirect the caller to eBay's consent page.

    Role floor: admin (storing a credential is admin-only, parity with /credentials).
    """
    # Manually resolve membership/role since the path has no {workspace_id}.
    from app.tenancy import _resolve_membership, role_at_least
    role, entity_scope, _hipaa, _slug = await _resolve_membership(
        auth.uc_uid, workspace_id, autoprovision=auth.may_autoprovision
    )
    if not role_at_least(role, "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"role '{role}' insufficient (needs 'admin' to connect a connector)",
        )
    if entity_scope is not None and entity_id not in entity_scope:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Entity outside this membership's scope",
        )

    cfg = _ebay_env()
    state_payload = {
        "wid": str(workspace_id),
        "eid": str(entity_id),
        "uid": auth.uc_uid,
        "nonce": secrets.token_urlsafe(16),
        "exp": int(time.time()) + STATE_TTL_SECONDS,
    }
    state_token = _sign_state(state_payload)

    base = (
        EBAY_AUTHORIZE_URL
        if cfg["environment"] == "production"
        else EBAY_SANDBOX_AUTHORIZE_URL
    )
    # eBay-ism: `redirect_uri` is the RuName (a developer-dashboard label),
    # not the actual https:// URL. eBay's redirect to the actual URL is
    # configured in the dashboard against the RuName.
    from urllib.parse import urlencode

    params = {
        "client_id": cfg["app_id"],
        "response_type": "code",
        "redirect_uri": cfg["ru_name"],
        "scope": " ".join(DEFAULT_SCOPES),
        "state": state_token,
        "prompt": "login",
    }
    return RedirectResponse(f"{base}?{urlencode(params)}", status_code=302)


# ---------------------------------------------------------------------------
# /callback — exchange code, encrypt + store
# ---------------------------------------------------------------------------


async def _exchange_code(
    code: str, *, app_id: str, cert_id: str, ru_name: str, environment: str
) -> dict[str, Any]:
    """Authorization-code -> tokens. Returns the full eBay token response."""
    token_url = EBAY_TOKEN_URL if environment == "production" else EBAY_SANDBOX_TOKEN_URL
    basic = base64.b64encode(f"{app_id}:{cert_id}".encode()).decode()
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            token_url,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": ru_name,
            },
        )
    if resp.status_code != 200:
        # Never log resp.text — could leak the auth code or eBay's internal error
        # response shape. The eBay docs page covers troubleshooting.
        _log.warning("ebay_oauth_token_exchange_failed status=%s", resp.status_code)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"eBay token exchange returned HTTP {resp.status_code}. "
                "Common causes: wrong RuName, wrong client_secret, or expired code."
            ),
        )
    try:
        data = resp.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="eBay token response was not JSON",
        )
    for required in ("access_token", "refresh_token", "expires_in"):
        if required not in data:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"eBay token response missing '{required}'",
            )
    return data


async def _fetch_ebay_user_handle(access_token: str, environment: str) -> str | None:
    """Best-effort: look up the seller username via /sell/account/v1/privilege.

    Used only to populate the UI's "Connected as <handle>" label. Failure is
    not fatal — we store the credential either way.
    """
    base = (
        "https://api.ebay.com"
        if environment == "production"
        else "https://api.sandbox.ebay.com"
    )
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # /sell/account/v1/privilege returns the seller's status/eligibility.
            # It doesn't always return the username; try it for sellerEligibleForB2BVAT
            # then fall back to /commerce/identity/v1/user if needed.
            resp = await client.get(f"{base}/commerce/identity/v1/user", headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("username") or data.get("userId")
            # Some token scopes don't include identity; try fulfillment for a hint.
            resp = await client.get(
                f"{base}/sell/fulfillment/v1/order?limit=1",
                headers={**headers, "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"},
            )
            if resp.status_code == 200:
                data = resp.json()
                orders = data.get("orders") or []
                if orders:
                    return orders[0].get("sellerId")
    except Exception as exc:
        _log.info("ebay_user_handle_lookup_failed: %s", exc.__class__.__name__)
    return None


@router.get("/api/connectors/ebay/oauth/callback")
async def ebay_oauth_callback(
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    error_description: str | None = Query(default=None),
) -> RedirectResponse:
    """eBay redirects here AFTER user grants consent.

    No bearer auth required — the user is bouncing back from eBay's UI in a
    plain browser session and the security comes from (a) the signed state
    token tying the callback to the exact workspace+entity that started the
    flow, and (b) the single-use TTL on the state token.

    NB: NextAuth-protected pages are usually behind /connectors etc; this
    endpoint lives under /api/connectors/ebay/oauth/callback and is left
    OUTSIDE the FastAPI bearer-token guard so eBay's third-party browser
    redirect can hit it. The state token's HMAC + workspace_id binding is
    what proves the caller actually initiated the flow.
    """
    settings = get_settings()
    base_redirect = settings.public_base_url.rstrip("/")

    if error:
        _log.info(
            "ebay_oauth_user_denied: %s (%s)",
            error,
            error_description or "no description",
        )
        return RedirectResponse(
            f"{base_redirect}/connectors?ebay_error={error}",
            status_code=302,
        )
    if not code or not state:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing 'code' or 'state' in OAuth callback",
        )

    payload = _verify_state(state)
    _mark_state_redeemed(state)

    workspace_id = UUID(payload["wid"])
    entity_id = UUID(payload["eid"])
    cfg = _ebay_env()

    # ---- 1. Exchange code -> tokens ----
    tokens = await _exchange_code(
        code,
        app_id=cfg["app_id"],
        cert_id=cfg["cert_id"],
        ru_name=cfg["ru_name"],
        environment=cfg["environment"],
    )
    access_token = tokens["access_token"]
    refresh_token = tokens["refresh_token"]
    expires_in = int(tokens["expires_in"])
    refresh_token_expires_in = int(tokens.get("refresh_token_expires_in", 0))
    granted_scopes = (tokens.get("scope") or "").split() or DEFAULT_SCOPES

    # ---- 2. Best-effort: fetch the seller username for UI display ----
    ebay_user_handle = await _fetch_ebay_user_handle(access_token, cfg["environment"])

    # ---- 3. Build the credential blob ----
    # Stored shape is the SAME blob the existing ebay-finances client expects
    # (client_id, client_secret, refresh_token, ru_name, environment) PLUS
    # the OAuth lifecycle metadata we need for auto-refresh + display:
    blob = {
        "client_id": cfg["app_id"],
        "client_secret": cfg["cert_id"],
        "refresh_token": refresh_token,
        "ru_name": cfg["ru_name"],
        "environment": cfg["environment"],
        # Lifecycle metadata (non-secret, useful for the UI + sync worker)
        "access_token": access_token,
        "access_token_expires_at": int(time.time()) + expires_in,
        "refresh_token_expires_at": (
            int(time.time()) + refresh_token_expires_in
            if refresh_token_expires_in > 0
            else None
        ),
        "granted_scopes": granted_scopes,
        "granted_at": int(time.time()),
        "ebay_user_handle": ebay_user_handle,
        "entity_id": str(entity_id),
    }

    # ---- 4. Persist (encrypted) ----
    # Label is "<eBay user> — entity <eid short>" so the credentials list page
    # shows what the connection actually points at.
    short_eid = str(entity_id)[:8]
    label = (
        f"{ebay_user_handle or 'eBay seller'} ({cfg['environment']}) — entity {short_eid}"
    )
    async with SessionLocal() as session:
        async with session.begin():
            await store_credential(
                session,
                workspace_id,
                "ebay_finances",
                label,
                json.dumps(blob),
                created_by_uc_uid=payload.get("uid"),
                expires_at=None,  # the blob carries its own expiry
            )

    # ---- 5. Redirect back to /connectors ----
    return RedirectResponse(
        f"{base_redirect}/connectors?ebay=connected"
        f"&seller={ebay_user_handle or 'connected'}",
        status_code=302,
    )


# ---------------------------------------------------------------------------
# /status — UI polling endpoint
# ---------------------------------------------------------------------------


from app.util.ttl_cache import TTLCache as _TTLCache  # noqa: E402

# eBay status is read on every dashboard navigation; cache it briefly so
# inter-screen nav reuses it instead of re-decrypting + re-querying the DB.
# Staleness is bounded by the TTL (a connect/disconnect shows within ~10s).
_EBAY_STATUS_CACHE = _TTLCache(10.0)


@router.get("/api/workspaces/{workspace_id}/connectors/ebay/status")
async def ebay_status(
    workspace_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    """Return the eBay connection state for the workspace dashboard.

    Read floor: viewer.

    Shape:
        {
          "configured": bool,
          "seller_handle": str | null,
          "environment": "production" | "sandbox" | null,
          "granted_scopes": [str],
          "granted_at_iso": str | null,
          "access_token_expires_at_iso": str | null,
          "refresh_token_expires_at_iso": str | null,
          "entity_id": str | null,
          "last_sync_at_iso": str | null,
          "last_sync_outcome": str | null,
          "recent_imports_count": int,
        }
    """
    require_role(workspace, "viewer")
    _ck = str(workspace_id)
    _hit = _EBAY_STATUS_CACHE.get(_ck)
    if _hit is not None:
        return _hit
    # Pull the credential metadata + decrypt the blob so we can show the UI.
    from app.security.credentials import get_credential_plaintext
    from datetime import datetime, timezone

    async with SessionLocal() as session:
        async with session.begin():
            plaintext = await get_credential_plaintext(
                session,
                workspace_id,
                "ebay_finances",
                requesting_service="ebay_status",
                actor_uc_uid=workspace.user_uc_uid,
                actor_agent_id=workspace.agent_id,
            )
    if not plaintext:
        return {"configured": False}
    try:
        blob = json.loads(plaintext)
    except json.JSONDecodeError:
        return {"configured": False}

    def _iso(ts: int | None) -> str | None:
        if not ts:
            return None
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()

    # Recent imports count (this calendar month) + last sync, best-effort.
    from app.workspace_session import workspace_session

    recent_imports = 0
    last_sync_at = None
    last_sync_outcome = None
    try:
        async with workspace_session(SessionLocal, workspace_id) as ws:
            r = (
                await ws.execute(
                    text(
                        """
                        SELECT count(*) AS c FROM journal_entry
                        WHERE source = 'ebay_finances'
                          AND posted_at >= date_trunc('month', now())
                        """
                    )
                )
            ).first()
            recent_imports = int(r.c or 0) if r else 0
            cs = (
                await ws.execute(
                    text(
                        """
                        SELECT last_sync_at, state FROM connector_state
                        WHERE connector_name = 'ebay_finances'
                        ORDER BY last_sync_at DESC NULLS LAST
                        LIMIT 1
                        """
                    )
                )
            ).first()
            if cs:
                last_sync_at = cs.last_sync_at.isoformat() if cs.last_sync_at else None
                state = cs.state or {}
                if isinstance(state, dict):
                    last_sync_outcome = state.get("last_outcome")
    except Exception as exc:
        _log.info("ebay_status_dashboard_lookup_failed: %s", exc.__class__.__name__)

    return _EBAY_STATUS_CACHE.put(_ck, {
        "configured": True,
        "seller_handle": blob.get("ebay_user_handle"),
        "environment": blob.get("environment", "production"),
        "granted_scopes": blob.get("granted_scopes") or [],
        "granted_at_iso": _iso(blob.get("granted_at")),
        "access_token_expires_at_iso": _iso(blob.get("access_token_expires_at")),
        "refresh_token_expires_at_iso": _iso(blob.get("refresh_token_expires_at")),
        "entity_id": blob.get("entity_id"),
        "last_sync_at_iso": last_sync_at,
        "last_sync_outcome": last_sync_outcome,
        "recent_imports_count": recent_imports,
    })


# ---------------------------------------------------------------------------
# /disconnect — wipe the credential
# ---------------------------------------------------------------------------


@router.delete(
    "/api/workspaces/{workspace_id}/connectors/ebay",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def ebay_disconnect(
    workspace_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> None:
    """Remove the eBay credential for this workspace."""
    require_role(workspace, "admin")
    # Find the cred_id then delete via the shared helper (which logs the access).
    async with SessionLocal() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.current_workspace', :wid, true)"),
                {"wid": str(workspace_id)},
            )
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT id FROM public.tenant_credentials
                        WHERE workspace_id = :wid AND connector = 'ebay_finances'
                        """
                    ),
                    {"wid": str(workspace_id)},
                )
            ).all()
    if not rows:
        return None
    from app.security.credentials import delete_credential

    async with SessionLocal() as session:
        async with session.begin():
            for row in rows:
                await delete_credential(
                    session,
                    workspace_id,
                    UUID(str(row.id)),
                    actor_uc_uid=workspace.user_uc_uid,
                )


__all__ = ("router",)
