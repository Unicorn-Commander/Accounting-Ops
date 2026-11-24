import time
from collections.abc import Callable

from fastapi import Request
try:
    from opentelemetry import propagate, trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
except ImportError:  # pragma: no cover - permits local import checks before dependency install
    propagate = None
    trace = None
    OTLPSpanExporter = None
    FastAPIInstrumentor = None
    Resource = None
    TracerProvider = None
    BatchSpanProcessor = None

from opentelemetry.trace import Span
from opentelemetry.trace.status import Status

from app.config import get_settings


import re

# A Python type name: a dotted path whose final segment is a class name
# (e.g. `ValueError`, `sqlalchemy.exc.OperationalError`).
_TYPE_NAME = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*\.[A-Z][A-Za-z0-9_]*$"
    r"|[A-Z][A-Za-z0-9_]*$"
)


def _scrub_exception_text(value: object) -> str:
    """Collapse an OTel status description / exception attribute to the TYPE only.

    A recorded exception's description and `exception.message` carry the raw
    exception text (SQL with amounts and names, connector payloads). Telemetry
    keeps the type for triage; the message is dropped.

    `exception.message` attributes are either '<Type>: <message>' (the SDK
    prepends the type) or the bare message; a status description is usually the
    bare message. When the leading token is a Python type name we keep it,
    otherwise the whole value is treated as message text and replaced.
    """
    text = str(value).strip()
    if not text:
        return "Exception"
    first = text.split(": ", 1)[0].strip()
    return first if _TYPE_NAME.match(first) else "Exception"


def _scrub_span_exception_text(span: Span) -> None:
    """Scrub exception text out of a span's status + `exception` events.

    The SDK's `Span.record_exception` and FastAPI's exception middleware put
    the raw exception text in the span status description and in the
    `exception.message` event attribute (SQL with amounts and names, connector
    payloads). Both are replaced with the exception TYPE only. Events already
    carry `exception.type`, so the type survives; only the message text is
    dropped.
    """
    try:
        # ReadableSpan exposes `status` as a read-only property; mutate the
        # backing field directly (the exporter serializes whatever is there).
        if span.status is not None and span.status.description:
            object.__setattr__(
                span,
                "_status",
                Status(
                    status_code=span.status.status_code,
                    description=_scrub_exception_text(span.status.description),
                ),
            )
        for event in span.events:
            if event.name != "exception":
                continue
            scrubbed = dict(event.attributes or {})
            for key in ("exception.message", "exception.type"):
                if key in scrubbed:
                    scrubbed[key] = _scrub_exception_text(scrubbed[key])
            # The traceback frames repeat the raw message (SQL, amounts,
            # names). Drop it entirely: the type is already kept.
            scrubbed.pop("exception.stacktrace", None)
            # `Event.attributes` is a read-only property over `_attributes`;
            # the OTLP encoder calls `.items()` on it, so store a mapping.
            event._attributes = dict(scrubbed)
    except Exception:  # noqa: BLE001 - scrubbing must never break a request
        pass


class _RedactingSpanExporter:
    """Wraps a SpanExporter: scrubs each span's exception text on export.

    Export runs in the BatchSpanProcessor worker thread, after `on_end` has
    queued the span, so the scrubbed values are what gets serialized to the
    OTLP endpoint. Delegates everything else (result codes, shutdown, flush).
    """

    def __init__(self, exporter) -> None:
        self._exporter = exporter

    def export(self, spans) -> object:
        for span in spans:
            _scrub_span_exception_text(span)
        return self._exporter.export(spans)

    def shutdown(self) -> None:
        self._exporter.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._exporter.force_flush(timeout_millis)


def setup_otel(app) -> None:
    if trace is None:
        return
    settings = get_settings()
    provider = TracerProvider(resource=Resource.create({"service.name": settings.otel_service_name}))
    if settings.otel_exporter_otlp_endpoint:
        processor = BatchSpanProcessor(
            _RedactingSpanExporter(
                OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint)
            )
        )
        provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)
    propagate.set_global_textmap(propagate.get_global_textmap())
    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)


def set_actor_attributes(uc_uid: str, agent_id: str | None) -> None:
    """Stamp the audit span with the VALIDATED caller identity.

    Audit attribution must come from the verified AuthContext, never from
    client-supplied headers (audit finding M2): a caller could otherwise set
    x-uc-uid / x-agent-id to any value and frame another user in the trail.
    Called from app.auth.validate_bearer_token once the bearer token (JWT or
    service token) has been verified; it overwrites the placeholder values the
    middleware seeded. No-op if OTel isn't installed or there's no live span.
    """
    if trace is None:
        return
    span = trace.get_current_span()
    if span is None or not getattr(span, "is_recording", lambda: False)():
        return
    span.set_attribute("user_uc_uid", uc_uid)
    span.set_attribute("agent_id", agent_id or "none")


async def audit_span_middleware(request: Request, call_next: Callable):
    if trace is None:
        return await call_next(request)
    tracer = trace.get_tracer("accounting_ops.audit")
    started = time.perf_counter()
    with tracer.start_as_current_span("http.request") as span:
        # This middleware runs BEFORE routing (the same fact app/rate_limit.py
        # documents for its own pre-routing parse): request.path_params is
        # ALWAYS {} here, so tenant ids can only be seeded as placeholders. The
        # authoritative values are stamped in `finally` below, after the router
        # has populated this request's scope. Tenant identity is intentionally
        # NOT read from request headers — the old tenant-header fallback was an
        # unvalidated client-controlled value (the finding-M2 spoof class), and
        # no client in the repo ever sent it.
        span.set_attribute("workspace_id", "unknown")
        span.set_attribute("entity_id", "unknown")
        # Actor identity is intentionally NOT read from request headers — it is
        # set from the validated AuthContext by set_actor_attributes() (called
        # in validate_bearer_token). Seed placeholders so the attribute always
        # exists even on requests that never authenticate (e.g. /healthz, 401s).
        span.set_attribute("agent_id", "unauthenticated")
        span.set_attribute("user_uc_uid", "unauthenticated")
        span.set_attribute("tool_name", request.headers.get("x-tool-name", "none"))
        try:
            response = await call_next(request)
            span.set_attribute("outcome", str(response.status_code))
            return response
        except Exception:
            span.set_attribute("outcome", "error")
            raise
        finally:
            # Routing has run by now, even when the endpoint raised: the router
            # mutates this request's scope dict in place, so the matched
            # route's params are visible here. Requests that never matched a
            # route (404) keep the "unknown" placeholder.
            workspace_id = request.path_params.get("workspace_id")
            if workspace_id:
                span.set_attribute("workspace_id", str(workspace_id))
            entity_id = request.path_params.get("entity_id")
            if entity_id:
                span.set_attribute("entity_id", str(entity_id))
            span.set_attribute("latency_ms", round((time.perf_counter() - started) * 1000, 2))
