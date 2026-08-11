"""A service token's declared scope must bind on the MCP surface, not just HTTP.

Audit 2026-08-11: ``service_scope`` was applied at exactly ONE call site, the
HTTP ``resolve_workspace`` dependency. ``authorize_workspace`` -- the entry
point for every MCP tool, on an endpoint reachable from the public internet --
never consulted it. A token declaring one workspace, one entity and
role_floor=accountant could, over MCP, reach any workspace and any entity its
impersonated user could reach, at that user's full role. Proven live at the
time with paired requests: HTTP 403 / MCP 200 for the same out-of-scope target.

These tests pin the three gates on the MCP path and, crucially, assert that
BOTH surfaces run the same helper -- the divergence, not any single missing
check, was the actual defect.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app import mcp_auth
from app.tenancy import apply_service_scope

WS_ALLOWED = uuid4()
WS_OTHER = uuid4()
ENT_ALLOWED = uuid4()
ENT_OTHER = uuid4()


class _Scope:
    def __init__(self, workspace_ids=None, entity_ids=None, role_floor=None):
        self.workspace_ids = workspace_ids
        self.entity_ids = entity_ids
        self.role_floor = role_floor


class _Auth:
    uc_uid = "impersonated-user"
    agent_id = "listing-ops-ao-sync"
    may_autoprovision = False
    claims: dict = {}

    def __init__(self, scope):
        self.service_scope = scope


def _authorize(auth, workspace_id, *, entity_id=None, min_role="viewer",
               membership_role="owner", membership_entity_scope=None):
    """Run authorize_workspace with membership resolution faked."""

    async def fake_membership(uc_uid, wsid, autoprovision=False):
        return membership_role, membership_entity_scope, False, "slug"

    async def run():
        # The contextvar is the real binding the middleware sets; set it the
        # same way rather than patching (a ContextVar's .get is read-only).
        token = mcp_auth._current_auth.set(auth)
        try:
            return await mcp_auth.authorize_workspace(
                workspace_id, entity_id, min_role=min_role
            )
        finally:
            mcp_auth._current_auth.reset(token)

    with patch.object(
        mcp_auth, "_resolve_membership", AsyncMock(side_effect=fake_membership)
    ):
        return asyncio.run(run())


# ---------------------------------------------------------------------------
# The three gates, on the MCP path
# ---------------------------------------------------------------------------


def test_out_of_scope_workspace_is_denied_over_mcp():
    auth = _Auth(_Scope(workspace_ids=[WS_ALLOWED]))
    # In scope: allowed.
    ctx = _authorize(auth, WS_ALLOWED)
    assert ctx.workspace_id == WS_ALLOWED
    # Out of scope: denied, even though the impersonated user is an owner.
    with pytest.raises(PermissionError, match="not scoped to this workspace"):
        _authorize(auth, WS_OTHER)


def test_out_of_scope_entity_is_denied_over_mcp():
    auth = _Auth(_Scope(entity_ids=[ENT_ALLOWED]))
    ctx = _authorize(auth, WS_ALLOWED, entity_id=ENT_ALLOWED)
    assert ctx.entity_scope == (ENT_ALLOWED,)
    with pytest.raises(PermissionError):
        _authorize(auth, WS_ALLOWED, entity_id=ENT_OTHER)


def test_role_floor_caps_the_effective_role_over_mcp():
    # Impersonated user is an owner; the token is capped at accountant, so an
    # admin-floor tool (decide_approval, close_period, ...) must be refused.
    auth = _Auth(_Scope(role_floor="accountant"))
    ctx = _authorize(auth, WS_ALLOWED, min_role="accountant")
    assert ctx.role == "accountant", "role must be capped to the token's floor"
    with pytest.raises(PermissionError):
        _authorize(auth, WS_ALLOWED, min_role="admin")


def test_unscoped_token_and_human_callers_are_unaffected():
    ctx = _authorize(_Auth(None), WS_OTHER, min_role="admin", membership_role="owner")
    assert ctx.role == "owner"


def test_membership_entity_scope_is_intersected_not_replaced():
    auth = _Auth(_Scope(entity_ids=[ENT_ALLOWED, ENT_OTHER]))
    ctx = _authorize(
        auth, WS_ALLOWED, entity_id=ENT_ALLOWED, membership_entity_scope=(ENT_ALLOWED,)
    )
    assert ctx.entity_scope == (ENT_ALLOWED,)

    # Disjoint membership vs token scope -> denied outright.
    auth2 = _Auth(_Scope(entity_ids=[ENT_OTHER]))
    with pytest.raises(PermissionError, match="no overlap"):
        _authorize(auth2, WS_ALLOWED, membership_entity_scope=(ENT_ALLOWED,))


# ---------------------------------------------------------------------------
# The anti-divergence property
# ---------------------------------------------------------------------------


def test_both_surfaces_call_the_same_helper():
    """The defect was divergence, so pin that both paths use one implementation."""
    import inspect

    import app.tenancy as tenancy

    mcp_src = inspect.getsource(mcp_auth.authorize_workspace)
    http_src = inspect.getsource(tenancy.resolve_workspace)
    assert "apply_service_scope" in mcp_src, "MCP path must apply service scope"
    assert "apply_service_scope" in http_src, "HTTP path must apply service scope"


def test_helper_applies_scope_before_the_role_check_on_mcp():
    """Order matters: a capped role must be what require_role evaluates.

    If scope were applied after the context was built, an admin-floor tool
    would authorize against the impersonated user's owner role and the cap
    would be cosmetic.
    """
    import inspect

    src = inspect.getsource(mcp_auth.authorize_workspace)
    assert src.index("apply_service_scope") < src.index("require_role(ctx")


@pytest.mark.parametrize(
    ("scope", "expect_role"),
    [(_Scope(role_floor="viewer"), "viewer"), (_Scope(role_floor="admin"), "admin")],
)
def test_helper_never_escalates_only_caps(scope, expect_role):
    role, _ = apply_service_scope(
        _Auth(scope), WS_ALLOWED, role="admin", entity_scope=None
    )
    assert role == expect_role
    # A floor above the membership role cannot promote the caller.
    role2, _ = apply_service_scope(
        _Auth(_Scope(role_floor="owner")), WS_ALLOWED, role="viewer", entity_scope=None
    )
    assert role2 == "viewer"
