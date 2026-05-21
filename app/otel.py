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


async def audit_span_middleware(request: Request, call_next: Callable):
    if trace is None:
        return await call_next(request)
    tracer = trace.get_tracer("accounting_ops.audit")
    started = time.perf_counter()
    with tracer.start_as_current_span("http.request") as span:
        span.set_attribute("tenant_id", request.headers.get("x-tenant-id", "unknown"))
        span.set_attribute("agent_id", request.headers.get("x-agent-id", "unknown"))
        span.set_attribute("user_uc_uid", request.headers.get("x-uc-uid", "unknown"))
        span.set_attribute("tool_name", request.headers.get("x-tool-name", "none"))
        try:
            response = await call_next(request)
            span.set_attribute("outcome", str(response.status_code))
            return response
        except Exception:
            span.set_attribute("outcome", "error")
            raise
        finally:
            span.set_attribute("latency_ms", round((time.perf_counter() - started) * 1000, 2))
