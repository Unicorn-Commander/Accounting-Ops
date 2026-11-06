"""Brigade-signed JWT verification.

Brigade (https://brigade.unicorncommander.ai) is the Unicorn Commander federated
MCP gateway. It mints short-lived JWTs that proxy a UC user's identity into
member apps. This module is the SECOND auth strategy bolted alongside the
Keycloak-validated bearer path in app.auth — same shape of result, different
issuer.

Why a separate verifier rather than adding Brigade's JWKS to the Keycloak
verifier:
  - Different issuer (must validate ``iss == https://brigade.unicorncommander.ai``)
  - Different audience (must validate ``aud == accounting-ops``)
  - Different signing scheme (ed25519 / EdDSA OR RS256 — the existing path is
    Keycloak's RS256 only; we don't want any chance that a Brigade JWKS ever
    affects Keycloak token validation)
  - Separate JWKS cache + refresh policy

SAFETY:
  - ``aud`` MUST equal ``accounting-ops`` — without this gate, a JWT minted by
    Brigade for app B could be replayed against us.
  - ``iss`` MUST equal the trusted issuer.
  - ``exp`` MUST be in the future; ``nbf`` (if present) MUST be in the past;
    ``iat`` (if present) MUST be in the past (small clock skew tolerated).
  - The header ``alg`` MUST be in an explicit allowlist (no ``none``, no
    HS-family token confusion).
  - The header ``kid`` MUST exist and MUST match a key in the served JWKS.

JWKS caching:
  - Default TTL: 24 hours (Brigade keys rotate infrequently).
  - On a kid-not-found, force a one-shot refresh before failing — covers normal
    rotation without a 24h gap.
  - On a JWKS-fetch failure during force-refresh, fail closed (401).

Rate limit:
  - The JWKS fetch is cached in-process. A burst of bad-kid tokens would force
    at most one network call per process per token attempt; the caller (Brigade
    is not authenticated for this fetch) is rate-limited by the per-request
    network timeout (5s) plus the cache.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx
import jwt
from jwt.exceptions import (
    ExpiredSignatureError,
    InvalidAudienceError,
    InvalidIssuerError,
    InvalidSignatureError,
    MissingRequiredClaimError,
    PyJWTError,
)

_log = logging.getLogger("acctops.brigade_jwt")

# JWKS cache for Brigade public keys. Long TTL because Brigade keys are not
# expected to rotate often; the kid-not-found fallback handles unexpected rotation.
_JWKS_CACHE_TTL_SEC = 24 * 3600
_jwks_cache: dict[str, Any] = {"keys": None, "fetched_at": 0.0}

# Algorithms we will ever accept on a Brigade JWT. Strict allowlist — never
# trust the token's declared algorithm without checking it against this set.
# ``none`` is intentionally absent. HS* are absent (no shared secrets with
# Brigade — only public keys).
_ACCEPTED_ALGS = frozenset({"EdDSA", "RS256"})

# Clock skew tolerance (seconds). Keep modest — same order of magnitude as the
# Keycloak path's explicit PyJWT clock-skew policy.
_CLOCK_SKEW_SEC = 30


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------


def _brigade_trusted_issuer() -> str:
    return os.getenv(
        "BRIGADE_TRUSTED_ISSUER", "https://brigade.unicorncommander.ai"
    )


def _brigade_jwks_url() -> str:
    return os.getenv(
        "BRIGADE_JWKS_URL",
        "https://brigade.unicorncommander.ai/.well-known/jwks.json",
    )


def _brigade_expected_audience() -> str:
    # Default tracks the manifest's tool_namespace + name expectations.
    return os.getenv("BRIGADE_EXPECTED_AUDIENCE", "accounting-ops")


# ---------------------------------------------------------------------------
# JWKS fetch / cache
# ---------------------------------------------------------------------------


async def _fetch_jwks() -> dict[str, Any]:
    """Fetch Brigade's JWKS. Cached for ``_JWKS_CACHE_TTL_SEC`` seconds.

    Same User-Agent guard as the Keycloak fetch — Cloudflare's WAF blocks the
    default Python UA. We never trust the body type without checking shape.
    """
    now = time.time()
    if (
        _jwks_cache["keys"] is not None
        and (now - _jwks_cache["fetched_at"]) < _JWKS_CACHE_TTL_SEC
    ):
        return _jwks_cache["keys"]
    url = _brigade_jwks_url()
    async with httpx.AsyncClient(
        timeout=5,
        headers={
            "User-Agent": "accounting-ops/0.1 (+https://accounting.magicunicorn.dev)"
        },
    ) as client:
        response = await client.get(url)
        response.raise_for_status()
        keys = response.json()
    if not isinstance(keys, dict) or "keys" not in keys:
        raise ValueError("Brigade JWKS response missing 'keys' array")
    _jwks_cache["keys"] = keys
    _jwks_cache["fetched_at"] = now
    return keys


async def _fetch_jwks_force_refresh() -> dict[str, Any]:
    """Bypass the cache. Used on kid-not-found (key rotation)."""
    _jwks_cache["keys"] = None
    return await _fetch_jwks()


def _reset_cache_for_tests() -> None:
    """Test helper: clear the in-process JWKS cache."""
    _jwks_cache["keys"] = None
    _jwks_cache["fetched_at"] = 0.0


# ---------------------------------------------------------------------------
# Key material loading (from a JWK)
# ---------------------------------------------------------------------------


def _load_jwk_for_alg(jwk: dict[str, Any], alg: str):
    """Return a verifying key object for ``alg`` from a JWK dict.

    PyJWT validates the JWK's key type against the requested algorithm and
    returns the cryptography public-key object used for verification.
    """
    if alg not in _ACCEPTED_ALGS:
        raise ValueError(f"unsupported alg {alg!r}")
    return jwt.PyJWK.from_dict(jwk, algorithm=alg).key


# ---------------------------------------------------------------------------
# Public API: verify_brigade_jwt
# ---------------------------------------------------------------------------


class BrigadeJWTError(Exception):
    """Brigade JWT validation failure. Carries a short reason for logging."""


async def verify_brigade_jwt(token: str) -> dict[str, Any] | None:
    """Verify a Brigade-signed JWT. Returns the claims dict on success, None on failure.

    On any failure (bad shape, bad signature, wrong issuer/audience, expired,
    unknown kid after a refresh, bad alg), returns None. The caller (auth chain)
    treats None as "this token is not a valid Brigade JWT" and either falls
    through to the next strategy or 401s.

    No exception is raised on validation failure — keeps the auth chain simple
    and avoids leaking specifics to the wire. A debug log line records the
    reason for operators.

    Returns a dict on success containing AT MINIMUM:
        sub:   the Brigade-mapped user identifier (uc_uid analog)
        iss:   the trusted issuer
        aud:   the expected audience
        plus any other claims Brigade attached (org_id, scopes, etc.).
    """
    if not token or not isinstance(token, str):
        _log.debug("brigade jwt verify: empty/non-string token")
        return None

    # 1. Header — PyJWT parses the JWT shape without trusting its claims.
    try:
        header = jwt.get_unverified_header(token)
    except (PyJWTError, TypeError, ValueError) as exc:
        _log.debug("brigade jwt verify: bad shape: %s", exc)
        return None

    # 2. Header — alg allowlist + kid required
    alg = header.get("alg")
    if alg not in _ACCEPTED_ALGS:
        _log.debug("brigade jwt verify: alg %r not in allowlist", alg)
        return None
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        _log.debug("brigade jwt verify: missing kid")
        return None

    # 3. Resolve the verifying key by kid (with one rotation-aware retry).
    try:
        jwks = await _fetch_jwks()
    except (httpx.HTTPError, ValueError) as exc:
        _log.warning("brigade jwt verify: jwks fetch failed: %s", exc)
        return None
    key_jwk = _find_key(jwks, kid)
    if key_jwk is None:
        try:
            jwks = await _fetch_jwks_force_refresh()
        except (httpx.HTTPError, ValueError) as exc:
            _log.warning("brigade jwt verify: jwks refresh failed: %s", exc)
            return None
        key_jwk = _find_key(jwks, kid)
        if key_jwk is None:
            _log.debug("brigade jwt verify: unknown kid %r after refresh", kid)
            return None

    # 4. Signature and claim verification. The allowlist is explicit and the
    # issuer/audience checks remain enforced by PyJWT. Expiry, nbf, and iat are
    # checked below to preserve the old numeric-type and clock-skew decisions.
    try:
        verifying_key = _load_jwk_for_alg(key_jwk, alg)
    except (PyJWTError, TypeError, ValueError) as exc:
        _log.debug("brigade jwt verify: bad JWK for alg %r: %s", alg, exc)
        return None

    try:
        claims = jwt.decode(
            token,
            verifying_key,
            algorithms=sorted(_ACCEPTED_ALGS),
            issuer=_brigade_trusted_issuer(),
            audience=_brigade_expected_audience(),
            leeway=_CLOCK_SKEW_SEC,
            options={
                "verify_exp": False,
                "verify_iat": False,
                "verify_nbf": False,
            },
        )
    except InvalidSignatureError:
        _log.info("brigade jwt verify: signature verification FAILED for kid=%r", kid)
        return None
    except InvalidIssuerError:
        _log.info("brigade jwt verify: issuer mismatch")
        return None
    except InvalidAudienceError:
        _log.info("brigade jwt verify: audience mismatch")
        return None
    except ExpiredSignatureError:
        _log.info("brigade jwt verify: token expired")
        return None
    except MissingRequiredClaimError as exc:
        _log.debug("brigade jwt verify: %s", exc)
        return None
    except PyJWTError as exc:
        _log.debug("brigade jwt verify: %s", exc)
        return None

    # Time-based claims not delegated to PyJWT so malformed values keep the
    # previous fail-closed/fail-open decisions exactly as documented above.
    now = time.time()
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)):
        _log.debug("brigade jwt verify: exp missing or not numeric")
        return None
    if now > float(exp) + _CLOCK_SKEW_SEC:
        _log.info("brigade jwt verify: token expired (exp=%s now=%s)", exp, now)
        return None
    nbf = claims.get("nbf")
    if isinstance(nbf, (int, float)) and now + _CLOCK_SKEW_SEC < float(nbf):
        _log.info("brigade jwt verify: token not yet valid (nbf=%s now=%s)", nbf, now)
        return None
    iat = claims.get("iat")
    if isinstance(iat, (int, float)) and now + _CLOCK_SKEW_SEC < float(iat):
        _log.info("brigade jwt verify: token iat in the future (iat=%s now=%s)", iat, now)
        return None

    # sub MUST be present — it's the uc_uid analog we resolve against
    # public.user_account in the auth chain.
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        _log.debug("brigade jwt verify: missing sub claim")
        return None

    return claims


def _find_key(jwks: dict[str, Any], kid: str) -> dict[str, Any] | None:
    keys = jwks.get("keys")
    if not isinstance(keys, list):
        return None
    for k in keys:
        if isinstance(k, dict) and k.get("kid") == kid:
            return k
    return None


__all__ = (
    "BrigadeJWTError",
    "verify_brigade_jwt",
)
