"""A2 — BYOK base_url: outbound guard at write (POST /inference/byok) and at use
(resolver + chat runner), and no server credential ever reaches a BYOK URL.

No network: DNS is a dict and the socket layer is a recording httpcore mock.
"""
from __future__ import annotations

import json
import sys
import types
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpcore
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import agents.chat.runner as runner_mod
import app.api.inference_settings as inference_settings
import app.security.outbound as ob
from agents.chat.inference_config import resolve_chat_inference
from app.llm_client import FailoverAsyncClient
from app.tenancy import WorkspaceContext, resolve_workspace

_DNS = {
    "llm.example.com": ["93.184.215.14"],
    "metadata.example.com": ["169.254.169.254"],
    "litellm.example.com": ["172.18.0.4"],
}
_CUSTOMER_KEY = "sk-customer-own-key-1234"
_SERVER_KEY = "sk-or-SERVER-KEY-must-not-leak"
_INTERNAL_BASE_URLS = [
    "https://169.254.169.254/latest",
    "https://127.0.0.1:4000/v1",
    "https://10.0.0.7/v1",
    "https://100.64.0.9/v1",
    "https://192.0.0.10/v1",
    "https://[::1]/v1",
    "https://[fd12::1]/v1",
    "https://localhost/v1",
    "https://metadata.example.com/v1",
    "https://litellm.example.com/v1",
    "http://llm.example.com/v1",
]
_COMPLETION = json.dumps(
    {
        "id": "c1",
        "object": "chat.completion",
        "created": 0,
        "model": "m",
        "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "hi"}}
        ],
    }
).encode()


class _RecordingBackend(httpcore.AsyncMockBackend):
    def __init__(self, net) -> None:
        super().__init__(
            [
                b"HTTP/1.1 200 OK\r\n",
                f"Content-Type: application/json\r\nContent-Length: {len(_COMPLETION)}\r\n\r\n".encode(),
                _COMPLETION,
            ]
        )
        self.net = net

    async def connect_tcp(self, host, port, **kw):
        self.net.dialled.append((host, port))
        stream = await super().connect_tcp(host, port, **kw)
        net = self.net
        orig_write = stream.write

        async def _write(buffer, timeout=None):
            net.sent += buffer
            return await orig_write(buffer, timeout)

        stream.write = _write
        return stream


@pytest.fixture()
def net(monkeypatch):
    n = types.SimpleNamespace(dialled=[], sent=b"")

    async def _fake_resolve(host, port):
        if host not in _DNS:
            raise OSError("NXDOMAIN")
        return list(_DNS[host])

    monkeypatch.setattr(ob, "_resolve", _fake_resolve)
    monkeypatch.setattr(ob, "_new_inner_backend", lambda: _RecordingBackend(n))
    monkeypatch.setenv("OPENROUTER_API_KEY", _SERVER_KEY)
    monkeypatch.delenv("LLM_FALLBACK_BASE_URL", raising=False)
    return n


# ---------------------------------------------------------------------------
# Write time: POST /api/workspaces/{id}/inference/byok
# ---------------------------------------------------------------------------

_WID = uuid.uuid4()


@pytest.fixture()
def stored(monkeypatch):
    calls: list = []

    async def _store(session, wid, connector, label, value, **kw):
        calls.append((connector, label, value))
        return uuid.uuid4()

    @asynccontextmanager
    async def _session():
        class _S:
            async def commit(self):
                return None

        yield _S()

    monkeypatch.setattr(inference_settings, "store_credential", _store)
    monkeypatch.setattr(inference_settings, "SessionLocal", _session)
    return calls


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(inference_settings.router)
    app.dependency_overrides[resolve_workspace] = lambda: WorkspaceContext(
        workspace_id=_WID,
        workspace_slug="acme",
        role="owner",
        user_uc_uid="uc-test-user",
        agent_id=None,
        entity_scope=None,
        hipaa_protected=False,
    )
    return TestClient(app)


@pytest.mark.parametrize("base_url", _INTERNAL_BASE_URLS)
def test_post_byok_refuses_internal_base_url(net, stored, base_url):
    resp = _client().post(
        f"/api/workspaces/{_WID}/inference/byok",
        json={"provider": "openrouter", "api_key": _CUSTOMER_KEY, "base_url": base_url},
    )
    assert resp.status_code == 400, resp.text
    assert "refused" in resp.json()["detail"]
    assert stored == []
    assert net.dialled == []


def test_post_byok_accepts_public_base_url(net, stored):
    resp = _client().post(
        f"/api/workspaces/{_WID}/inference/byok",
        json={
            "provider": "openrouter",
            "api_key": _CUSTOMER_KEY,
            "base_url": "https://llm.example.com/v1",
        },
    )
    assert resp.status_code == 200, resp.text
    assert json.loads(stored[0][2])["base_url"] == "https://llm.example.com/v1"
    assert net.dialled == []


# ---------------------------------------------------------------------------
# Use time: resolver
# ---------------------------------------------------------------------------


@pytest.fixture()
def settings_reset(monkeypatch):
    from app.config import get_settings

    monkeypatch.delenv("AO_ROUTE_CHAT_THROUGH_GATEWAY", raising=False)
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_resolver_marks_byok_endpoint_untrusted(net, settings_reset, monkeypatch):
    import app.security.workspace_llm_keys as wk

    monkeypatch.setattr(
        wk, "get_byok",
        AsyncMock(return_value=(_CUSTOMER_KEY, "openrouter", "https://llm.example.com/v1")),
    )
    r = await resolve_chat_inference(workspace_id=str(_WID))
    assert (r.source, r.base_url, r.api_key) == ("byok", "https://llm.example.com/v1", _CUSTOMER_KEY)
    assert r.untrusted_base_url is True


@pytest.mark.parametrize(
    "stored_base", ["http://169.254.169.254/v1", "https://10.0.0.7/v1", "https://localhost/v1"]
)
async def test_resolver_drops_a_stored_internal_byok_endpoint(
    net, settings_reset, monkeypatch, stored_base
):
    """A pre-guard row pointing inside: the endpoint is dropped, the CUSTOMER's
    key goes to the operator default, never to the internal target."""
    import app.security.workspace_llm_keys as wk

    monkeypatch.setattr(
        wk, "get_byok", AsyncMock(return_value=(_CUSTOMER_KEY, "openrouter", stored_base))
    )
    r = await resolve_chat_inference(workspace_id=str(_WID))
    assert r.source == "byok"
    assert r.base_url == "https://openrouter.ai/api/v1"
    assert r.api_key == _CUSTOMER_KEY
    assert r.untrusted_base_url is False


async def test_operator_routes_are_not_marked_untrusted(net, settings_reset):
    r = await resolve_chat_inference(workspace_id=str(_WID))
    assert r.source == "openrouter_default"
    assert r.untrusted_base_url is False


# ---------------------------------------------------------------------------
# Use time: chat runner
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_mcp(monkeypatch):
    class _Tools:
        tools: list = []

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def initialize(self):
            return None

        async def list_tools(self):
            return _Tools()

    @asynccontextmanager
    async def _streamable(url, headers=None):
        yield ("r", "w", None)

    mcp_mod = types.ModuleType("mcp")
    mcp_mod.ClientSession = _Session
    shttp = types.ModuleType("mcp.client.streamable_http")
    shttp.streamablehttp_client = _streamable
    monkeypatch.setitem(sys.modules, "mcp", mcp_mod)
    monkeypatch.setitem(sys.modules, "mcp.client", types.ModuleType("mcp.client"))
    monkeypatch.setitem(sys.modules, "mcp.client.streamable_http", shttp)


async def _run(**kw) -> list[dict]:
    events = []
    async for ev in runner_mod.stream_assistant_turn(
        system_prompt="s",
        history=[],
        user_message="hello",
        allowed_mcp_tools=(),
        user_bearer_token="bearer-x",
        max_iterations=1,
        **kw,
    ):
        events.append(ev)
    return events


@pytest.mark.parametrize("base_url", _INTERNAL_BASE_URLS)
async def test_runner_refuses_internal_byok_endpoint(net, fake_mcp, monkeypatch, base_url):
    built = []
    monkeypatch.setattr(runner_mod, "FailoverAsyncClient", lambda **k: built.append(k))
    events = await _run(
        openrouter_base_url=base_url, openrouter_api_key=_CUSTOMER_KEY, untrusted_base_url=True
    )
    assert events[-1]["type"] == "error"
    assert "refused" in events[-1]["message"]
    assert built == []
    assert net.dialled == []


async def test_runner_never_sends_server_key_to_byok_endpoint(net, fake_mcp, monkeypatch):
    built = []
    monkeypatch.setattr(runner_mod, "FailoverAsyncClient", lambda **k: built.append(k))
    events = await _run(
        openrouter_base_url="https://llm.example.com/v1",
        openrouter_api_key=None,
        untrusted_base_url=True,
    )
    assert events[-1]["type"] == "error"
    assert built == []
    assert net.dialled == []
    assert _SERVER_KEY.encode() not in net.sent


async def test_runner_byok_turn_uses_guarded_client_end_to_end(net, fake_mcp):
    """Public BYOK endpoint: dialled at the checked IP, with the CUSTOMER key."""
    events = await _run(
        openrouter_base_url="https://llm.example.com/v1",
        openrouter_api_key=_CUSTOMER_KEY,
        untrusted_base_url=True,
        model="m",
    )
    assert net.dialled == [("93.184.215.14", 443)]
    assert f"Bearer {_CUSTOMER_KEY}".encode() in net.sent
    assert _SERVER_KEY.encode() not in net.sent
    assert events[-1]["type"] == "stop"


async def test_guarded_primary_refuses_rebound_endpoint(net):
    """The runner's primary client re-checks DNS at connect: a host that now
    resolves inside is refused and nothing is dialled."""
    client = FailoverAsyncClient(
        base_url="https://metadata.example.com/v1",
        api_key=_CUSTOMER_KEY,
        model="m",
        primary_http_client=ob.guarded_async_client(),
        max_retries=0,
    )
    with pytest.raises(Exception):
        await client.chat.completions.create(
            model="m", messages=[{"role": "user", "content": "x"}]
        )
    assert net.dialled == []


def test_primary_http_client_does_not_leak_to_fallback(net, monkeypatch):
    monkeypatch.setenv("LLM_FALLBACK_BASE_URL", "https://fallback.example.org/v1")
    monkeypatch.setenv("LLM_FALLBACK_MODEL", "m")
    monkeypatch.setenv("LLM_FALLBACK_API_KEY", "sk-fallback-test")
    guarded = ob.guarded_async_client()
    client = FailoverAsyncClient(
        base_url="https://llm.example.com/v1", api_key=_CUSTOMER_KEY, model="m",
        primary_http_client=guarded,
    )
    assert client._primary_client._client is guarded
    assert client._fallback_client._client is not guarded
