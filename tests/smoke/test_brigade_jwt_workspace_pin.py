"""AO-TOK-2: a Brigade workspace-scoped JWT is pinned to its claim on HTTP.

MCP already refuses a federation token used outside the workspace it was
scoped to (app/mcp_auth.py). On HTTP the claim was only a fallback: the path or
X-Workspace-Id could name any other workspace the mapped user belongs to, at
that user's full role. resolve_workspace (and the hand-rolled eBay start) now
apply the same pin.

No live stack: a tiny app mounts a resolve_workspace route, the bearer is
overridden, and the membership resolver plus the registry lookup are faked.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

import app.tenancy as tenancy
from app.auth import AuthContext, validate_bearer_token
from app.platform import workspace_registry as wr

BRIGADE_ISS = "https://brigade.example.com"
W1 = uuid.uuid4()
W2 = uuid.uuid4()
W1_REGISTRY = uuid.uuid4()  # uc-registry id that maps to the internal W1
ENTITY = uuid.uuid4()


def _brigade(claim_wsid: uuid.UUID | None) -> AuthContext:
    claims: dict = {"iss": BRIGADE_ISS, "sub": "jane@example.com"}
    if claim_wsid is not None:
        claims["workspace_id"] = str(claim_wsid)
    return AuthContext(uc_uid="jane-uid", agent_id=None, scopes=(), claims=claims)


def _keycloak() -> AuthContext:
    return AuthContext(
        uc_uid="jane-uid",
        agent_id=None,
        scopes=(),
        claims={"iss": "https://auth.example.com/realms/uchub"},
    )


@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
    monkeypatch.setenv("BRIGADE_TRUSTED_ISSUER", BRIGADE_ISS)

    async def _membership(uc_uid, workspace_id, *, autoprovision=False):
        # The mapped user is admin in BOTH W1 and W2.
        if workspace_id in (W1, W2):
            return "admin", None, False, f"ws-{workspace_id}"
        raise HTTPException(status_code=403, detail="No active membership for this workspace")

    async def _resolve(workspace_id):
        return W1 if workspace_id == W1_REGISTRY else workspace_id

    monkeypatch.setattr(tenancy, "_resolve_membership", _membership)
    monkeypatch.setattr(wr, "resolve_internal_workspace_id", _resolve)


def _client(auth: AuthContext) -> TestClient:
    app = FastAPI()

    @app.get("/probe")
    async def probe(ws: tenancy.WorkspaceContext = Depends(tenancy.resolve_workspace)):
        return {"workspace_id": str(ws.workspace_id), "role": ws.role}

    @app.get("/api/workspaces/{workspace_id}/probe")
    async def path_probe(
        workspace_id: uuid.UUID,
        ws: tenancy.WorkspaceContext = Depends(tenancy.resolve_workspace),
    ):
        return {"workspace_id": str(ws.workspace_id)}

    app.dependency_overrides[validate_bearer_token] = lambda: auth
    return TestClient(app)


def test_brigade_jwt_for_w1_with_header_w2_is_403():
    r = _client(_brigade(W1)).get("/probe", headers={"X-Workspace-Id": str(W2)})
    assert r.status_code == 403
    assert r.json() == {
        "detail": "workspace does not match the federation token's scoped workspace"
    }


def test_brigade_jwt_for_w1_on_path_w2_is_403():
    r = _client(_brigade(W1)).get(f"/api/workspaces/{W2}/probe")
    assert r.status_code == 403


def test_brigade_jwt_for_w1_with_header_w1_is_200():
    r = _client(_brigade(W1)).get("/probe", headers={"X-Workspace-Id": str(W1)})
    assert r.status_code == 200
    assert r.json()["workspace_id"] == str(W1)


def test_brigade_registry_claim_accepts_internal_id_and_maps_registry_id():
    client = _client(_brigade(W1_REGISTRY))
    r = client.get("/probe", headers={"X-Workspace-Id": str(W1)})
    assert r.status_code == 200
    assert r.json()["workspace_id"] == str(W1)
    # The registry id itself (or the claim fallback) maps to internal W1.
    r = client.get("/probe", headers={"X-Workspace-Id": str(W1_REGISTRY)})
    assert r.status_code == 200
    assert r.json()["workspace_id"] == str(W1)
    r = client.get("/probe")
    assert r.status_code == 200
    assert r.json()["workspace_id"] == str(W1)
    r = client.get("/probe", headers={"X-Workspace-Id": str(W2)})
    assert r.status_code == 403


def test_brigade_jwt_without_workspace_claim_is_unpinned():
    r = _client(_brigade(None)).get("/probe", headers={"X-Workspace-Id": str(W2)})
    assert r.status_code == 200


def test_non_federation_token_unchanged():
    r = _client(_keycloak()).get("/probe", headers={"X-Workspace-Id": str(W2)})
    assert r.status_code == 200


async def test_ebay_start_pins_brigade_jwt(monkeypatch):
    from app.api import ebay_oauth

    monkeypatch.setattr(
        ebay_oauth,
        "_ebay_env",
        lambda: {"app_id": "a", "cert_id": "c", "ru_name": "r", "environment": "sandbox"},
    )
    monkeypatch.setattr(ebay_oauth, "_sign_state", lambda payload: "signed-state")
    with pytest.raises(HTTPException) as exc:
        await ebay_oauth.ebay_oauth_start(
            workspace_id=W2, entity_id=ENTITY, auth=_brigade(W1)
        )
    assert exc.value.status_code == 403
    resp = await ebay_oauth.ebay_oauth_start(
        workspace_id=W1, entity_id=ENTITY, auth=_brigade(W1)
    )
    assert resp.status_code == 302
