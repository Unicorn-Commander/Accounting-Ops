"""AO-TOK-1: /api/connectors/ebay/oauth/start honours a token's declared scope.

The route has no {workspace_id} path param, so it resolves the membership by
hand instead of through ``resolve_workspace``. It used to skip
``apply_service_scope``: a service token / PAT declaring workspace_ids=[A] and
role_floor=viewer, whose mapped user is admin in B, could start an eBay connect
(an admin credential write) in B.

No live stack: the membership resolver and the eBay env are faked, so what is
under test is the gate itself.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import HTTPException

import app.tenancy as tenancy
from app.api import ebay_oauth
from app.auth import AuthContext, ServiceScope

WS_A = uuid.uuid4()
WS_B = uuid.uuid4()
ENTITY = uuid.uuid4()


def _auth(service_scope: ServiceScope | None) -> AuthContext:
    return AuthContext(
        uc_uid="owner-user",
        agent_id=None,
        scopes=("service",),
        claims={},
        service_scope=service_scope,
    )


@pytest.fixture(autouse=True)
def _admin_everywhere(monkeypatch):
    """The mapped user is admin in both A and B; only the token varies."""

    async def _membership(uc_uid, workspace_id, *, autoprovision=False):
        return "admin", None, False, f"ws-{workspace_id}"

    monkeypatch.setattr(tenancy, "_resolve_membership", _membership)
    monkeypatch.setattr(
        ebay_oauth,
        "_ebay_env",
        lambda: {
            "app_id": "app-id",
            "cert_id": "cert-id",
            "ru_name": "ru-name",
            "environment": "sandbox",
        },
    )
    monkeypatch.setattr(ebay_oauth, "_sign_state", lambda payload: "signed-state")


async def _start(auth: AuthContext, workspace_id: uuid.UUID) -> Any:
    return await ebay_oauth.ebay_oauth_start(
        workspace_id=workspace_id, entity_id=ENTITY, auth=auth
    )


async def test_scoped_token_cannot_start_in_other_workspace():
    scoped = _auth(ServiceScope(workspace_ids=frozenset({WS_A}), role_floor="viewer"))
    with pytest.raises(HTTPException) as exc:
        await _start(scoped, WS_B)
    assert exc.value.status_code == 403
    assert exc.value.detail == "Service token is not scoped to this workspace."


async def test_viewer_floor_cannot_start_in_its_own_workspace():
    scoped = _auth(ServiceScope(workspace_ids=frozenset({WS_A}), role_floor="viewer"))
    with pytest.raises(HTTPException) as exc:
        await _start(scoped, WS_A)
    assert exc.value.status_code == 403
    assert "needs 'admin'" in exc.value.detail


async def test_entity_scope_of_token_is_enforced():
    scoped = _auth(
        ServiceScope(
            workspace_ids=frozenset({WS_A}),
            entity_ids=frozenset({uuid.uuid4()}),
            role_floor="admin",
        )
    )
    with pytest.raises(HTTPException) as exc:
        await _start(scoped, WS_A)
    assert exc.value.status_code == 403
    assert exc.value.detail == "Entity outside this membership's scope"


async def test_admin_scoped_token_in_its_workspace_still_redirects():
    scoped = _auth(ServiceScope(workspace_ids=frozenset({WS_A}), role_floor="admin"))
    resp = await _start(scoped, WS_A)
    assert resp.status_code == 302
    assert "state=signed-state" in resp.headers["location"]


async def test_human_admin_unchanged():
    resp = await _start(_auth(None), WS_B)
    assert resp.status_code == 302
