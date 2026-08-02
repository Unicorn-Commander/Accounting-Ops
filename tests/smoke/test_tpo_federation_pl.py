"""Smoke tests for TPO → AO P&L federation read (feat/tpo-pl-federation-read).

Tests the trusted M2M caller path: a Keycloak client-credentials JWT minted by
`tax-planning-ops` must be accepted on GET /api/workspaces/{workspace_id}/reports/pl
without a workspace_membership row, using a synthesized viewer WorkspaceContext.

All tests run WITHOUT a live stack (no Keycloak, no Postgres) by:
  - Minting RSA-signed JWTs locally (mirrors test_jwt_audience.py pattern)
  - Monkeypatching the JWKS fetch and settings cache
  - Monkeypatching _workspace_exists and _resolve_membership in tenancy

Four required cases per spec:
  1. valid tax-planning-ops token → 200 P&L response with net_income field
  2. missing/invalid token → 401
  3. valid token but wrong client (not in allow-list) → falls through to normal
     membership path → 403 (no membership row)
  4. RLS scoping: M2M caller gets viewer role + entity_scope=None (full workspace,
     read-only); synthesized uc_uid is m2m:<azp> (not a real user_account)
"""
from __future__ import annotations

import contextlib
import time
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException
from jose import jwt

import app.auth as auth_module
import app.tenancy as tenancy_module
from app.api.reports import WorkspaceContext, compute_profit_and_loss
from app.auth import authenticate_token
from app.config import get_settings
from app.tenancy import resolve_workspace_for_federation

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
ISSUER = "https://auth.unicorncommander.ai/realms/uchub"
APP_AUD = "accounting-ops-app"
TPO_CLIENT = "tax-planning-ops"
OTHER_CLIENT = "some-other-service"
KID = "tpo-test-kid"
TEST_WORKSPACE_ID = uuid4()
TEST_WORKSPACE_SLUG = "test-workspace"

# ---------------------------------------------------------------------------
# Key material (one RSA keypair, same pattern as test_jwt_audience.py)
# ---------------------------------------------------------------------------


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
_OTHER_PRIVATE_PEM, _ = _make_keypair()


def _mint(private_pem: str, claims: dict, *, kid: str = KID) -> str:
    return jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": kid})


def _tpo_claims(**overrides) -> dict:
    """Base claims for a client-credentials JWT from tax-planning-ops."""
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        # In a KC client-credentials grant, sub is the service account's internal
        # UUID; azp is the client_id. We use a placeholder sub here.
        "sub": "service-account-tax-planning-ops",
        "azp": TPO_CLIENT,
        "aud": APP_AUD,
        "iat": now,
        "exp": now + 300,
        "scope": "openid",
    }
    claims.update(overrides)
    return claims


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_jwks_and_settings(monkeypatch):
    """Serve the test keypair's public JWK and pin settings with TPO in allow-list."""
    get_settings.cache_clear()

    async def _fake_jwks():
        return {"keys": [_PUBLIC_JWK]}

    monkeypatch.setattr(auth_module, "_fetch_jwks", _fake_jwks)
    monkeypatch.setattr(auth_module, "_fetch_jwks_force_refresh", _fake_jwks)
    monkeypatch.setattr(auth_module, "_service_tokens_cache", {})

    # Patch settings to include TPO in the trusted M2M allow-list
    monkeypatch.setenv("ACCTOPS_TRUSTED_M2M_CALLERS", TPO_CLIENT)
    monkeypatch.setenv("KEYCLOAK_REALM_URL", ISSUER)
    monkeypatch.setenv("ACCEPTED_AUDIENCE", APP_AUD)
    get_settings.cache_clear()

    yield
    get_settings.cache_clear()


@pytest.fixture()
def _patch_workspace_exists(monkeypatch):
    """Patch _workspace_exists so no DB call is needed."""

    async def _fake_workspace_exists(workspace_id):
        if workspace_id == TEST_WORKSPACE_ID:
            return (TEST_WORKSPACE_SLUG, False)
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Workspace not found or not active")

    monkeypatch.setattr(tenancy_module, "_workspace_exists", _fake_workspace_exists)


@pytest.fixture()
def _patch_resolve_membership_raises(monkeypatch):
    """Patch _resolve_membership to raise 403 (no membership row) — simulates
    what happens when a non-member calls the normal membership path."""

    async def _no_membership(user_uc_uid, workspace_id, *, autoprovision=False):
        raise HTTPException(status_code=403, detail="No active membership for this workspace")

    monkeypatch.setattr(tenancy_module, "_resolve_membership", _no_membership)


# ---------------------------------------------------------------------------
# Test 1: valid TPO token → AuthContext has is_trusted_m2m=True
# ---------------------------------------------------------------------------


async def test_tpo_token_sets_is_trusted_m2m():
    """A valid uchub KC token from tax-planning-ops sets is_trusted_m2m=True
    and uses a synthetic uc_uid (m2m:tax-planning-ops)."""
    token = _mint(_PRIVATE_PEM, _tpo_claims())
    ctx = await authenticate_token(token)
    assert ctx.is_trusted_m2m is True
    assert ctx.uc_uid == f"m2m:{TPO_CLIENT}"
    assert ctx.agent_id is None


# ---------------------------------------------------------------------------
# Test 2: missing/invalid token → 401
# ---------------------------------------------------------------------------


async def test_missing_token_raises_401():
    """An empty string is not a valid bearer — 401."""
    with pytest.raises(HTTPException) as exc:
        await authenticate_token("")
    assert exc.value.status_code == 401


async def test_invalid_token_raises_401():
    """A random string that isn't a JWT raises 401."""
    with pytest.raises(HTTPException) as exc:
        await authenticate_token("not-a-jwt-at-all")
    assert exc.value.status_code == 401


async def test_bad_signature_raises_401():
    """A token signed by an unknown key raises 401."""
    token = _mint(_OTHER_PRIVATE_PEM, _tpo_claims())
    with pytest.raises(HTTPException) as exc:
        await authenticate_token(token)
    assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# Test 3: valid token but wrong client (not in allow-list) → is_trusted_m2m=False
# and resolve_workspace_for_federation falls through to membership path → 403
# ---------------------------------------------------------------------------


async def test_non_allowlisted_caller_not_trusted_m2m():
    """A valid KC token whose azp is NOT in ACCTOPS_TRUSTED_M2M_CALLERS must
    NOT set is_trusted_m2m=True — the caller gets the normal membership path."""
    token = _mint(_PRIVATE_PEM, _tpo_claims(azp=OTHER_CLIENT, sub="service-account-other"))
    ctx = await authenticate_token(token)
    assert ctx.is_trusted_m2m is False
    # uc_uid is from sub (no m2m: prefix since it's not trusted)
    assert not ctx.uc_uid.startswith("m2m:")


async def test_non_allowlisted_caller_gets_403_on_federation_endpoint(
    monkeypatch, _patch_resolve_membership_raises
):
    """A caller that isn't in the allow-list falls through to the normal membership
    path. With no membership row that must be 403."""
    token = _mint(_PRIVATE_PEM, _tpo_claims(azp=OTHER_CLIENT, sub="service-account-other"))
    ctx = await authenticate_token(token)
    assert ctx.is_trusted_m2m is False

    # Simulate the FastAPI dependency call path: resolve_workspace_for_federation
    # falls through to resolve_workspace, which calls _resolve_membership (patched
    # to raise 403).
    class _FakeRequest:
        path_params = {"workspace_id": str(TEST_WORKSPACE_ID)}
        headers: dict = {}

    with pytest.raises(HTTPException) as exc:
        await resolve_workspace_for_federation(
            request=_FakeRequest(),  # type: ignore[arg-type]
            auth=ctx,
            workspace_id_header=None,
        )
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# Test 4: RLS scoping — M2M caller gets viewer role + entity_scope=None + no user_account
# ---------------------------------------------------------------------------


async def test_trusted_m2m_workspace_context_is_viewer_scoped(
    _patch_workspace_exists,
):
    """resolve_workspace_for_federation for a trusted M2M caller must synthesize
    a WorkspaceContext with role='viewer', entity_scope=None (full read, no entity
    restriction), and uc_uid='m2m:tax-planning-ops' (no real user_account row)."""
    token = _mint(_PRIVATE_PEM, _tpo_claims())
    ctx = await authenticate_token(token)
    assert ctx.is_trusted_m2m is True

    class _FakeRequest:
        path_params = {"workspace_id": str(TEST_WORKSPACE_ID)}
        headers: dict = {}

    ws = await resolve_workspace_for_federation(
        request=_FakeRequest(),  # type: ignore[arg-type]
        auth=ctx,
        workspace_id_header=None,
    )

    # Role must be viewer — never elevated
    assert ws.role == "viewer"
    # entity_scope=None means full workspace read (all entities visible)
    assert ws.entity_scope is None
    # Synthetic uc_uid — not a real user_account
    assert ws.user_uc_uid == f"m2m:{TPO_CLIENT}"
    # Correct workspace id and slug
    assert ws.workspace_id == TEST_WORKSPACE_ID
    assert ws.workspace_slug == TEST_WORKSPACE_SLUG


async def test_trusted_m2m_cannot_write(
    _patch_workspace_exists,
):
    """The viewer role synthesized for M2M callers must be rejected by
    require_role for any write-level operation."""
    from app.tenancy import require_role

    token = _mint(_PRIVATE_PEM, _tpo_claims())
    ctx = await authenticate_token(token)

    class _FakeRequest:
        path_params = {"workspace_id": str(TEST_WORKSPACE_ID)}
        headers: dict = {}

    ws = await resolve_workspace_for_federation(
        request=_FakeRequest(),  # type: ignore[arg-type]
        auth=ctx,
        workspace_id_header=None,
    )
    # viewer role is accepted for read
    require_role(ws, "viewer")  # must not raise
    # accountant+ is rejected
    with pytest.raises(HTTPException) as exc:
        require_role(ws, "accountant")
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# Test 5: P&L net_income field and units
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _FakePLSession:
    """Fake session for P&L tests: routes entity-label vs aggregation queries."""

    def __init__(self, journal_rows):
        self._journal_rows = journal_rows

    async def execute(self, statement, params=None):
        sql = str(statement)
        # _entity_label queries the entity table for a display_name
        if "FROM entity" in sql:
            return _FakeResult([])  # entity_id=None path returns early, no query
        # Main aggregation query
        return _FakeResult(self._journal_rows)


@contextlib.asynccontextmanager
async def _fake_workspace_session_ctx(session):
    yield session


async def test_pl_net_income_field_present_and_is_dollar_string():
    """compute_profit_and_loss returns a top-level 'net_income' key that is a
    dollar-denominated Decimal serialised as a string (NOT integer cents).
    TPO should parse it as Decimal(response['net_income']).
    """
    import app.api.reports as reports_mod

    # Simulate: revenue $1000, expenses $400 → net_income = $600
    journal_rows = [
        {
            "account_number": "4010",
            "canonical_form": "4010·Revenue",
            "sum_debit": Decimal("0"),
            "sum_credit": Decimal("1000"),
        },
        {
            "account_number": "6210",
            "canonical_form": "6210·Rent",
            "sum_debit": Decimal("400"),
            "sum_credit": Decimal("0"),
        },
    ]
    fake = _FakePLSession(journal_rows)
    original_ws = reports_mod.workspace_session
    reports_mod.workspace_session = lambda *_a, **_k: _fake_workspace_session_ctx(fake)

    ws = WorkspaceContext(
        workspace_id=uuid4(),
        workspace_slug="test",
        role="viewer",
        user_uc_uid=f"m2m:{TPO_CLIENT}",
        agent_id=None,
        entity_scope=None,
        hipaa_protected=False,
    )
    try:
        result = await compute_profit_and_loss(ws, "ytd")
    finally:
        reports_mod.workspace_session = original_ws

    # TPO's first match: 'net_income'
    assert "net_income" in result
    # Must be a dollar-denominated string (e.g. "600" or "600.00"), NOT cents
    net = Decimal(result["net_income"])
    assert net == Decimal("600"), f"Expected 600 (dollars), got {net!r}"
    # Confirm this is dollars, not cents (600 dollars ≠ 600 cents)
    assert net < Decimal("1_000_000"), "Sanity: net_income looks like dollars, not cents"
    # Revenue and expense totals also present
    assert Decimal(result["revenue"]["total"]) == Decimal("1000")
    assert Decimal(result["expenses"]["total"]) == Decimal("400")


# ---------------------------------------------------------------------------
# Test 6: wrong audience → 403 (not trusted even if azp matches)
# ---------------------------------------------------------------------------


async def test_token_with_wrong_audience_rejected():
    """A token from tax-planning-ops that does NOT carry aud=accounting-ops-app
    must be rejected with 403 (audience mismatch) before the azp check."""
    token = _mint(_PRIVATE_PEM, _tpo_claims(aud="some-other-resource"))
    with pytest.raises(HTTPException) as exc:
        await authenticate_token(token)
    assert exc.value.status_code == 403
    assert "audience" in str(exc.value.detail).lower()


# ---------------------------------------------------------------------------
# Test 7: non-existent workspace → 404 for M2M caller
# ---------------------------------------------------------------------------


async def test_m2m_caller_404_for_unknown_workspace(
    _patch_workspace_exists,
):
    """A trusted M2M caller asking for a workspace that doesn't exist gets 404."""
    token = _mint(_PRIVATE_PEM, _tpo_claims())
    ctx = await authenticate_token(token)

    unknown_id = uuid4()

    class _FakeRequest:
        path_params = {"workspace_id": str(unknown_id)}
        headers: dict = {}

    with pytest.raises(HTTPException) as exc:
        await resolve_workspace_for_federation(
            request=_FakeRequest(),  # type: ignore[arg-type]
            auth=ctx,
            workspace_id_header=None,
        )
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Test 8: HIPAA guard — trusted M2M caller is blocked on HIPAA workspaces
# ---------------------------------------------------------------------------


async def test_hipaa_workspace_blocks_federation(monkeypatch):
    """A trusted M2M caller (tax-planning-ops) targeting a HIPAA-flagged
    workspace must get 403 with detail='HIPAA workspace: federation not permitted'.
    No PHI must leave over the unauthenticated federation channel."""
    hipaa_workspace_id = uuid4()

    async def _fake_hipaa_workspace(workspace_id):
        if workspace_id == hipaa_workspace_id:
            return ("hipaa-workspace", True)  # hipaa_protected=True
        raise HTTPException(status_code=404, detail="Workspace not found or not active")

    monkeypatch.setattr(tenancy_module, "_workspace_exists", _fake_hipaa_workspace)

    token = _mint(_PRIVATE_PEM, _tpo_claims())
    ctx = await authenticate_token(token)
    assert ctx.is_trusted_m2m is True

    class _FakeRequest:
        path_params = {"workspace_id": str(hipaa_workspace_id)}
        headers: dict = {}

    with pytest.raises(HTTPException) as exc:
        await resolve_workspace_for_federation(
            request=_FakeRequest(),  # type: ignore[arg-type]
            auth=ctx,
            workspace_id_header=None,
        )
    assert exc.value.status_code == 403
    assert exc.value.detail == "HIPAA workspace: federation not permitted"


async def test_non_hipaa_workspace_allows_federation(monkeypatch):
    """A trusted M2M caller (tax-planning-ops) targeting a non-HIPAA workspace
    must succeed and receive a viewer WorkspaceContext (hipaa_protected=False)."""
    non_hipaa_workspace_id = uuid4()
    non_hipaa_slug = "standard-workspace"

    async def _fake_non_hipaa_workspace(workspace_id):
        if workspace_id == non_hipaa_workspace_id:
            return (non_hipaa_slug, False)  # hipaa_protected=False
        raise HTTPException(status_code=404, detail="Workspace not found or not active")

    monkeypatch.setattr(tenancy_module, "_workspace_exists", _fake_non_hipaa_workspace)

    token = _mint(_PRIVATE_PEM, _tpo_claims())
    ctx = await authenticate_token(token)
    assert ctx.is_trusted_m2m is True

    class _FakeRequest:
        path_params = {"workspace_id": str(non_hipaa_workspace_id)}
        headers: dict = {}

    ws = await resolve_workspace_for_federation(
        request=_FakeRequest(),  # type: ignore[arg-type]
        auth=ctx,
        workspace_id_header=None,
    )

    assert ws.role == "viewer"
    assert ws.entity_scope is None
    assert ws.workspace_id == non_hipaa_workspace_id
    assert ws.workspace_slug == non_hipaa_slug
    assert ws.hipaa_protected is False
    assert ws.user_uc_uid == f"m2m:{TPO_CLIENT}"
