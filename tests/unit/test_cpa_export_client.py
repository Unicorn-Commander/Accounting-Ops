"""Unit coverage for the cpa-export identity assertion boundary."""
from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest
from fastapi import HTTPException

from app.security.internal_assertion import verify_assertion
from app.tools import _cpa_export_client as client


REQUESTED_WORKSPACE = UUID("11111111-1111-4111-8111-111111111111")
CANONICAL_WORKSPACE = UUID("22222222-2222-4222-8222-222222222222")
ENTITY = UUID("33333333-3333-4333-8333-333333333333")


class _Response:
    status_code = 200
    text = ""

    def json(self) -> dict[str, object]:
        return {"workspace_id": str(CANONICAL_WORKSPACE)}


class _AsyncClient:
    last_call: tuple[str, dict[str, str]] | None = None

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url: str, *, headers: dict[str, str]):
        type(self).last_call = (url, headers)
        return _Response()


def _bind_authorized_caller(monkeypatch, *, auth=None):
    async def fake_authorize(*_args, **_kwargs):
        return _authorized_workspace()

    monkeypatch.setattr(
        client,
        "authorize_workspace",
        fake_authorize,
    )
    monkeypatch.setattr(client, "current_auth", lambda: auth)
    monkeypatch.setattr(
        client,
        "get_settings",
        lambda: SimpleNamespace(cpa_export_url="http://cpa-export:8001"),
    )


async def _fetch(monkeypatch, *, auth=None):
    _bind_authorized_caller(monkeypatch, auth=auth)
    return await client.fetch_return_package(
        form_path="1120",
        form_label="1120",
        workspace_id=REQUESTED_WORKSPACE,
        entity_id=ENTITY,
        tax_year=2024,
    )


def _authorized_workspace():
    return SimpleNamespace(workspace_id=CANONICAL_WORKSPACE, role="owner")


def _broken_crypto():
    class BrokenCrypto:
        def master_key_bytes(self):
            raise RuntimeError("test master key unavailable")

    return BrokenCrypto()


@pytest.mark.asyncio
async def test_assertion_mint_failure_returns_503_and_does_not_call_sidecar(
    monkeypatch, caplog
):
    """The default is fail-closed: bearer-only is never an implicit fallback."""
    monkeypatch.delenv("CPA_EXPORT_ALLOW_BEARER_ONLY", raising=False)
    monkeypatch.setenv("CPA_EXPORT_TOKEN", "transport-token-for-test")
    monkeypatch.setattr(client, "get_crypto", _broken_crypto)
    monkeypatch.setattr(
        client.httpx,
        "AsyncClient",
        lambda **_kwargs: pytest.fail("sidecar must not be called after assertion failure"),
    )

    auth = SimpleNamespace(
        uc_uid="canonical-subject",
        scopes=("accounting:read",),
        agent_id=None,
    )
    with pytest.raises(HTTPException) as exc_info:
        await _fetch(monkeypatch, auth=auth)

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "cpa-export identity assertion unavailable"
    assert "failure_class=RuntimeError" in caplog.text
    assert "test master key unavailable" not in caplog.text


@pytest.mark.asyncio
async def test_allow_bearer_only_transition_calls_sidecar_without_assertion(monkeypatch):
    """The documented transition flag permits the legacy transport only."""
    monkeypatch.setenv("CPA_EXPORT_ALLOW_BEARER_ONLY", "true")
    monkeypatch.setenv("CPA_EXPORT_TOKEN", "transport-token-for-test")
    monkeypatch.setattr(client, "get_crypto", _broken_crypto)
    _AsyncClient.last_call = None
    monkeypatch.setattr(client.httpx, "AsyncClient", _AsyncClient)

    auth = SimpleNamespace(
        uc_uid="canonical-subject",
        scopes=("accounting:read",),
        agent_id=None,
    )
    data = await _fetch(monkeypatch, auth=auth)

    assert data["_draft_notice"].startswith("DRAFT")
    assert _AsyncClient.last_call is not None
    url, headers = _AsyncClient.last_call
    query = parse_qs(urlsplit(url).query)
    assert query["workspace_id"] == [str(CANONICAL_WORKSPACE)]
    assert query["entity_id"] == [str(ENTITY)]
    assert headers == {"Authorization": "Bearer transport-token-for-test"}


@pytest.mark.asyncio
async def test_successful_assertion_forwards_canonical_claim_shapes(monkeypatch):
    master_key = b"m" * 32
    monkeypatch.delenv("CPA_EXPORT_ALLOW_BEARER_ONLY", raising=False)
    monkeypatch.setenv("CPA_EXPORT_TOKEN", "transport-token-for-test")
    monkeypatch.setattr(
        client,
        "get_crypto",
        lambda: SimpleNamespace(master_key_bytes=lambda: master_key),
    )
    _AsyncClient.last_call = None
    monkeypatch.setattr(client.httpx, "AsyncClient", _AsyncClient)

    auth = SimpleNamespace(
        uc_uid="canonical-subject",
        scopes=("accounting:read", "tax:read"),
        agent_id="agent-shape",
    )
    await _fetch(monkeypatch, auth=auth)

    assert _AsyncClient.last_call is not None
    url, headers = _AsyncClient.last_call
    assertion = verify_assertion(master_key, headers["X-Internal-Assertion"])
    assert assertion.uc_uid == "canonical-subject"
    assert assertion.workspace_id == str(CANONICAL_WORKSPACE)
    assert assertion.entity_id == str(ENTITY)
    assert assertion.role == "owner"
    assert assertion.scopes == ("accounting:read", "tax:read")
    assert assertion.agent_id == "agent-shape"
    assert parse_qs(urlsplit(url).query)["workspace_id"] == [str(CANONICAL_WORKSPACE)]


@pytest.mark.asyncio
async def test_consolidated_request_uses_sidecar_all_sentinel_and_null_entity_claim(
    monkeypatch,
):
    master_key = b"c" * 32
    monkeypatch.delenv("CPA_EXPORT_ALLOW_BEARER_ONLY", raising=False)
    monkeypatch.setenv("CPA_EXPORT_TOKEN", "transport-token-for-test")
    monkeypatch.setattr(
        client,
        "get_crypto",
        lambda: SimpleNamespace(master_key_bytes=lambda: master_key),
    )
    _AsyncClient.last_call = None
    monkeypatch.setattr(client.httpx, "AsyncClient", _AsyncClient)

    auth = SimpleNamespace(
        uc_uid="canonical-subject",
        scopes=("accounting:read",),
        agent_id=None,
    )
    _bind_authorized_caller(monkeypatch, auth=auth)
    await client.fetch_return_package(
        form_path="1120",
        form_label="1120",
        workspace_id=REQUESTED_WORKSPACE,
        entity_id="all",
        tax_year=2024,
        require_unscoped=True,
    )

    assert _AsyncClient.last_call is not None
    url, headers = _AsyncClient.last_call
    assertion = verify_assertion(master_key, headers["X-Internal-Assertion"])
    assert assertion.entity_id is None
    query = parse_qs(urlsplit(url).query)
    assert query["entity_id"] == ["all"]


@pytest.mark.asyncio
async def test_http_form_1120_route_delegates_to_assertion_aware_tool(monkeypatch):
    from app.api import tools as http_tools

    calls: list[tuple[UUID, UUID, int]] = []

    async def fake_get_form_1120_data(workspace_id, entity_id, tax_year):
        calls.append((workspace_id, entity_id, tax_year))
        return {"ok": True}

    monkeypatch.setattr(http_tools, "get_form_1120_data", fake_get_form_1120_data)
    ctx = SimpleNamespace(
        workspace=SimpleNamespace(workspace_id=CANONICAL_WORKSPACE),
        entity_id=ENTITY,
    )

    result = await http_tools.http_get_form_1120_data(2024, ctx)

    assert result == {"ok": True}
    assert calls == [(CANONICAL_WORKSPACE, ENTITY, 2024)]
