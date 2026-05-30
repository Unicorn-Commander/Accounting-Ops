"""Smoke tests for the expanded 3-persona role hierarchy.

Proves:
  * Every existing role-floor check (viewer / accountant / admin / owner) STILL
    accepts the same set of roles it accepted before the expansion. No
    regression for the original 4-role surface.
  * New roles slot in at the documented ranks (client_user > viewer,
    bookkeeper > client_user, accountant == cpa, owner == client_owner,
    platform_admin above everything).
  * The invite endpoint's role whitelist excludes platform_admin (the
    privilege-escalation gate).
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.members import ELEVATED_ROLES, INVITEABLE_ROLES
from app.tenancy import (
    WorkspaceContext,
    is_platform_admin,
    require_role,
    role_at_least,
)


def _ctx(role: str) -> WorkspaceContext:
    import uuid

    return WorkspaceContext(
        workspace_id=uuid.uuid4(),
        workspace_slug="ws",
        role=role,
        user_uc_uid="someone",
        agent_id=None,
        entity_scope=None,
        hipaa_protected=False,
    )


# ---------------------------------------------------------------------------
# Regression: existing 4 roles still pass their original floors exactly.
# ---------------------------------------------------------------------------


def test_owner_passes_every_old_floor():
    for floor in ("viewer", "accountant", "admin", "owner"):
        require_role(_ctx("owner"), floor)


def test_admin_passes_floors_up_to_admin():
    for floor in ("viewer", "accountant", "admin"):
        require_role(_ctx("admin"), floor)
    with pytest.raises(HTTPException):
        require_role(_ctx("admin"), "owner")


def test_accountant_passes_viewer_and_accountant():
    for floor in ("viewer", "accountant"):
        require_role(_ctx("accountant"), floor)
    with pytest.raises(HTTPException):
        require_role(_ctx("accountant"), "admin")
    with pytest.raises(HTTPException):
        require_role(_ctx("accountant"), "owner")


def test_viewer_passes_only_viewer():
    require_role(_ctx("viewer"), "viewer")
    for floor in ("accountant", "admin", "owner"):
        with pytest.raises(HTTPException):
            require_role(_ctx("viewer"), floor)


# ---------------------------------------------------------------------------
# New roles: ranking proofs.
# ---------------------------------------------------------------------------


def test_cpa_and_accountant_are_same_rank():
    # Same role floor checks accept both identically.
    for role in ("cpa", "accountant"):
        for floor in ("viewer", "accountant"):
            require_role(_ctx(role), floor)
        with pytest.raises(HTTPException):
            require_role(_ctx(role), "admin")


def test_client_owner_equals_owner():
    require_role(_ctx("client_owner"), "owner")
    require_role(_ctx("client_owner"), "admin")
    require_role(_ctx("client_owner"), "accountant")
    require_role(_ctx("client_owner"), "viewer")


def test_bookkeeper_above_viewer_below_accountant():
    require_role(_ctx("bookkeeper"), "viewer")
    require_role(_ctx("bookkeeper"), "client_user")
    require_role(_ctx("bookkeeper"), "bookkeeper")
    # Bookkeeper CANNOT decide approvals — accountant floor required.
    with pytest.raises(HTTPException):
        require_role(_ctx("bookkeeper"), "accountant")


def test_client_user_above_viewer_below_bookkeeper():
    require_role(_ctx("client_user"), "viewer")
    require_role(_ctx("client_user"), "client_user")
    with pytest.raises(HTTPException):
        require_role(_ctx("client_user"), "bookkeeper")
    with pytest.raises(HTTPException):
        require_role(_ctx("client_user"), "accountant")


def test_platform_admin_passes_every_floor():
    pa = _ctx("platform_admin")
    for floor in (
        "viewer",
        "client_user",
        "bookkeeper",
        "accountant",
        "cpa",
        "admin",
        "owner",
        "client_owner",
        "platform_admin",
    ):
        require_role(pa, floor)
    assert is_platform_admin("platform_admin")
    assert not is_platform_admin("owner")


# ---------------------------------------------------------------------------
# Invitation whitelist defenses.
# ---------------------------------------------------------------------------


def test_platform_admin_is_NOT_inviteable():
    """The role whitelist excludes platform_admin so no HTTP path can grant it."""
    assert "platform_admin" not in INVITEABLE_ROLES


def test_elevated_roles_are_subset_of_inviteable():
    """Every role flagged as 'elevated' (needs owner to grant) is still inviteable."""
    assert ELEVATED_ROLES.issubset(INVITEABLE_ROLES)


def test_all_new_roles_are_inviteable():
    assert {"cpa", "client_owner", "bookkeeper", "client_user"}.issubset(
        INVITEABLE_ROLES
    )


# ---------------------------------------------------------------------------
# role_at_least is consistent with require_role.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "actual,floor,expected",
    [
        ("owner", "owner", True),
        ("owner", "platform_admin", False),
        ("client_owner", "owner", True),
        ("platform_admin", "owner", True),
        ("cpa", "accountant", True),
        ("cpa", "admin", False),
        ("bookkeeper", "accountant", False),
        ("bookkeeper", "viewer", True),
        ("client_user", "viewer", True),
        ("client_user", "bookkeeper", False),
        ("viewer", "viewer", True),
        ("viewer", "client_user", False),
    ],
)
def test_role_at_least_matrix(actual: str, floor: str, expected: bool):
    assert role_at_least(actual, floor) is expected
