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

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any

import httpx

from app.config import get_settings

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
# Keycloak path's implicit jose default.
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
# Base64url + JWT parse helpers
# ---------------------------------------------------------------------------


def _b64url_decode(value: str) -> bytes:
    pad = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + pad)


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _split_jwt(token: str) -> tuple[str, str, str]:
    """Return (header_b64, payload_b64, signature_b64); raise ValueError on shape."""
    if not isinstance(token, str):
        raise ValueError("token must be str")
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("JWT must have three segments")
    if not all(parts):
        raise ValueError("JWT segments must be non-empty")
    return parts[0], parts[1], parts[2]


def _decode_header(header_b64: str) -> dict[str, Any]:
    try:
        raw = _b64url_decode(header_b64)
        header = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JWT header: {exc}") from exc
    if not isinstance(header, dict):
        raise ValueError("JWT header must be a JSON object")
    return header


def _decode_payload(payload_b64: str) -> dict[str, Any]:
    try:
        raw = _b64url_decode(payload_b64)
        payload = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JWT payload: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("JWT payload must be a JSON object")
    return payload


# ---------------------------------------------------------------------------
# Key material loading (from a JWK)
# ---------------------------------------------------------------------------


def _load_jwk_for_alg(jwk: dict[str, Any], alg: str):
    """Return a verifying key object for ``alg`` from a JWK dict.

    Validates the JWK's ``kty`` matches the algorithm family. Returns an object
    consumed by ``_verify_signature`` (ed25519: cryptography Ed25519PublicKey;
    RS256: jose JWK / cryptography RSA public key).
    """
    kty = jwk.get("kty")
    if alg == "EdDSA":
        # OKP / Ed25519. RFC 8037.
        if kty != "OKP":
            raise ValueError(f"EdDSA requires kty=OKP, got kty={kty!r}")
        if jwk.get("crv") != "Ed25519":
            raise ValueError(f"EdDSA requires crv=Ed25519, got crv={jwk.get('crv')!r}")
        x = jwk.get("x")
        if not isinstance(x, str):
            raise ValueError("EdDSA JWK missing 'x' (base64url raw public key)")
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        raw = _b64url_decode(x)
        if len(raw) != 32:
            raise ValueError(f"Ed25519 raw key must be 32 bytes, got {len(raw)}")
        return Ed25519PublicKey.from_public_bytes(raw)
    if alg == "RS256":
        if kty != "RSA":
            raise ValueError(f"RS256 requires kty=RSA, got kty={kty!r}")
        # Reuse python-jose's RSA loader (already a dependency, exact same
        # path the Keycloak verifier uses for RSA tokens).
        try:
            from jose.backends.cryptography_backend import CryptographyRSAKey
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("python-jose[cryptography] required for RS256") from exc
        return CryptographyRSAKey(jwk, "RS256")
    raise ValueError(f"unsupported alg {alg!r}")


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


def _verify_signature(
    alg: str, key: Any, signing_input: bytes, signature: bytes
) -> bool:
    """Constant-time-ish signature verification for ``alg``.

    Returns True on success, False on any verification failure. Never raises
    on a verification miss — caller decides the HTTP status.
    """
    if alg == "EdDSA":
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        if not isinstance(key, Ed25519PublicKey):
            return False
        try:
            key.verify(signature, signing_input)
            return True
        except InvalidSignature:
            return False
        except Exception:  # pragma: no cover - defensive
            return False
    if alg == "RS256":
        try:
            # jose JWK objects expose ``verify(message, signature) -> bool``.
            ok = key.verify(signing_input, signature)
            return bool(ok)
        except Exception:  # pragma: no cover - defensive
            return False
    return False


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

    # 1. Shape
    try:
        header_b64, payload_b64, signature_b64 = _split_jwt(token)
    except ValueError as exc:
        _log.debug("brigade jwt verify: bad shape: %s", exc)
        return None

    # 2. Header — alg allowlist + kid required
    try:
        header = _decode_header(header_b64)
    except ValueError as exc:
        _log.debug("brigade jwt verify: %s", exc)
        return None
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

    # 4. Signature verification
    try:
        verifying_key = _load_jwk_for_alg(key_jwk, alg)
    except (ValueError, RuntimeError) as exc:
        _log.debug("brigade jwt verify: bad JWK for alg %r: %s", alg, exc)
        return None
    try:
        signature = _b64url_decode(signature_b64)
    except ValueError as exc:
        _log.debug("brigade jwt verify: bad signature encoding: %s", exc)
        return None
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    if not _verify_signature(alg, verifying_key, signing_input, signature):
        _log.info("brigade jwt verify: signature verification FAILED for kid=%r", kid)
        return None

    # 5. Claims — only after the signature is good.
    try:
        claims = _decode_payload(payload_b64)
    except ValueError as exc:
        _log.debug("brigade jwt verify: %s", exc)
        return None

    # iss
    expected_iss = _brigade_trusted_issuer()
    if claims.get("iss") != expected_iss:
        _log.info(
            "brigade jwt verify: issuer mismatch (got %r want %r)",
            claims.get("iss"),
            expected_iss,
        )
        return None

    # aud — string OR list-of-strings. MUST include the configured audience.
    expected_aud = _brigade_expected_audience()
    aud = claims.get("aud")
    aud_list = aud if isinstance(aud, list) else [aud] if aud else []
    if expected_aud not in aud_list:
        _log.info(
            "brigade jwt verify: audience mismatch (got %r want %r)",
            aud,
            expected_aud,
        )
        return None

    # Time-based claims (with small skew tolerance).
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
