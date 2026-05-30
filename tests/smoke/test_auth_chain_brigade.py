"""Auth-chain integration tests: authenticate_token routes to Brigade verifier.

Confirms the chain wired in app/auth.py:
  Service token -> Keycloak verify -> Brigade verify -> 401

Specifically:
  - A valid Brigade JWT passes (Keycloak fails first, Brigade succeeds).
  - A Keycloak token still works unchanged (no Brigade involvement).
  - A token that's neither still 401s.
  - Brigade-side 403 (audience mismatch on a Keycloak token) does NOT
    silently fall through to Brigade (the existing audience check stays
    authoritative — Brigade is a SECOND chance for token recognition, not
    a workaround for Keycloak authz).
"""
from __future__ import annotations

import base64
import json
import time

import pytest
from fastapi import HTTPException

import app.auth as auth
import app.security.brigade_jwt as bj
from app.config import get_settings

# Re-use the helpers from test_brigade_jwt
from tests.smoke.test_brigade_jwt import (
    EXPECTED_AUD,
    KID_ED,
    TRUSTED_ISS,
    _ED_JWK,
    _ED_PRIV,
    _RS_JWK,
    _b64url,
    _base_claims,
    _make_ed25519_keys,
    _mint_ed25519,
)

# Keycloak test material: re-use the existing test_jwt_audience helpers.
from tests.smoke.test_jwt_audience import (
    APP_AUD,
    ISSUER as KC_ISSUER,
    KID as KC_KID,
    _PRIVATE_PEM as KC_PRIV_PEM,
    _PUBLIC_JWK as KC_PUB_JWK,
    _base_claims as kc_base_claims,
    _mint as kc_mint,
)


@pytest.fixture(autouse=True)
def _serve_both_jwks(monkeypatch):
    """Serve BOTH the Keycloak JWKS and the Brigade JWKS, depending on caller."""
    get_settings.cache_clear()
    bj._reset_cache_for_tests()

    async def _fake_kc_jwks():
        return {"keys": [KC_PUB_JWK]}

    async def _fake_brigade_jwks():
        return {"keys": [_ED_JWK, _RS_JWK]}

    monkeypatch.setattr(auth, "_fetch_jwks", _fake_kc_jwks)
    monkeypatch.setattr(auth, "_fetch_jwks_force_refresh", _fake_kc_jwks)
    monkeypatch.setattr(bj, "_fetch_jwks", _fake_brigade_jwks)
    monkeypatch.setattr(bj, "_fetch_jwks_force_refresh", _fake_brigade_jwks)
    # Ensure no stray service tokens.
    monkeypatch.setattr(auth, "_service_tokens_cache", {})
    yield
    get_settings.cache_clear()
    bj._reset_cache_for_tests()


async def test_brigade_jwt_passes_via_authenticate_token():
    """A valid Brigade JWT must succeed via the unified authenticate_token entrypoint.

    Keycloak verifier fails first (kid not in KC JWKS), the chain falls through
    to Brigade, Brigade succeeds, ctx is returned.
    """
    token = _mint_ed25519(_ED_PRIV, _base_claims(sub="aaron-from-brigade"))
    ctx = await auth.authenticate_token(token)
    assert ctx.uc_uid == "aaron-from-brigade"


async def test_brigade_jwt_with_uc_uid_claim_prefers_it():
    """When Brigade carries an explicit uc_uid claim, prefer it over sub."""
    claims = _base_claims(sub="brigade-internal-id", uc_uid="aaron")
    token = _mint_ed25519(_ED_PRIV, claims)
    ctx = await auth.authenticate_token(token)
    assert ctx.uc_uid == "aaron"


async def test_brigade_jwt_extracts_scope_list():
    """Brigade may pass scopes as a list; the AuthContext.scopes tuple must reflect it."""
    claims = _base_claims(scope=None)
    claims["scopes"] = ["read:accounting", "write:accounting"]
    token = _mint_ed25519(_ED_PRIV, claims)
    ctx = await auth.authenticate_token(token)
    assert "read:accounting" in ctx.scopes
    assert "write:accounting" in ctx.scopes


async def test_brigade_jwt_extracts_act_claim():
    """RFC 8693 act handling works on the Brigade path too."""
    claims = _base_claims(act={"sub": "tax-1120-preparer"})
    token = _mint_ed25519(_ED_PRIV, claims)
    ctx = await auth.authenticate_token(token)
    assert ctx.agent_id == "tax-1120-preparer"


async def test_keycloak_token_still_works():
    """The existing Keycloak path is unchanged — a uchub token still passes."""
    token = kc_mint(KC_PRIV_PEM, kc_base_claims(aud=APP_AUD))
    ctx = await auth.authenticate_token(token)
    assert ctx.uc_uid == "aaron"


async def test_brigade_aud_mismatch_falls_through_to_401():
    """A Brigade JWT for a DIFFERENT audience fails Brigade verify -> 401.

    Critical: a brigade-signed token for app B must NOT authenticate against
    app A even though the issuer + signature are fine.
    """
    token = _mint_ed25519(_ED_PRIV, _base_claims(aud="some-other-app"))
    with pytest.raises(HTTPException) as exc:
        await auth.authenticate_token(token)
    assert exc.value.status_code == 401


async def test_garbage_token_still_401s():
    """A token that's neither Keycloak nor Brigade still 401s."""
    with pytest.raises(HTTPException) as exc:
        await auth.authenticate_token("garbage")
    assert exc.value.status_code == 401


async def test_keycloak_403_does_not_silently_fall_through_to_brigade():
    """If a Keycloak token authenticates but has the wrong audience -> 403.

    The chain MUST NOT then try Brigade with that same token (Brigade has its
    own audience gate; the operator's intent was clearly to use Keycloak and
    the audience denial is the correct answer). Confirms we propagate 403
    immediately rather than burying it under a generic 401.
    """
    bad_aud_kc_token = kc_mint(
        KC_PRIV_PEM, kc_base_claims(aud=["account", "some-other-app"])
    )
    with pytest.raises(HTTPException) as exc:
        await auth.authenticate_token(bad_aud_kc_token)
    assert exc.value.status_code == 403


async def test_brigade_unknown_kid_still_fails(monkeypatch):
    """A Brigade-style token signed by a key NOT in the served JWKS -> 401."""
    other_priv, _ = _make_ed25519_keys("not-in-jwks")
    token = _mint_ed25519(other_priv, _base_claims(), kid="not-in-jwks")
    with pytest.raises(HTTPException) as exc:
        await auth.authenticate_token(token)
    assert exc.value.status_code == 401


async def test_service_token_still_wins(monkeypatch):
    """Service tokens still short-circuit ahead of Keycloak AND Brigade."""
    secret = "svc" + "x" * 40
    monkeypatch.setattr(
        auth,
        "_service_tokens_cache",
        {secret: {
            "uc_uid": "svc-user",
            "agent_id": "receipt-reconciler",
            "workspace_ids": None,
            "entity_ids": None,
            "role_floor": None,
        }},
    )
    ctx = await auth.authenticate_token(secret)
    assert ctx.uc_uid == "svc-user"
    assert ctx.agent_id == "receipt-reconciler"
