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

The second half of the file is the TENANT twin of M2: audit_span_middleware
used to read request.path_params at span start — but the middleware runs
BEFORE routing, where path_params is always {}, so the only live source of
workspace_id / entity_id was the spoofable x-workspace-id / x-entity-id header
fallback. The fix seeds "unknown", re-stamps from the real matched-route
params after call_next (the router mutates the request scope in place), and
drops the header fallback entirely.
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


def test_authenticate_token_stamps_actor():
    """The token-validation core must stamp the VALIDATED actor onto the span.

    Stamping lives in authenticate_token (the transport-agnostic core shared by
    the HTTP dependency validate_bearer_token AND the /mcp auth middleware), so
    both surfaces attribute the audit trail to the verified identity — not to
    spoofable headers (finding M2). validate_bearer_token must delegate to it."""
    import app.auth as auth

    core_src = inspect.getsource(auth.authenticate_token)
    assert "_stamp_audit_actor" in core_src, (
        "authenticate_token no longer stamps the validated actor onto the span"
    )
    wrapper_src = inspect.getsource(auth.validate_bearer_token)
    assert "authenticate_token" in wrapper_src, (
        "validate_bearer_token must delegate to the shared authenticate_token core"
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

    # set_tracer_provider is a process-global singleton: once ANY earlier test
    # (or import) installs a provider, later set calls are ignored with a
    # warning. So we don't depend on our set winning — we attach the capture
    # exporter to whichever provider is actually global. This keeps the test
    # robust to collection/run ordering (e.g. test_app_imports importing the app
    # first) instead of silently capturing zero spans.
    try:
        trace.set_tracer_provider(TracerProvider())
    except Exception:  # pragma: no cover - provider already set in this process
        pass
    active_provider = trace.get_tracer_provider()
    if not hasattr(active_provider, "add_span_processor"):  # pragma: no cover
        pytest.skip("global tracer provider is not an SDK TracerProvider")
    active_provider.add_span_processor(SimpleSpanProcessor(_Capture()))

    # Register an in-process service token so /api/me validates without a DB.
    # Audit 2026-05-31 Bug 7: declare role_floor so the loader admits it in
    # production mode (the unscoped legacy shape is now rejected at load time).
    # role_floor=viewer matches /api/me's read-only nature.
    token = "x" * 40
    monkeypatch.setenv(
        "ACCTOPS_SERVICE_TOKENS",
        json.dumps({
            token: {
                "uc_uid": "audit-real",
                "agent_id": "audit-agent",
                "role_floor": "viewer",
            }
        }),
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

    active_provider.force_flush()
    spans = [s for s in captured if s.name == "http.request"]
    assert spans, "audit middleware did not emit an http.request span"
    attrs = dict(spans[-1].attributes or {})
    assert attrs.get("user_uc_uid") == "audit-real", attrs
    assert attrs.get("user_uc_uid") != "ATTACKER-SPOOF", "spoofed uc_uid leaked!"
    assert attrs.get("agent_id") != "evil-agent", "spoofed agent_id leaked!"


# ---------------------------------------------------------------------------
# Tenant attribution (workspace_id / entity_id) — the M2 twin.
# ---------------------------------------------------------------------------


def _attach_span_capture():
    """Attach a capturing exporter to the process-global tracer provider.

    Same singleton dance as the actor E2E test above: set_tracer_provider only
    honours the FIRST call in a process, so we attach to whichever SDK provider
    is actually global instead of assuming our set won. Returns
    (captured_spans, provider).
    """
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

    try:
        trace.set_tracer_provider(TracerProvider())
    except Exception:  # pragma: no cover - provider already set in this process
        pass
    provider = trace.get_tracer_provider()
    if not hasattr(provider, "add_span_processor"):  # pragma: no cover
        pytest.skip("global tracer provider is not an SDK TracerProvider")
    provider.add_span_processor(SimpleSpanProcessor(_Capture()))
    return captured, provider


def _last_http_request_span_attrs(captured, provider) -> dict:
    provider.force_flush()
    spans = [s for s in captured if s.name == "http.request"]
    assert spans, "audit middleware did not emit an http.request span"
    return dict(spans[-1].attributes or {})


def _tiny_app():
    """Throwaway FastAPI app with ONLY the audit middleware — no auth, no DB.

    The middleware is registered exactly as app/main.py does it
    (app.middleware("http") → BaseHTTPMiddleware), so the routing/scope
    semantics under test match the real app.
    """
    from fastapi import FastAPI

    tiny = FastAPI()
    tiny.middleware("http")(otel.audit_span_middleware)

    @tiny.get("/api/workspaces/{workspace_id}/entities/{entity_id}/ping")
    async def _ping(workspace_id: str, entity_id: str):
        return {"ok": True}

    @tiny.get("/api/workspaces/{workspace_id}/boom")
    async def _boom(workspace_id: str):
        raise RuntimeError("endpoint blew up")

    return tiny


def test_middleware_does_not_trust_tenant_headers():
    """Source guard, tenant twin of the actor guard above: the middleware must
    not read the spoofable x-workspace-id / x-entity-id headers at all —
    tenant attribution comes from the matched route's path params."""
    src = inspect.getsource(otel.audit_span_middleware)
    assert "x-workspace-id" not in src, "middleware still reads spoofable x-workspace-id header"
    assert "x-entity-id" not in src, "middleware still reads spoofable x-entity-id header"


def test_routed_request_stamps_real_path_params_post_call():
    """A routed request must stamp the REAL workspace_id / entity_id from the
    matched route's path params — which only exist AFTER call_next, since the
    middleware itself runs pre-routing where path_params is always {}. Spoof
    headers riding along must not influence either attribute."""
    pytest.importorskip("opentelemetry")
    from starlette.testclient import TestClient

    captured, provider = _attach_span_capture()
    client = TestClient(_tiny_app())
    resp = client.get(
        "/api/workspaces/ws-real/entities/ent-real/ping",
        headers={"x-workspace-id": "SPOOF-WS", "x-entity-id": "SPOOF-ENT"},
    )
    assert resp.status_code == 200

    attrs = _last_http_request_span_attrs(captured, provider)
    assert attrs.get("workspace_id") == "ws-real", attrs
    assert attrs.get("entity_id") == "ent-real", attrs
    assert attrs.get("outcome") == "200"


def test_unrouted_request_keeps_unknown_never_the_header():
    """A request that never matches a route (404) keeps the neutral "unknown"
    placeholder even when spoof headers are sent: pre-routing emptiness must
    fall back to a marker, never to a client-controlled header value."""
    pytest.importorskip("opentelemetry")
    from starlette.testclient import TestClient

    captured, provider = _attach_span_capture()
    client = TestClient(_tiny_app())
    resp = client.get(
        "/no/such/route",
        headers={"x-workspace-id": "SPOOF-WS", "x-entity-id": "SPOOF-ENT"},
    )
    assert resp.status_code == 404

    attrs = _last_http_request_span_attrs(captured, provider)
    assert attrs.get("workspace_id") == "unknown", attrs
    assert attrs.get("entity_id") == "unknown", attrs


def test_endpoint_exception_still_stamps_tenant_ids():
    """The re-stamp lives in `finally`, so a request whose endpoint raises
    still gets tenant attribution (routing had already matched)."""
    pytest.importorskip("opentelemetry")
    from starlette.testclient import TestClient

    captured, provider = _attach_span_capture()
    client = TestClient(_tiny_app(), raise_server_exceptions=False)
    resp = client.get("/api/workspaces/ws-err/boom")
    assert resp.status_code == 500

    attrs = _last_http_request_span_attrs(captured, provider)
    assert attrs.get("workspace_id") == "ws-err", attrs
    assert attrs.get("outcome") == "error"


def test_spoofed_tenant_headers_do_not_reach_the_audit_span():
    """End-to-end through app.main's real app (real middleware ordering): a
    workspace-scoped request carrying forged x-workspace-id / x-entity-id
    headers must attribute the audit span to the URL's workspace id — never
    the header. The request is deliberately unauthenticated (missing bearer →
    immediate 401, no DB touched), but the route has already MATCHED by then —
    which is the point: tenant stamping is post-routing and survives
    dependency failures.

    Skips where the OTel SDK isn't installed; runs in the app image / CI.
    """
    import asyncio

    pytest.importorskip("opentelemetry")
    captured, provider = _attach_span_capture()

    import httpx
    from httpx import ASGITransport

    from app.main import app

    wid = "1e6d9f6e-0000-4000-8000-000000000abc"

    async def _drive():
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            return await c.get(
                f"/api/workspaces/{wid}/approvals/clusters",
                headers={"x-workspace-id": "SPOOF-WS", "x-entity-id": "SPOOF-ENT"},
            )

    resp = asyncio.run(_drive())
    assert resp.status_code == 401, resp.text  # unauthenticated — but ROUTED

    attrs = _last_http_request_span_attrs(captured, provider)
    assert attrs.get("workspace_id") == wid, attrs
    assert attrs.get("workspace_id") != "SPOOF-WS", "spoofed workspace_id leaked!"
    # The clusters route has no entity path param → neutral placeholder wins,
    # never the client header.
    assert attrs.get("entity_id") == "unknown", attrs
