"""AO111-R2-F13: upstream error bodies stay out of logs and responses (no network).

Three sites echoed a raw upstream body:
  * contact_ops_client.exchange_token logged 300 chars of the token endpoint's
    reply (the request carried client_secret + subject_token);
  * uc_api_key.meter_uc_mcp_call logged 200 chars of the gateway's reply (the
    request carried the caller's uc_ bearer);
  * usps_tracking.lookup_tracking returned 120 chars of the USPS reply in
    TrackingResult.fault (served by GET /usps/track), and str(exc) on an
    httpx error, whose message can carry the request URL with USERID.

Each test answers through httpx.MockTransport with a body full of sentinels
and asserts only the status and a length leave the function.
"""
from __future__ import annotations

import logging

import httpx
import pytest

from app.api import usps_tracking
from app.contacts import contact_ops_client
from app.security import uc_api_key

SENTINEL = "FIXTURE-UPSTREAM-SECRET-ECHO"
BODY = f'{{"error":"invalid_client","echo":"{SENTINEL}","client_secret":"{SENTINEL}"}}'


def _patch_client(monkeypatch, handler) -> None:
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


async def test_contact_ops_token_exchange_logs_status_and_length_only(monkeypatch, caplog):
    _patch_client(monkeypatch, lambda req: httpx.Response(401, text=BODY))
    monkeypatch.setattr(
        contact_ops_client, "_exchange_client_creds",
        lambda: ("fixture-client", f"secret-{SENTINEL}"),
    )
    caplog.set_level(logging.DEBUG)
    with pytest.raises(contact_ops_client.ContactOpsError) as exc:
        await contact_ops_client.exchange_token(f"subject-{SENTINEL}")
    assert SENTINEL not in str(exc.value)
    assert SENTINEL not in caplog.text
    assert "HTTP 401" in caplog.text and f"{len(BODY)} bytes" in caplog.text


async def test_uc_meter_logs_status_and_length_only(monkeypatch, caplog):
    _patch_client(monkeypatch, lambda req: httpx.Response(502, text=BODY))
    caplog.set_level(logging.DEBUG)
    out = await uc_api_key.meter_uc_mcp_call(token="uc_" + "a1" * 16, tool="list_entities")
    assert out is None
    assert SENTINEL not in caplog.text
    assert "HTTP 502" in caplog.text and f"{len(BODY)} bytes" in caplog.text


async def test_usps_non_200_fault_is_status_and_length_only(monkeypatch):
    monkeypatch.setenv("USPS_WEB_TOOLS_USER_ID", f"USERID-{SENTINEL}")
    _patch_client(monkeypatch, lambda req: httpx.Response(500, text=BODY))
    result = await usps_tracking.lookup_tracking("9400100000000000000000")
    assert SENTINEL not in result.fault and SENTINEL not in result.summary
    assert result.fault == f"HTTP 500 (body {len(BODY)} bytes)"


async def test_usps_transport_error_does_not_echo_the_url(monkeypatch):
    monkeypatch.setenv("USPS_WEB_TOOLS_USER_ID", f"USERID-{SENTINEL}")

    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {req.url}", request=req)

    _patch_client(monkeypatch, boom)
    result = await usps_tracking.lookup_tracking("9400100000000000000000")
    assert SENTINEL not in result.fault and SENTINEL not in result.summary
    assert result.fault == "ConnectError"
