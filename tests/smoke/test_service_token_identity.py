"""Service-token identity-orphan guard + autoprovision gating.

Companion to tests/smoke/test_auth_identity_guard.py (live incident
2026-07-22). That incident's fix (d064c20) re-keys ``user_account.uc_uid``
to the Keycloak ``sub`` on a verified-email login — Keycloak path only. Any
identity consumer that stored the OLD uc_uid (``ACCTOPS_SERVICE_TOKENS``
first among them) then resolved to a freshly ghost-provisioned,
memberships-less row: auth succeeded and every membership-filtered surface
(``list_my_workspaces``) silently returned ``[]``.

Covers:
  (a) service token whose mapped uc_uid matches no user_account row
      → 403 + ``service_token_identity_orphaned`` ERROR + NO INSERT
  (a') service token whose uc_uid resolves → authenticates as before,
      ``may_autoprovision`` stays False
  (a'') DB blip during the guard → soft-allow (tripwire, not authz gate)
  (b) ``_ensure_user_account`` default (no autoprovision) → 403 + no INSERT;
      ``autoprovision=True`` keeps the historical first-sight INSERT
  (c) app.api.workspaces re-exports the ONE guarded implementation
  (d) CE local mode mints ``may_autoprovision=True`` (self-host first-run
      provisioning keeps working); the dataclass default is False

No live stack required — SessionLocal is faked.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException

import app.auth as auth_mod
from app.auth import AuthContext, authenticate_token


# ---------------------------------------------------------------------------
# Fake SQLAlchemy session / SessionLocal (mirrors test_auth_identity_guard)
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, row: Any):
        self._row = row

    def first(self):
        return self._row

    def scalar_one(self):
        return self._row


class _FakeSession:
    """Minimal async session recording execute() calls, returning scripted rows."""

    def __init__(self, script: list[Any]):
        self._script = list(script)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._idx = 0

    async def execute(self, statement, params=None):  # noqa: ANN001
        sql = str(statement)
        self.calls.append((sql, dict(params or {})))
        if self._idx < len(self._script):
            row = self._script[self._idx]
            self._idx += 1
            return _FakeResult(row)
        return _FakeResult(None)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def begin(self):
        return self  # supports `async with session.begin()`


class _FakeSessionLocal:
    def __init__(self, session: _FakeSession):
        self._session = session

    def __call__(self):
        return self._session


class _RaisingSessionLocal:
    def __call__(self):
        raise RuntimeError("db down")


def _patch_app_db(monkeypatch: pytest.MonkeyPatch, script: list[Any]) -> _FakeSession:
    """Fake SessionLocal under app.db (lazily imported by the auth-side guard)."""
    session = _FakeSession(script)
    import app.db as db_mod

    monkeypatch.setattr(db_mod, "SessionLocal", _FakeSessionLocal(session))
    return session


def _patch_tenancy_db(
    monkeypatch: pytest.MonkeyPatch, script: list[Any]
) -> _FakeSession:
    """Fake SessionLocal under app.tenancy (module-level import there)."""
    session = _FakeSession(script)
    import app.tenancy as tenancy_mod

    monkeypatch.setattr(tenancy_mod, "SessionLocal", _FakeSessionLocal(session))
    return session


TOKEN = "s" * 40


@pytest.fixture(autouse=True)
def _fresh_service_tokens(monkeypatch):
    """Register one unscoped service token per test (dev escape hatch on)."""
    monkeypatch.setattr(auth_mod, "_service_tokens_cache", None)
    monkeypatch.setenv("ACCTOPS_ALLOW_UNSCOPED_SERVICE_TOKEN", "true")
    monkeypatch.setenv(
        "ACCTOPS_SERVICE_TOKENS",
        '{"%s": {"uc_uid": "legacy-uid", "agent_id": "unit-test-worker"}}' % TOKEN,
    )
    yield
    monkeypatch.setattr(auth_mod, "_service_tokens_cache", None)


def _assert_no_insert(session: _FakeSession) -> None:
    assert not any("INSERT" in sql.upper() for sql, _ in session.calls), (
        "identity paths must never mint user_account rows for this caller: "
        f"{[sql for sql, _ in session.calls]}"
    )


# ===========================================================================
# (a) orphaned service token fails loud — never ghost-empty
# ===========================================================================
class TestServiceTokenOrphanGuard:
    async def test_orphaned_uc_uid_403_logs_and_never_inserts(
        self, monkeypatch, caplog
    ):
        session = _patch_app_db(monkeypatch, [None])  # SELECT finds no row
        with caplog.at_level(logging.ERROR, logger="acctops.auth"):
            with pytest.raises(HTTPException) as exc_info:
                await authenticate_token(TOKEN)
        assert exc_info.value.status_code == 403
        assert "orphaned" in str(exc_info.value.detail)
        assert "service_token_identity_orphaned" in caplog.text
        assert "unit-test-worker" in caplog.text  # label, never the token
        assert TOKEN not in caplog.text
        assert len(session.calls) == 1
        assert "SELECT 1 FROM public.user_account" in session.calls[0][0]
        _assert_no_insert(session)

    async def test_resolvable_uc_uid_still_authenticates(self, monkeypatch):
        session = _patch_app_db(monkeypatch, [(1,)])  # row exists
        ctx = await authenticate_token(TOKEN)
        assert ctx.uc_uid == "legacy-uid"
        assert ctx.agent_id == "unit-test-worker"
        assert ctx.scopes == ("service",)
        assert ctx.may_autoprovision is False
        _assert_no_insert(session)

    async def test_db_blip_soft_allows(self, monkeypatch):
        """The guard is a tripwire, not the authz gate: indeterminate → proceed."""
        import app.db as db_mod

        monkeypatch.setattr(db_mod, "SessionLocal", _RaisingSessionLocal())
        ctx = await authenticate_token(TOKEN)
        assert ctx.uc_uid == "legacy-uid"


# ===========================================================================
# (b) _ensure_user_account: resolve-or-403 by default, INSERT only on opt-in
# ===========================================================================
class TestEnsureUserAccountGating:
    async def test_default_no_autoprovision_403_and_no_insert(
        self, monkeypatch, caplog
    ):
        from app.tenancy import _ensure_user_account

        session = _patch_tenancy_db(monkeypatch, [None])
        with caplog.at_level(logging.ERROR, logger="acctops.tenancy"):
            with pytest.raises(HTTPException) as exc_info:
                await _ensure_user_account("ghost-uid")
        assert exc_info.value.status_code == 403
        assert "not provisioned" in str(exc_info.value.detail)
        assert "caller_identity_unprovisioned" in caplog.text
        assert len(session.calls) == 1
        _assert_no_insert(session)

    async def test_autoprovision_true_keeps_first_sight_insert(self, monkeypatch):
        from app.tenancy import _ensure_user_account

        new_id = uuid4()
        session = _patch_tenancy_db(monkeypatch, [None, new_id])
        got = await _ensure_user_account("brand-new-uid", autoprovision=True)
        assert got == new_id
        assert any("INSERT INTO public.user_account" in sql for sql, _ in session.calls)

    async def test_existing_row_resolves_without_flag(self, monkeypatch):
        from app.tenancy import _ensure_user_account

        row_id = uuid4()
        session = _patch_tenancy_db(
            monkeypatch, [SimpleNamespace(id=row_id)]
        )
        got = await _ensure_user_account("known-uid")
        assert got == row_id
        _assert_no_insert(session)


# ===========================================================================
# (c) exactly one implementation — the duplicate copy is how the hole survived
# ===========================================================================
def test_workspaces_reexports_the_guarded_helper():
    from app.api.workspaces import _ensure_user_account as via_workspaces
    from app.tenancy import _ensure_user_account as via_tenancy

    assert via_workspaces is via_tenancy


# ===========================================================================
# (d) caller-class flag: default False; CE local mode opts in
# ===========================================================================
def test_authcontext_default_denies_autoprovision():
    ctx = AuthContext(uc_uid="u", agent_id=None, scopes=(), claims={})
    assert ctx.may_autoprovision is False


def test_local_mode_context_may_autoprovision(monkeypatch):
    """CE self-host: the single admin still auto-provisions on first request."""
    from jose import jwt as jose_jwt

    secret = "unit-test-local-secret"
    settings = SimpleNamespace(
        is_local_auth=True,
        local_auth_secret=secret,
        accepted_audience="accounting-ops",
    )
    monkeypatch.setattr(auth_mod, "get_settings", lambda: settings)
    token = jose_jwt.encode(
        {
            "sub": "admin",
            "uc_uid": "admin",
            "iss": auth_mod._LOCAL_TOKEN_ISSUER,
            "aud": "accounting-ops",
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        },
        secret,
        algorithm="HS256",
    )
    ctx = auth_mod._try_authenticate_local(token)
    assert ctx is not None
    assert ctx.uc_uid == "admin"
    assert ctx.may_autoprovision is True
