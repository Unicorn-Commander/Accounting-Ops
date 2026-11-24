"""AO-SW-1: a Stripe Checkout failure is logged, retried and surfaced."""
from __future__ import annotations

import logging
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest

from app.api import invoices as invoices_api
from app.connectors.stripe import checkout


class _Resp:
    def __init__(self, status: int, body: dict | None = None):
        self.status_code = status
        self._body = body or {}

    def json(self):
        return self._body


def _client_factory(script: list, calls: list):
    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, *, data, headers):
            calls.append(headers["Idempotency-Key"])
            step = script.pop(0)
            if isinstance(step, Exception):
                raise step
            return step

    return _Client


async def _no_sleep(_s):
    return None


def _kwargs():
    return dict(
        amount=Decimal("10.00"), currency="USD", invoice_id="inv-1",
        workspace_id="ws-1", invoice_number="INV-1",
        success_url="https://app.example.com/pay/x", cancel_url="https://app.example.com/pay/x",
    )


@pytest.mark.asyncio
async def test_transient_failure_is_retried_with_the_same_idempotency_key(monkeypatch):
    calls: list = []
    script = [httpx.ConnectError("boom"), _Resp(503), _Resp(200, {"url": "https://checkout.example.com/cs"})]
    monkeypatch.setenv("STRIPE_CHECKOUT_SECRET_KEY", "sk_test_unit")
    monkeypatch.setattr(checkout.httpx, "AsyncClient", _client_factory(script, calls))
    monkeypatch.setattr(checkout.asyncio, "sleep", _no_sleep)
    url = await checkout.create_checkout_session(**_kwargs())
    assert url == "https://checkout.example.com/cs"
    assert calls == ["invoice-checkout-inv-1"] * 3


@pytest.mark.asyncio
async def test_persistent_failure_raises_and_logs(monkeypatch, caplog):
    calls: list = []
    script = [httpx.ReadTimeout("t")] * 3
    monkeypatch.setenv("STRIPE_CHECKOUT_SECRET_KEY", "sk_test_unit")
    monkeypatch.setattr(checkout.httpx, "AsyncClient", _client_factory(script, calls))
    monkeypatch.setattr(checkout.asyncio, "sleep", _no_sleep)
    with caplog.at_level(logging.WARNING, logger=checkout.__name__):
        with pytest.raises(checkout.CheckoutUnavailableError):
            await checkout.create_checkout_session(**_kwargs())
    assert len(calls) == 3
    assert "inv-1" in caplog.text and "sk_test_unit" not in caplog.text


@pytest.mark.asyncio
async def test_non_transient_4xx_is_not_retried(monkeypatch):
    calls: list = []
    monkeypatch.setenv("STRIPE_CHECKOUT_SECRET_KEY", "sk_test_unit")
    monkeypatch.setattr(checkout.httpx, "AsyncClient", _client_factory([_Resp(400)], calls))
    monkeypatch.setattr(checkout.asyncio, "sleep", _no_sleep)
    with pytest.raises(checkout.CheckoutUnavailableError):
        await checkout.create_checkout_session(**_kwargs())
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_unconfigured_checkout_is_still_a_quiet_none(monkeypatch):
    monkeypatch.delenv("STRIPE_CHECKOUT_SECRET_KEY", raising=False)
    assert await checkout.create_checkout_session(**_kwargs()) is None


@pytest.mark.asyncio
async def test_posted_invoice_surfaces_pay_link_unavailable(monkeypatch):
    async def _fail(**_k):
        raise checkout.CheckoutUnavailableError("HTTP 503")

    monkeypatch.setattr(invoices_api, "create_checkout_session", _fail)
    view = {
        "id": str(uuid4()), "status": "sent", "hosted_pay_url": None,
        "balance_due": "10.00",
    }
    out = await invoices_api._attach_pay_link(uuid4(), view)
    assert out["hosted_pay_url"] is None
    assert out["pay_link_status"] == "unavailable"
    assert "retry" in out["pay_link_warning"].lower()


@pytest.mark.asyncio
async def test_failed_link_is_recreated_by_reissue_exactly_once(monkeypatch):
    """AO110-R2-P2-1: Stripe fails on issue, a re-issue of the now posted invoice
    (no raw token in its view) creates the link, a third issue makes no call."""
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    inv_id, ws_id = uuid4(), uuid4()
    db = {"hosted_pay_url": None, "updates": 0}

    def _posted_view():
        # What issue_invoice_draft returns for an already posted invoice: no
        # public_token, so no public_url can be built.
        return {
            "id": str(inv_id), "status": "sent", "entity_id": None,
            "external_source": None, "balance_due": "10.00", "currency": "USD",
            "invoice_number": "INV-7", "hosted_pay_url": db["hosted_pay_url"],
        }

    class _Session:
        async def execute(self, stmt, params):
            assert "hosted_pay_url IS NULL" in str(stmt)
            if db["hosted_pay_url"] is None:
                db["hosted_pay_url"] = params["url"]
                db["updates"] += 1

    @asynccontextmanager
    async def _ws_session(_factory, _wid):
        yield _Session()

    async def _get(_s, **_k):
        return _posted_view()

    async def _issue(_s, **_k):
        return _posted_view()

    async def _noop(*_a, **_k):
        return None

    async def _stage(_v):
        return "skipped"

    calls: list = []
    script = [httpx.ConnectError("down")] * 3 + [
        _Resp(200, {"url": "https://checkout.example.com/cs_1"}),
    ]
    monkeypatch.setenv("STRIPE_CHECKOUT_SECRET_KEY", "sk_test_unit")
    monkeypatch.setattr(checkout.httpx, "AsyncClient", _client_factory(script, calls))
    monkeypatch.setattr(checkout.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(invoices_api, "require_role", lambda *_a: None)
    monkeypatch.setattr(invoices_api, "assert_entity_in_scope", lambda *_a: None)
    monkeypatch.setattr(invoices_api, "_gate_cross_app", _noop)
    monkeypatch.setattr(invoices_api, "get_invoice", _get)
    monkeypatch.setattr(invoices_api, "issue_invoice_draft", _issue)
    monkeypatch.setattr(invoices_api, "workspace_session", _ws_session)
    monkeypatch.setattr(invoices_api, "stage_invoice_email", _stage)
    ws = SimpleNamespace(workspace_id=ws_id, user_uc_uid="u-1")

    first = await invoices_api.issue_invoice(ws_id, inv_id, None, ws)
    assert first["hosted_pay_url"] is None
    assert first["pay_link_status"] == "unavailable"

    second = await invoices_api.issue_invoice(ws_id, inv_id, None, ws)
    assert second["hosted_pay_url"] == "https://checkout.example.com/cs_1"
    assert "pay_link_status" not in second

    third = await invoices_api.issue_invoice(ws_id, inv_id, None, ws)
    assert third["hosted_pay_url"] == "https://checkout.example.com/cs_1"

    assert db["updates"] == 1
    assert len(calls) == 4  # 3 failed attempts + 1 success, none on the third issue
    assert set(calls) == {f"invoice-checkout-{inv_id}"}


def test_checkout_return_url_carries_no_token():
    url = checkout.checkout_return_url("https://app.example.com/", "inv-9")
    assert url == "https://app.example.com/pay/complete/inv-9"
