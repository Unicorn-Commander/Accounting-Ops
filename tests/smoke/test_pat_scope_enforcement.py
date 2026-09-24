"""PAT scope enforcement at the require_role chokepoint.

Until this change, PAT scopes were validated and stored at mint, populated
onto AuthContext at auth time, and then never consulted for authorization.
These tests pin the independent scope AND-gate that now lives in
``require_role`` so a regression that deletes the one-line gate goes red.

Coverage required by the audit fix:
  * Keycloak/session callers (pat_scopes is None) are byte-for-byte unchanged
  * An entitled PAT still reaches a gated route
  * An under-scoped PAT is refused with a scope-specific 403 (not a role 403)
  * Empty PAT scopes fail closed (distinct from "not a PAT")
  * Unknown role minima fail closed for PATs
  * Service-token lane is unaffected
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from app.auth import AuthContext, ServiceScope
from app import mcp_auth
from app.tenancy import (
    WorkspaceContext,
    _pat_holds_scope,
    _pat_scopes_from_auth,
    require_role,
    required_scope_for_role_minimum,
    role_at_least,
)


def _ws(
    role: str = "owner",
    *,
    pat_scopes: tuple[str, ...] | None = None,
) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=uuid.uuid4(),
        workspace_slug="ws",
        role=role,
        user_uc_uid="user-1",
        agent_id=None,
        entity_scope=None,
        hipaa_protected=False,
        pat_scopes=pat_scopes,
    )


def _auth(
    *,
    scopes: tuple[str, ...] = (),
    claims: dict[str, Any] | None = None,
    service_scope: ServiceScope | None = None,
) -> AuthContext:
    return AuthContext(
        uc_uid="user-1",
        agent_id=None,
        scopes=scopes,
        claims=claims if claims is not None else {},
        service_scope=service_scope,
    )


# ---------------------------------------------------------------------------
# required_scope_for_role_minimum — derived from rank, not a second table
# ---------------------------------------------------------------------------


class TestRequiredScopeMapping:
    @pytest.mark.parametrize(
        "minimum,expected",
        [
            ("viewer", "accounting:read"),
            ("client_user", "accounting:read"),
            ("bookkeeper", "accounting:read"),
            ("accountant", "accounting:write"),
            ("cpa", "accounting:write"),
            ("admin", "accounting:admin"),
            ("owner", "accounting:admin"),
            ("client_owner", "accounting:admin"),
            ("platform_admin", "accounting:admin"),
        ],
    )
    def test_known_roles_map_to_expected_scope(self, minimum: str, expected: str):
        assert required_scope_for_role_minimum(minimum) == expected

    def test_unknown_role_returns_none_for_fail_closed(self):
        assert required_scope_for_role_minimum("not_a_real_role") is None
        assert required_scope_for_role_minimum("") is None


class TestPatHoldsScope:
    def test_exact_match(self):
        assert _pat_holds_scope(("accounting:read",), "accounting:read") is True

    def test_missing_scope(self):
        assert _pat_holds_scope(("accounting:read",), "accounting:write") is False

    def test_admin_implies_write_and_read(self):
        assert _pat_holds_scope(("accounting:admin",), "accounting:write") is True
        assert _pat_holds_scope(("accounting:admin",), "accounting:read") is True
        assert _pat_holds_scope(("accounting:admin",), "accounting:admin") is True

    def test_write_implies_read_not_admin(self):
        assert _pat_holds_scope(("accounting:write",), "accounting:read") is True
        assert _pat_holds_scope(("accounting:write",), "accounting:write") is True
        assert _pat_holds_scope(("accounting:write",), "accounting:admin") is False

    def test_empty_scopes_hold_nothing(self):
        assert _pat_holds_scope((), "accounting:read") is False

    def test_unknown_required_fails_closed(self):
        assert _pat_holds_scope(("accounting:admin",), "accounting:superuser") is False


# ---------------------------------------------------------------------------
# _pat_scopes_from_auth — distinguish PAT from non-PAT
# ---------------------------------------------------------------------------


class TestPatScopesFromAuth:
    def test_keycloak_session_returns_none(self):
        # JWT scopes may exist but without pat_id this is NOT a PAT request.
        auth = _auth(
            scopes=("openid", "profile", "accounting:read"),
            claims={"sub": "user-1", "uc_uid": "user-1"},
        )
        assert _pat_scopes_from_auth(auth) is None

    def test_service_token_returns_none(self):
        auth = _auth(scopes=("service",), claims={"sub": "svc", "uc_uid": "svc"})
        assert _pat_scopes_from_auth(auth) is None

    def test_pat_with_scopes_returns_tuple(self):
        auth = _auth(
            scopes=("accounting:read", "accounting:write"),
            claims={"sub": "user-1", "pat_id": str(uuid.uuid4()), "pat_prefix": "aops_pat_ab"},
        )
        assert _pat_scopes_from_auth(auth) == ("accounting:read", "accounting:write")

    def test_pat_with_empty_scopes_returns_empty_tuple_not_none(self):
        """Critical: empty grant ≠ not a PAT. Confusing them locks out humans."""
        auth = _auth(
            scopes=(),
            claims={"sub": "user-1", "pat_id": str(uuid.uuid4())},
        )
        result = _pat_scopes_from_auth(auth)
        assert result is not None
        assert result == ()


# ---------------------------------------------------------------------------
# require_role — non-PAT callers unchanged
# ---------------------------------------------------------------------------


class TestNonPatUnaffected:
    """Keycloak / session / service / plain WorkspaceContext: no scope gate."""

    def test_session_owner_passes_every_floor(self):
        ctx = _ws("owner", pat_scopes=None)
        for floor in ("viewer", "accountant", "admin", "owner"):
            require_role(ctx, floor)  # must not raise

    def test_session_viewer_still_blocked_by_role(self):
        ctx = _ws("viewer", pat_scopes=None)
        with pytest.raises(HTTPException) as ei:
            require_role(ctx, "accountant")
        assert ei.value.status_code == 403
        assert "role 'viewer' insufficient" in ei.value.detail
        # Must NOT look like a scope failure.
        assert "token scope" not in ei.value.detail

    def test_default_pat_scopes_is_none(self):
        """Existing WorkspaceContext(...) constructors omit pat_scopes."""
        ctx = WorkspaceContext(
            workspace_id=uuid.uuid4(),
            workspace_slug="ws",
            role="accountant",
            user_uc_uid="u",
            agent_id=None,
            entity_scope=None,
            hipaa_protected=False,
        )
        assert ctx.pat_scopes is None
        require_role(ctx, "accountant")

    def test_service_lane_has_no_pat_scopes(self):
        # Service tokens never set pat_id; resolved ctx has pat_scopes=None.
        auth = _auth(scopes=("service",), claims={"sub": "svc"})
        assert _pat_scopes_from_auth(auth) is None
        ctx = _ws("owner", pat_scopes=_pat_scopes_from_auth(auth))
        require_role(ctx, "owner")


# ---------------------------------------------------------------------------
# require_role — entitled PAT allowed
# ---------------------------------------------------------------------------


class TestEntitledPatAllowed:
    def test_read_pat_passes_viewer_floor(self):
        ctx = _ws("owner", pat_scopes=("accounting:read",))
        require_role(ctx, "viewer")
        require_role(ctx, "bookkeeper")

    def test_write_pat_passes_accountant_floor(self):
        ctx = _ws(
            "accountant",
            pat_scopes=("accounting:read", "accounting:write"),
        )
        require_role(ctx, "viewer")
        require_role(ctx, "accountant")

    def test_admin_pat_passes_admin_floor(self):
        ctx = _ws(
            "owner",
            pat_scopes=("accounting:read", "accounting:write", "accounting:admin"),
        )
        require_role(ctx, "admin")
        require_role(ctx, "owner")

    def test_admin_scope_alone_satisfies_write_floor(self):
        """Higher scopes imply lower — admin ⊇ write."""
        ctx = _ws("owner", pat_scopes=("accounting:admin",))
        require_role(ctx, "accountant")
        require_role(ctx, "viewer")


# ---------------------------------------------------------------------------
# require_role — under-scoped PAT refused with scope-specific 403
# ---------------------------------------------------------------------------


class TestUnderScopedPatRefused:
    def test_read_only_pat_blocked_from_write(self):
        ctx = _ws("owner", pat_scopes=("accounting:read",))
        with pytest.raises(HTTPException) as ei:
            require_role(ctx, "accountant")
        assert ei.value.status_code == 403
        detail = ei.value.detail
        assert "token scope" in detail
        assert "accounting:read" in detail
        assert "needs accounting:write" in detail
        # Distinct from the role failure message.
        assert "role '" not in detail or "insufficient (needs '" not in detail
        assert "role 'owner' insufficient" not in detail

    def test_write_pat_blocked_from_admin(self):
        ctx = _ws(
            "owner",
            pat_scopes=("accounting:read", "accounting:write"),
        )
        with pytest.raises(HTTPException) as ei:
            require_role(ctx, "admin")
        assert ei.value.status_code == 403
        assert "token scope" in ei.value.detail
        assert "needs accounting:admin" in ei.value.detail

    def test_empty_pat_scopes_fail_closed(self):
        ctx = _ws("owner", pat_scopes=())
        with pytest.raises(HTTPException) as ei:
            require_role(ctx, "viewer")
        assert ei.value.status_code == 403
        assert "token scope" in ei.value.detail
        assert "(none)" in ei.value.detail
        assert "needs accounting:read" in ei.value.detail

    def test_role_failure_message_unchanged_when_both_would_fail(self):
        """Role is checked first; a viewer with a write PAT still sees a role 403."""
        ctx = _ws("viewer", pat_scopes=("accounting:read", "accounting:write"))
        with pytest.raises(HTTPException) as ei:
            require_role(ctx, "accountant")
        assert "role 'viewer' insufficient" in ei.value.detail
        assert "token scope" not in ei.value.detail

    def test_unknown_minimum_fails_closed_for_pat(self):
        ctx = _ws("platform_admin", pat_scopes=("accounting:admin",))
        with pytest.raises(HTTPException) as ei:
            require_role(ctx, "not_a_real_role")
        assert ei.value.status_code == 403
        # role_at_least defaults unknown minima to rank 99; platform_admin
        # (100) passes the role check, then the scope gate fails closed.
        assert "token scope" in ei.value.detail
        assert "cannot determine required scope" in ei.value.detail


# ---------------------------------------------------------------------------
# Regression pin: deleting the scope gate must turn a test red
# ---------------------------------------------------------------------------


class TestGateIsPinned:
    """If someone removes the PAT scope check from require_role, this fails.

    This is the most likely regression: one line deleted at the chokepoint
    silently re-opens write access for every read-only PAT.
    """

    def test_under_scoped_pat_must_not_reach_write_floor(self):
        # Owner role would pass the role floor alone. Scope must still block.
        ctx = _ws("owner", pat_scopes=("accounting:read",))
        with pytest.raises(HTTPException) as ei:
            require_role(ctx, "accountant")
        assert ei.value.status_code == 403
        assert "token scope" in ei.value.detail
        assert "needs accounting:write" in ei.value.detail
        # Prove the role was sufficient on its own (so failure is scope-only).
        assert role_at_least(ctx.role, "accountant") is True

    def test_session_caller_still_works_if_scopes_were_wrongly_treated_as_empty(self):
        """Guard the dual of the gate: None must not be treated as ()."""
        ctx = _ws("owner", pat_scopes=None)
        # If the gate treated None as empty, this would 403 with token scope.
        require_role(ctx, "owner")


@pytest.mark.asyncio
async def test_mcp_authorize_workspace_enforces_pat_scope_and_allows_read(
    monkeypatch: pytest.MonkeyPatch,
):
    """MCP entry point passes the PAT scope into the shared role chokepoint."""
    workspace_id = uuid.uuid4()
    auth = _auth(
        scopes=("accounting:read",),
        claims={"sub": "user-1", "pat_id": str(uuid.uuid4())},
    )

    async def fake_membership(*args: Any, **kwargs: Any):
        # Fake session/membership result: this smoke test never opens a DB.
        return "owner", None, False, "ws"

    monkeypatch.setattr(mcp_auth, "_resolve_membership", fake_membership)
    monkeypatch.setattr(
        "app.config.get_settings", lambda: SimpleNamespace(is_community=True)
    )
    reset = mcp_auth._current_auth.set(auth)
    try:
        read_ctx = await mcp_auth.authorize_workspace(workspace_id, min_role="viewer")
        assert read_ctx.pat_scopes == ("accounting:read",)

        with pytest.raises(PermissionError, match="needs accounting:write"):
            await mcp_auth.authorize_workspace(workspace_id, min_role="accountant")
    finally:
        mcp_auth._current_auth.reset(reset)
