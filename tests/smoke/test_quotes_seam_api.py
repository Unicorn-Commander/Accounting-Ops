"""Smoke tests for POST /api/workspaces/{wid}/entities/{eid}/quotes/accepted (M2M rail).

Runs WITHOUT a live stack: RSA-signed JWTs minted locally, the JWKS fetch and
settings patched (mirrors tests/smoke/test_tpo_federation_pl.py), the
workspace existence check + the seam service + the DB session patched.

  1. customer-ops client-credentials JWT → 201, actor m2m:customer-ops
  2. replay → 200 with the same ids
  3. a different trusted M2M caller (tax-planning-ops) → 403 (wrong actor)
  4. missing / bad token → 401; wrong audience → 403
  5. HIPAA workspace → 403; unknown workspace → 404
  6. token scoped to another workspace → 403 (re-gate)
  7. a plain member with viewer role → 403; accountant member → 201
  8. seam errors carry {code, reason, federated} (PERIOD_CLOSED → 409)
  9. review H5: a HUMAN-shaped token riding azp=customer-ops (sid/session_state,
     no clientId, no service-account identity) → 403; the harness shape
     (sub=service-account-…) and the realm shape (preferred_username) both pass
 10. review H6: a Brigade-minted m2m:customer-ops context → 403 on this write route
 11. review L19: QUOTES_ENABLED off → 404 QUOTES_DISABLED
"""
from __future__ import annotations

import contextlib
import time
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from jose import jwt

import app.api.quotes_seam as seam_api
import app.auth as auth_module
import app.tenancy as tenancy_module
from app.ar.quotes_seam import QuoteSeamError
from app.config import get_settings
from app.tenancy import WorkspaceContext

ISSUER = "https://auth.unicorncommander.ai/realms/uchub"
APP_AUD = "accounting-ops-app"
CUSTOMER_OPS = "customer-ops"
TPO = "tax-planning-ops"
KID = "quotes-seam-test-kid"
WORKSPACE_ID = uuid4()
OTHER_WORKSPACE_ID = uuid4()
HIPAA_WORKSPACE_ID = uuid4()
ENTITY_ID = uuid4()


def _make_keypair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jose.backends.cryptography_backend import CryptographyRSAKey

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ).decode()
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    jwk = CryptographyRSAKey(public_pem, "RS256").to_dict()
    jwk["kid"] = KID
    jwk["use"] = "sig"
    return private_pem, jwk


_PRIVATE_PEM, _PUBLIC_JWK = _make_keypair()


def _mint(claims: dict) -> str:
    return jwt.encode(claims, _PRIVATE_PEM, algorithm="RS256", headers={"kid": KID})


def _m2m_claims(azp: str = CUSTOMER_OPS, **overrides) -> dict:
    """A Keycloak client-credentials token as a real uchub realm mints it:
    sub = the service-account user's UUID, preferred_username =
    service-account-<client>, clientId = <client>, no sid/session_state."""
    now = int(time.time())
    claims = {
        "iss": ISSUER, "sub": str(uuid4()), "azp": azp, "aud": APP_AUD,
        "clientId": azp, "preferred_username": f"service-account-{azp}",
        "iat": now, "exp": now + 300, "scope": "openid",
    }
    claims.update(overrides)
    return claims


def _human_claims(azp: str = CUSTOMER_OPS, **overrides) -> dict:
    """A HUMAN who logged in through the customer-ops client: same azp, but a
    browser session, a real username and no clientId mapper claim."""
    now = int(time.time())
    claims = {
        "iss": ISSUER, "sub": str(uuid4()), "azp": azp, "aud": APP_AUD,
        "preferred_username": "aaron", "email": "aaron@example.invalid", "email_verified": True,
        "sid": str(uuid4()), "session_state": str(uuid4()),
        "iat": now, "exp": now + 300, "scope": "openid",
    }
    claims.update(overrides)
    return claims


def _payload() -> dict:
    return {
        "quote_ref": "Q-2026-0001",
        "version": 2,
        "customer": {"display_name": "Ops Lead", "email": "ops@apparel-client.example",
                     "org": "the apparel client", "contact_id": "co-2467436b"},
        "currency": "USD",
        "lines": [
            {"kind": "one_time", "label": "Build", "amount_cents": 140_000, "position": 1},
            {"kind": "milestone", "label": "Milestone 1", "amount_cents": 200_000, "position": 2},
            {"kind": "recurring", "label": "Managed hosting", "amount_cents": 35_000, "position": 3,
             "recurring": {"interval": "month", "term_months": 12}},
        ],
        "totals": {"one_time_total_cents": 140_000, "milestone_total_cents": 200_000,
                   "deposit_cents": 170_000, "recurring": {"month": 35_000}},
        "delivery_partner": {"name": "Synthetic Delivery Partner LLC", "contact_id": "co-partner-1"},
        "split_rule": {"kind": "percent_net", "percent": 50, "hosting_to_partner": True,
                       "direct_costs_total_cents": 20_000},
    }


class _Recorder:
    """Stands in for apply_quote_acceptance: records the actor, returns canned ids."""

    def __init__(self):
        self.calls: list[dict] = []
        self.raise_with: QuoteSeamError | None = None
        self.ids = {
            "ar_customer_id": str(uuid4()), "deposit_invoice_id": str(uuid4()),
            "milestone_invoice_ids": [str(uuid4())], "schedule_ids": [str(uuid4())],
            "partner_invoice_id": str(uuid4()),
        }

    async def __call__(self, session, *, workspace_id, entity_id, actor_uc_uid, payload, today=None):
        self.calls.append({"workspace_id": workspace_id, "entity_id": entity_id,
                           "actor_uc_uid": actor_uc_uid, "quote_ref": payload.quote_ref,
                           "version": payload.version})
        if self.raise_with is not None:
            raise self.raise_with
        created = len(self.calls) == 1
        return {
            "quote_ref": f"{payload.quote_ref} v{payload.version}", "created": created,
            "replayed": not created, **self.ids,
            "federated": {"accounting_ops": "done", "partner": "done", "reason": None},
        }


@pytest.fixture(autouse=True)
def _patch_jwks_and_settings(monkeypatch):
    get_settings.cache_clear()

    async def _fake_jwks():
        return {"keys": [_PUBLIC_JWK]}

    monkeypatch.setattr(auth_module, "_fetch_jwks", _fake_jwks)
    monkeypatch.setattr(auth_module, "_fetch_jwks_force_refresh", _fake_jwks)
    monkeypatch.setattr(auth_module, "_service_tokens_cache", {})
    monkeypatch.setenv("ACCTOPS_TRUSTED_M2M_CALLERS", f"{CUSTOMER_OPS} {TPO}")
    monkeypatch.setenv("KEYCLOAK_REALM_URL", ISSUER)
    monkeypatch.setenv("ACCEPTED_AUDIENCE", APP_AUD)
    monkeypatch.setenv("AO_RATE_LIMIT_ENABLED", "off")
    monkeypatch.setenv("QUOTES_ENABLED", "1")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def recorder(monkeypatch) -> _Recorder:
    rec = _Recorder()

    async def _fake_workspace_exists(workspace_id):
        if workspace_id == WORKSPACE_ID:
            return ("apparel-vendor", False)
        if workspace_id == HIPAA_WORKSPACE_ID:
            return ("hipaa-ws", True)
        raise HTTPException(status_code=404, detail="Workspace not found or not active")

    @contextlib.asynccontextmanager
    async def _fake_session(_sessionmaker, _workspace_id):
        yield object()

    monkeypatch.setattr(tenancy_module, "_workspace_exists", _fake_workspace_exists)
    monkeypatch.setattr(seam_api, "_workspace_exists", _fake_workspace_exists)
    monkeypatch.setattr(seam_api, "workspace_session", _fake_session)
    monkeypatch.setattr(seam_api, "apply_quote_acceptance", rec)
    return rec


@pytest.fixture()
def client():
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _url(wid=WORKSPACE_ID, eid=ENTITY_ID) -> str:
    return f"/api/workspaces/{wid}/entities/{eid}/quotes/accepted"


async def test_customer_ops_m2m_creates_then_replays(client, recorder):
    token = _mint(_m2m_claims())
    async with client as c:
        r = await c.post(_url(), json=_payload(), headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["created"] is True
        assert body["deposit_invoice_id"] == recorder.ids["deposit_invoice_id"]
        assert body["federated"]["accounting_ops"] == "done"
        # Actor + workspace + entity flow through to the service.
        call = recorder.calls[-1]
        assert call["actor_uc_uid"] == f"m2m:{CUSTOMER_OPS}"
        assert call["workspace_id"] == str(WORKSPACE_ID)
        assert call["entity_id"] == str(ENTITY_ID)
        assert (call["quote_ref"], call["version"]) == ("Q-2026-0001", 2)
        # Replay: 200, same ids.
        r2 = await c.post(_url(), json=_payload(), headers={"Authorization": f"Bearer {token}"})
        assert r2.status_code == 200, r2.text
        assert r2.json()["deposit_invoice_id"] == body["deposit_invoice_id"]
        assert r2.json()["replayed"] is True


async def test_other_trusted_m2m_caller_is_refused(client, recorder):
    token = _mint(_m2m_claims(azp=TPO))
    async with client as c:
        r = await c.post(_url(), json=_payload(), headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403, r.text
    assert CUSTOMER_OPS in r.json()["detail"]
    assert recorder.calls == []


async def test_missing_bad_token_and_wrong_audience(client, recorder):
    async with client as c:
        r = await c.post(_url(), json=_payload())
        assert r.status_code == 401
        r = await c.post(_url(), json=_payload(), headers={"Authorization": "Bearer not-a-jwt"})
        assert r.status_code == 401
        token = _mint(_m2m_claims(aud="some-other-resource"))
        r = await c.post(_url(), json=_payload(), headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403
    assert recorder.calls == []


async def test_hipaa_and_unknown_workspace(client, recorder):
    token = _mint(_m2m_claims())
    async with client as c:
        r = await c.post(_url(wid=HIPAA_WORKSPACE_ID), json=_payload(),
                         headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403 and "HIPAA" in r.json()["detail"]
        r = await c.post(_url(wid=uuid4()), json=_payload(), headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 404
    assert recorder.calls == []


async def test_workspace_claim_regate(client, recorder):
    # A token scoped (workspace_id claim) to ANOTHER workspace cannot federate this one.
    token = _mint(_m2m_claims(workspace_id=str(OTHER_WORKSPACE_ID)))
    async with client as c:
        r = await c.post(_url(), json=_payload(), headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403 and "scoped workspace" in r.json()["detail"]
        # ... while a matching claim passes.
        token_ok = _mint(_m2m_claims(workspace_id=str(WORKSPACE_ID)))
        r = await c.post(_url(), json=_payload(), headers={"Authorization": f"Bearer {token_ok}"})
        assert r.status_code == 201, r.text


async def test_member_path_role_floor(client, recorder, monkeypatch):
    """A non-M2M caller rides the normal membership path: viewer 403, accountant 201."""
    role_holder = {"role": "viewer"}

    async def _fake_membership(user_uc_uid, workspace_id, *, autoprovision=False):
        return role_holder["role"], None, False, "apparel-vendor"

    monkeypatch.setattr(tenancy_module, "_resolve_membership", _fake_membership)

    async def _no_identity_ensure(uc_uid, claims):
        return None

    monkeypatch.setattr(auth_module, "_ensure_keycloak_user_identity", _no_identity_ensure)
    now = int(time.time())
    token = _mint({"iss": ISSUER, "sub": "user-accountant-1", "aud": APP_AUD, "azp": "accounting-ops-web",
                   "iat": now, "exp": now + 300, "scope": "openid", "email_verified": True})
    async with client as c:
        r = await c.post(_url(), json=_payload(), headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403, r.text
        assert recorder.calls == []
        role_holder["role"] = "accountant"
        r = await c.post(_url(), json=_payload(), headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 201, r.text
        assert recorder.calls[-1]["actor_uc_uid"] == "user-accountant-1"


async def test_seam_error_carries_federated_status(client, recorder):
    recorder.raise_with = QuoteSeamError("PERIOD_CLOSED", "issue date is in a closed period", http_status=409)
    token = _mint(_m2m_claims())
    async with client as c:
        r = await c.post(_url(), json=_payload(), headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "PERIOD_CLOSED"
    assert detail["federated"] == {"accounting_ops": "failed", "partner": "skipped",
                                   "reason": "issue date is in a closed period"}


async def test_payload_validation_is_422(client, recorder):
    token = _mint(_m2m_claims())
    bad = _payload()
    bad["lines"] = []
    async with client as c:
        r = await c.post(_url(), json=bad, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 422
    assert recorder.calls == []


async def test_human_shaped_token_riding_customer_ops_azp_is_refused(client, recorder):
    """H5: azp=customer-ops alone is not a machine. A user session through that
    client is refused on the write rail — and never reaches the service."""
    token = _mint(_human_claims())
    async with client as c:
        r = await c.post(_url(), json=_payload(), headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403, r.text
        assert "client-credentials" in r.json()["detail"]
        # Missing clientId mapper claim alone is also refused ...
        token = _mint(_m2m_claims(clientId=None))
        r = await c.post(_url(), json=_payload(), headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403, r.text
        # ... and so is a session marker on an otherwise machine-shaped token.
        token = _mint(_m2m_claims(sid=str(uuid4())))
        r = await c.post(_url(), json=_payload(), headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403, r.text
    assert recorder.calls == []


async def test_both_service_account_identity_shapes_are_accepted(client, recorder):
    """The suite's harness shape (sub = service-account-<client>) and the realm
    shape (preferred_username) both satisfy the machine proof."""
    harness = _mint(_m2m_claims(sub=f"service-account-{CUSTOMER_OPS}", preferred_username=None))
    realm = _mint(_m2m_claims())
    async with client as c:
        r = await c.post(_url(), json=_payload(), headers={"Authorization": f"Bearer {harness}"})
        assert r.status_code == 201, r.text
        r = await c.post(_url(), json=_payload(), headers={"Authorization": f"Bearer {realm}"})
        assert r.status_code == 200, r.text   # replay of the same quote_ref + version
    assert all(c["actor_uc_uid"] == f"m2m:{CUSTOMER_OPS}" for c in recorder.calls)


async def test_brigade_minted_m2m_context_is_excluded_from_the_write_route(recorder):
    """H6: the Brigade door sets is_trusted_m2m + uc_uid=m2m:customer-ops too, but
    carries no client-credentials shape (m2m_proof='brigade-actor') → 403 here."""
    from app.auth import AuthContext

    ctx = AuthContext(
        uc_uid=f"m2m:{CUSTOMER_OPS}", agent_id=CUSTOMER_OPS, scopes=(), claims={"iss": "https://brigade.example"},
        is_trusted_m2m=True, m2m_proof="brigade-actor",
    )

    class _FakeRequest:
        path_params = {"workspace_id": str(WORKSPACE_ID)}
        headers: dict = {}

    with pytest.raises(HTTPException) as exc:
        await seam_api.resolve_workspace_for_quotes_seam(
            request=_FakeRequest(), auth=ctx, workspace_id_header=None,  # type: ignore[arg-type]
        )
    assert exc.value.status_code == 403
    assert "Brigade" in str(exc.value.detail)
    # The same identity WITH the Keycloak client-credentials proof is accepted.
    ok = AuthContext(
        uc_uid=f"m2m:{CUSTOMER_OPS}", agent_id=None, scopes=(), claims={},
        is_trusted_m2m=True, m2m_proof="keycloak-client-credentials",
    )
    ws = await seam_api.resolve_workspace_for_quotes_seam(
        request=_FakeRequest(), auth=ok, workspace_id_header=None,  # type: ignore[arg-type]
    )
    assert ws.role == "accountant" and ws.user_uc_uid == f"m2m:{CUSTOMER_OPS}"


async def test_fence_off_returns_404_quotes_disabled(client, recorder, monkeypatch):
    monkeypatch.setenv("QUOTES_ENABLED", "0")
    get_settings.cache_clear()
    token = _mint(_m2m_claims())
    async with client as c:
        r = await c.post(_url(), json=_payload(), headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 404, r.text
        assert r.json()["detail"]["code"] == "QUOTES_DISABLED"
    assert recorder.calls == []
    # The schedule routes and the MCP tools share the same fence helper.
    from app.api.quotes_seam import _assert_enabled

    with pytest.raises(HTTPException) as exc:
        _assert_enabled()
    assert exc.value.status_code == 404 and exc.value.detail["code"] == "QUOTES_DISABLED"


def test_dependency_synthesizes_seam_context_only_for_customer_ops():
    """The synthesized context is accountant + agent_id=customer-ops + synthetic uc_uid."""
    ctx = WorkspaceContext(
        workspace_id=WORKSPACE_ID, workspace_slug="apparel-vendor", role="accountant",
        user_uc_uid=f"m2m:{CUSTOMER_OPS}", agent_id=CUSTOMER_OPS, entity_scope=None, hipaa_protected=False,
    )
    assert ctx.pat_scopes is None
    assert seam_api.QUOTES_SEAM_ACTOR == CUSTOMER_OPS
