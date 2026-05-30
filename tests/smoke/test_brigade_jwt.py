"""Smoke tests for the Brigade JWT verifier (app/security/brigade_jwt.py).

Runs without a live stack by minting ed25519 + RSA tokens locally and patching
the JWKS fetch. Mirrors the style of test_jwt_audience.py.

Covers the eight Brigade-team-supplied gates:
  1. iss must equal trusted issuer
  2. aud must include the expected audience
  3. exp must be in the future (with skew tolerance)
  4. nbf, iat must be in the past (with skew tolerance)
  5. kid must be present and resolve in the JWKS
  6. alg must be in the EdDSA/RS256 allowlist
  7. Signature must verify against the resolved key
  8. JWKS rotation: kid-not-found triggers ONE refresh attempt
"""
from __future__ import annotations

import base64
import json
import time

import pytest

import app.security.brigade_jwt as bj

TRUSTED_ISS = "https://brigade.unicorncommander.ai"
EXPECTED_AUD = "accounting-ops"
KID_ED = "brigade-ed25519-1"
KID_RS = "brigade-rs256-1"
KID_UNKNOWN = "brigade-ed25519-rotated"


# ---------------------------------------------------------------------------
# Key material — generate once per module run.
# ---------------------------------------------------------------------------


def _make_ed25519_keys(kid: str):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    raw_pub = pub.public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    jwk = {
        "kty": "OKP",
        "crv": "Ed25519",
        "x": base64.urlsafe_b64encode(raw_pub).rstrip(b"=").decode(),
        "kid": kid,
        "alg": "EdDSA",
        "use": "sig",
    }
    return priv, jwk


def _make_rsa_keys(kid: str):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jose.backends.cryptography_backend import CryptographyRSAKey

    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub_pem = (
        priv.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    priv_pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    jwk = CryptographyRSAKey(pub_pem, "RS256").to_dict()
    jwk["kid"] = kid
    jwk["use"] = "sig"
    return priv_pem, jwk


_ED_PRIV, _ED_JWK = _make_ed25519_keys(KID_ED)
_ED_OTHER_PRIV, _ED_OTHER_JWK = _make_ed25519_keys("brigade-ed25519-other")  # NOT in JWKS
_RS_PRIV, _RS_JWK = _make_rsa_keys(KID_RS)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _mint_ed25519(priv, claims: dict, *, kid: str = KID_ED, alg: str = "EdDSA") -> str:
    """Sign an ed25519 JWT manually (jose doesn't do EdDSA)."""
    header = {"alg": alg, "typ": "JWT", "kid": kid}
    h = _b64url(json.dumps(header, separators=(",", ":")).encode())
    p = _b64url(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{h}.{p}".encode("ascii")
    sig = priv.sign(signing_input)
    return f"{h}.{p}.{_b64url(sig)}"


def _mint_rs256(priv_pem: str, claims: dict, *, kid: str = KID_RS) -> str:
    from jose import jwt

    return jwt.encode(claims, priv_pem, algorithm="RS256", headers={"kid": kid})


def _base_claims(**overrides) -> dict:
    now = int(time.time())
    claims = {
        "iss": TRUSTED_ISS,
        "sub": "aaron",
        "aud": EXPECTED_AUD,
        "iat": now - 5,
        "nbf": now - 5,
        "exp": now + 300,
        "scope": "read:accounting write:accounting",
    }
    claims.update(overrides)
    return claims


# ---------------------------------------------------------------------------
# Fixture: serve our JWKS, reset cache between tests.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _serve_jwks(monkeypatch):
    """Patch the JWKS fetch + force-refresh to return the test JWKS only.

    The default fixture serves BOTH the ed25519 and the RSA keypair. Tests can
    monkeypatch ``_fetch_jwks`` directly to override behavior (e.g. simulate a
    rotation that doesn't include the old key).
    """
    bj._reset_cache_for_tests()
    refresh_count = {"count": 0}
    initial_jwks = {"keys": [_ED_JWK, _RS_JWK]}

    async def _fake_fetch():
        return initial_jwks

    async def _fake_refresh():
        refresh_count["count"] += 1
        return initial_jwks

    monkeypatch.setattr(bj, "_fetch_jwks", _fake_fetch)
    monkeypatch.setattr(bj, "_fetch_jwks_force_refresh", _fake_refresh)
    yield refresh_count
    bj._reset_cache_for_tests()


# ---------------------------------------------------------------------------
# Positive paths
# ---------------------------------------------------------------------------


async def test_ed25519_happy_path():
    token = _mint_ed25519(_ED_PRIV, _base_claims())
    claims = await bj.verify_brigade_jwt(token)
    assert claims is not None
    assert claims["sub"] == "aaron"
    assert claims["aud"] == EXPECTED_AUD


async def test_rs256_happy_path():
    token = _mint_rs256(_RS_PRIV, _base_claims())
    claims = await bj.verify_brigade_jwt(token)
    assert claims is not None
    assert claims["sub"] == "aaron"


async def test_aud_list_including_app_accepted():
    """aud as a LIST including the expected audience passes."""
    token = _mint_ed25519(_ED_PRIV, _base_claims(aud=["some-other-app", EXPECTED_AUD]))
    claims = await bj.verify_brigade_jwt(token)
    assert claims is not None


# ---------------------------------------------------------------------------
# Audience / issuer / time gates
# ---------------------------------------------------------------------------


async def test_wrong_audience_rejected():
    """JWT signed correctly but for a different app's audience -> rejected.

    This is the CRITICAL replay-prevention gate.
    """
    token = _mint_ed25519(_ED_PRIV, _base_claims(aud="another-app"))
    assert await bj.verify_brigade_jwt(token) is None


async def test_aud_missing_rejected():
    claims = _base_claims()
    claims.pop("aud", None)
    token = _mint_ed25519(_ED_PRIV, claims)
    assert await bj.verify_brigade_jwt(token) is None


async def test_wrong_issuer_rejected():
    token = _mint_ed25519(_ED_PRIV, _base_claims(iss="https://evil.example"))
    assert await bj.verify_brigade_jwt(token) is None


async def test_expired_rejected():
    now = int(time.time())
    token = _mint_ed25519(_ED_PRIV, _base_claims(iat=now - 600, exp=now - 300))
    assert await bj.verify_brigade_jwt(token) is None


async def test_nbf_future_rejected():
    now = int(time.time())
    token = _mint_ed25519(_ED_PRIV, _base_claims(nbf=now + 3600))
    assert await bj.verify_brigade_jwt(token) is None


async def test_iat_far_future_rejected():
    now = int(time.time())
    token = _mint_ed25519(_ED_PRIV, _base_claims(iat=now + 3600, exp=now + 7200))
    assert await bj.verify_brigade_jwt(token) is None


async def test_no_sub_rejected():
    claims = _base_claims()
    claims.pop("sub", None)
    token = _mint_ed25519(_ED_PRIV, claims)
    assert await bj.verify_brigade_jwt(token) is None


# ---------------------------------------------------------------------------
# Header / alg gates
# ---------------------------------------------------------------------------


async def test_missing_kid_rejected():
    """No kid -> rejected. We don't try to verify against every key."""
    # Manually construct a JWT with no kid in header.
    header = {"alg": "EdDSA", "typ": "JWT"}
    payload = _base_claims()
    h = _b64url(json.dumps(header, separators=(",", ":")).encode())
    p = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{h}.{p}".encode("ascii")
    sig = _ED_PRIV.sign(signing_input)
    token = f"{h}.{p}.{_b64url(sig)}"
    assert await bj.verify_brigade_jwt(token) is None


async def test_alg_none_rejected():
    """alg=none must NEVER be accepted (classic JWT attack)."""
    header = {"alg": "none", "typ": "JWT", "kid": KID_ED}
    payload = _base_claims()
    h = _b64url(json.dumps(header, separators=(",", ":")).encode())
    p = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    # Empty signature for alg=none
    token = f"{h}.{p}."
    assert await bj.verify_brigade_jwt(token) is None


async def test_alg_hs256_rejected():
    """HS-family algs must be rejected (no shared secrets with Brigade)."""
    import hashlib
    import hmac

    header = {"alg": "HS256", "typ": "JWT", "kid": KID_ED}
    payload = _base_claims()
    h = _b64url(json.dumps(header, separators=(",", ":")).encode())
    p = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{h}.{p}".encode("ascii")
    sig = hmac.new(b"any-secret", signing_input, hashlib.sha256).digest()
    token = f"{h}.{p}.{_b64url(sig)}"
    assert await bj.verify_brigade_jwt(token) is None


async def test_bad_signature_rejected():
    """Same kid in header but signed by a DIFFERENT key -> verification fails."""
    # Sign with the "other" ed25519 key but claim kid = the served one.
    # This simulates an attacker who knows our kid but not our private key.
    token = _mint_ed25519(_ED_OTHER_PRIV, _base_claims(), kid=KID_ED)
    assert await bj.verify_brigade_jwt(token) is None


async def test_garbage_token_rejected():
    assert await bj.verify_brigade_jwt("not-a-jwt") is None
    assert await bj.verify_brigade_jwt("") is None
    assert await bj.verify_brigade_jwt("a.b") is None
    assert await bj.verify_brigade_jwt("a.b.c.d") is None


# ---------------------------------------------------------------------------
# JWKS rotation: unknown kid -> ONE refresh attempt -> updated keys served.
# ---------------------------------------------------------------------------


async def test_jwks_rotation_picks_up_new_key(monkeypatch):
    """Simulate a kid rotation: token signed with KID_UNKNOWN, the served JWKS
    initially returns the old key set, then the force-refresh returns the new
    set including KID_UNKNOWN. The verifier must succeed after the refresh.
    """
    bj._reset_cache_for_tests()
    rotated_priv, rotated_jwk = _make_ed25519_keys(KID_UNKNOWN)
    refresh_count = {"count": 0}

    async def _initial():
        return {"keys": [_ED_JWK, _RS_JWK]}  # no rotated key

    async def _refresh():
        refresh_count["count"] += 1
        return {"keys": [_ED_JWK, _RS_JWK, rotated_jwk]}  # NOW includes it

    monkeypatch.setattr(bj, "_fetch_jwks", _initial)
    monkeypatch.setattr(bj, "_fetch_jwks_force_refresh", _refresh)

    token = _mint_ed25519(rotated_priv, _base_claims(), kid=KID_UNKNOWN)
    claims = await bj.verify_brigade_jwt(token)
    assert claims is not None
    assert refresh_count["count"] == 1  # exactly one refresh


async def test_jwks_unknown_kid_after_refresh_rejected(monkeypatch):
    """If even after refresh the kid isn't in the JWKS -> rejected."""
    bj._reset_cache_for_tests()
    refresh_count = {"count": 0}

    async def _initial():
        return {"keys": [_ED_JWK]}

    async def _refresh():
        refresh_count["count"] += 1
        return {"keys": [_ED_JWK]}  # still no KID_UNKNOWN

    monkeypatch.setattr(bj, "_fetch_jwks", _initial)
    monkeypatch.setattr(bj, "_fetch_jwks_force_refresh", _refresh)

    # Sign with a key whose kid simply doesn't exist anywhere.
    fake_priv, _ = _make_ed25519_keys("nope")
    token = _mint_ed25519(fake_priv, _base_claims(), kid="nope")
    assert await bj.verify_brigade_jwt(token) is None
    assert refresh_count["count"] == 1


async def test_jwks_fetch_failure_returns_none(monkeypatch):
    """A network error fetching JWKS must fail closed (None), not raise."""
    bj._reset_cache_for_tests()
    import httpx

    async def _fail():
        raise httpx.HTTPError("brigade down")

    monkeypatch.setattr(bj, "_fetch_jwks", _fail)
    token = _mint_ed25519(_ED_PRIV, _base_claims())
    assert await bj.verify_brigade_jwt(token) is None


# ---------------------------------------------------------------------------
# Env var overrides
# ---------------------------------------------------------------------------


async def test_expected_audience_env_override(monkeypatch):
    monkeypatch.setenv("BRIGADE_EXPECTED_AUDIENCE", "accounting-ops-staging")
    token = _mint_ed25519(_ED_PRIV, _base_claims(aud="accounting-ops-staging"))
    claims = await bj.verify_brigade_jwt(token)
    assert claims is not None

    # The OLD default is now wrong:
    bad = _mint_ed25519(_ED_PRIV, _base_claims())  # aud=accounting-ops
    assert await bj.verify_brigade_jwt(bad) is None


async def test_trusted_issuer_env_override(monkeypatch):
    monkeypatch.setenv("BRIGADE_TRUSTED_ISSUER", "https://staging.brigade.uc.test")
    token = _mint_ed25519(
        _ED_PRIV, _base_claims(iss="https://staging.brigade.uc.test")
    )
    claims = await bj.verify_brigade_jwt(token)
    assert claims is not None
