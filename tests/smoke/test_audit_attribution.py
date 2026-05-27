"""Regression tests for audit finding M2 — spoofable audit attribution.

Before the fix, app/otel.audit_span_middleware set the span attributes
`user_uc_uid` and `agent_id` straight from the client-controlled request
headers `x-uc-uid` / `x-agent-id`. Any caller could thus forge the audit trail
and frame another user. The fix:

  * the middleware no longer reads those headers — it seeds "unauthenticated";
  * app.auth.validate_bearer_token stamps the VALIDATED identity onto the
    current span via app.otel.set_actor_attributes.

These tests are pure: they drive set_actor_attributes against a fake span and
assert the middleware seeds placeholders rather than echoing headers. (OTel is
optional, so set_actor_attributes is a no-op when trace is None — we exercise
the real-span path by installing a fake current span.)
"""
from __future__ import annotations

import inspect

import pytest

import app.otel as otel


class _FakeSpan:
    def __init__(self, recording: bool = True):
        self.attributes: dict[str, object] = {}
        self._recording = recording

    def is_recording(self) -> bool:
        return self._recording

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value


def test_set_actor_attributes_writes_validated_identity(monkeypatch):
    span = _FakeSpan()

    class _FakeTrace:
        @staticmethod
        def get_current_span():
            return span

    # Force the "OTel installed" path with our fake span.
    monkeypatch.setattr(otel, "trace", _FakeTrace)
    otel.set_actor_attributes("real-user", "receipt-reconciler")

    assert span.attributes["user_uc_uid"] == "real-user"
    assert span.attributes["agent_id"] == "receipt-reconciler"


def test_set_actor_attributes_none_agent_becomes_none_string(monkeypatch):
    span = _FakeSpan()

    class _FakeTrace:
        @staticmethod
        def get_current_span():
            return span

    monkeypatch.setattr(otel, "trace", _FakeTrace)
    otel.set_actor_attributes("real-user", None)
    assert span.attributes["agent_id"] == "none"


def test_set_actor_attributes_noop_when_otel_absent(monkeypatch):
    # When the SDK isn't installed (trace is None) it must be a safe no-op.
    monkeypatch.setattr(otel, "trace", None)
    otel.set_actor_attributes("real-user", "agent")  # must not raise


def test_middleware_does_not_trust_actor_headers():
    """The middleware source must not read x-uc-uid / x-agent-id headers.

    Those are exactly the spoofable inputs finding M2 was about; attribution
    now comes only from validate_bearer_token -> set_actor_attributes.
    """
    src = inspect.getsource(otel.audit_span_middleware)
    assert "x-uc-uid" not in src, "middleware still reads spoofable x-uc-uid header"
    assert "x-agent-id" not in src, "middleware still reads spoofable x-agent-id header"
    # And it seeds an unauthenticated placeholder instead.
    assert "unauthenticated" in src


def test_validate_bearer_token_stamps_actor():
    """auth.validate_bearer_token must call the audit-stamp helper."""
    import app.auth as auth

    src = inspect.getsource(auth.validate_bearer_token)
    assert "_stamp_audit_actor" in src, (
        "validate_bearer_token no longer stamps the validated actor onto the span"
    )
    helper_src = inspect.getsource(auth._stamp_audit_actor)
    assert "set_actor_attributes" in helper_src


def test_spoofed_actor_headers_do_not_reach_the_audit_span(monkeypatch):
    """End-to-end (in-process ASGI): a request with forged x-uc-uid / x-agent-id
    headers must produce an audit span attributed to the VALIDATED service-token
    identity — not the spoofed header values (audit finding M2).

    Skips where the OTel SDK isn't installed (e.g. a bare host running
    `make test`); runs wherever OTel is present (the app image / CI).
    """
    import asyncio
    import json

    pytest.importorskip("opentelemetry")
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        SimpleSpanProcessor,
        SpanExporter,
        SpanExportResult,
    )

    captured: list = []

    class _Capture(SpanExporter):
        def export(self, spans):
            captured.extend(spans)
            return SpanExportResult.SUCCESS

        def shutdown(self):  # pragma: no cover - nothing to clean up
            pass

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(_Capture()))
    # Install BEFORE importing the app so FastAPIInstrumentor binds to it.
    try:
        trace.set_tracer_provider(provider)
    except Exception:  # pragma: no cover - provider already set in this process
        pass

    # Register an in-process service token so /api/me validates without a DB.
    token = "x" * 40
    monkeypatch.setenv(
        "ACCTOPS_SERVICE_TOKENS",
        json.dumps({token: {"uc_uid": "audit-real", "agent_id": "audit-agent"}}),
    )
    import app.auth as auth

    auth._service_tokens_cache = None  # force re-parse of the env we just set

    import httpx
    from httpx import ASGITransport

    from app.main import app

    async def _drive():
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            return await c.get(
                "/api/me",
                headers={
                    "Authorization": f"Bearer {token}",
                    "x-uc-uid": "ATTACKER-SPOOF",
                    "x-agent-id": "evil-agent",
                },
            )

    resp = asyncio.run(_drive())
    assert resp.status_code == 200, resp.text
    assert resp.json()["uc_uid"] == "audit-real"

    provider.force_flush()
    spans = [s for s in captured if s.name == "http.request"]
    assert spans, "audit middleware did not emit an http.request span"
    attrs = dict(spans[-1].attributes or {})
    assert attrs.get("user_uc_uid") == "audit-real", attrs
    assert attrs.get("user_uc_uid") != "ATTACKER-SPOOF", "spoofed uc_uid leaked!"
    assert attrs.get("agent_id") != "evil-agent", "spoofed agent_id leaked!"
