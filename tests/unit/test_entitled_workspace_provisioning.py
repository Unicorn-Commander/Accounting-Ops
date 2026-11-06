"""Unit coverage for the opt-in Ops-Center first-call workspace path."""
from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

import app.api.onboarding as onboarding
import app.mcp_auth as mcp_auth
from app.auth import AuthContext
from app.security.entitlements import OpsCenterEntitlementsClient

_ORG_ID = "01924a3c-0000-7000-8000-000000000123"
_OWNER_UID = "fictional-basil-owner"
_BEARER = "fictional-uchub-bearer"


def _auth(uid: str = _OWNER_UID) -> AuthContext:
    return AuthContext(
        uc_uid=uid,
        agent_id=None,
        scopes=("openid", "profile", "email", "mcp"),
        claims={
            "sub": uid,
            "iss": "https://auth.fictional.example/realms/fictional",
            "email": f"{uid}@example.com",
        },
        may_autoprovision=True,
    )


class FakeOpsCenter:
    def __init__(self, payload: dict, *, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code
        self.authorization_headers: list[str | None] = []
        self.app = FastAPI()
        self.app.add_api_route(
            "/api/v1/entitlements", self.entitlements, methods=["GET"]
        )

    async def entitlements(self, request: Request):
        self.authorization_headers.append(request.headers.get("authorization"))
        if self.status_code != 200:
            return Response(status_code=self.status_code)
        return JSONResponse(self.payload)


@pytest.fixture
async def fake_ops_client():
    server = FakeOpsCenter(
        {
            "org_id": _ORG_ID,
            "org_name": "Fictional Juniper Books",
            "tier": "byok",
            "role": "owner",
            "entitlements": ["accounting_ops_access"],
        }
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.app),
        base_url="http://ops-center.fictional.test",
    ) as http_client:
        yield server, OpsCenterEntitlementsClient(
            base_url="http://ops-center.fictional.test",
            http_client=http_client,
        )


@pytest.fixture
def in_memory_provisioning(monkeypatch):
    user_id = uuid4()
    workspaces: dict[UUID, dict] = {}
    memberships: dict[tuple[UUID, UUID], str] = {}
    provision_calls: list[dict] = []

    async def ensure_user(_uc_uid: str, *, autoprovision: bool) -> UUID:
        assert autoprovision is True
        return user_id

    async def first_workspace(_user_id: UUID):
        for (member_id, workspace_id), _role in memberships.items():
            if member_id == _user_id:
                workspace = workspaces[workspace_id]
                return {
                    "id": str(workspace_id),
                    "slug": workspace["slug"],
                    "display_name": workspace["display_name"],
                }
        return None

    async def no_backfill(_user_id: UUID, _claims: dict) -> None:
        return None

    async def provision(**kwargs):
        provision_calls.append(kwargs)
        workspace_id = kwargs["workspace_id"]
        workspaces.setdefault(
            workspace_id,
            {
                "slug": kwargs["slug"],
                "display_name": kwargs["display_name"],
                "uc_org_id": kwargs["uc_org_id"],
            },
        )
        memberships.setdefault((kwargs["user_id"], workspace_id), kwargs["role"])
        return "books", kwargs["slug"]

    monkeypatch.setattr(onboarding, "_ensure_user_account", ensure_user)
    monkeypatch.setattr(onboarding, "_first_active_workspace", first_workspace)
    monkeypatch.setattr(onboarding, "_backfill_user_profile", no_backfill)
    monkeypatch.setattr(onboarding, "_provision_workspace_with_membership", provision)
    monkeypatch.setenv("AO_ENTITLED_PROVISIONING", "1")
    return user_id, workspaces, memberships, provision_calls


async def test_entitled_first_call_provisions_once_and_second_reuses(
    fake_ops_client, in_memory_provisioning
):
    server, client = fake_ops_client
    _user_id, workspaces, memberships, provision_calls = in_memory_provisioning

    first = await onboarding.ensure_entitled_workspace(_auth(), _BEARER, entitlement_client=client)
    second = await onboarding.ensure_entitled_workspace(_auth(), _BEARER, entitlement_client=client)

    workspace_id = UUID(_ORG_ID)
    assert first.state == "provisioned"
    assert first.workspace_id == workspace_id
    assert first.display_name == "Fictional Juniper Books"
    assert second.state == "existing"
    assert second.workspace_id == workspace_id
    assert len(workspaces) == 1
    assert len(provision_calls) == 1
    assert memberships[(next(iter(memberships))[0], workspace_id)] == "owner"
    assert server.authorization_headers == [f"Bearer {_BEARER}"]


async def test_entitled_member_uses_fallback_name_and_accountant_role(
    fake_ops_client, in_memory_provisioning
):
    server, client = fake_ops_client
    server.payload = {
        "org_id": _ORG_ID,
        "tier": "founder_workspace",
        "role": "member",
        "entitlements": ["accounting_ops_access"],
    }
    _user_id, workspaces, memberships, _provision_calls = in_memory_provisioning

    result = await onboarding.ensure_entitled_workspace(
        _auth("fictional-member"), _BEARER, entitlement_client=client
    )

    workspace_id = UUID(_ORG_ID)
    assert result.display_name == f"Workspace {_ORG_ID[:8]}"
    assert workspaces[workspace_id]["display_name"] == f"Workspace {_ORG_ID[:8]}"
    assert memberships[(next(iter(memberships))[0], workspace_id)] == "accountant"


async def test_concurrent_entitled_first_calls_converge_on_one_workspace(
    fake_ops_client, in_memory_provisioning, monkeypatch
):
    _server, client = fake_ops_client
    _user_id, workspaces, memberships, provision_calls = in_memory_provisioning
    gate = asyncio.Barrier(2)

    async def first_workspace(_user_id: UUID):
        existing = None
        for (member_id, workspace_id), _role in memberships.items():
            if member_id == _user_id:
                workspace = workspaces[workspace_id]
                existing = {
                    "id": str(workspace_id),
                    "slug": workspace["slug"],
                    "display_name": workspace["display_name"],
                }
                break
        await gate.wait()
        return existing

    monkeypatch.setattr(onboarding, "_first_active_workspace", first_workspace)
    results = await asyncio.gather(
        onboarding.ensure_entitled_workspace(_auth(), _BEARER, entitlement_client=client),
        onboarding.ensure_entitled_workspace(_auth(), _BEARER, entitlement_client=client),
    )

    assert all(result.workspace_id == UUID(_ORG_ID) for result in results)
    assert len(workspaces) == 1
    assert len(memberships) == 1
    # Both requests may reach the idempotent DB upsert; the stable key still
    # leaves exactly one effective workspace.
    assert len(provision_calls) == 2


async def test_not_entitled_does_not_create_workspace(fake_ops_client, in_memory_provisioning):
    server, client = fake_ops_client
    server.payload = {
        "org_id": _ORG_ID,
        "tier": "free",
        "role": "owner",
        "entitlements": ["contact_ops_access"],
    }
    _user_id, workspaces, _memberships, provision_calls = in_memory_provisioning

    result = await onboarding.ensure_entitled_workspace(_auth(), _BEARER, entitlement_client=client)

    assert result.state == "not_entitled"
    assert workspaces == {}
    assert provision_calls == []


async def test_flag_off_keeps_first_call_inert(fake_ops_client, in_memory_provisioning, monkeypatch):
    server, client = fake_ops_client
    monkeypatch.setenv("AO_ENTITLED_PROVISIONING", "0")
    _user_id, workspaces, _memberships, provision_calls = in_memory_provisioning

    result = await onboarding.ensure_entitled_workspace(_auth(), _BEARER, entitlement_client=client)

    assert result.state == "disabled"
    assert server.authorization_headers == []
    assert workspaces == {}
    assert provision_calls == []


async def test_ops_center_down_fails_closed_without_workspace(
    fake_ops_client, in_memory_provisioning
):
    _server, _client = fake_ops_client
    down = FakeOpsCenter({}, status_code=503)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=down.app),
        base_url="http://ops-center.fictional.test",
    ) as http_client:
        client = OpsCenterEntitlementsClient(
            base_url="http://ops-center.fictional.test",
            http_client=http_client,
        )
        _user_id, workspaces, _memberships, provision_calls = in_memory_provisioning
        result = await onboarding.ensure_entitled_workspace(
            _auth(), _BEARER, entitlement_client=client
        )

    assert result.state == "unavailable"
    assert "no workspace was created" in (result.detail or "")
    assert workspaces == {}
    assert provision_calls == []


async def test_mcp_downstream_gets_clear_provisioning_error(monkeypatch):
    async def fake_authenticate(_token: str) -> AuthContext:
        return _auth()

    async def fake_ensure(_auth_context, _bearer):
        return onboarding.EntitledProvisioningResult(
            "unavailable", detail=onboarding.ENTITLED_PROVISIONING_UNAVAILABLE_DETAIL
        )

    called = False

    async def inner(scope, receive, send):
        nonlocal called
        called = True

    monkeypatch.setattr(mcp_auth, "authenticate_token", fake_authenticate)
    monkeypatch.setattr(onboarding, "ensure_entitled_workspace", fake_ensure)
    wrapped = mcp_auth.MCPAuthMiddleware(inner)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=wrapped), base_url="http://accounting.fictional.test"
    ) as client:
        response = await client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {_BEARER}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        )

    assert response.status_code == 503
    assert "no workspace was created" in response.json()["detail"]
    assert called is False
