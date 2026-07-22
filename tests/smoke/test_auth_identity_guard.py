"""SECURITY: Keycloak-login identity guard (task dfae26f2 / live incident 2026-07-22).

Covers:
  (a) token sub=X, email matches ACTIVE row keyed uc_uid=Y, email_verified=true
      → BIND (re-key uc_uid), no new row
  (a') same match but email_verified=false → fail closed 409, no bind/insert
  (b) genuinely new user with email → provisioned WITH email (never NULL)
  (c) synthetic m2m:*/agent-* never touch the email path
  (d) uc_uid_diverges_from_sub ERROR is once-per-identity-per-process

Brigade path is deliberately out of scope (already has _uc_uid_for_email).
No live stack required — SessionLocal is faked.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

import app.auth as auth


# ---------------------------------------------------------------------------
# Fake SQLAlchemy session / SessionLocal
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, row: Any):
        self._row = row

    def first(self):
        return self._row


class _FakeSession:
    """Minimal async session that records execute() calls + returns scripted rows."""

    def __init__(self, script: list[Any]):
        # script: list of values returned by successive execute().first()
        # Use the sentinel object _NO_ROW for "no row"; INSERT/UPDATE return None.
        self._script = list(script)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._idx = 0

    async def execute(self, statement, params=None):  # noqa: ANN001
        sql = str(statement)
        params = dict(params or {})
        self.calls.append((sql, params))
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
        return self  # async with session.begin()


class _FakeSessionLocal:
    def __init__(self, session: _FakeSession):
        self._session = session

    def __call__(self):
        return self._session


@pytest.fixture(autouse=True)
def _clear_diverge_log():
    auth._reset_uc_uid_diverge_log_for_tests()
    yield
    auth._reset_uc_uid_diverge_log_for_tests()


def _patch_db(monkeypatch: pytest.MonkeyPatch, script: list[Any]) -> _FakeSession:
    """Install a fake SessionLocal under app.db (imported lazily inside the helper)."""
    session = _FakeSession(script)
    import app.db as db_mod

    monkeypatch.setattr(db_mod, "SessionLocal", _FakeSessionLocal(session))
    return session


# ===========================================================================
# (a) BIND on verified-email match
# ===========================================================================
class TestKeycloakEmailBind:
    async def test_a_verified_email_match_binds_no_insert(self, monkeypatch, caplog):
        """sub=X + email matching ACTIVE uc_uid=Y + email_verified → re-key, no INSERT."""
        existing_id = uuid4()
        session = _patch_db(
            monkeypatch,
            script=[
                None,  # SELECT by uc_uid=X → miss
                SimpleNamespace(id=existing_id, uc_uid="legacy-Y"),  # SELECT by email
                None,  # UPDATE bind
            ],
        )
        with caplog.at_level(logging.WARNING, logger="acctops.auth"):
            await auth._ensure_keycloak_user_identity(
                "new-sub-X",
                {
                    "sub": "new-sub-X",
                    "email": "Owner@Example.COM",
                    "email_verified": True,
                },
            )

        sqls = [c[0].lower() for c in session.calls]
        assert any("update public.user_account" in s and "uc_uid" in s for s in sqls), (
            session.calls
        )
        assert not any("insert into public.user_account" in s for s in sqls), session.calls
        # One-way re-key params
        update_params = [
            p for s, p in session.calls if "update public.user_account" in s.lower()
        ]
        assert update_params and update_params[0]["new_uc"] == "new-sub-X"
        assert update_params[0]["id"] == existing_id
        assert any("keycloak_email_bind" in r.message for r in caplog.records)

    async def test_a_unverified_email_match_fails_closed(self, monkeypatch):
        """Match without email_verified=True must 409 — no bind, no insert."""
        session = _patch_db(
            monkeypatch,
            script=[
                None,
                SimpleNamespace(id=uuid4(), uc_uid="legacy-Y"),
            ],
        )
        with pytest.raises(HTTPException) as exc:
            await auth._ensure_keycloak_user_identity(
                "new-sub-X",
                {
                    "sub": "new-sub-X",
                    "email": "owner@example.com",
                    "email_verified": False,
                },
            )
        assert exc.value.status_code == 409
        assert "different identity" in exc.value.detail.lower()
        sqls = [c[0].lower() for c in session.calls]
        assert not any("update" in s and "uc_uid" in s for s in sqls)
        assert not any("insert" in s for s in sqls)


# ===========================================================================
# (b) New user provisioned with email
# ===========================================================================
class TestKeycloakProvision:
    async def test_b_new_user_with_email_provisions_with_email(self, monkeypatch):
        session = _patch_db(
            monkeypatch,
            script=[
                None,  # by uc_uid miss
                None,  # by email miss
                None,  # INSERT
            ],
        )
        await auth._ensure_keycloak_user_identity(
            "brand-new-sub",
            {
                "sub": "brand-new-sub",
                "email": "new@example.com",
                "email_verified": True,
            },
        )
        inserts = [
            (s, p)
            for s, p in session.calls
            if "insert into public.user_account" in s.lower()
        ]
        assert len(inserts) == 1
        _sql, params = inserts[0]
        assert params["uc"] == "brand-new-sub"
        assert params["email"] == "new@example.com"
        # Must not be a NULL-email insert shape
        assert "email" in inserts[0][0].lower()

    async def test_b_existing_uid_backfills_null_email(self, monkeypatch):
        """Already-keyed row with NULL email + token email → UPDATE email, no ghost."""
        row_id = uuid4()
        session = _patch_db(
            monkeypatch,
            script=[SimpleNamespace(id=row_id, email=None)],
        )
        await auth._ensure_keycloak_user_identity(
            "already-keyed",
            {"sub": "already-keyed", "email": "fill@example.com", "email_verified": True},
        )
        updates = [
            (s, p)
            for s, p in session.calls
            if "update public.user_account" in s.lower()
        ]
        assert updates
        assert updates[0][1]["email"] == "fill@example.com"
        assert not any("insert" in s.lower() for s, _ in session.calls)


# ===========================================================================
# (c) Synthetic identities never email-bind
# ===========================================================================
class TestSyntheticSkip:
    async def test_c_m2m_never_opens_db(self, monkeypatch):
        opened = MagicMock()

        def _boom():
            opened()
            raise AssertionError("SessionLocal must not open for m2m")

        import app.db as db_mod

        monkeypatch.setattr(db_mod, "SessionLocal", _boom)
        await auth._ensure_keycloak_user_identity(
            "m2m:tax-planning-ops",
            {"email": "human@example.com", "email_verified": True},
        )
        opened.assert_not_called()

    async def test_c_agent_prefix_never_opens_db(self, monkeypatch):
        import app.db as db_mod

        monkeypatch.setattr(
            db_mod,
            "SessionLocal",
            lambda: (_ for _ in ()).throw(AssertionError("no db for agent-*")),
        )
        await auth._ensure_keycloak_user_identity(
            "agent-receipt-reconciler",
            {"email": "human@example.com", "email_verified": True},
        )

    def test_c_is_synthetic_helpers(self):
        assert auth._is_synthetic_uc_uid("m2m:foo") is True
        assert auth._is_synthetic_uc_uid("agent-bar") is True
        assert auth._is_synthetic_uc_uid("real-human-sub") is False


# ===========================================================================
# (d) diverge ERROR once-per-identity-per-process
# ===========================================================================
class TestDivergeLogDedup:
    def test_d_logs_error_once_per_sub(self, caplog):
        claims = {"sub": "canonical-sub", "uc_uid": "divergent-legacy"}
        with caplog.at_level(logging.ERROR, logger="acctops.auth"):
            assert auth._resolve_uc_uid(claims) == "canonical-sub"
            assert auth._resolve_uc_uid(claims) == "canonical-sub"
            assert auth._resolve_uc_uid(claims) == "canonical-sub"
        messages = [
            r.message for r in caplog.records if "uc_uid_diverges_from_sub" in r.message
        ]
        assert len(messages) == 1, messages

    def test_d_different_subs_each_log_once(self, caplog):
        with caplog.at_level(logging.ERROR, logger="acctops.auth"):
            auth._resolve_uc_uid({"sub": "sub-a", "uc_uid": "x"})
            auth._resolve_uc_uid({"sub": "sub-b", "uc_uid": "y"})
            auth._resolve_uc_uid({"sub": "sub-a", "uc_uid": "x"})
        messages = [
            r.message for r in caplog.records if "uc_uid_diverges_from_sub" in r.message
        ]
        assert len(messages) == 2, messages

    def test_d_conformant_still_silent(self, caplog):
        with caplog.at_level(logging.ERROR, logger="acctops.auth"):
            assert auth._resolve_uc_uid({"sub": "u1", "uc_uid": "u1"}) == "u1"
        assert not any(
            "uc_uid_diverges_from_sub" in r.message for r in caplog.records
        )
