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
    # True when the bearer is a Keycloak client-credentials JWT whose azp
    # (authorized party) is in the ACCTOPS_TRUSTED_M2M_CALLERS allow-list.
    # These callers have no workspace membership row; specific endpoints
    # (P&L federation read) opt in via resolve_workspace_for_federation.
    # This flag is NEVER set by the service-token or Brigade paths — only
    # by the Keycloak JWT path after audience + azp allow-list validation.
    is_trusted_m2m: bool = False


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
        headers={"User-Agent": "accounting-ops/0.1 (+https://accounting-ops.example.com)"},
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
            "uc_uid": "jane",
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

# SUITE-IDENTITY D1 divergence log dedup: once-per-identity-per-process.
# After the guard shipped, every request from a legacy-attributed user with a
# divergent mapper flooded ERROR logs (sub deterministically wins; re-logging
# every request is pure noise). Keyed by the canonical identity (``sub``).
_UC_UID_DIVERGE_LOGGED: set[str] = set()


def _reset_uc_uid_diverge_log_for_tests() -> None:
    """Test helper: clear the once-per-identity diverge-log set."""
    _UC_UID_DIVERGE_LOGGED.clear()


def _resolve_uc_uid(claims: dict[str, Any]) -> str | None:
    """Resolve the canonical user key from a verified-claims dict.

    SUITE-IDENTITY D1 (binding): the user key is the uchub ``sub``; ``uc_uid`` is
    a STRICT alias defined to equal ``sub``. No client may emit a divergent
    ``uc_uid`` mapper. If a token nonetheless carries ``uc_uid != sub``, that is a
    silent re-keying trap — we log LOUDLY at ERROR and resolve to ``sub`` (never
    the divergent ``uc_uid``). The ERROR is emitted once per identity (``sub``)
    per process so legacy mappers do not spam the log on every request.

    Returns the resolved user key, or ``None`` when neither claim is present
    (the caller turns that into a 401 / a fall-through).

    Behaviour-preserving for the conformant case (``uc_uid == sub`` or ``uc_uid``
    absent): returns exactly what ``claims.get("uc_uid") or claims.get("sub")``
    returned before. The only change is the divergent case, which previously
    silently preferred ``uc_uid`` and now prefers ``sub`` with an ERROR log.
    """
    claimed_uc_uid = claims.get("uc_uid")
    sub = claims.get("sub")
    if claimed_uc_uid is not None and sub is not None and claimed_uc_uid != sub:
        identity_key = str(sub)
        if identity_key not in _UC_UID_DIVERGE_LOGGED:
            _UC_UID_DIVERGE_LOGGED.add(identity_key)
            _log.error(
                "[acctops.auth] uc_uid_diverges_from_sub: uc_uid=%r != sub=%r — "
                "using sub per SUITE-IDENTITY D1 (uc_uid is a strict alias of sub)",
                claimed_uc_uid,
                sub,
            )
        return sub
    return claimed_uc_uid or sub


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


# ---------------------------------------------------------------------------
# Local self-host token (Community Edition — AO_AUTH_MODE=local)
# ---------------------------------------------------------------------------
# In local mode there is no Keycloak/uchub. The web NextAuth Credentials
# provider signs a short HS256 bearer with the shared AO_LOCAL_AUTH_SECRET
# after verifying the single admin's password. The token mirrors the shape a
# Keycloak token would carry (sub/uc_uid + this resource server's audience) so
# every downstream RBAC / workspace-membership check resolves identically to
# the Keycloak path — the only difference is the signature scheme + issuer.
_LOCAL_TOKEN_ISSUER = "ao-local"


def _try_authenticate_local(token: str) -> AuthContext | None:
    """Validate a locally-issued HS256 bearer. None on any miss/failure.

    Active ONLY when auth_mode == "local" AND a signing secret is configured.
    Returns None (→ caller falls through / 401s) rather than raising, so a
    stray non-local token in local mode still gets a clean 401 downstream.
    """
    settings = get_settings()
    if not settings.is_local_auth:
        return None
    secret = settings.local_auth_secret
    if not secret:
        # Fail closed: local mode without a secret cannot mint or verify.
        _log.warning(
            "[acctops.auth] AO_AUTH_MODE=local but AO_LOCAL_AUTH_SECRET is unset "
            "— local login disabled (set the secret to enable self-host auth)."
        )
        return None
    if jwt is None:  # pragma: no cover - jose always present in practice
        return None
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            issuer=_LOCAL_TOKEN_ISSUER,
            audience=settings.accepted_audience,
            options={"verify_at_hash": False},
        )
    except Exception:  # noqa: BLE001 - any decode failure = not a valid local token
        return None
    # SUITE-IDENTITY D1: uc_uid ≡ sub. Local tokens are minted by AO itself with
    # both equal; a divergent uc_uid logs at ERROR and resolves to sub anyway.
    uc_uid = _resolve_uc_uid(claims)
    if not uc_uid:
        return None
    scope = tuple(str(claims.get("scope", "")).split())
    ctx = AuthContext(
        uc_uid=str(uc_uid),
        agent_id=None,
        scopes=scope,
        claims=claims,
    )
    _stamp_audit_actor(ctx)
    return ctx


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

    # Local self-host token branch (Community Edition — AO_AUTH_MODE=local).
    # No-op in keycloak mode (returns None immediately). When it validates, we
    # return the AuthContext directly — the Keycloak/Brigade paths below are for
    # the suite deployment and are never reached in local mode. Service tokens +
    # PATs above still work in local mode (both are checked first).
    local_ctx = _try_authenticate_local(token)
    if local_ctx is not None:
        return local_ctx

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
            # RFC-8693 act claim: on a Brigade federation exchange the ACTING
            # APP is ``act.client_id`` (``act.sub`` is the federated subject).
            # A trusted server-to-server caller (ACCTOPS_TRUSTED_M2M_CALLERS,
            # e.g. tax-planning-ops) is authorized for the federation endpoints
            # WITHOUT an AO user identity — same allow-list the Keycloak M2M
            # path uses, just keyed off the Brigade actor instead of azp.
            b_act = brigade_claims.get("act") or {}
            b_actor_app = b_act.get("client_id") if isinstance(b_act, dict) else None
            b_is_m2m = bool(
                b_actor_app and b_actor_app in get_settings().trusted_m2m_callers
            )
            # SUITE-IDENTITY D1: uc_uid ≡ sub. Diverging uc_uid logs + uses sub.
            b_uc_uid = _resolve_uc_uid(brigade_claims)
            # Brigade mints sub = the customer's EMAIL today; AO keys accounts on
            # the opaque uc_uid. Translate an email-shaped identifier to the
            # canonical uc_uid via public.user_account.email (mirrors project-ops's
            # fallback). Unresolved → b_uc_uid becomes None and we fall through to
            # the 401 path: Brigade can't conjure an AO identity by minting a JWT.
            # Forward-compatible: an opaque sub skips this and matches directly.
            # M2M callers skip resolution entirely — they get a synthetic uc_uid
            # (m2m:<actor>) that never matches a real user_account row.
            if b_is_m2m:
                b_uc_uid = f"m2m:{b_actor_app}"
            elif isinstance(b_uc_uid, str) and "@" in b_uc_uid:
                b_uc_uid = await _uc_uid_for_email(b_uc_uid)
            if b_uc_uid:
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
                    is_trusted_m2m=b_is_m2m,
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

    # SUITE-IDENTITY D1: the user key is the uchub sub; uc_uid is a strict alias.
    # A divergent uc_uid is logged at ERROR and we resolve to sub (never uc_uid).
    uc_uid = _resolve_uc_uid(claims)
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

    # Trusted M2M detection: if the token's azp is in the configured allow-list
    # of trusted server-to-server callers, mark the context so the federation
    # endpoint can synthesize a workspace context without a membership row.
    # We use a stable synthetic uc_uid (m2m:<azp>) so these callers never
    # collide with real user accounts and never auto-provision user_account rows.
    # SECURITY: is_trusted_m2m is ONLY set here, after full KC JWT validation
    # (signature + issuer + expiry + audience all already verified above).
    azp: str | None = claims.get("azp") or claims.get("client_id")
    trusted_callers = settings.trusted_m2m_callers
    is_trusted_m2m = bool(azp and azp in trusted_callers)
    if is_trusted_m2m:
        # Synthetic uc_uid — never matches a real user_account row.
        uc_uid = f"m2m:{azp}"
    else:
        # Keycloak-login only (NOT Brigade, NOT M2M): bind-or-provision so a
        # verified-email match re-keys an existing ACTIVE row instead of minting
        # a blank ghost (live incident 2026-07-22). See helper docstring for the
        # bind-vs-fail-closed abuse analysis.
        await _ensure_keycloak_user_identity(str(uc_uid), claims)

    ctx = AuthContext(
        uc_uid=uc_uid,
        agent_id=agent_id,
        scopes=scope,
        claims=claims,
        is_trusted_m2m=is_trusted_m2m,
    )
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


def _is_synthetic_uc_uid(uc_uid: str) -> bool:
    """True for non-human synthetic keys that must never email-bind.

    Covers trusted-M2M (``m2m:<azp>``) and agent-style prefixes (``agent-*``).
    These identities have no human email and must never re-key a human row.
    """
    return uc_uid.startswith("m2m:") or uc_uid.startswith("agent-")


def _token_email(claims: dict[str, Any]) -> str | None:
    """Return the token email claim (stripped) or None if absent/blank/non-str."""
    raw = claims.get("email")
    if not isinstance(raw, str):
        return None
    email = raw.strip()
    return email or None


def _token_email_verified(claims: dict[str, Any]) -> bool:
    """OIDC ``email_verified`` must be the boolean True — string/1 do not count.

    Unverified email is the spoofing surface for the Keycloak bind path: a
    hostile IdP client or pre-verify session could assert any address. We only
    trust the claim when Keycloak has stamped the boolean true.
    """
    return claims.get("email_verified") is True


async def _ensure_keycloak_user_identity(
    uc_uid: str, claims: dict[str, Any]
) -> None:
    """Keycloak-login path only: bind-or-provision ``public.user_account``.

    LIVE INCIDENT 2026-07-22: after SUITE-IDENTITY D1 (sub wins on
    ``uc_uid != sub``), SSO resolved to a sub with no row and the later
    auto-provision path minted a blank ghost (no email, no memberships) → the
    owner of production books landed on "create your workspace" as if data was
    gone. Hand-seeded / imported / migrated accounts keyed under a legacy
    ``uc_uid`` with a real email are the landmine.

    **Bind choice (preferred over fail-closed-on-match):** when an ACTIVE row
    matches the token email (``lower(email)``) AND ``email_verified is True``,
    re-key that row's ``uc_uid`` one-way to the resolved sub and log at WARNING
    (old → new). Why bind, not always-409:

    * A verified email is the only OIDC claim that safely links a new sub to an
      already-seeded AO account (KC ``verifyEmail`` is on for uchub).
    * Fail-closed-on-every-match would permanently lock out every migrated
      human until an operator re-keys by hand — the exact outage we just had.
    * The spoofing abuse case is **unverified** email claims: we fail closed
      with HTTP 409 when a match exists but ``email_verified`` is not True, so
      an attacker cannot re-key someone else's account by asserting their
      address without KC verification.

    Never provisions NULL email when the token carries one. Synthetic
    ``m2m:*`` / ``agent-*`` ids are no-ops (caller should also skip). Brigade
    is deliberately NOT wired here — it already resolves via
    ``_uc_uid_for_email`` and must not auto-provision.

    DB blips: log + soft-return so pure JWT validation still works when the
    identity store is briefly unavailable (downstream membership resolution
    will fail closed on its own). The 409 for unverified-email match is hard.
    """
    if _is_synthetic_uc_uid(uc_uid):
        return

    email = _token_email(claims)
    email_verified = _token_email_verified(claims)

    from sqlalchemy import text

    from app.db import SessionLocal

    try:
        async with SessionLocal() as session:
            async with session.begin():
                by_uid = (
                    await session.execute(
                        text(
                            "SELECT id, email FROM public.user_account "
                            "WHERE uc_uid = :uc"
                        ),
                        {"uc": uc_uid},
                    )
                ).first()
                if by_uid is not None:
                    # Already keyed on sub. Backfill email if the row is blank
                    # and the token carries one — never leave a known address
                    # as NULL when we have it.
                    if email and not by_uid.email:
                        await session.execute(
                            text(
                                "UPDATE public.user_account "
                                "SET email = :email, last_seen_at = now() "
                                "WHERE id = :id"
                            ),
                            {"email": email, "id": by_uid.id},
                        )
                    else:
                        await session.execute(
                            text(
                                "UPDATE public.user_account "
                                "SET last_seen_at = now() WHERE id = :id"
                            ),
                            {"id": by_uid.id},
                        )
                    return

                if email:
                    match = (
                        await session.execute(
                            text(
                                """
                                SELECT id, uc_uid FROM public.user_account
                                WHERE lower(email) = lower(:email) AND is_active
                                ORDER BY last_seen_at DESC NULLS LAST
                                LIMIT 1
                                """
                            ),
                            {"email": email},
                        )
                    ).first()
                    if match is not None:
                        if not email_verified:
                            # Fail closed: match without verified email is the
                            # spoofing surface. Do NOT bind; do NOT mint a ghost.
                            raise HTTPException(
                                status_code=status.HTTP_409_CONFLICT,
                                detail=(
                                    "An account already exists for this email under "
                                    "a different identity. Verify your email with "
                                    "the identity provider, or contact support."
                                ),
                            )
                        old_uc = match.uc_uid
                        if old_uc != uc_uid:
                            _log.warning(
                                "[acctops.auth] keycloak_email_bind: re-keying "
                                "user_account uc_uid old=%r new=%r "
                                "(email_verified=true)",
                                old_uc,
                                uc_uid,
                            )
                            await session.execute(
                                text(
                                    """
                                    UPDATE public.user_account
                                    SET uc_uid = :new_uc, last_seen_at = now()
                                    WHERE id = :id
                                    """
                                ),
                                {"new_uc": uc_uid, "id": match.id},
                            )
                        return

                # Genuinely new user — provision with email when the token has
                # one so later tenancy auto-provision paths find this row and
                # never insert a NULL-email ghost.
                if email:
                    await session.execute(
                        text(
                            """
                            INSERT INTO public.user_account
                                (id, uc_uid, email, is_active, created_at, last_seen_at)
                            VALUES
                                (gen_random_uuid(), :uc, :email, true, now(), now())
                            """
                        ),
                        {"uc": uc_uid, "email": email},
                    )
                else:
                    await session.execute(
                        text(
                            """
                            INSERT INTO public.user_account
                                (id, uc_uid, is_active, created_at, last_seen_at)
                            VALUES
                                (gen_random_uuid(), :uc, true, now(), now())
                            """
                        ),
                        {"uc": uc_uid},
                    )
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover - DB blip → soft-return
        _log.warning(
            "[acctops.auth] keycloak identity ensure skipped (db error %s) — "
            "falling through; downstream may still auto-provision",
            exc.__class__.__name__,
        )
        return


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

    # The sub claim IS the uc_uid in Brigade's contract (SUITE-IDENTITY D1:
    # uc_uid is a strict alias of sub). _resolve_uc_uid logs LOUDLY at ERROR and
    # resolves to sub if a token ever carries a divergent uc_uid — never the
    # divergent uc_uid. Either way the value must resolve to an existing
    # user_account in the workspace's tenancy layer for any tool call to succeed
    # (no auto-provision here).
    # RFC 8693 act claim — the acting APP is act.client_id (act.sub is the
    # federated subject). A trusted M2M caller is authorized without an AO user.
    act = claims.get("act") or {}
    actor_app = act.get("client_id") if isinstance(act, dict) else None
    is_m2m = bool(actor_app and actor_app in get_settings().trusted_m2m_callers)

    uc_uid = _resolve_uc_uid(claims)
    if is_m2m:
        # Synthetic uc_uid — never matches a real user_account; the M2M caller
        # has no AO membership and never auto-provisions one. Skip resolution.
        uc_uid = f"m2m:{actor_app}"
    else:
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

    agent_id: str | None = None
    if isinstance(act, dict):
        agent_id = act.get("sub") or act.get("client_id")
    # Brigade may carry scopes as a space-delimited string under ``scope``
    # (OIDC default) OR as a JSON list under ``scopes`` (Brigade preferred).
    # Both routed through one helper so the plural-vs-singular precedence and
    # the malformed-claim → empty-tuple safety contract live in one place.
    scopes = _extract_brigade_scopes(claims)
    ctx = AuthContext(
        uc_uid=uc_uid,
        agent_id=agent_id,
        scopes=scopes,
        claims=claims,
        is_trusted_m2m=is_m2m,
    )
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
