"""
OpenTelemetry Observability Configuration for AutoMaintainer
Provides distributed tracing, metrics, and structured logging.
"""
import os
import logging
from typing import Optional, Any
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# --- Safe Fallback Implementations for Environments Without Full OpenTelemetry ---
class _NoOpSpan:
    def set_status(self, *args, **kwargs): pass
    def record_exception(self, *args, **kwargs): pass
    def set_attribute(self, *args, **kwargs): pass
    def get_span_context(self):
        class _Ctx:
            is_valid = False
            trace_id = 0
            span_id = 0
        return _Ctx()
    def __enter__(self): return self
    def __exit__(self, *args): pass

class _NoOpTracer:
    def start_as_current_span(self, *args, **kwargs):
        return _NoOpSpan()

class _NoOpMetric:
    def add(self, *args, **kwargs): pass
    def record(self, *args, **kwargs): pass
    def set(self, *args, **kwargs): pass

class _NoOpMeter:
    def create_counter(self, *args, **kwargs): return _NoOpMetric()
    def create_histogram(self, *args, **kwargs): return _NoOpMetric()
    def create_up_down_counter(self, *args, **kwargs): return _NoOpMetric()
    def create_gauge(self, *args, **kwargs): return _NoOpMetric()

try:
    from opentelemetry import trace, metrics, baggage
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource, SERVICE_NAME, SERVICE_VERSION, DEPLOYMENT_ENVIRONMENT
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.prometheus import PrometheusMetricReader
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.requests import RequestsInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor
    from opentelemetry.instrumentation.logging import LoggingInstrumentor
    from opentelemetry.trace import Status, StatusCode
    from opentelemetry.metrics import CallbackOptions, Observation
    from opentelemetry.propagate import inject, extract
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
    OPENTELEMETRY_AVAILABLE = True
except ImportError:
    OPENTELEMETRY_AVAILABLE = False
    trace = None
    metrics = None
    baggage = None
    inject = lambda h: h
    extract = lambda h: {}
    class Status:
        def __init__(self, *args, **kwargs): pass
    class StatusCode:
        ERROR = "ERROR"
        OK = "OK"

# Global state
_tracer_provider = None
_meter_provider = None
_initialized = False


def init_observability(
    service_name: str = "automaintainer-backend",
    service_version: str = "1.0.0",
    environment: str = "development",
    otlp_endpoint: Optional[str] = None,
    enable_console_export: bool = True,
    enable_prometheus: bool = True,
) -> None:
    """
    Initialize OpenTelemetry observability stack.
    """
    global _tracer_provider, _meter_provider, _initialized
    
    if not OPENTELEMETRY_AVAILABLE:
        logger.warning("OpenTelemetry packages not available; operating in no-op telemetry mode")
        return

    if _initialized:
        logger.warning("Observability already initialized")
        return
    
    try:
        # Create resource with service metadata
        resource = Resource.create({
            SERVICE_NAME: service_name,
            SERVICE_VERSION: service_version,
            DEPLOYMENT_ENVIRONMENT: environment,
        })
        
        # --- Tracing Setup ---
        _tracer_provider = TracerProvider(resource=resource)
        
        # Add span processors
        if enable_console_export:
            _tracer_provider.add_span_processor(
                BatchSpanProcessor(ConsoleSpanExporter())
            )
        
        if otlp_endpoint:
            try:
                otlp_exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)
                _tracer_provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
                logger.info(f"OTLP trace exporter configured: {otlp_endpoint}")
            except Exception as e:
                logger.error(f"Failed to configure OTLP trace exporter: {e}")
        
        trace.set_tracer_provider(_tracer_provider)
        
        # --- Metrics Setup ---
        readers = []
        
        if enable_prometheus:
            try:
                prometheus_reader = PrometheusMetricReader()
                readers.append(prometheus_reader)
                logger.info("Prometheus metrics reader enabled")
            except Exception as e:
                logger.error(f"Failed to configure Prometheus metrics: {e}")
        
        if otlp_endpoint:
            try:
                otlp_metric_exporter = OTLPMetricExporter(endpoint=otlp_endpoint, insecure=True)
                readers.append(PeriodicExportingMetricReader(otlp_metric_exporter, export_interval_millis=30000))
                logger.info(f"OTLP metric exporter configured: {otlp_endpoint}")
            except Exception as e:
                logger.error(f"Failed to configure OTLP metric exporter: {e}")
        
        if readers:
            _meter_provider = MeterProvider(resource=resource, metric_readers=readers)
            metrics.set_meter_provider(_meter_provider)
        
        # --- Instrumentations ---
        try:
            HTTPXClientInstrumentor().instrument()
            RequestsInstrumentor().instrument()
            RedisInstrumentor().instrument()
            LoggingInstrumentor().instrument(set_logging_format=True)
            trace.set_trace_context_propagator(TraceContextTextMapPropagator())
        except Exception as e:
            logger.warning(f"Failed to apply some instrumentations: {e}")
        
        _initialized = True
        logger.info(f"Observability initialized for {service_name} v{service_version} ({environment})")
    except Exception as e:
        logger.error(f"Failed to fully initialize OpenTelemetry: {e}")


def instrument_fastapi(app):
    """Instrument FastAPI application with OpenTelemetry."""
    if not OPENTELEMETRY_AVAILABLE or not _initialized:
        logger.warning("Observability not initialized, skipping FastAPI instrumentation")
        return
    
    try:
        FastAPIInstrumentor.instrument_app(
            app,
            tracer_provider=_tracer_provider,
            meter_provider=_meter_provider,
            excluded_urls="healthz,/metrics,/health",
        )
        logger.info("FastAPI instrumentation enabled")
    except Exception as e:
        logger.warning(f"FastAPI instrumentation failed: {e}")


def get_tracer(name: str = "automaintainer"):
    """Get a tracer instance."""
    if not OPENTELEMETRY_AVAILABLE or trace is None:
        return _NoOpTracer()
    return trace.get_tracer(name)


def get_meter(name: str = "automaintainer"):
    """Get a meter instance."""
    if not OPENTELEMETRY_AVAILABLE or metrics is None:
        return _NoOpMeter()
    return metrics.get_meter(name)


# --- Custom Metrics ---

_meter = None

def get_custom_meter():
    global _meter
    if _meter is None:
        _meter = get_meter("automaintainer.custom")
    return _meter


agent_runs_total = _NoOpMetric()
agent_run_duration = _NoOpMetric()
agent_run_active = _NoOpMetric()
llm_tokens_total = _NoOpMetric()
llm_request_duration = _NoOpMetric()
github_api_calls_total = _NoOpMetric()
celery_queue_depth = _NoOpMetric()
celery_task_duration = _NoOpMetric()


def create_custom_metrics():
    """Create custom application metrics."""
    global agent_runs_total, agent_run_duration, agent_run_active
    global llm_tokens_total, llm_request_duration
    global github_api_calls_total, celery_queue_depth, celery_task_duration
    
    if not OPENTELEMETRY_AVAILABLE or metrics is None:
        return
        
    try:
        meter = get_custom_meter()
        
        agent_runs_total = meter.create_counter(
            "agent_runs_total",
            description="Total number of agent runs",
            unit="1",
        )
        
        agent_run_duration = meter.create_histogram(
            "agent_run_duration_seconds",
            description="Duration of agent runs",
            unit="s",
        )
        
        agent_run_active = meter.create_up_down_counter(
            "agent_runs_active",
            description="Currently active agent runs",
            unit="1",
        )
        
        llm_tokens_total = meter.create_counter(
            "llm_tokens_total",
            description="Total LLM tokens consumed",
            unit="1",
        )
        
        llm_request_duration = meter.create_histogram(
            "llm_request_duration_seconds",
            description="Duration of LLM requests",
            unit="s",
        )
        
        github_api_calls_total = meter.create_counter(
            "github_api_calls_total",
            description="Total GitHub API calls",
            unit="1",
        )
        
        celery_queue_depth = meter.create_gauge(
            "celery_queue_depth",
            description="Current depth of Celery queues",
            unit="1",
        )
        
        celery_task_duration = meter.create_histogram(
            "celery_task_duration_seconds",
            description="Duration of Celery tasks",
            unit="s",
        )
    except Exception as e:
        logger.warning(f"Could not create custom metrics: {e}")


# Initialize metrics on import
create_custom_metrics()


# --- Context Managers for Tracing ---

@contextmanager
def trace_agent_execution(mode: str, repo: str, run_id: Optional[str] = None):
    """Context manager for tracing agent run execution."""
    tracer = get_tracer()
    with tracer.start_as_current_span(
        f"agent_run_{mode}",
        attributes={
            "agent.mode": mode,
            "agent.repo": repo,
            "agent.run_id": run_id or "unknown",
        }
    ) as span:
        try:
            agent_run_active.add(1, {"mode": mode})
            yield span
        except Exception as e:
            if hasattr(span, "set_status"):
                span.set_status(Status(StatusCode.ERROR, str(e)))
            if hasattr(span, "record_exception"):
                span.record_exception(e)
            raise
        finally:
            agent_run_active.add(-1, {"mode": mode})


@contextmanager
def trace_llm_call(model: str, estimated_tokens: int):
    """Context manager for tracing LLM calls."""
    tracer = get_tracer()
    with tracer.start_as_current_span(
        "llm_call",
        attributes={
            "model": model,
            "estimated_tokens": estimated_tokens,
        }
    ) as span:
        try:
            yield span
        except Exception as e:
            if hasattr(span, "set_status"):
                span.set_status(Status(StatusCode.ERROR, str(e)))
            if hasattr(span, "record_exception"):
                span.record_exception(e)
            raise


@contextmanager
def trace_github_api(operation: str, repo: str = ""):
    """Context manager for tracing GitHub API calls."""
    tracer = get_tracer()
    with tracer.start_as_current_span(
        "github_api",
        attributes={
            "operation": operation,
            "repo": repo,
        }
    ) as span:
        try:
            yield span
        except Exception as e:
            if hasattr(span, "set_status"):
                span.set_status(Status(StatusCode.ERROR, str(e)))
            if hasattr(span, "record_exception"):
                span.record_exception(e)
            raise


@contextmanager
def trace_celery_task(task_name: str, queue: str = ""):
    """Context manager for tracing Celery tasks."""
    tracer = get_tracer()
    with tracer.start_as_current_span(
        "celery_task",
        attributes={
            "task_name": task_name,
            "queue": queue,
        }
    ) as span:
        try:
            yield span
        except Exception as e:
            if hasattr(span, "set_status"):
                span.set_status(Status(StatusCode.ERROR, str(e)))
            if hasattr(span, "record_exception"):
                span.record_exception(e)
            raise


# --- Helper Functions ---

def record_agent_run_complete(run_id: str, repo_name: str, mode: str, status: str, duration: float):
    """Record metrics for a completed agent run."""
    attributes = {
        "repo_name": repo_name,
        "mode": mode,
        "status": status,
    }
    agent_runs_total.add(1, attributes)
    agent_run_duration.record(duration, attributes)


def record_llm_tokens(model: str, tokens: int, run_id: str = ""):
    """Record LLM token usage."""
    attributes = {"model": model}
    if run_id:
        attributes["run_id"] = run_id
    llm_tokens_total.add(tokens, attributes)


def record_llm_request(model: str, duration: float, success: bool):
    """Record LLM request duration."""
    llm_request_duration.record(duration, {"model": model, "success": str(success)})


def record_github_api_call(operation: str, repo: str, success: bool):
    """Record GitHub API call."""
    github_api_calls_total.add(1, {
        "operation": operation,
        "repo": repo,
        "success": str(success),
    })


def set_celery_queue_depth(queue: str, depth: int):
    """Set Celery queue depth gauge."""
    celery_queue_depth.set(depth, {"queue": queue})


def record_celery_task_duration(task_name: str, queue: str, duration: float, success: bool):
    """Record Celery task duration."""
    celery_task_duration.record(duration, {
        "task_name": task_name,
        "queue": queue,
        "success": str(success),
    })


# --- Trace Context Propagation ---

def inject_trace_context(headers: dict) -> dict:
    """Inject current trace context into headers for downstream calls."""
    if OPENTELEMETRY_AVAILABLE and inject:
        inject(headers)
    return headers


def extract_trace_context(headers: dict):
    """Extract trace context from incoming headers."""
    if OPENTELEMETRY_AVAILABLE and extract:
        return extract(headers)
    return {}


# --- Structured Logging with Trace Context ---

class TraceContextFilter(logging.Filter):
    """Add trace context to log records."""
    
    def filter(self, record):
        if OPENTELEMETRY_AVAILABLE and trace:
            span = trace.get_current_span()
            if span and span.get_span_context().is_valid:
                ctx = span.get_span_context()
                record.trace_id = format(ctx.trace_id, '032x')
                record.span_id = format(ctx.span_id, '016x')
                return True
        record.trace_id = "0" * 32
        record.span_id = "0" * 16
        return True


def setup_structured_logging(level: int = logging.INFO):
    """Setup structured logging with trace context."""
    handler = logging.StreamHandler()
    formatter = logging.Formatter(
        '%(asctime)s %(levelname)s [%(name)s] [trace_id=%(trace_id)s span_id=%(span_id)s] %(message)s'
    )
    handler.setFormatter(formatter)
    
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.addHandler(handler)
    root_logger.addFilter(TraceContextFilter())
    
    # Also add to specific loggers
    for logger_name in ['uvicorn', 'fastapi', 'celery', 'httpx']:
        logging.getLogger(logger_name).addFilter(TraceContextFilter())


# --- Health Check for Observability ---

def check_observability_health() -> dict:
    """Check health of observability components."""
    health = {
        "tracing": _tracer_provider is not None,
        "metrics": _meter_provider is not None,
        "initialized": _initialized,
    }
    
    if _tracer_provider and hasattr(_tracer_provider, "_span_processors"):
        health["span_processors"] = len(_tracer_provider._span_processors)
    
    return health


# --- Shutdown ---

def shutdown_observability():
    """Gracefully shutdown observability."""
    global _initialized
    
    if _tracer_provider and hasattr(_tracer_provider, "shutdown"):
        _tracer_provider.shutdown()
    
    if _meter_provider and hasattr(_meter_provider, "shutdown"):
        _meter_provider.shutdown()
    
    _initialized = False
    logger.info("Observability shutdown complete")