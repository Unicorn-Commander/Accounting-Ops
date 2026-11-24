"""AO114-R3-2: POST /api/me/clients honours a PAT's own scopes.

The add-client gate checked only the roles _caller_account returned (which
R2-5 already scoped to the token's workspaces and role floor). A PAT's
``scopes`` list was never consulted, so a read-only PAT held by a cpa
provisioned a workspace and seated its owner as ``owner``. The route now
demands the PAT scope a cpa/accountant seat demands on every require_role
route (accounting:write). Humans and non-PAT tokens are unchanged.

No live stack: _caller_account and provision_workspace are faked; the fake
provisioner raises a sentinel so the test stops right where the workspace
would be created.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

import scripts.provision_workspace as prov
from app.api import clients
from app.auth import AuthContext

USER_ID = uuid.uuid4()


class _Provisioned(Exception):
    """Raised by the fake provisioner: the gate let the caller through."""


def _pat(*scopes: str) -> AuthContext:
    return AuthContext(
        uc_uid="owner-user",
        agent_id=None,
        scopes=tuple(scopes),
        claims={"sub": "owner-user", "pat_id": str(uuid.uuid4())},
    )


def _human() -> AuthContext:
    return AuthContext(
        uc_uid="owner-user",
        agent_id=None,
        scopes=(),
        claims={"iss": "https://identity.example.com"},
        may_autoprovision=True,
    )


@pytest.fixture
def calls(monkeypatch):
    seen: list[str] = []

    async def _caller_account(_auth, **_kw):
        return USER_ID, "jane@example.com", ["cpa"], 1

    async def _provision(**kwargs):
        seen.append(kwargs["display_name"])
        raise _Provisioned

    monkeypatch.setattr(clients, "_caller_account", _caller_account)
    monkeypatch.setattr(prov, "provision_workspace", _provision)
    return seen


BODY = clients.AddClientRequest(business_name="Fictional Bakery LLC")


@pytest.mark.parametrize("scopes", [("accounting:read",), ()])
async def test_read_only_pat_cannot_add_client(calls, scopes) -> None:
    with pytest.raises(HTTPException) as exc:
        await clients.add_client(body=BODY, auth=_pat(*scopes))
    assert exc.value.status_code == 403
    assert "needs accounting:write" in str(exc.value.detail)
    assert calls == []


@pytest.mark.parametrize("scope", ["accounting:write", "accounting:admin"])
async def test_write_pat_still_adds_client(calls, scope) -> None:
    with pytest.raises(_Provisioned):
        await clients.add_client(body=BODY, auth=_pat(scope))
    assert calls == ["Fictional Bakery LLC"]


async def test_human_unchanged(calls) -> None:
    with pytest.raises(_Provisioned):
        await clients.add_client(body=BODY, auth=_human())
    assert calls == ["Fictional Bakery LLC"]
