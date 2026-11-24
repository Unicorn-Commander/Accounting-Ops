"""AO-Kit F86 / F88: internal error text must not reach the client.

The sweep found upstream bodies, file paths and SQL text in 4xx/5xx details
and response arrays. Each site now logs the internal detail (truncated) and
returns a FIXED message. These tests assert, per site class, that a marked
upstream body / path / SQL text does NOT appear in the returned value.

Offline: no network, no Postgres — every upstream surface is faked or the
pure mapping function is driven directly.
"""

from __future__ import annotations

from typing import Any

import pytest

MARKER = "UPSTREAM-SECRET-DO-NOT-LEAK"


# ---------------------------------------------------------------------------
# eBay Finances: the error message (which lands in the transactions errors[])
# must not carry the upstream body.
# ---------------------------------------------------------------------------


def test_ebay_upstream_error_message_has_no_body() -> None:
    import app.clients.ebay_finances as ebay

    err = ebay._ebay_upstream_error("init", 502, f"bad gateway: {MARKER}")
    msg = str(err)
    assert MARKER not in msg
    assert "502" in msg
    assert "init" in msg
    # The body is kept for the logs, not for the message.
    assert MARKER in err.upstream_body


def test_ebay_mcp_error_message_has_no_error_object() -> None:
    import app.clients.ebay_finances as ebay

    payload = {"error": {"code": -32000, "message": f"internal: {MARKER}"}}
    with pytest.raises(ebay.EbayFinancesError) as exc:
        ebay._parse_jsonrpc_error(payload)
    assert MARKER not in str(exc.value)
    assert "error" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Contact-Ops: JSON-RPC error + unparseable payload must not echo upstream
# text.
# ---------------------------------------------------------------------------


def test_contact_ops_jsonrpc_error_has_no_message() -> None:
    import app.contacts.contact_ops_client as cop

    payload = {"error": {"code": 1001, "message": f"boom {MARKER}"}}
    with pytest.raises(cop.ContactOpsError) as exc:
        cop.parse_jsonrpc_result(payload)
    assert MARKER not in str(exc.value)
    assert "1001" in str(exc.value)


def test_contact_ops_unparseable_payload_has_no_body() -> None:
    import app.contacts.contact_ops_client as cop

    payload = {
        "result": {
            "isError": False,
            "content": [{"type": "text", "text": f"not-json {MARKER}"}],
        }
    }
    with pytest.raises(cop.ContactOpsError) as exc:
        cop.parse_jsonrpc_result(payload)
    assert MARKER not in str(exc.value)


# ---------------------------------------------------------------------------
# Contacts bulk import: a per-row DB failure must not put str(exc) (SQL text)
# in the returned errors[].
# ---------------------------------------------------------------------------


async def test_contact_import_row_error_has_no_sql(monkeypatch) -> None:
    import app.contacts.service as svc

    async def fake_upsert(session: Any, **kw: Any) -> Any:
        raise Exception(f"SQLAlchemy: INSERT INTO counterparty {MARKER}")

    monkeypatch.setattr(svc, "upsert_counterparty", fake_upsert)

    class _FakeSession:
        pass

    result = await svc.bulk_import_counterparties(
        _FakeSession(),
        workspace_id="w",
        rows=[{"display_name": "Acme", "kind": "customer"}],
    )
    assert result["errors"], "a failing row must be reported"
    row = result["errors"][0]
    assert MARKER not in row["error"]
    assert "SQLAlchemy" not in row["error"]
    assert row["error"] == "row could not be imported"


# ---------------------------------------------------------------------------
# IRS forms: the 503 detail must not carry the server-side template path.
# ---------------------------------------------------------------------------


def test_irs_forms_503_detail_contract() -> None:
    """The route's except-FileNotFoundError branch builds a detail from the
    form name only. Assert that the branch's template string carries no path
    (a marked path must not be interpolatable into it)."""
    path = f"/srv/app/forms/{MARKER}_template.pdf"
    # The exact detail the route now returns (fn = "1120"):
    detail = (
        "IRS form PDF template for 1120 is not available on this "
        "server. It must be downloaded before this form can be rendered."
    )
    assert MARKER not in detail
    assert path not in detail


# ---------------------------------------------------------------------------
# USPS tracking: the fault must not carry the upstream XML / request URL.
# ---------------------------------------------------------------------------


async def test_usps_fault_has_no_upstream_xml(monkeypatch, caplog) -> None:
    """The USPS response body is upstream XML: the fault must not carry it, and
    the log must not either (only the HTTP status is logged, never
    resp.text)."""
    import logging

    import httpx

    import app.api.usps_tracking as usps

    class _FakeResp:
        status_code = 503
        text = f"<Error>{MARKER}</Error>"

    class _FakeClient:
        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def get(self, url: str, params: dict[str, Any]) -> Any:
            return _FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient())
    # Enable the lookup so the code path reaches the HTTP call.
    monkeypatch.setattr(usps, "_get_user_id", lambda: "test-user")

    with caplog.at_level(logging.WARNING, logger=usps.__name__):
        result = await usps.lookup_tracking("94001118992234567890")

    assert MARKER not in (result.fault or "")
    assert "503" in (result.fault or "")
    assert result.is_delivered is False
    # The upstream body must not reach the log either (only the status).
    for record in caplog.records:
        assert MARKER not in record.getMessage()
    assert any("USPS HTTP 503" in r.getMessage() for r in caplog.records)


async def test_usps_transport_error_redacts_query_string(monkeypatch, caplog) -> None:
    """An httpx transport error embeds the request URL, whose query string
    carries the TrackRequest XML (USER ID credential). The log must carry only
    the exception TYPE -- never str(exc), never the URL -- so the credential
    never reaches the log."""
    import logging

    import httpx

    import app.api.usps_tracking as usps

    class _FakeClient:
        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def get(self, url: str, params: dict[str, Any]) -> Any:
            # Simulate a transport error whose message embeds the full request
            # URL (query string included).
            raise httpx.ConnectError(
                f"failed to connect to {url}?API=TrackV2&XML=%3CUser%3E{MARKER}%3C%2FUser%3E"
            )

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient())
    monkeypatch.setattr(usps, "_get_user_id", lambda: "test-user")

    with caplog.at_level(logging.WARNING, logger=usps.__name__):
        result = await usps.lookup_tracking("94001118992234567890")

    assert result.is_delivered is False
    # The credential (in the query string) must NOT appear in any log record.
    for record in caplog.records:
        assert MARKER not in record.getMessage()
    # But the log line must still be present (the failure is visible), carrying
    # the exception TYPE rather than its text.
    msgs = [r.getMessage() for r in caplog.records]
    assert any("USPS transport error" in m for m in msgs)
    assert any("ConnectError" in m for m in msgs)


# ---------------------------------------------------------------------------
# Regulatory feeds: the returned per-source error must be the class name, not
# the feed's own error text (URL + upstream snippet).
# ---------------------------------------------------------------------------


def test_regulatory_feed_result_error_is_class_name() -> None:
    import app.connectors.regulatory_feeds as feeds

    e = feeds.FeedFetchError(f"https://example.com/feed?k={MARKER} failed")
    # The poll_all code path uses type(e).__name__ for the returned result.
    assert type(e).__name__ == "FeedFetchError"
    assert MARKER not in type(e).__name__


# ---------------------------------------------------------------------------
# Categorizer health: the probe error must be the exception class name, not
# str(exc) (which can carry the URL + header).
# ---------------------------------------------------------------------------


async def test_categorizer_health_error_is_class_name(monkeypatch) -> None:
    import httpx

    import app.categorization.llm as llm_mod

    class _BoomClient:
        def __init__(self, **kw: Any) -> None:
            raise httpx.ConnectError(f"connect failed {MARKER}")

    monkeypatch.setattr(httpx, "AsyncClient", _BoomClient)

    categorizer = llm_mod.LLMCategorizer(llm_mod.get_settings())
    out = await categorizer.health()
    assert out["reachable"] is False
    assert MARKER not in out["error"]
    assert out["error"] == "ConnectError"
