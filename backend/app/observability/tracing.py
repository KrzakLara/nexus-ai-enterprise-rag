"""OpenTelemetry tracing (optional, OTEL_ENABLED=true) with W3C trace-context propagation across
the durable queue.

Trace shape: browser request -> FastAPI server span -> SQLAlchemy spans -> `job.enqueue` (the
current context is injected into processing_jobs.trace_context) ... worker: `job.execute` is
started as a CHILD of the stored context -> download / parse (incl. OCR) / chunk / extract /
embed / storage spans. One trace therefore shows the upload request and the asynchronous
processing that it caused.

Exported over OTLP/HTTP (OTEL_EXPORTER_OTLP_ENDPOINT, e.g. http://jaeger:4318). When disabled,
the global no-op tracer makes every span call a cheap no-op. Span attributes carry identifiers,
counts and categories only; the OIDC callback (authorization code in the query string) and the
health probes are excluded from automatic HTTP instrumentation.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager

from opentelemetry import context as otel_context
from opentelemetry import propagate, trace
from opentelemetry.trace import Status, StatusCode

log = logging.getLogger("nexus.tracing")
tracer = trace.get_tracer("nexus")
_initialized = False
EXCLUDED_URLS = "api/health/live,api/health/ready,api/auth/oidc/callback,api/auth/oidc/backchannel-logout"


def init_tracing(cfg, service_name: str, app=None, engines=()) -> bool:
    global _initialized, tracer
    if not cfg.otel_enabled or _initialized:
        return _initialized
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    provider = TracerProvider(
        resource=Resource.create({"service.name": service_name, "service.version": cfg.service_version,
                                  "deployment.environment": cfg.app_env}),
        sampler=ParentBased(TraceIdRatioBased(max(0.0, min(1.0, cfg.otel_sample_ratio)))),
    )
    endpoint = cfg.otel_endpoint.rstrip("/")
    exporter = OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces") if endpoint else OTLPSpanExporter()
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("nexus")
    if app is not None:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, excluded_urls=EXCLUDED_URLS)
    if engines:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        SQLAlchemyInstrumentor().instrument(engines=list(engines), enable_commenter=False)
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    HTTPXClientInstrumentor().instrument()
    _initialized = True
    log.info("tracing_enabled", extra={"event": "tracing_enabled", "service_name": service_name})
    return True


def inject_current() -> dict:
    """W3C traceparent / tracestate of the active span (empty when tracing is off)."""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier


@contextmanager
def span(name: str, parent_carrier: dict | None = None, **attributes):
    """Start a span; with `parent_carrier`, as a child of a context extracted from a job row."""
    token = None
    if parent_carrier:
        token = otel_context.attach(propagate.extract(parent_carrier))
    try:
        with tracer.start_as_current_span(name) as current:
            for key, value in attributes.items():
                if value is not None:
                    current.set_attribute(key, value)
            try:
                yield current
            except Exception as exc:
                current.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                current.set_attribute("error.type", type(exc).__name__)
                raise
    finally:
        if token is not None:
            otel_context.detach(token)


def get_tracer():
    return tracer
