"""Unit tests for app.authz.assert_entity_in_scope (OWASP API1:2023 / BOLA).

These are pure (no stack, no DB) so the entity-scope decision logic is proven
deterministically and always runs in `make test`. The end-to-end enforcement
through the HTTP endpoints is exercised by tests/integration/test_tenant_isolation.py.

Decision matrix under test:

  membership scope   request entity_id     -> result
  ----------------   -------------------   -------------------------------------
  None (unscoped)    anything              -> None  (no restriction; full access)
  (A,)               A (in scope)          -> None  (concrete id already scopes)
  (A,)               B (out of scope)      -> 403
  (A,)               None / consolidated   -> (A,)  (restrict consolidated query)
  (A,B)              None / consolidated   -> (A,B)
  (A,)               "all"                 -> (A,)
  (A,)               "garbage-not-uuid"    -> 400
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.authz import assert_entity_in_scope
from app.tenancy import WorkspaceContext

ENTITY_A = uuid.uuid4()
ENTITY_B = uuid.uuid4()


def _ctx(entity_scope: tuple[uuid.UUID, ...] | None, role: str = "accountant") -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=uuid.uuid4(),
        workspace_slug="ws",
        role=role,
        user_uc_uid="someone",
        agent_id=None,
        entity_scope=entity_scope,
        hipaa_protected=False,
    )


def test_unscoped_member_never_restricted():
    ctx = _ctx(None, role="owner")
    # Owner/unscoped: every shape returns None (no restriction, full access).
    assert assert_entity_in_scope(ctx, None) is None
    assert assert_entity_in_scope(ctx, "consolidated") is None
    assert assert_entity_in_scope(ctx, "all") is None
    assert assert_entity_in_scope(ctx, ENTITY_A) is None
    assert assert_entity_in_scope(ctx, str(ENTITY_B)) is None


def test_scoped_member_in_scope_concrete_id_allowed():
    ctx = _ctx((ENTITY_A,))
    # In-scope concrete id: allowed, and no extra restriction needed (the
    # concrete id already narrows the query).
    assert assert_entity_in_scope(ctx, ENTITY_A) is None
    assert assert_entity_in_scope(ctx, str(ENTITY_A)) is None


def test_scoped_member_out_of_scope_concrete_id_denied():
    ctx = _ctx((ENTITY_A,))
    with pytest.raises(HTTPException) as exc:
        assert_entity_in_scope(ctx, ENTITY_B)
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc2:
        assert_entity_in_scope(ctx, str(ENTITY_B))
    assert exc2.value.status_code == 403


def test_scoped_member_consolidated_is_restricted_not_denied():
    ctx = _ctx((ENTITY_A,))
    # Consolidated / None / all / "" -> restrict to the allowed set, never 403.
    assert assert_entity_in_scope(ctx, None) == (ENTITY_A,)
    assert assert_entity_in_scope(ctx, "consolidated") == (ENTITY_A,)
    assert assert_entity_in_scope(ctx, "all") == (ENTITY_A,)
    assert assert_entity_in_scope(ctx, "") == (ENTITY_A,)
    assert assert_entity_in_scope(ctx, "  CONSOLIDATED ") == (ENTITY_A,)


def test_scoped_member_multi_entity_consolidated_returns_full_set():
    ctx = _ctx((ENTITY_A, ENTITY_B))
    restriction = assert_entity_in_scope(ctx, None)
    assert set(restriction) == {ENTITY_A, ENTITY_B}
    # An in-scope concrete id is still allowed with no extra restriction.
    assert assert_entity_in_scope(ctx, ENTITY_A) is None


def test_scoped_member_malformed_entity_id_is_400():
    ctx = _ctx((ENTITY_A,))
    with pytest.raises(HTTPException) as exc:
        assert_entity_in_scope(ctx, "not-a-uuid")
    assert exc.value.status_code == 400
