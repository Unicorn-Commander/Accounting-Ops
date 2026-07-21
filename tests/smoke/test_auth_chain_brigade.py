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


import pytest
from fastapi import HTTPException

import app.auth as auth
import app.security.brigade_jwt as bj
from app.config import get_settings

# Re-use the helpers from test_brigade_jwt
from tests.smoke.test_brigade_jwt import (
    _ED_JWK,
    _ED_PRIV,
    _RS_JWK,
    _base_claims,
    _make_ed25519_keys,
    _mint_ed25519,
)

# Keycloak test material: re-use the existing test_jwt_audience helpers.
from tests.smoke.test_jwt_audience import (
    APP_AUD,
    ISSUER as KC_ISSUER,
    _PRIVATE_PEM as KC_PRIV_PEM,
    _PUBLIC_JWK as KC_PUB_JWK,
    _base_claims as kc_base_claims,
    _mint as kc_mint,
)


@pytest.fixture(autouse=True)
def _serve_both_jwks(monkeypatch):
    """Serve BOTH the Keycloak JWKS and the Brigade JWKS, depending on caller.

    KEYCLOAK_REALM_URL is pinned to the issuer these tokens are minted with;
    the shipped Settings default is deployment/edition-specific and must not
    be what the Keycloak leg of this chain validates against.
    """
    monkeypatch.setenv("KEYCLOAK_REALM_URL", KC_ISSUER)
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
    token = _mint_ed25519(_ED_PRIV, _base_claims(sub="jane-from-brigade"))
    ctx = await auth.authenticate_token(token)
    assert ctx.uc_uid == "jane-from-brigade"


async def test_brigade_jwt_uc_uid_equals_sub_conformant():
    """SUITE-IDENTITY D1: a conformant Brigade token carries uc_uid == sub and
    resolves to that single identity (the normal case)."""
    claims = _base_claims(sub="jane", uc_uid="jane")
    token = _mint_ed25519(_ED_PRIV, claims)
    ctx = await auth.authenticate_token(token)
    assert ctx.uc_uid == "jane"


async def test_brigade_jwt_diverging_uc_uid_uses_sub_and_logs(caplog):
    """SUITE-IDENTITY D1 (binding): if a token carries uc_uid != sub, AO MUST log
    LOUDLY at ERROR and resolve to ``sub`` — never the divergent uc_uid (which
    would be a silent re-keying trap). This replaces the retired pre-convergence
    'prefer uc_uid over sub' behaviour.
    """
    import logging

    claims = _base_claims(sub="brigade-canonical-sub", uc_uid="jane-divergent")
    token = _mint_ed25519(_ED_PRIV, claims)
    with caplog.at_level(logging.ERROR, logger="acctops.auth"):
        ctx = await auth.authenticate_token(token)
    # Resolves to sub, NOT the divergent uc_uid.
    assert ctx.uc_uid == "brigade-canonical-sub"
    # And it logged the divergence loudly with the stable grep-able marker.
    assert any(
        "uc_uid_diverges_from_sub" in rec.message for rec in caplog.records
    ), [rec.message for rec in caplog.records]


async def test_brigade_jwt_extracts_scope_list():
    """Brigade may pass scopes as a list; the AuthContext.scopes tuple must reflect it."""
    claims = _base_claims(scope=None)
    claims["scopes"] = ["read:accounting", "write:accounting"]
    token = _mint_ed25519(_ED_PRIV, claims)
    ctx = await auth.authenticate_token(token)
    assert "read:accounting" in ctx.scopes
    assert "write:accounting" in ctx.scopes


async def test_brigade_jwt_scope_string_and_list_both_parse():
    """Both the OIDC ``scope`` (space-delimited string) and Brigade-preferred
    ``scopes`` (list) forms must populate AuthContext.scopes identically.

    Per the federation contract (``agents/_shared/brigade_federation.md``):
      * ``scopes`` (list) — preferred, order preserved.
      * ``scope`` (str)   — OIDC default, whitespace-split.
      * Both present     — plural ``scopes`` wins.
    """
    # 1. String form (OIDC default).
    claims_str = _base_claims(scope="read:accounting write:accounting admin:journal")
    claims_str.pop("scopes", None)
    token_str = _mint_ed25519(_ED_PRIV, claims_str)
    ctx_str = await auth.authenticate_token(token_str)
    assert ctx_str.scopes == ("read:accounting", "write:accounting", "admin:journal"), (
        f"OIDC scope string did not parse to ordered tuple: {ctx_str.scopes!r}"
    )

    # 2. List form (Brigade-preferred).
    claims_list = _base_claims(scope=None)
    claims_list["scopes"] = ["read:accounting", "write:accounting", "admin:journal"]
    token_list = _mint_ed25519(_ED_PRIV, claims_list)
    ctx_list = await auth.authenticate_token(token_list)
    assert ctx_list.scopes == (
        "read:accounting",
        "write:accounting",
        "admin:journal",
    ), f"List scopes did not parse to ordered tuple: {ctx_list.scopes!r}"

    # 3. Both present — the plural form wins (Brigade's preferred semantics).
    claims_both = _base_claims(scope="only:from:singular")
    claims_both["scopes"] = ["from:plural:1", "from:plural:2"]
    token_both = _mint_ed25519(_ED_PRIV, claims_both)
    ctx_both = await auth.authenticate_token(token_both)
    assert ctx_both.scopes == ("from:plural:1", "from:plural:2"), (
        f"Plural ``scopes`` should win when both present, got {ctx_both.scopes!r}"
    )
    assert "only:from:singular" not in ctx_both.scopes


async def test_brigade_jwt_malformed_scopes_returns_empty_tuple():
    """A malformed scopes claim (wrong type / non-list garbage) must NOT crash
    the auth chain. Authentication still succeeds (the rest of the token is
    valid); the AuthContext just carries an empty scope tuple. Downstream
    subset-validation in app/authz treats empty as "no grant", which is the
    SAFE default (narrower than intent, never wider).
    """
    # ``scopes`` set to an obviously-wrong type (int) — must NOT raise.
    claims = _base_claims(scope=None)
    claims["scopes"] = 42  # not a list, not a string
    token = _mint_ed25519(_ED_PRIV, claims)
    ctx = await auth.authenticate_token(token)
    assert ctx.scopes == (), f"Malformed scopes should degrade to (), got {ctx.scopes!r}"
    assert ctx.uc_uid == "jane"  # auth still succeeded


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
    assert ctx.uc_uid == "jane"


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


async def test_brigade_email_sub_resolves_to_canonical_uc_uid(monkeypatch):
    """Brigade mints sub = email today; AO translates it to the opaque uc_uid via
    user_account.email (the project-ops-style fallback). The AuthContext must
    carry the CANONICAL uc_uid, never the raw email."""

    async def _fake_lookup(email: str):
        assert email == "jane@example.com"
        return "kc-opaque-sub-123"

    monkeypatch.setattr(auth, "_uc_uid_for_email", _fake_lookup)
    token = _mint_ed25519(_ED_PRIV, _base_claims(sub="jane@example.com"))
    ctx = await auth.authenticate_token(token)
    assert ctx.uc_uid == "kc-opaque-sub-123"


async def test_brigade_email_sub_unknown_fails_closed(monkeypatch):
    """An email sub that maps to no active account must FAIL CLOSED (401) — never
    authenticate as the raw email. Brigade can't conjure an AO account."""

    async def _no_match(email: str):
        return None

    monkeypatch.setattr(auth, "_uc_uid_for_email", _no_match)
    token = _mint_ed25519(_ED_PRIV, _base_claims(sub="ghost@nowhere.test"))
    with pytest.raises(HTTPException) as exc:
        await auth.authenticate_token(token)
    assert exc.value.status_code == 401


async def test_brigade_opaque_sub_skips_email_lookup(monkeypatch):
    """A non-email (opaque) sub must NOT trigger the email lookup — forward-compat
    for when Brigade upgrades to minting the opaque uchub sub directly."""
    called = {"n": 0}

    async def _should_not_call(email: str):
        called["n"] += 1
        return None

    monkeypatch.setattr(auth, "_uc_uid_for_email", _should_not_call)
    token = _mint_ed25519(_ED_PRIV, _base_claims(sub="opaque-kc-sub"))
    ctx = await auth.authenticate_token(token)
    assert ctx.uc_uid == "opaque-kc-sub"
    assert called["n"] == 0
