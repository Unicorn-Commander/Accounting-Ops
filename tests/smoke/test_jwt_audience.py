"""Smoke tests for JWT resource-server audience validation (finding H3).

These run WITHOUT a live stack (no Keycloak, no network) by minting RSA-signed
tokens locally and monkeypatching the JWKS fetch to return the matching public
key. They prove the H3 fix: a *valid* uchub token is authorized for this
resource server iff its `aud` (string or list) OR `resource_access` includes the
configured audience (settings.accepted_audience, default = client id) — so a
token minted by ANOTHER uchub client (an agent / Brigade) is accepted when it
carries this app's resource id, while signature / issuer / expiry remain
strictly enforced and the service-token fast-path is unaffected.

Mirrors the monkeypatch style of test_mcp_auth.py (patch the auth module's
collaborators; force re-parse of any cached state) rather than touching the
network.
"""
from __future__ import annotations

import time

import pytest
from fastapi import HTTPException
from jose import jwt

import app.auth as auth
from app.auth import authenticate_token
from app.config import get_settings

# Realm issuer used for both minting and validation (default Settings value).
ISSUER = "https://auth.unicorncommander.ai/realms/uchub"
# This app's own client id == default accepted audience.
APP_AUD = "accounting-ops-app"
# A different uchub client (e.g. an agent / Brigade) minting the token.
AGENT_CLIENT = "majiks-brigade"
KID = "test-signing-kid"


# --------------------------------------------------------------------------
# Key material: one RSA keypair for the whole module. The public half is served
# as a JWKS; the private half signs the test tokens.
# --------------------------------------------------------------------------
def _make_keypair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jose.backends.cryptography_backend import CryptographyRSAKey

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private_key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    jwk = CryptographyRSAKey(public_pem, "RS256").to_dict()
    jwk["kid"] = KID
    jwk["use"] = "sig"
    return private_pem, jwk


_PRIVATE_PEM, _PUBLIC_JWK = _make_keypair()
# A SECOND keypair whose public half is NOT in the served JWKS — used to mint a
# token with a valid structure but a signature our key set can't verify.
_OTHER_PRIVATE_PEM, _ = _make_keypair()


def _mint(private_pem: str, claims: dict, *, kid: str = KID) -> str:
    return jwt.encode(
        claims, private_pem, algorithm="RS256", headers={"kid": kid}
    )


def _base_claims(**overrides) -> dict:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "sub": "user-123",
        "uc_uid": "aaron",
        "azp": AGENT_CLIENT,  # minted by an agent client, NOT the app itself
        "iat": now,
        "exp": now + 300,
        "scope": "openid profile",
    }
    claims.update(overrides)
    return claims


@pytest.fixture(autouse=True)
def _serve_jwks(monkeypatch):
    """Serve the module keypair's public JWK and pin a clean settings cache.

    Both _fetch_jwks and the force-refresh variant are patched so the
    key-rotation fallback never reaches the network either.
    """
    get_settings.cache_clear()

    async def _fake_jwks():
        return {"keys": [_PUBLIC_JWK]}

    monkeypatch.setattr(auth, "_fetch_jwks", _fake_jwks)
    monkeypatch.setattr(auth, "_fetch_jwks_force_refresh", _fake_jwks)
    # Ensure no stray service tokens interfere with the JWT path.
    monkeypatch.setattr(auth, "_service_tokens_cache", {})
    yield
    get_settings.cache_clear()


# --------------------------------------------------------------------------
# Positive: audience present -> accepted (the unblock-agents case)
# --------------------------------------------------------------------------
async def test_aud_string_equal_accepted():
    token = _mint(_PRIVATE_PEM, _base_claims(aud=APP_AUD))
    ctx = await authenticate_token(token)
    assert ctx.uc_uid == "aaron"


async def test_aud_list_including_app_accepted():
    """An agent-minted token whose aud LIST includes our resource id passes —
    this is the H3 unblock: azp is a different client, but aud carries us."""
    token = _mint(_PRIVATE_PEM, _base_claims(aud=["account", APP_AUD]))
    ctx = await authenticate_token(token)
    assert ctx.uc_uid == "aaron"


async def test_resource_access_key_accepted():
    """Audience carried via resource_access (Keycloak Audience mapper) passes."""
    token = _mint(
        _PRIVATE_PEM,
        _base_claims(
            aud="account",
            resource_access={APP_AUD: {"roles": ["uma_protection"]}},
        ),
    )
    ctx = await authenticate_token(token)
    assert ctx.uc_uid == "aaron"


async def test_act_claim_sets_agent_id():
    """uc_uid + RFC 8693 act handling stay intact on the accepted JWT path."""
    token = _mint(
        _PRIVATE_PEM,
        _base_claims(aud=APP_AUD, act={"sub": "receipt-reconciler"}),
    )
    ctx = await authenticate_token(token)
    assert ctx.uc_uid == "aaron"
    assert ctx.agent_id == "receipt-reconciler"


async def test_configurable_accepted_audience(monkeypatch):
    """accepted_audience is configurable: a token scoped to a custom resource id
    is accepted only when it matches settings.accepted_audience."""
    monkeypatch.setenv("ACCEPTED_AUDIENCE", "accounting-ops-resource")
    get_settings.cache_clear()
    assert get_settings().accepted_audience == "accounting-ops-resource"

    good = _mint(_PRIVATE_PEM, _base_claims(aud="accounting-ops-resource"))
    ctx = await authenticate_token(good)
    assert ctx.uc_uid == "aaron"

    # The app's OWN default client id no longer suffices once the knob is set.
    bad = _mint(_PRIVATE_PEM, _base_claims(aud=APP_AUD))
    with pytest.raises(HTTPException) as exc:
        await authenticate_token(bad)
    assert exc.value.status_code == 403


# --------------------------------------------------------------------------
# Negative: missing/wrong audience -> rejected (the loose-check fix, H3)
# --------------------------------------------------------------------------
async def test_aud_without_app_rejected():
    """A perfectly valid, well-signed uchub token whose audience does NOT
    include this resource server is rejected (403) — closing the loose check."""
    token = _mint(_PRIVATE_PEM, _base_claims(aud=["account", "some-other-app"]))
    with pytest.raises(HTTPException) as exc:
        await authenticate_token(token)
    assert exc.value.status_code == 403
    assert "audience" in str(exc.value.detail).lower()


async def test_no_aud_no_resource_access_rejected():
    claims = _base_claims()
    claims.pop("aud", None)  # no aud at all, no resource_access
    token = _mint(_PRIVATE_PEM, claims)
    with pytest.raises(HTTPException) as exc:
        await authenticate_token(token)
    assert exc.value.status_code == 403


async def test_azp_match_alone_is_not_enough():
    """The OLD behaviour accepted a token solely because azp == our client id.
    The fix removes that: azp matching but audience absent must be rejected."""
    token = _mint(
        _PRIVATE_PEM, _base_claims(azp=APP_AUD, aud=["account"])
    )
    with pytest.raises(HTTPException) as exc:
        await authenticate_token(token)
    assert exc.value.status_code == 403


async def test_resource_access_wrong_client_rejected():
    token = _mint(
        _PRIVATE_PEM,
        _base_claims(aud="account", resource_access={"some-other-app": {}}),
    )
    with pytest.raises(HTTPException) as exc:
        await authenticate_token(token)
    assert exc.value.status_code == 403


# --------------------------------------------------------------------------
# Negative: signature / issuer / expiry remain strictly enforced
# --------------------------------------------------------------------------
async def test_bad_signature_rejected():
    """Signed by a key NOT in the served JWKS (same kid) -> signature fails."""
    token = _mint(_OTHER_PRIVATE_PEM, _base_claims(aud=APP_AUD))
    with pytest.raises(HTTPException) as exc:
        await authenticate_token(token)
    assert exc.value.status_code == 401


async def test_wrong_issuer_rejected():
    token = _mint(_PRIVATE_PEM, _base_claims(aud=APP_AUD, iss="https://evil.example/realms/x"))
    with pytest.raises(HTTPException) as exc:
        await authenticate_token(token)
    assert exc.value.status_code == 401


async def test_expired_token_rejected():
    now = int(time.time())
    token = _mint(
        _PRIVATE_PEM,
        _base_claims(aud=APP_AUD, iat=now - 600, exp=now - 300),
    )
    with pytest.raises(HTTPException) as exc:
        await authenticate_token(token)
    assert exc.value.status_code == 401


async def test_missing_kid_rejected():
    token = jwt.encode(_base_claims(aud=APP_AUD), _PRIVATE_PEM, algorithm="RS256")
    with pytest.raises(HTTPException) as exc:
        await authenticate_token(token)
    assert exc.value.status_code == 401


async def test_unknown_kid_rejected():
    token = _mint(_PRIVATE_PEM, _base_claims(aud=APP_AUD), kid="no-such-kid")
    with pytest.raises(HTTPException) as exc:
        await authenticate_token(token)
    assert exc.value.status_code == 401


# --------------------------------------------------------------------------
# Service-token fast-path is unaffected by the audience change.
# --------------------------------------------------------------------------
async def test_service_token_path_still_works(monkeypatch):
    secret = "s" * 40
    monkeypatch.setattr(
        auth,
        "_service_tokens_cache",
        {secret: {"uc_uid": "svc-user", "agent_id": "receipt-reconciler"}},
    )
    ctx = await authenticate_token(secret)
    assert ctx.uc_uid == "svc-user"
    assert ctx.agent_id == "receipt-reconciler"
    assert ctx.scopes == ("service",)
