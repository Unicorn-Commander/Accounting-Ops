"""AO-Kit F86/F88: cpa-export failures must not echo the upstream body to the
client.

The cpa-export client embedded the sidecar's response body in the exception
message (``response.text[:300]``), and the API returned ``str(exc)`` as the
HTTP detail. The sidecar body is internal (and can echo the assertion header
we just sent), so it must never reach the client: log it truncated, return a
fixed detail that carries only the status + the caller's own identifiers.

Offline: the httpx layer is faked to return an over-cap / marked body; the
test asserts the raised message and the mapped HTTP detail contain no part of
it. No network.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

import app.api.tools as api
import app.tools._cpa_export_client as client


class _FakeResponse:
    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.text = body
        self._json: dict[str, Any] = {}

    def json(self) -> dict[str, Any]:
        return self._json


class _FakeClient:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None

    async def get(self, url: str, headers: dict[str, Any]) -> _FakeResponse:
        return self._response


def _patch_transport(monkeypatch, status_code: int, body: str) -> None:
    monkeypatch.setattr(
        client.httpx, "AsyncClient", lambda **kw: _FakeClient(_FakeResponse(status_code, body))
    )
    # fetch_return_package authorizes + mints an assertion first; skip that
    # (the leak is in the transport handling, not the auth handshake). Allow
    # the bearer-only path so a missing assertion context is not fatal.
    monkeypatch.setattr(client, "authorize_workspace", _fake_auth)
    monkeypatch.setattr(client, "current_auth", lambda: None)
    monkeypatch.setattr(client, "resolve_cpa_export_bearer", lambda: "")
    monkeypatch.setenv(client._CPA_EXPORT_ALLOW_BEARER_ONLY_ENV, "1")


async def _fake_auth(*a: Any, **kw: Any) -> Any:
    class _Ctx:
        workspace_id = uuid4()
        role = "viewer"

    return _Ctx()


MARKER = "INTERNAL-SECRET-BODY-DO-NOT-LEAK"


async def test_400_body_not_in_message(monkeypatch) -> None:
    wid, eid = uuid4(), uuid4()
    _patch_transport(monkeypatch, 400, f"validation failed: {MARKER}")

    with pytest.raises(ValueError) as exc:
        await client.fetch_return_package(
            form_path="1120s",
            form_label="1120-S",
            workspace_id=wid,
            entity_id=eid,
            tax_year=2025,
        )
    msg = str(exc.value)
    assert MARKER not in msg
    assert "1120-S" in msg  # the caller's own identifiers remain


async def test_non_200_body_not_in_message(monkeypatch) -> None:
    wid, eid = uuid4(), uuid4()
    _patch_transport(monkeypatch, 500, f"boom: {MARKER}")

    with pytest.raises(RuntimeError) as exc:
        await client.fetch_return_package(
            form_path="1120s",
            form_label="1120-S",
            workspace_id=wid,
            entity_id=eid,
            tax_year=2025,
        )
    msg = str(exc.value)
    assert MARKER not in msg
    assert "500" in msg


def test_api_maps_unknown_failure_to_generic_detail() -> None:
    """An unexpected (non-client-class) failure must NOT echo its message."""
    # A client-class failure: the fixed message is safe to return as-is.
    err = api._cpa_export_http_error(
        ValueError("cpa-export rejected the 1120-S request (entity e, year 2025).")
    )
    assert err.status_code == 400
    assert "cpa-export rejected" in err.detail

    # An unexpected failure (e.g. a leaked traceback text): generic detail.
    err = api._cpa_export_http_error(RuntimeError(f"traceback {MARKER} /etc/passwd"))
    assert err.status_code == 502
    assert MARKER not in err.detail
    assert "/etc/passwd" not in err.detail
    assert "cpa-export request failed" in err.detail
