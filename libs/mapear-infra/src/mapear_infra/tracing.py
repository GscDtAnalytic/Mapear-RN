"""OpenTelemetry tracing for distributed pipeline observability.

Provides a tracer that propagates context across pipeline stages.
No-ops gracefully if opentelemetry is not installed.
"""

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from loguru import logger

try:
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
        OTLPSpanExporter,
    )
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        ConsoleSpanExporter,
    )

    _HAS_OTEL = True
except ImportError:
    _HAS_OTEL = False


_tracer = None


def setup_tracing(
    service_name: str = "mapear-pipeline",
    otlp_endpoint: str | None = None,
    console_export: bool = False,
) -> None:
    """Initialize OpenTelemetry tracing."""
    global _tracer

    if not _HAS_OTEL:
        logger.warning("opentelemetry not installed, tracing disabled")
        return

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)

    if otlp_endpoint:
        exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        logger.info(
            "OTLP tracing enabled → {endpoint}",
            endpoint=otlp_endpoint,
        )

    if console_export:
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer(service_name)
    logger.info("OpenTelemetry tracing initialized")


def get_tracer() -> Any:
    """Return the configured tracer, or a no-op."""
    global _tracer
    if _tracer is not None:
        return _tracer
    if _HAS_OTEL:
        return trace.get_tracer("mapear-pipeline")
    return _NoopTracer()


class _NoopSpan:
    """No-op span for when OTEL is not available."""

    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def set_status(self, *args: Any, **kwargs: Any) -> None:
        pass

    def record_exception(self, exc: Exception) -> None:
        pass

    def __enter__(self) -> "_NoopSpan":
        return self

    def __exit__(self, *args: Any) -> None:
        pass


class _NoopTracer:
    """No-op tracer for when OTEL is not available."""

    def start_as_current_span(self, name: str, **kwargs: Any) -> "_NoopSpan":
        return _NoopSpan()


@contextmanager
def trace_stage(
    stage_name: str,
    attributes: dict[str, Any] | None = None,
) -> Generator[Any, None, None]:
    """Context manager that creates a span for a pipeline stage."""
    tracer = get_tracer()
    with tracer.start_as_current_span(stage_name) as span:
        if attributes:
            for key, value in attributes.items():
                span.set_attribute(key, str(value))
        try:
            yield span
        except Exception as exc:
            if hasattr(span, "record_exception"):
                span.record_exception(exc)
            raise
