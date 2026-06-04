"""Keycloak OAuth 2.1 + OIDC bearer token validation with JWKS caching.

The `act` claim (RFC 8693) carries the acting agent identity when an agent
calls on behalf of a delegating user. uc_uid is the canonical user identifier
across the Unicorn ecosystem (preferred over `preferred_username` per
the OIDC-user-id canary lesson).

Service tokens: if the presented bearer token matches a registered service
token from ACCTOPS_SERVICE_TOKENS env (JSON map of token→{uc_uid, agent_id,
…}), we synthesize an AuthContext for the impersonated user. This is the
path used by long-running internal workers (receipt-reconciler, etc.) that
operate on behalf of a workspace owner without a Keycloak session.

H2 (2026-05-28): service tokens may carry optional scope fields that
constrain the token's reach below the impersonated user's full membership:

  - ``workspace_ids``: list of workspace UUIDs the token may access. If set
    and the request's workspace_id is not in the list, resolve_workspace
    raises 403 (independent of the user's membership).
  - ``entity_ids``: list of entity UUIDs the token may access. Intersected
    with the user's membership entity_scope; an empty intersection raises
    403. None = inherit the user's full membership.
  - ``role_floor``: caps the token's effective role at this value (e.g.
    ``viewer``). The token gets ``min(membership_role, role_floor)``.

A service token WITHOUT these fields keeps full impersonation reach for
backward compatibility — useful for trusted internal workers during the
transition. New tokens SHOULD declare the minimum scope they need.

PATs (2026-05-31): Personal Access Tokens are user-minted long-lived bearers
formatted ``aops_pat_<43 chars>``. They sit BEFORE the Keycloak JWT branch in
the chain: prefix-match → sha256 hash compare → resolve owner uc_uid. PATs
are owner-scoped to a uc_uid; the optional ``workspace_scope`` field travels
on the AuthContext via service_scope.workspace_ids so resolve_workspace
applies the same gate that service tokens get.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from typing import Annotated, Any
from uuid import UUID

import httpx
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

try:
    from jose import JWTError, jwt
except ImportError:  # pragma: no cover - exercised only without python-jose installed
    JWTError = Exception  # type: ignore[assignment]
    jwt = None  # type: ignore[assignment]

from app.config import get_settings

bearer = HTTPBearer(auto_error=False)


# Role hierarchy for the role_floor cap. Lower index = lower privilege.
# Must stay in sync with the ROLE_ORDER used in app.tenancy.require_role.
# Multi-persona expansion (2026-05-30): see app/tenancy.py for the rationale
# behind the ordering. Existing role_floor values keep working unchanged.
_ROLE_ORDER = (
    "viewer",
    "client_user",
    "bookkeeper",
    "accountant",
    "cpa",
    "admin",
    "owner",
    "client_owner",
    "platform_admin",
)


@dataclass(frozen=True)
class ServiceScope:
    """Per-service-token scope constraints applied at resolve_workspace.

    Each field is "additive restriction" — if None, the token inherits
    whatever the impersonated user has in the membership row. If set, it
    further restricts the effective reach.
    """

    workspace_ids: frozenset[UUID] | None = None
    entity_ids: frozenset[UUID] | None = None
    role_floor: str | None = None


@dataclass(frozen=True)
class AuthContext:
    uc_uid: str
    agent_id: str | None
    scopes: tuple[str, ...]
    claims: dict[str, Any]
    # H2: service-token scope constraints. None for Keycloak-validated user
    # tokens (no constraint — the user's full membership applies). Set when
    # the bearer matched a service token that declared scope fields.
    service_scope: ServiceScope | None = None


# JWKS cache: avoid refetching Keycloak's signing keys on every request.
# TTL chosen so a rotated key propagates within 5 minutes of next request.
_JWKS_CACHE_TTL_SEC = 300
_jwks_cache: dict[str, Any] = {"keys": None, "fetched_at": 0.0}


async def _fetch_jwks() -> dict[str, Any]:
    """Fetch the JWKS from Keycloak. Cached for _JWKS_CACHE_TTL_SEC seconds.

    Cloudflare's default WAF blocks the default httpx/Python User-Agent
    with error 1010 ("banned based on browser signature"). We set an
    explicit UA so server-to-server traffic passes through.
    """
    now = time.time()
    if (
        _jwks_cache["keys"] is not None
        and (now - _jwks_cache["fetched_at"]) < _JWKS_CACHE_TTL_SEC
    ):
        return _jwks_cache["keys"]
    settings = get_settings()
    async with httpx.AsyncClient(
        timeout=5,
        headers={"User-Agent": "accounting-ops/0.1 (+https://accounting-ops.magicunicorn.dev)"},
    ) as client:
        response = await client.get(
            f"{settings.keycloak_realm_url}/protocol/openid-connect/certs"
        )
        response.raise_for_status()
        keys = response.json()
    _jwks_cache["keys"] = keys
    _jwks_cache["fetched_at"] = now
    return keys


async def _fetch_jwks_force_refresh() -> dict[str, Any]:
    """Bypass the cache. Used when an unknown kid is seen (rotation)."""
    _jwks_cache["keys"] = None
    return await _fetch_jwks()


_service_tokens_cache: dict[str, dict[str, Any]] | None = None


def _parse_uuid_list(value: Any) -> frozenset[UUID] | None:
    """Parse a JSON list-of-strings into a frozenset of UUIDs.

    Returns None on missing/None (= unbounded). Returns an empty frozenset on
    an explicit empty list (= deny-all, intentional). Skips invalid UUIDs.
    """
    if value is None:
        return None
    if not isinstance(value, list):
        return None
    out: set[UUID] = set()
    for v in value:
        try:
            out.add(UUID(str(v)))
        except (ValueError, TypeError):
            # Skip malformed entries — better to drop than to widen scope.
            continue
    return frozenset(out)


def _parse_role_floor(value: Any) -> str | None:
    """Validate a role_floor string against the known hierarchy. Drop unknowns."""
    if value is None or not isinstance(value, str):
        return None
    value = value.strip().lower()
    if value in _ROLE_ORDER:
        return value
    return None


def _allow_unscoped_service_token() -> bool:
    """Dev-only escape hatch for unscoped service tokens (audit 2026-05-31 Bug 7).

    Honors ``ACCTOPS_ALLOW_UNSCOPED_SERVICE_TOKEN`` env (any truthy value: ``1``,
    ``true``, ``yes``). Default OFF — production REQUIRES every service token to
    declare at least one of ``workspace_ids`` / ``entity_ids`` / ``role_floor``.
    """
    return os.getenv("ACCTOPS_ALLOW_UNSCOPED_SERVICE_TOKEN", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _load_service_tokens() -> dict[str, dict[str, Any]]:
    """Parse ACCTOPS_SERVICE_TOKENS env var.

    Format::

        {
          "<token>": {
            "uc_uid": "aaron",
            "agent_id": "receipt-reconciler",
            # H2 scope fields (REQUIRED in production — at least one must be set):
            "workspace_ids": ["<wid>", "<wid>"],   // None = all workspaces the user is in
            "entity_ids":    ["<eid>"],            // None = all entities in membership
            "role_floor":    "accountant"          // None = inherit membership role
          }
        }

    Token strings must be at least 32 chars (entropy) and are compared with
    ``secrets.compare_digest``. Returns {} if unset or malformed.

    Audit 2026-05-31 Bug 7: in production a token must declare at least one of
    (workspace_ids, entity_ids, role_floor) — an entry omitting all three is
    REJECTED at load time (dropped from the cache + WARN). The dev escape hatch
    ``ACCTOPS_ALLOW_UNSCOPED_SERVICE_TOKEN=true`` re-permits unscoped tokens
    (still logs a warning). This closes the "leaked worker token becomes
    owner/admin everywhere the user can reach" footgun; tokens that need the
    receipt-reconciler-style broad reach must spell it out explicitly.
    """
    global _service_tokens_cache
    if _service_tokens_cache is not None:
        return _service_tokens_cache
    raw = os.getenv("ACCTOPS_SERVICE_TOKENS", "").strip()
    if not raw:
        _service_tokens_cache = {}
        return _service_tokens_cache
    allow_unscoped = _allow_unscoped_service_token()
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("ACCTOPS_SERVICE_TOKENS must be a JSON object")
        tokens: dict[str, dict[str, Any]] = {}
        for token, ident in parsed.items():
            if not isinstance(ident, dict):
                continue
            uc_uid = ident.get("uc_uid")
            if not uc_uid or len(token) < 32:
                continue
            wsids = _parse_uuid_list(ident.get("workspace_ids"))
            eids = _parse_uuid_list(ident.get("entity_ids"))
            floor = _parse_role_floor(ident.get("role_floor"))
            agent_id = ident.get("agent_id") or "service"
            has_any_scope = (
                wsids is not None or eids is not None or floor is not None
            )
            if not has_any_scope:
                if not allow_unscoped:
                    # Production: refuse to load — log a stable, grep-able warn
                    # naming the agent_id so operators can see which token to fix.
                    _log.warning(
                        "[acctops.auth] REJECTING unscoped service token for "
                        "agent_id=%r (uc_uid=%r) — at least one of "
                        "workspace_ids, entity_ids, role_floor must be set "
                        "(audit 2026-05-31 Bug 7). Set "
                        "ACCTOPS_ALLOW_UNSCOPED_SERVICE_TOKEN=true to override "
                        "in dev only.",
                        agent_id,
                        uc_uid,
                    )
                    continue
                # Dev (flag set): allow + warn so dev operators see the smell.
                _log.warning(
                    "[acctops.auth] ALLOWING unscoped service token for "
                    "agent_id=%r (uc_uid=%r) — "
                    "ACCTOPS_ALLOW_UNSCOPED_SERVICE_TOKEN is set. "
                    "Do NOT use this in production.",
                    agent_id,
                    uc_uid,
                )
            tokens[token] = {
                "uc_uid": uc_uid,
                "agent_id": agent_id,
                "workspace_ids": wsids,
                "entity_ids": eids,
                "role_floor": floor,
            }
        _service_tokens_cache = tokens
        return tokens
    except (ValueError, TypeError):
        _service_tokens_cache = {}
        return _service_tokens_cache


def _match_service_token(presented: str) -> dict[str, Any] | None:
    """Constant-time compare against registered service tokens. None on miss."""
    for token, ident in _load_service_tokens().items():
        if secrets.compare_digest(token, presented):
            return ident
    return None


_log = logging.getLogger("acctops.auth")


def _extract_brigade_scopes(claims: dict[str, Any]) -> tuple[str, ...]:
    """Pull Brigade-style scopes from a verified-claims dict, tolerantly.

    Brigade's contract (see ``agents/_shared/brigade_federation.md``) accepts
    EITHER:

      * ``scopes`` (preferred): a JSON ARRAY of strings — order preserved.
      * ``scope`` (OIDC default): a SPACE-DELIMITED STRING — split on whitespace.

    Precedence: when BOTH are present, the plural ``scopes`` wins (Brigade's
    preferred form). When only ``scope`` is present, fall through to the OIDC
    parse. When neither is present (or both are None/empty), the result is an
    empty tuple — downstream scope checks in ``app/authz`` treat that as "no
    scope granted" and refuse anything that requires a scope.

    Safety:
      * A malformed ``scopes`` (not a list, contains non-string elements) is
        tolerated by ``str()``-coercing each element and dropping plain ``None``
        entries; never raises. A wholly-wrong shape (e.g. ``scopes: 42``) falls
        back to the ``scope`` branch, which itself never raises.
      * Returning an empty tuple is the SAFE default — narrower than the
        caller's intent, never wider. Subset-validation downstream catches
        anything that needs an explicit grant.
    """
    plural = claims.get("scopes")
    if isinstance(plural, list):
        out: list[str] = []
        for item in plural:
            if item is None:
                continue
            out.append(str(item))
        return tuple(out)
    # No usable plural; try the singular space-delimited form.
    singular = claims.get("scope")
    if isinstance(singular, str) and singular:
        return tuple(singular.split())
    if isinstance(singular, list):
        # Some callers may put a list under the singular key too — be tolerant.
        return tuple(str(s) for s in singular if s is not None)
    return ()


# ---------------------------------------------------------------------------
# PAT verification
# ---------------------------------------------------------------------------
# Imported lazily inside the helpers to avoid a hard import-time dependency on
# the DB layer at module load (so the auth module stays usable in test bodies
# that patch in mock sessions, mirroring the pattern used by _stamp_audit_actor
# for OTel).


def _looks_like_pat_token(token: str) -> bool:
    """Cheap pre-check before hitting the DB."""
    from app.security.pat import looks_like_pat

    return looks_like_pat(token)


async def _try_authenticate_pat(token: str) -> AuthContext | None:
    """Resolve a presented bearer to an AuthContext if it's a valid live PAT.

    Returns None on any kind of failure (no match, revoked, expired, DB
    blip). Never raises — the caller decides whether None means 401 or
    fall-through.
    """
    from app.db import SessionLocal
    from app.security.pat import (
        find_active_pat_by_token,
    )

    try:
        async with SessionLocal() as session:
            async with session.begin():
                hit = await find_active_pat_by_token(session, token)
    except Exception as exc:  # pragma: no cover - DB failure → fall through
        _log.warning("PAT lookup failed: %s", exc.__class__.__name__)
        return None
    if hit is None:
        return None
    pat, _owner_id, owner_uc_uid = hit

    # PAT scope: mirror workspace_scope into ServiceScope.workspace_ids so the
    # tenancy layer applies its existing per-workspace gate. The owner's full
    # entity_scope and role still come from workspace_membership — a PAT is
    # owner-scoped, not below-owner-scoped.
    svc_scope: ServiceScope | None = None
    if pat.workspace_scope is not None:
        svc_scope = ServiceScope(workspace_ids=frozenset(pat.workspace_scope))

    ctx = AuthContext(
        uc_uid=owner_uc_uid,
        agent_id=None,  # PATs are NOT agent calls — no RFC 8693 `act`
        scopes=tuple(pat.scopes),
        claims={
            "sub": owner_uc_uid,
            "uc_uid": owner_uc_uid,
            "pat_id": str(pat.id),
            "pat_prefix": pat.secret_prefix,
        },
        service_scope=svc_scope,
    )

    # Fire-and-forget bump. We deliberately open a NEW session so any failure
    # here can't poison the auth path's transaction. asyncio.create_task fires
    # it without awaiting; if it crashes, the auth still succeeds.
    import asyncio

    asyncio.create_task(_touch_pat_last_used(pat.id))
    return ctx


async def _touch_pat_last_used(pat_id) -> None:  # pragma: no cover - background
    from app.db import SessionLocal
    from app.security.pat import touch_last_used

    try:
        async with SessionLocal() as session:
            async with session.begin():
                await touch_last_used(session, pat_id)
    except Exception as exc:
        _log.debug("PAT last_used_at bump failed: %s", exc.__class__.__name__)


async def authenticate_token(token: str) -> AuthContext:
    """Validate a raw bearer token string and return its AuthContext.

    This is the transport-agnostic core of bearer authentication. The FastAPI
    dependency ``validate_bearer_token`` is a thin wrapper around it; the MCP
    sub-app (mounted under /mcp, which bypasses FastAPI dependencies) calls this
    directly from its ASGI auth middleware. Both paths therefore validate
    identically — there is exactly one token-validation implementation.

    Auth strategy chain (first match wins; same AuthContext result type):
      1. Service token (fast-path; shared-secret for internal workers)
      2. Keycloak-issued bearer (the user/agent flow)
      3. Brigade-issued JWT (federated MCP gateway; ed25519 / RS256)

    Raises HTTPException(401) on any failure (so the FastAPI wrapper propagates
    it unchanged; the MCP middleware translates the status into a JSON 401).
    """
    # Service token fast-path: long-lived shared-secret tokens for internal
    # workers. Bypass JWT validation; impersonate the configured uc_uid.
    # H2: any declared scope constraints travel with the AuthContext and are
    # applied by resolve_workspace below.
    service_ident = _match_service_token(token)
    if service_ident is not None:
        wsids = service_ident.get("workspace_ids")
        eids = service_ident.get("entity_ids")
        floor = service_ident.get("role_floor")
        scope: ServiceScope | None = None
        if wsids is not None or eids is not None or floor is not None:
            scope = ServiceScope(
                workspace_ids=wsids, entity_ids=eids, role_floor=floor
            )
        ctx = AuthContext(
            uc_uid=service_ident["uc_uid"],
            agent_id=service_ident["agent_id"],
            scopes=("service",),
            claims={"sub": service_ident["uc_uid"], "uc_uid": service_ident["uc_uid"]},
            service_scope=scope,
        )
        _stamp_audit_actor(ctx)
        return ctx

    # PAT fast-path: user-minted personal access tokens (aops_pat_*). Look up
    # by indexed prefix → constant-time hash compare. The owner's uc_uid
    # populates AuthContext so downstream membership resolution treats the
    # request as a regular user call. If a PAT carries a workspace_scope, we
    # mirror it into ServiceScope.workspace_ids so resolve_workspace applies
    # the same gate it already enforces for scoped service tokens (no new
    # plumbing in tenancy.py). A successful match is fire-and-forget about
    # last_used_at — never block the request on that bookkeeping.
    if _looks_like_pat_token(token):
        pat_ctx = await _try_authenticate_pat(token)
        if pat_ctx is not None:
            _stamp_audit_actor(pat_ctx)
            return pat_ctx
        # Looked like a PAT but didn't validate → 401 immediately. Falling
        # through to JWT would be wasted work and confusing logs (JWT decode
        # would reject an obviously-non-JWT body).
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked personal access token",
        )

    if jwt is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="python-jose is required for token validation",
        )

    # Brigade-issued token path — verifier returns None when the feature is OFF
    # (BRIGADE_TRUSTED_ISSUER unset / unreachable / token isn't Brigade-issued).
    # In those cases we fall through to the Keycloak path below unchanged.
    try:
        from app.security.brigade_jwt import verify_brigade_jwt
    except ImportError:  # pragma: no cover - module must exist
        verify_brigade_jwt = None  # type: ignore[assignment]
    if verify_brigade_jwt is not None:
        try:
            brigade_claims = await verify_brigade_jwt(token)
        except Exception as exc:  # pragma: no cover - hardening only
            _log.info("brigade verifier raised %s, falling through to Keycloak", exc)
            brigade_claims = None
        if brigade_claims is not None:
            b_uc_uid = brigade_claims.get("uc_uid") or brigade_claims.get("sub")
            # Brigade mints sub = the customer's EMAIL today; AO keys accounts on
            # the opaque uc_uid. Translate an email-shaped identifier to the
            # canonical uc_uid via public.user_account.email (mirrors project-ops's
            # fallback). Unresolved → b_uc_uid becomes None and we fall through to
            # the 401 path: Brigade can't conjure an AO identity by minting a JWT.
            # Forward-compatible: an opaque sub skips this and matches directly.
            if isinstance(b_uc_uid, str) and "@" in b_uc_uid:
                b_uc_uid = await _uc_uid_for_email(b_uc_uid)
            if b_uc_uid:
                b_act = brigade_claims.get("act") or {}
                b_agent_id: str | None = None
                if isinstance(b_act, dict):
                    b_agent_id = b_act.get("sub") or b_act.get("client_id")
                # Brigade may carry scopes as a SPACE-DELIMITED STRING under
                # ``scope`` (OIDC default) OR as a JSON ARRAY under ``scopes``
                # (Brigade's preferred plural form). Tolerate either — both are
                # parsed into the same ordered tuple. The plural ``scopes`` wins
                # when both are present (Brigade-preferred semantics). Malformed
                # claims (non-string elements, non-list/string values) degrade to
                # an empty tuple rather than raising — downstream subset checks
                # in app/authz treat empty as "no scope granted". See
                # ``agents/_shared/brigade_federation.md`` for the contract.
                b_scopes = _extract_brigade_scopes(brigade_claims)
                ctx = AuthContext(
                    uc_uid=b_uc_uid,
                    agent_id=b_agent_id,
                    scopes=b_scopes,
                    claims=brigade_claims,
                )
                _stamp_audit_actor(ctx)
                return ctx

    settings = get_settings()
    try:
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        if not kid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Token missing kid"
            )
        jwks = await _fetch_jwks()
        key = next((k for k in jwks["keys"] if k["kid"] == kid), None)
        if key is None:
            # Key rotation: force refresh once before giving up.
            jwks = await _fetch_jwks_force_refresh()
            key = next((k for k in jwks["keys"] if k["kid"] == kid), None)
            if key is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Unknown signing key",
                )
        # Strictly validate signature + issuer + expiry here. We do our OWN
        # audience check below against the configurable accepted_audience, so
        # `verify_aud` stays off in jwt.decode: Keycloak routinely stamps
        # aud="account" on tokens, and jose's built-in check has no notion of
        # "also accept via resource_access". (Signature/issuer/exp are NOT
        # weakened — they are enforced exactly as before.)
        claims = jwt.decode(
            token,
            key,
            algorithms=[header["alg"]],
            issuer=str(settings.keycloak_realm_url),
            options={"verify_at_hash": False, "verify_aud": False},
        )
        # OAuth resource-server audience validation (finding H3). A valid uchub
        # token is authorized for THIS resource server iff its configured
        # audience (settings.accepted_audience, default = our client id) is
        # present in the `aud` claim (string OR list) OR appears as a client key
        # under `resource_access`. Agent/Brigade clients carry it via a Keycloak
        # Audience mapper, so a token minted by a different client is accepted
        # when — and only when — it is explicitly scoped to this resource. We no
        # longer key off azp: that would reject valid cross-client tokens (H3).
        expected_aud = settings.accepted_audience
        aud = claims.get("aud")
        aud_list = aud if isinstance(aud, list) else [aud] if aud else []
        resource_access = claims.get("resource_access")
        in_resource_access = (
            isinstance(resource_access, dict) and expected_aud in resource_access
        )
        if expected_aud not in aud_list and not in_resource_access:
            _log.warning(
                "[acctops.auth] token rejected: aud=%r / resource_access keys=%r "
                "do not include required audience %r",
                aud,
                list(resource_access) if isinstance(resource_access, dict) else None,
                expected_aud,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Token audience does not include this resource server",
            )
    except HTTPException as http_exc:
        # Keycloak rejected the token. For 401s (token-recognition failures —
        # missing kid, unknown kid, signature failure) we fall through to
        # Brigade. For 403s (audience mismatch — token IS a valid Keycloak
        # token but not for this resource), propagate immediately: a 403 means
        # the caller authenticated successfully on the Keycloak path, just to
        # the wrong resource; trying Brigade then would let them retry as a
        # different identity which is wrong.
        if http_exc.status_code != 401:
            raise
        brigade_ctx = await _try_brigade(token)
        if brigade_ctx is not None:
            return brigade_ctx
        raise
    except (JWTError, httpx.HTTPError, KeyError) as exc:
        # Keycloak verifier couldn't authenticate this token. Before giving up,
        # try the Brigade JWT verifier — it covers a different issuer + audience
        # + signing scheme. A success here returns a clean AuthContext; a miss
        # falls through to the 401 below.
        brigade_ctx = await _try_brigade(token)
        if brigade_ctx is not None:
            return brigade_ctx
        _log.info("JWT validation failed: %s: %s", exc.__class__.__name__, exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid bearer token"
        ) from exc

    uc_uid = claims.get("uc_uid") or claims.get("sub")
    if not uc_uid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token lacks uc_uid/sub"
        )

    # RFC 8693 `act` claim: acting agent on behalf of delegating user.
    act = claims.get("act") or {}
    agent_id: str | None = None
    if isinstance(act, dict):
        agent_id = act.get("sub") or act.get("client_id")
    scope = tuple(str(claims.get("scope", "")).split())
    ctx = AuthContext(uc_uid=uc_uid, agent_id=agent_id, scopes=scope, claims=claims)
    _stamp_audit_actor(ctx)
    return ctx


async def validate_bearer_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> AuthContext:
    """FastAPI dependency: extract the bearer credential and validate it.

    Thin wrapper over ``authenticate_token`` so HTTP routes and the mounted MCP
    sub-app share one validation path.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token"
        )
    return await authenticate_token(credentials.credentials)


async def _uc_uid_for_email(email: str) -> str | None:
    """Resolve an email to its canonical uc_uid via public.user_account.

    Used only by the Brigade path: Brigade mints ``sub = email`` today, while AO
    keys accounts on the opaque uc_uid. Active accounts only; returns None (→ the
    caller fails closed) on no match or any DB error. Case-insensitive; if more
    than one active row shares an email, the most-recently-seen wins.
    """
    from sqlalchemy import text

    from app.db import SessionLocal

    try:
        async with SessionLocal() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT uc_uid FROM public.user_account "
                        "WHERE lower(email) = lower(:email) AND is_active "
                        "ORDER BY last_seen_at DESC NULLS LAST LIMIT 1"
                    ),
                    {"email": email},
                )
            ).first()
    except Exception as exc:  # pragma: no cover - DB blip → fail closed
        _log.warning("brigade email→uc_uid lookup failed: %s", exc.__class__.__name__)
        return None
    return row[0] if row and row[0] else None


async def _try_brigade(token: str) -> AuthContext | None:
    """Try the Brigade JWT verifier. Return an AuthContext on success, None on miss.

    The Brigade ``sub`` claim is the federated user id. We resolve it the same
    way Keycloak's uc_uid resolves — by looking up (or auto-provisioning) the
    public.user_account row. We DO NOT auto-provision here: if the sub doesn't
    resolve to a known uc_uid, fail closed (None) so Brigade can't create
    accounts on this side just by minting a JWT. Brigade is expected to map its
    user identifier to an established uc_uid before minting; the manifest's
    auth.federated_auth contract surfaces this explicitly.
    """
    try:
        from app.security.brigade_jwt import verify_brigade_jwt
    except ImportError:  # pragma: no cover - defensive
        return None

    claims = await verify_brigade_jwt(token)
    if claims is None:
        return None

    # The sub claim IS the uc_uid in Brigade's contract. If Brigade chooses to
    # use a different field (e.g. uc_uid claim or org_id/user_id), prefer
    # claims['uc_uid'] when present; otherwise fall back to sub. Either way,
    # the value must resolve to an existing user_account in the workspace's
    # tenancy layer for any tool call to succeed.
    uc_uid = claims.get("uc_uid") or claims.get("sub")
    if not isinstance(uc_uid, str) or not uc_uid:
        return None

    # Brigade mints sub = the customer's EMAIL today (its shared identifier; it
    # will upgrade to the opaque uchub sub once it stores keycloakId at OIDC
    # login). AO keys accounts on the opaque uc_uid, not email — so when the
    # Brigade identifier is email-shaped, translate it to the canonical uc_uid via
    # public.user_account.email (mirrors project-ops's email fallback). Forward-
    # compatible: once Brigade mints the opaque sub this branch is skipped and
    # uc_uid matches directly. Fail CLOSED when the email maps to no active
    # account — Brigade must not conjure an AO identity just by minting a JWT.
    if "@" in uc_uid:
        resolved = await _uc_uid_for_email(uc_uid)
        if not resolved:
            _log.info("brigade sub email did not resolve to an active user_account")
            return None
        uc_uid = resolved

    # RFC 8693 act claim — Brigade may carry the acting agent identity here.
    act = claims.get("act") or {}
    agent_id: str | None = None
    if isinstance(act, dict):
        agent_id = act.get("sub") or act.get("client_id")
    # Brigade may carry scopes as a space-delimited string under ``scope``
    # (OIDC default) OR as a JSON list under ``scopes`` (Brigade preferred).
    # Both routed through one helper so the plural-vs-singular precedence and
    # the malformed-claim → empty-tuple safety contract live in one place.
    scopes = _extract_brigade_scopes(claims)
    ctx = AuthContext(uc_uid=uc_uid, agent_id=agent_id, scopes=scopes, claims=claims)
    _stamp_audit_actor(ctx)
    return ctx


def _stamp_audit_actor(ctx: AuthContext) -> None:
    """Record the VALIDATED caller on the current audit span (finding M2).

    Imported lazily to avoid any import-time coupling to the OTel SDK (which is
    optional). Best-effort: never let telemetry break request auth.
    """
    try:
        from app.otel import set_actor_attributes

        set_actor_attributes(ctx.uc_uid, ctx.agent_id)
    except Exception:  # pragma: no cover - telemetry must never break auth
        pass
