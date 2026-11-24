"""Regression tests for OTel exception-text leakage (batch P3 hardening).

Two items, one file:

AO-OTEL-2 (latent P2): a span that records a raised exception ships the raw
exception text (SQL with amounts and names, connector payloads) in its status
description, its `exception` event's `exception.message` attribute and its
`exception.stacktrace` attribute. app/otel now scrubs all three to the
exception TYPE only at export time (the existing span processor wraps the OTLP
exporter in _RedactingSpanExporter).

AO-OTEL-3 (P3): the LLM failover span attributes (and event) used the raw
primary/fallback base_url, which can embed userinfo or a query token. They now
use LlmTarget.redacted().

The AO-OTEL-2 tests drive the REAL SDK span pipeline (TracerProvider +
BatchSpanProcessor + _RedactingSpanExporter) and inspect the span exactly as
the OTLP encoder would serialize it, so they pin the shipped wire, not an
in-memory convenience. The AO-OTEL-3 test drives _annotate_span against a fake
current span and asserts the redacted identity is what lands in the attributes.
"""
from __future__ import annotations

import pytest

import app.otel as otel


# ===========================================================================
# AO-OTEL-2: exception text must not reach the exported span
# ===========================================================================


class _CaptureExporter:
    """SpanExporter stand-in: keeps the spans exactly as handed to export()."""

    def __init__(self) -> None:
        self.spans: list = []

    def export(self, spans):
        self.spans.extend(spans)
        from opentelemetry.sdk.trace.export import SpanExportResult

        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:  # pragma: no cover - nothing to clean up
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def _span_wire_blob(span) -> str:
    """Serialize a span the same way the OTLP exporter does.

    Encoding through the real encoder is the strongest proof: it is exactly the
    byte string that ships to the OTLP endpoint. We fall back to a repr of the
    readable fields if the encoder module isn't importable (it is a private
    path, so it may move).
    """
    try:
        from opentelemetry.exporter.otlp.proto.common._internal.trace_encoder import (
            encode_spans,
        )

        return encode_spans([span]).SerializeToString().decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover - encoder path moved
        return repr(span)


def test_raised_exception_text_does_not_reach_exported_span():
    """AO-OTEL-2: a span for a raised exception carrying 'SELECT secret_amount'
    must have no such text in its attributes, events or status on the wire."""
    pytest.importorskip("opentelemetry")
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.trace.status import Status, StatusCode

    capture = _CaptureExporter()
    provider = TracerProvider()
    provider.add_span_processor(
        BatchSpanProcessor(otel._RedactingSpanExporter(capture))
    )
    try:
        trace.set_tracer_provider(provider)
    except Exception:  # pragma: no cover - provider already set in this process
        pass
    tracer = provider.get_tracer("acctops.otel2")

    with tracer.start_as_current_span("boom") as span:
        try:
            raise ValueError(
                "SELECT secret_amount FROM ledger WHERE name='Jane Doe'"
            )
        except Exception:
            span.record_exception(__import__("sys").exc_info()[1])
        span.set_status(
            Status(StatusCode.ERROR, "SELECT secret_amount FOR UPDATE")
        )

    provider.force_flush(5000)
    assert capture.spans, "no span was exported"
    wire = _span_wire_blob(capture.spans[-1])
    for marker in ("secret_amount", "Jane Doe"):
        assert marker not in wire, f"raw exception text {marker!r} leaked on the wire"
    # The exception TYPE must survive (it is what operators triage on).
    assert "ValueError" in wire


def test_instrumentation_exception_event_is_scrubbed_to_type():
    """AO-OTEL-2: an instrumentation-style `exception` event (add_event) whose
    message + stacktrace carry SQL/amounts must be scrubbed to the type only."""
    pytest.importorskip("opentelemetry")
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    capture = _CaptureExporter()
    provider = TracerProvider()
    provider.add_span_processor(
        BatchSpanProcessor(otel._RedactingSpanExporter(capture))
    )
    try:
        trace.set_tracer_provider(provider)
    except Exception:  # pragma: no cover
        pass
    tracer = provider.get_tracer("acctops.otel2b")

    with tracer.start_as_current_span("boom2") as span:
        span.add_event(
            "exception",
            {
                "exception.message": "UPDATE ledger SET amount=999 WHERE name='Jane'",
                "exception.type": "sqlalchemy.exc.OperationalError",
                "exception.stacktrace": "Traceback (most recent call last): secret stack",
            },
        )

    provider.force_flush(5000)
    assert capture.spans, "no span was exported"
    wire = _span_wire_blob(capture.spans[-1])
    for marker in ("UPDATE ledger", "amount=999", "secret stack", "Jane"):
        assert marker not in wire, f"raw exception text {marker!r} leaked on the wire"
    assert "sqlalchemy.exc.OperationalError" in wire


def test_scrub_exception_text_collapses_to_type():
    """AO-OTEL-2 unit: the scrubber keeps a leading Python type name and drops
    bare message text."""
    scrub = otel._scrub_exception_text
    assert scrub("ValueError") == "ValueError"
    assert scrub("ValueError: SELECT secret_amount") == "ValueError"
    assert scrub("sqlalchemy.exc.OperationalError: disk full") == (
        "sqlalchemy.exc.OperationalError"
    )
    assert scrub("httpx.ConnectError: dns failure") == "httpx.ConnectError"
    assert scrub("SELECT secret_amount") == "Exception"
    assert scrub("UPDATE ledger SET amount=999") == "Exception"
    assert scrub("500: SELECT secret_amount FOR UPDATE") == "Exception"
    assert scrub("") == "Exception"


# ===========================================================================
# AO-OTEL-3: failover span attributes must use the redacted base_url
# ===========================================================================


class _FakeFailoverSpan:
    """Records set_attribute / add_event so we can assert what lands on the span."""

    def __init__(self) -> None:
        self.attributes: dict = {}
        self.events: list = []

    def is_recording(self) -> bool:
        return True

    def set_attribute(self, key, value) -> None:
        self.attributes[key] = value

    def add_event(self, name, attributes=None) -> None:
        self.events.append((name, dict(attributes or {})))


def test_failover_span_attributes_are_redacted():
    """AO-OTEL-3: a base_url with userinfo or a query token must be redacted in
    the failover span attributes (and event) via the existing redacted() helper."""
    pytest.importorskip("opentelemetry")
    from app.llm_client import LlmTarget, _annotate_span
    from opentelemetry import trace

    span = _FakeFailoverSpan()
    primary = LlmTarget(
        base_url="https://api.magicunicorn.dev/v1",
        api_key="sk-primary-secret",
        model="qwen3.6-35b",
        label="primary",
    )
    fallback = LlmTarget(
        base_url="https://sk-or-v1-abc123secret@openrouter.ai/api/v1?token=leakquery123",
        api_key="sk-fallback-secret",
        model="cloud-model",
        label="fallback",
    )

    # _annotate_span does a local `from opentelemetry import trace`, so it
    # resolves opentelemetry.trace.get_current_span. Patch that for the call.
    real_get = trace.get_current_span
    try:
        trace.get_current_span = lambda: span  # type: ignore[method-assign]
        _annotate_span("http_503", primary, fallback, "chat.completions")
    finally:
        trace.get_current_span = real_get  # type: ignore[method-assign]

    # The failover marker + reason + op are expected.
    assert span.attributes.get("llm.failover") is True
    assert span.attributes.get("llm.failover.reason") == "http_503"
    assert span.attributes.get("llm.failover.op") == "chat.completions"

    # The base_url attributes must be the REDACTED identity (no raw userinfo /
    # query token), and the api_key must never appear.
    for key in ("llm.primary.base_url", "llm.fallback.base_url"):
        val = span.attributes.get(key)
        assert val is not None, f"missing span attribute {key}"
        assert "sk-or-v1-abc123secret" not in val, "raw userinfo leaked in span attr"
        assert "leakquery123" not in val, "raw query token leaked in span attr"
    assert "sk-primary-secret" not in repr(span.attributes), "primary key leaked"
    assert "sk-fallback-secret" not in repr(span.attributes), "fallback key leaked"

    # The event must carry the same redacted identity, not the raw url.
    failover_events = [attrs for name, attrs in span.events if name == "llm.failover"]
    assert failover_events, "no llm.failover event was recorded"
    for attrs in failover_events:
        val = attrs.get("fallback.base_url")
        assert val is not None
        assert "sk-or-v1-abc123secret" not in val, "raw userinfo leaked in event"
        assert "leakquery123" not in val, "raw query token leaked in event"
