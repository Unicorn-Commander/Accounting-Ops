"""Outbound guard (app.security.outbound) + SimpleFIN write/use sites (A1).

No network: DNS is a dict (``_resolve`` patched) and the socket layer is a
recording httpcore mock backend, so every test proves WHICH address was dialled
— or that nothing was.
"""
from __future__ import annotations

import base64
import uuid
from contextlib import asynccontextmanager

import httpcore
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import app.api.account_mappings as account_mappings
import app.api.cash_position as cash_position
import app.api.credentials as credentials_api
import app.api.transactions as transactions
import app.security.outbound as ob
from app.tenancy import WorkspaceContext, resolve_workspace

_DNS = {
    "bridge.example.com": ["93.184.215.14"],
    "beta-bridge.example.com": ["2606:2800:21f:cb07:6820:80da:af6b:8b2c"],
    "metadata.example.com": ["169.254.169.254"],
    "intranet.example.com": ["10.0.0.5"],
    "cgnat.example.com": ["100.64.1.1"],
    "mixed.example.com": ["93.184.215.14", "127.0.0.1"],
}

_INTERNAL_URLS = [
    "https://u:p@127.0.0.1/simplefin",
    "https://u:p@10.1.2.3/simplefin",
    "https://u:p@172.16.5.4/simplefin",
    "https://u:p@192.168.1.10/simplefin",
    "https://u:p@169.254.169.254/latest/meta-data",
    "https://u:p@100.64.0.1/simplefin",
    "https://u:p@192.0.0.8/simplefin",
    "https://u:p@0.0.0.0/simplefin",
    "https://u:p@224.0.0.1/simplefin",
    "https://u:p@[::1]/simplefin",
    "https://u:p@[::]/simplefin",
    "https://u:p@[fe80::1]/simplefin",
    "https://u:p@[fd00::1]/simplefin",
    "https://u:p@[ff02::1]/simplefin",
    "https://u:p@[::ffff:127.0.0.1]/simplefin",
    "https://u:p@[64:ff9b::a9fe:a9fe]/simplefin",
    "https://u:p@2130706433/simplefin",
    "https://u:p@0x7f.1/simplefin",
    "https://u:p@localhost/simplefin",
    "https://u:p@metadata.example.com/simplefin",
    "https://u:p@intranet.example.com/simplefin",
    "https://u:p@cgnat.example.com/simplefin",
    "https://u:p@mixed.example.com/simplefin",
    "https://u:p@unresolvable.example.com/simplefin",
    "http://u:p@bridge.example.com/simplefin",
    "ftp://u:p@bridge.example.com/simplefin",
]

_PUBLIC_ACCESS_URL = "https://demo:secret@bridge.example.com/simplefin"
_ACCOUNTS_BODY = (
    b'{"errors":[],"accounts":[{"id":"ACT-1","name":"Checking","currency":"USD",'
    b'"balance":"10.00","org":{"name":"Example Bank"},"transactions":[]}]}'
)


class _RecordingBackend(httpcore.AsyncMockBackend):
    def __init__(self, dialled: list, body: bytes, status: str = "200 OK") -> None:
        super().__init__(
            [
                f"HTTP/1.1 {status}\r\n".encode(),
                f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n".encode(),
                body,
            ]
        )
        self.dialled = dialled

    async def connect_tcp(self, host, port, **kw):
        self.dialled.append((host, port))
        return await super().connect_tcp(host, port, **kw)


@pytest.fixture()
def net(monkeypatch):
    """Fake DNS + recording socket layer. ``net.dialled`` = addresses dialled."""

    class _Net:
        dialled: list = []
        body: bytes = _ACCOUNTS_BODY
        status: str = "200 OK"

    n = _Net()
    n.dialled = []

    async def _fake_resolve(host, port):
        if host not in _DNS:
            raise OSError("NXDOMAIN")
        return list(_DNS[host])

    monkeypatch.setattr(ob, "_resolve", _fake_resolve)
    monkeypatch.setattr(
        ob, "_new_inner_backend", lambda: _RecordingBackend(n.dialled, n.body, n.status)
    )
    return n


# ---------------------------------------------------------------------------
# The guard itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", _INTERNAL_URLS)
async def test_guard_refuses_internal_targets(net, url):
    with pytest.raises(ob.OutboundURLRefused):
        await ob.assert_public_https_url(url)
    # And the guarded client refuses even if a caller skipped the pre-check.
    with pytest.raises(ob.OutboundURLRefused):
        async with ob.guarded_async_client() as client:
            await client.get(url)
    assert net.dialled == []


async def test_guard_accepts_public_simplefin_url_and_dials_checked_ip(net):
    await ob.assert_public_https_url(_PUBLIC_ACCESS_URL)
    async with ob.guarded_async_client() as client:
        resp = await client.get(_PUBLIC_ACCESS_URL + "/accounts")
    assert resp.status_code == 200
    assert net.dialled == [("93.184.215.14", 443)]


async def test_guard_rebinding_is_rechecked_at_connect(net):
    """Public at check time, internal at connect time → refused, never dialled."""
    await ob.assert_public_https_url("https://bridge.example.com/x")
    _DNS_before = dict(_DNS)
    _DNS["bridge.example.com"] = ["169.254.169.254"]
    try:
        with pytest.raises(ob.OutboundURLRefused):
            async with ob.guarded_async_client() as client:
                await client.get("https://bridge.example.com/x")
    finally:
        _DNS.clear()
        _DNS.update(_DNS_before)
    assert net.dialled == []


async def test_guard_does_not_follow_redirects(net):
    net.status = "302 Found\r\nLocation: https://169.254.169.254/latest"
    net.body = b"{}"
    async with ob.guarded_async_client() as client:
        resp = await client.get("https://bridge.example.com/x")
    assert resp.status_code == 302
    assert net.dialled == [("93.184.215.14", 443)]


# ---------------------------------------------------------------------------
# A1 — write time: POST /credentials with connector=simplefin
# ---------------------------------------------------------------------------

_WID = uuid.uuid4()


def _ws(role: str = "owner") -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=_WID,
        workspace_slug="acme",
        role=role,
        user_uc_uid="uc-test-user",
        agent_id=None,
        entity_scope=None,
        hipaa_protected=False,
    )


@pytest.fixture()
def stored(monkeypatch):
    calls: list = []

    async def _store(session, wid, connector, label, value, **kw):
        calls.append((connector, label, value))
        return uuid.uuid4()

    @asynccontextmanager
    async def _session():
        class _S:
            @asynccontextmanager
            async def begin(self):
                yield

        yield _S()

    monkeypatch.setattr(credentials_api, "store_credential", _store)
    monkeypatch.setattr(credentials_api, "SessionLocal", _session)
    return calls


def _creds_client() -> TestClient:
    app = FastAPI()
    app.include_router(credentials_api.router)
    app.dependency_overrides[resolve_workspace] = lambda: _ws()
    return TestClient(app)


@pytest.mark.parametrize(
    "url",
    [
        "https://u:p@169.254.169.254/latest",
        "https://u:p@127.0.0.1:8000/api",
        "https://u:p@intranet.example.com/simplefin",
        "http://u:p@bridge.example.com/simplefin",
    ],
)
def test_store_simplefin_credential_refuses_internal(net, stored, url):
    resp = _creds_client().post(
        f"/api/workspaces/{_WID}/credentials",
        json={"connector": "simplefin", "label": "x", "plaintext_value": url},
    )
    assert resp.status_code == 400, resp.text
    assert stored == []
    assert net.dialled == []


def test_store_simplefin_credential_accepts_public(net, stored):
    resp = _creds_client().post(
        f"/api/workspaces/{_WID}/credentials",
        json={"connector": "simplefin", "label": "x", "plaintext_value": _PUBLIC_ACCESS_URL},
    )
    assert resp.status_code == 200, resp.text
    assert stored == [("simplefin", "x", _PUBLIC_ACCESS_URL)]
    assert net.dialled == []  # storing never makes a request


# ---------------------------------------------------------------------------
# A1 — use time: every fetcher of the stored access URL
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://u:p@169.254.169.254/latest",
        "https://u:p@10.0.0.5/simplefin",
        "https://u:p@metadata.example.com/simplefin",
        "http://u:p@bridge.example.com/simplefin",
    ],
)
async def test_use_time_fetchers_refuse_internal(net, url):
    with pytest.raises(HTTPException) as e1:
        await transactions._fetch_simplefin_window(url)
    assert e1.value.status_code == 502
    with pytest.raises(HTTPException) as e2:
        await cash_position._fetch_simplefin_accounts(url)
    assert e2.value.status_code == 502
    assert await account_mappings._discover_simplefin_accounts(url) == []
    assert net.dialled == []


async def test_use_time_fetchers_accept_public(net):
    payload = await transactions._fetch_simplefin_window(_PUBLIC_ACCESS_URL)
    assert payload["accounts"][0]["id"] == "ACT-1"
    payload = await cash_position._fetch_simplefin_accounts(_PUBLIC_ACCESS_URL)
    assert payload["accounts"][0]["id"] == "ACT-1"
    accounts = await account_mappings._discover_simplefin_accounts(_PUBLIC_ACCESS_URL)
    assert [a["external_account_id"] for a in accounts] == ["ACT-1"]
    assert net.dialled == [("93.184.215.14", 443)] * 3



# ---------------------------------------------------------------------------
# A3 — SimpleFIN claim: the claim URL decoded from the setup token
# ---------------------------------------------------------------------------


def _token(url: str) -> str:
    return base64.b64encode(url.encode()).decode()


@pytest.mark.parametrize(
    "claim_url",
    [
        "https://169.254.169.254/latest/meta-data/iam",
        "https://127.0.0.1:8000/api/admin",
        "https://[::1]/claim",
        "https://intranet.example.com/claim/abc",
        "https://cgnat.example.com/claim/abc",
        "https://mixed.example.com/claim/abc",
    ],
)
def test_claim_refuses_internal_claim_url(net, stored, claim_url):
    resp = _creds_client().post(
        f"/api/workspaces/{_WID}/credentials/simplefin/claim",
        json={"setup_token": _token(claim_url)},
    )
    assert resp.status_code == 400, resp.text
    assert "refused" in resp.json()["detail"]
    assert net.dialled == []
    assert stored == []


def test_claim_refuses_internal_access_url_in_response(net, stored):
    """A public claim host that hands back an internal access URL is refused."""
    net.body = b"https://u:p@169.254.169.254/latest"
    resp = _creds_client().post(
        f"/api/workspaces/{_WID}/credentials/simplefin/claim",
        json={"setup_token": _token("https://bridge.example.com/simplefin/claim/abc")},
    )
    assert resp.status_code == 502, resp.text
    assert stored == []
    assert net.dialled == [("93.184.215.14", 443)]  # the claim POST only


def test_claim_accepts_public_simplefin_shaped_url(net, stored):
    net.body = _PUBLIC_ACCESS_URL.encode()
    resp = _creds_client().post(
        f"/api/workspaces/{_WID}/credentials/simplefin/claim",
        json={"setup_token": _token("https://bridge.example.com/simplefin/claim/abc")},
    )
    assert resp.status_code == 200, resp.text
    assert stored == [("simplefin", "SimpleFIN", _PUBLIC_ACCESS_URL)]
    # Claim POST + verify GET, both to the checked public IP.
    assert net.dialled == [("93.184.215.14", 443), ("93.184.215.14", 443)]
