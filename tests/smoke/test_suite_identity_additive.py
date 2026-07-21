"""Smoke tests for the additive suite-identity convergence changes.

Extracted subset of feat/suite-identity-additive (774bdcb), reworked against the
current tree. Covers pieces 1-4 of the extraction (new behaviour only):

  1. uc_uid divergence guard (app.auth._resolve_uc_uid) — D1, applied at all
     FOUR resolution sites (local, inline-Brigade, Keycloak, _try_brigade).
  2. Canonical suite-role projection (app.tenancy.canonical_role) — SUITE-IDENTITY §7.
  3. Membership status/is_default migration (public 0015, renumbered from the
     branch's 0013) — CHECK value set + revision chain.
  4. org_id / tenant_id-claim removal from the deprecated resolve_tenant — §1/D2.

Deliberately NOT covered (deferred with their pillars): the entitlement-SDK
delegation parity tests and the 0012 registry-mirror value-set tests — the
registry-mirror pillar is superseded by main's uc_org_id/uc_registry_id bridge.

No live stack required (pure-function + light fakes).
"""
from __future__ import annotations

import logging

import pytest


# ===========================================================================
# 1. uc_uid divergence guard (SUITE-IDENTITY D1)
# ===========================================================================
class TestUcUidDivergenceGuard:
    def test_uc_uid_equals_sub_returns_it(self):
        from app.auth import _resolve_uc_uid

        assert _resolve_uc_uid({"sub": "u1", "uc_uid": "u1"}) == "u1"

    def test_uc_uid_absent_falls_back_to_sub(self):
        from app.auth import _resolve_uc_uid

        assert _resolve_uc_uid({"sub": "u1"}) == "u1"

    def test_sub_absent_uses_uc_uid(self):
        # A token with only uc_uid (no sub) — still resolves (no divergence).
        from app.auth import _resolve_uc_uid

        assert _resolve_uc_uid({"uc_uid": "u1"}) == "u1"

    def test_neither_present_returns_none(self):
        from app.auth import _resolve_uc_uid

        assert _resolve_uc_uid({}) is None

    def test_divergence_uses_sub_and_logs_error(self, caplog):
        """The binding behaviour: uc_uid != sub ⇒ ERROR log + resolve to sub."""
        from app.auth import _resolve_uc_uid

        with caplog.at_level(logging.ERROR, logger="acctops.auth"):
            got = _resolve_uc_uid({"sub": "canonical-sub", "uc_uid": "divergent"})
        assert got == "canonical-sub"  # sub wins, NEVER the divergent uc_uid
        assert any(
            "uc_uid_diverges_from_sub" in rec.message for rec in caplog.records
        ), [r.message for r in caplog.records]


# ===========================================================================
# 2. Canonical suite-role projection (SUITE-IDENTITY §7)
# ===========================================================================
class TestCanonicalRole:
    @pytest.mark.parametrize(
        "app_role,expected",
        [
            ("viewer", "viewer"),
            ("client_user", "viewer"),
            ("bookkeeper", "member"),
            ("accountant", "manager"),
            ("cpa", "manager"),
            ("admin", "admin"),
            ("owner", "owner"),
            ("client_owner", "owner"),
        ],
    )
    def test_mapping_matches_spec(self, app_role, expected):
        from app.tenancy import canonical_role

        assert canonical_role(app_role) == expected

    def test_unknown_role_is_least_privilege(self):
        from app.tenancy import canonical_role

        # platform_admin is NOT a workspace role; it + any unknown -> viewer.
        assert canonical_role("platform_admin") == "viewer"
        assert canonical_role("totally-unknown") == "viewer"

    def test_internal_rank_is_untouched(self):
        """Canonicalisation is reporting-only — AO's authz rank is unchanged."""
        from app.tenancy import _ROLE_RANK, role_at_least

        # The rich 9-rank ladder still exists and still gates as before.
        assert _ROLE_RANK["owner"] == 5
        assert _ROLE_RANK["platform_admin"] == 100
        assert role_at_least("accountant", "bookkeeper") is True
        assert role_at_least("viewer", "accountant") is False


# ===========================================================================
# 3. org_id / tenant_id-claim removal from deprecated resolve_tenant (§1/D2)
# ===========================================================================
class _FakeRequest:
    def __init__(self, path_params: dict | None = None):
        self.path_params = path_params or {}


def _auth_ctx(claims: dict):
    from app.auth import AuthContext

    return AuthContext(
        uc_uid=claims.get("sub", "u"),
        agent_id=None,
        scopes=(),
        claims=claims,
    )


class TestResolveTenantWorkspaceIdOnly:
    async def test_org_id_is_not_a_fallback(self):
        """A token carrying ONLY org_id (the deprecated split-meaning key) must
        NOT resolve a tenant — resolve_tenant reads workspace_id only now."""
        from fastapi import HTTPException

        from app.tenancy import resolve_tenant

        req = _FakeRequest()
        auth = _auth_ctx({"sub": "u", "org_id": "11111111-1111-1111-1111-111111111111"})
        with pytest.raises(HTTPException) as exc:
            await resolve_tenant(req, auth=auth, tenant_id_header=None)
        assert exc.value.status_code == 400

    async def test_tenant_id_claim_is_not_a_fallback(self):
        """The legacy tenant_id CLAIM is also no longer read (only the path param
        / X-Tenant-ID header remain, for pre-existing URLs)."""
        from fastapi import HTTPException

        from app.tenancy import resolve_tenant

        req = _FakeRequest()
        auth = _auth_ctx({"sub": "u", "tenant_id": "22222222-2222-2222-2222-222222222222"})
        with pytest.raises(HTTPException) as exc:
            await resolve_tenant(req, auth=auth, tenant_id_header=None)
        assert exc.value.status_code == 400

    async def test_workspace_id_claim_still_resolves(self):
        """workspace_id is the canonical key and still resolves the deprecated path."""
        from app.tenancy import resolve_tenant

        wid = "33333333-3333-3333-3333-333333333333"
        req = _FakeRequest()
        auth = _auth_ctx({"sub": "u", "workspace_id": wid})
        ctx = await resolve_tenant(req, auth=auth, tenant_id_header=None)
        assert str(ctx.tenant_id) == wid


# ===========================================================================
# 4. Membership-status migration (public 0015) — value set + revision chain
#    (live DB enforcement is proven in the alembic run)
# ===========================================================================
def _load_migration(filename: str):
    """Load a migration module by path (digit-prefixed names aren't importable)."""
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    path = root / "migrations" / "public" / "versions" / filename
    spec = importlib.util.spec_from_file_location(filename[:-3], path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestMembershipStatusMigration:
    def test_membership_status_values(self):
        m = _load_migration("0015_membership_status.py")
        assert set(m.MEMBERSHIP_STATUS_VALUES) == {"active", "suspended", "revoked"}

    def test_revision_chain(self):
        """Renumbered onto the current public chain: 0015 revises 0014 (the
        branch's original 0013 slot is occupied by main's uc_registry_id
        bridge migration — extending the head instead keeps a single head)."""
        m = _load_migration("0015_membership_status.py")
        assert m.revision == "0015_membership_status"
        assert m.down_revision == "0014_regulatory_feed_health"
