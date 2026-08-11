"""create_workspace — the most privileged operation — is gated by the TOKEN.

Audit 2026-08-11: ``create_workspace`` was the one write tool that never built a
WorkspaceContext. It asked SQL a single question — "does this ACCOUNT hold an
admin+ membership anywhere" — and provisioned on a yes. ``require_role`` never
ran, so neither of the two gates that hang off it did: a read-only PAT, and a
service token capped at ``role_floor='viewer'``, could both CREATE SCHEMA +
alembic-upgrade a new workspace while the same token was refused a journal line.
The probe also ignored ``status``, so a membership on an archived workspace still
counted as the grant.

These tests run without a live stack: the probe and the membership resolver are
faked, so what is under test is the gate itself and nothing else.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest

import app.tools.setup_assistant as setup_assistant
from app.auth import AuthContext, ServiceScope

WS_A = uuid.uuid4()
WS_B = uuid.uuid4()
USER_ID = uuid.uuid4()


def _auth(
    *,
    scopes: tuple[str, ...] = ("service",),
    claims: dict[str, Any] | None = None,
    service_scope: ServiceScope | None = None,
) -> AuthContext:
    return AuthContext(
        uc_uid="owner-user",
        agent_id=None,
        scopes=scopes,
        claims=claims or {},
        service_scope=service_scope,
    )


def _pat(*scopes: str) -> AuthContext:
    """A PAT-authenticated caller. ``pat_id`` is what marks the request a PAT."""
    return _auth(scopes=scopes, claims={"pat_id": str(uuid.uuid4())})


@pytest.fixture
def provisioned(monkeypatch):
    """Caller is a real owner of WS_A and WS_B; only the token varies per test."""

    async def _ensure(uc_uid, *, autoprovision=False):
        return USER_ID

    async def _candidates(user_id):
        return [WS_A, WS_B]

    async def _membership(uc_uid, workspace_id, *, autoprovision=False):
        return "owner", None, False, f"ws-{workspace_id}"

    import app.api.workspaces as workspaces

    monkeypatch.setattr(workspaces, "_ensure_user_account", _ensure)
    monkeypatch.setattr(setup_assistant, "_admin_candidate_workspaces", _candidates)
    monkeypatch.setattr(setup_assistant, "_resolve_membership", _membership)


def _set_auth(auth: AuthContext | None):
    from app import mcp_auth

    return mcp_auth._current_auth.set(auth)


async def _create(auth: AuthContext):
    from app import mcp_auth

    token = _set_auth(auth)
    try:
        return await setup_assistant.create_workspace("Acme Holdings")
    finally:
        mcp_auth._current_auth.reset(token)


# ---------------------------------------------------------------------------
# The two escalations the missing chokepoint allowed
# ---------------------------------------------------------------------------


async def test_read_write_pat_cannot_provision(provisioned):
    """The PAT belongs to an owner, but only carries read+write."""
    with pytest.raises(PermissionError, match="accounting:admin"):
        await _create(_pat("accounting:read", "accounting:write"))


async def test_read_only_pat_cannot_provision(provisioned):
    with pytest.raises(PermissionError, match="accounting:admin"):
        await _create(_pat("accounting:read"))


async def test_viewer_floored_service_token_cannot_provision(provisioned):
    """role_floor caps the impersonated owner to viewer — provisioning is off."""
    auth = _auth(service_scope=ServiceScope(role_floor="viewer"))
    with pytest.raises(PermissionError, match="insufficient"):
        await _create(auth)


async def test_accountant_floored_service_token_cannot_provision(provisioned):
    auth = _auth(service_scope=ServiceScope(role_floor="accountant"))
    with pytest.raises(PermissionError, match="insufficient"):
        await _create(auth)


async def test_empty_pat_scopes_fail_closed(provisioned):
    """A PAT with no grants is not the same as "not a PAT"."""
    with pytest.raises(PermissionError):
        await _create(_pat())


# ---------------------------------------------------------------------------
# …without locking out the callers that legitimately provision
# ---------------------------------------------------------------------------


@pytest.fixture
def provisioner_stub(monkeypatch):
    """Intercept the provisioner AND the owner-membership write.

    These tests assert the gate LETS a legitimate caller through. Letting the
    call fall through would have it build a schema and INSERT a membership row
    against whatever database the environment happens to point at, and "it
    raised something that wasn't PermissionError" is a weak stand-in for "it
    got there" — a typo raising AttributeError would satisfy it just as well.
    Stub both writes and assert the provisioner was actually reached.
    """
    calls: list[tuple] = []

    async def _fake_provision(*args, **kwargs):
        calls.append((args, kwargs))
        return "books_stub", None

    import scripts.provision_workspace as pw

    monkeypatch.setattr(pw, "provision_workspace", _fake_provision)

    class _FakeSession:
        async def execute(self, *a, **k):
            return None

        def begin(self):
            return _FakeCtx()

    class _FakeCtx:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    class _FakeSessionMaker:
        def __call__(self):
            return _FakeSessionCtx()

    class _FakeSessionCtx:
        async def __aenter__(self):
            return _FakeSession()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(setup_assistant, "SessionLocal", _FakeSessionMaker())
    return calls


async def test_admin_scoped_pat_still_provisions(provisioned, provisioner_stub):
    """The claude-code-mcp PAT shape: owner membership + accounting:admin."""
    await _create(_pat("accounting:read", "accounting:write", "accounting:admin"))
    assert provisioner_stub, "gate blocked a caller that should provision"


async def test_keycloak_session_owner_still_provisions(provisioned, provisioner_stub):
    """No pat_id, no service_scope → the scope gates are skipped entirely."""
    await _create(_auth(scopes=(), claims={"sub": "owner-user"}))
    assert provisioner_stub, "gate blocked a Keycloak owner session"


async def test_scoped_pat_is_not_denied_by_candidate_row_order(monkeypatch):
    """A PAT scoped to WS_B must not lose to WS_A merely for sorting first.

    A ``workspace_scope`` PAT is refused outright on every workspace outside its
    allow-list, so a single-row probe would deny this token or not depending on
    which membership Postgres happened to return first.
    """

    async def _ensure(uc_uid, *, autoprovision=False):
        return USER_ID

    async def _candidates(user_id):
        return [WS_A, WS_B]

    async def _membership(uc_uid, workspace_id, *, autoprovision=False):
        return "owner", None, False, f"ws-{workspace_id}"

    import app.api.workspaces as workspaces

    monkeypatch.setattr(workspaces, "_ensure_user_account", _ensure)
    monkeypatch.setattr(setup_assistant, "_admin_candidate_workspaces", _candidates)
    monkeypatch.setattr(setup_assistant, "_resolve_membership", _membership)

    auth = _auth(
        scopes=("accounting:read", "accounting:write", "accounting:admin"),
        claims={"pat_id": str(uuid.uuid4())},
        service_scope=ServiceScope(workspace_ids=frozenset({WS_B})),
    )
    with pytest.raises(Exception) as excinfo:
        await _create(auth)
    assert not isinstance(excinfo.value, PermissionError)


async def test_no_admin_membership_anywhere_is_denied(monkeypatch):
    async def _ensure(uc_uid, *, autoprovision=False):
        return USER_ID

    async def _candidates(user_id):
        return []

    import app.api.workspaces as workspaces

    monkeypatch.setattr(workspaces, "_ensure_user_account", _ensure)
    monkeypatch.setattr(setup_assistant, "_admin_candidate_workspaces", _candidates)

    with pytest.raises(PermissionError, match="at least one existing workspace"):
        await _create(_auth())


async def test_unauthenticated_caller_is_denied():
    with pytest.raises(PermissionError, match="No authenticated caller"):
        await _create(None)


# ---------------------------------------------------------------------------
# The probe must not nominate a workspace the resolver would then refuse
# ---------------------------------------------------------------------------


def test_probe_filters_on_both_statuses():
    """Archived workspaces / revoked memberships are not provisioning grants."""
    import inspect

    src = inspect.getsource(setup_assistant._admin_candidate_workspaces)
    assert "m.status = 'active'" in src
    assert "w.status = 'active'" in src


def test_gate_runs_before_any_provisioning():
    """The DDL must be unreachable until require_role has passed."""
    import inspect

    src = inspect.getsource(setup_assistant.create_workspace)
    assert src.index("_authorize_provisioning") < src.index(
        "from scripts.provision_workspace"
    )
