"""Smoke tests for app/api/inference_settings.py (BYOK inference settings).

These exercise the FastAPI routing + handler logic only — no live Postgres,
Keycloak, or crypto. We:

  * mount the inference_settings router on a minimal FastAPI app,
  * override ``resolve_workspace`` to inject a WorkspaceContext at a chosen role,
  * patch the credentials store (store/get/list/delete + SessionLocal) so the
    handler runs end-to-end against an in-memory fake.

Coverage
--------
  1. POST stores the EXACT resolver contract: connector ``byo_llm_key``,
     label = provider, value = JSON {"key","provider","base_url"}.
  2. GET returns status only — configured/provider/key_last4 — and NEVER the key.
  3. DELETE removes the ``byo_llm_key`` credential.
  4. Role-gating: a non-owner/non-admin (viewer) POST → 403; viewer DELETE → 403.
  5. The plaintext key never appears in ANY response body (POST or GET).
  6. POST with an unsupported provider → 400 (and stores nothing).
"""
from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.inference_settings as mod
from app.security.credentials import CredentialMetadata
from app.tenancy import WorkspaceContext, resolve_workspace

_WORKSPACE_ID = uuid.uuid4()
_PLAINTEXT_KEY = "sk-ant-secret-DO-NOT-LEAK-abcdEFGH9999"


# ---------------------------------------------------------------------------
# Fake credentials store — a single dict keyed by (connector, label).
# ---------------------------------------------------------------------------


class _FakeStore:
    """In-memory stand-in for the encrypted tenant_credentials table.

    Records every store_credential call so tests can assert the exact shape the
    handler persisted (connector / label / value).
    """

    def __init__(self) -> None:
        # (connector, label) -> {"id", "value"}
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.store_calls: list[dict[str, Any]] = []
        self.delete_calls: list[uuid.UUID] = []

    async def store_credential(
        self,
        session,
        workspace_id,
        connector,
        label,
        plaintext_value,
        *,
        created_by_uc_uid=None,
        expires_at=None,
    ):
        self.store_calls.append(
            {
                "workspace_id": workspace_id,
                "connector": connector,
                "label": label,
                "value": plaintext_value,
                "created_by_uc_uid": created_by_uc_uid,
            }
        )
        cred_id = uuid.uuid4()
        self.rows[(connector, label)] = {"id": cred_id, "value": plaintext_value}
        return cred_id

    async def get_credential_plaintext(
        self,
        session,
        workspace_id,
        connector,
        *,
        label=None,
        requesting_service,
        actor_uc_uid=None,
        actor_agent_id=None,
    ):
        for (conn, lbl), row in self.rows.items():
            if conn == connector and (label is None or lbl == label):
                return row["value"]
        return None

    async def list_credentials(self, session, workspace_id):
        return [
            CredentialMetadata(
                id=row["id"],
                workspace_id=workspace_id,
                connector=conn,
                label=lbl,
                key_version=1,
                encrypted_at="2026-06-03T00:00:00+00:00",
                last_used_at=None,
                expires_at=None,
            )
            for (conn, lbl), row in self.rows.items()
        ]

    async def delete_credential(
        self, session, workspace_id, credential_id, *, actor_uc_uid=None
    ):
        self.delete_calls.append(credential_id)
        for key, row in list(self.rows.items()):
            if row["id"] == credential_id:
                del self.rows[key]
                return True
        return False


def _make_workspace(role: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=_WORKSPACE_ID,
        workspace_slug="acme",
        role=role,
        user_uc_uid="uc-test-user",
        agent_id=None,
        entity_scope=None,
        hipaa_protected=False,
    )


@pytest.fixture()
def store(monkeypatch) -> _FakeStore:
    """Patch the four credentials-store fns on the module + neutralize SessionLocal."""
    fake = _FakeStore()
    monkeypatch.setattr(mod, "store_credential", fake.store_credential)
    monkeypatch.setattr(mod, "get_credential_plaintext", fake.get_credential_plaintext)
    monkeypatch.setattr(mod, "list_credentials", fake.list_credentials)
    monkeypatch.setattr(mod, "delete_credential", fake.delete_credential)

    @asynccontextmanager
    async def _fake_session_local():
        class _S:
            async def commit(self):
                return None

        yield _S()

    monkeypatch.setattr(mod, "SessionLocal", _fake_session_local)
    return fake


def _client(role: str) -> TestClient:
    """Minimal app with resolve_workspace overridden to the given role."""
    app = FastAPI()
    app.include_router(mod.router)
    app.dependency_overrides[resolve_workspace] = lambda: _make_workspace(role)
    return TestClient(app)


def _url() -> str:
    return f"/api/workspaces/{_WORKSPACE_ID}/inference/byok"


# ---------------------------------------------------------------------------
# 1. POST stores the exact resolver contract
# ---------------------------------------------------------------------------


def test_post_stores_correct_connector_and_json_shape(store: _FakeStore):
    client = _client("owner")
    resp = client.post(
        _url(),
        json={"provider": "anthropic", "api_key": _PLAINTEXT_KEY, "base_url": None},
    )
    assert resp.status_code == 200, resp.text

    # Exactly one store call, with the resolver's connector + label.
    assert len(store.store_calls) == 1
    call = store.store_calls[0]
    assert call["connector"] == "byo_llm_key"
    assert call["label"] == "anthropic"
    assert call["created_by_uc_uid"] == "uc-test-user"

    # The stored VALUE is JSON with exactly {"key","provider","base_url"} and the
    # real key inside (so workspace_llm_keys.get_byok can parse it).
    blob = json.loads(call["value"])
    assert blob == {
        "key": _PLAINTEXT_KEY,
        "provider": "anthropic",
        "base_url": None,
    }


def test_post_base_url_is_persisted(store: _FakeStore):
    client = _client("admin")
    resp = client.post(
        _url(),
        json={
            "provider": "openrouter",
            "api_key": "sk-or-xxxxxxxxxxxx",
            "base_url": "https://openrouter.ai/api/v1",
        },
    )
    assert resp.status_code == 200, resp.text
    blob = json.loads(store.store_calls[0]["value"])
    assert blob["provider"] == "openrouter"
    assert blob["base_url"] == "https://openrouter.ai/api/v1"
    assert resp.json()["base_url"] == "https://openrouter.ai/api/v1"


# ---------------------------------------------------------------------------
# 2. GET returns status only (no key)
# ---------------------------------------------------------------------------


def test_get_returns_status_without_key(store: _FakeStore):
    # Seed a stored credential via POST first.
    owner = _client("owner")
    owner.post(
        _url(), json={"provider": "anthropic", "api_key": _PLAINTEXT_KEY}
    )

    viewer = _client("viewer")
    resp = viewer.get(_url())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["configured"] is True
    assert body["provider"] == "anthropic"
    assert body["key_last4"] == _PLAINTEXT_KEY[-4:]
    # The status payload exposes ONLY last-4, never a full-key field.
    assert "api_key" not in body
    assert "key" not in body


def test_get_unconfigured_when_nothing_stored(store: _FakeStore):
    resp = _client("viewer").get(_url())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["configured"] is False
    assert body["provider"] is None
    assert body["key_last4"] is None


# ---------------------------------------------------------------------------
# 3. DELETE removes the credential
# ---------------------------------------------------------------------------


def test_delete_removes_byok_credential(store: _FakeStore):
    owner = _client("owner")
    owner.post(_url(), json={"provider": "anthropic", "api_key": _PLAINTEXT_KEY})
    assert ("byo_llm_key", "anthropic") in store.rows  # sanity

    resp = owner.delete(_url())
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "removed": True}
    assert ("byo_llm_key", "anthropic") not in store.rows
    assert len(store.delete_calls) == 1

    # And GET now reports unconfigured.
    assert _client("viewer").get(_url()).json()["configured"] is False


def test_delete_is_idempotent_when_absent(store: _FakeStore):
    resp = _client("owner").delete(_url())
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "removed": False}


# ---------------------------------------------------------------------------
# 4. Role-gating
# ---------------------------------------------------------------------------


def test_viewer_cannot_post_returns_403(store: _FakeStore):
    resp = _client("viewer").post(
        _url(), json={"provider": "anthropic", "api_key": _PLAINTEXT_KEY}
    )
    assert resp.status_code == 403, resp.text
    # Nothing was stored.
    assert store.store_calls == []
    assert store.rows == {}


def test_accountant_cannot_post_returns_403(store: _FakeStore):
    # accountant (rank 3) is below admin (rank 4) — must be rejected.
    resp = _client("accountant").post(
        _url(), json={"provider": "anthropic", "api_key": _PLAINTEXT_KEY}
    )
    assert resp.status_code == 403, resp.text
    assert store.store_calls == []


def test_admin_cannot_delete_returns_403(store: _FakeStore):
    # DELETE requires owner; admin (rank 4) is below owner (rank 5).
    resp = _client("admin").delete(_url())
    assert resp.status_code == 403, resp.text
    assert store.delete_calls == []


# ---------------------------------------------------------------------------
# 5. Plaintext key never appears in any response
# ---------------------------------------------------------------------------


def test_plaintext_key_never_in_any_response(store: _FakeStore):
    owner = _client("owner")
    post_resp = owner.post(
        _url(), json={"provider": "anthropic", "api_key": _PLAINTEXT_KEY}
    )
    assert _PLAINTEXT_KEY not in post_resp.text

    get_resp = _client("viewer").get(_url())
    assert _PLAINTEXT_KEY not in get_resp.text

    del_resp = owner.delete(_url())
    assert _PLAINTEXT_KEY not in del_resp.text


# ---------------------------------------------------------------------------
# 6. Provider validation
# ---------------------------------------------------------------------------


def test_unsupported_provider_rejected(store: _FakeStore):
    resp = _client("owner").post(
        _url(), json={"provider": "definitely-not-real", "api_key": _PLAINTEXT_KEY}
    )
    assert resp.status_code == 400, resp.text
    # Rejected before any store happens.
    assert store.store_calls == []
    assert store.rows == {}
