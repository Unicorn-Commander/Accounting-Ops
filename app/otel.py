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

from app.config import get_settings


def setup_otel(app) -> None:
    if trace is None:
        return
    settings = get_settings()
    provider = TracerProvider(resource=Resource.create({"service.name": settings.otel_service_name}))
    if settings.otel_exporter_otlp_endpoint:
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint)))
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
