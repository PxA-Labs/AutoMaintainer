"""
OpenTelemetry Observability Configuration for AutoMaintainer
Provides distributed tracing, metrics, and structured logging.
"""

import os
import json
import logging
import re
from functools import lru_cache
from typing import Optional, Any
from contextlib import contextmanager

logger = logging.getLogger(__name__)


# --- Safe Fallback Implementations for Environments Without Full OpenTelemetry ---
class _NoOpSpan:
    def set_status(self, *args, **kwargs):
        pass

    def record_exception(self, *args, **kwargs):
        pass

    def set_attribute(self, *args, **kwargs):
        pass

    def get_span_context(self):
        class _Ctx:
            is_valid = False
            trace_id = 0
            span_id = 0

        return _Ctx()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class _NoOpTracer:
    def start_as_current_span(self, *args, **kwargs):
        return _NoOpSpan()


class _NoOpMetric:
    def add(self, *args, **kwargs):
        pass

    def record(self, *args, **kwargs):
        pass

    def set(self, *args, **kwargs):
        pass


class _NoOpMeter:
    def create_counter(self, *args, **kwargs):
        return _NoOpMetric()

    def create_histogram(self, *args, **kwargs):
        return _NoOpMetric()

    def create_up_down_counter(self, *args, **kwargs):
        return _NoOpMetric()

    def create_gauge(self, *args, **kwargs):
        return _NoOpMetric()


try:
    from opentelemetry import trace, metrics, baggage
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import (
        Resource,
        SERVICE_NAME,
        SERVICE_VERSION,
        DEPLOYMENT_ENVIRONMENT,
    )
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
        OTLPMetricExporter,
    )
    from opentelemetry.exporter.prometheus import PrometheusMetricReader
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.requests import RequestsInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor
    from opentelemetry.trace import Status, StatusCode
    from opentelemetry.metrics import CallbackOptions, Observation
    from opentelemetry.propagate import inject, extract, set_global_textmap
    from opentelemetry.trace.propagation.tracecontext import (
        TraceContextTextMapPropagator,
    )

    OPENTELEMETRY_AVAILABLE = True
except ImportError:
    OPENTELEMETRY_AVAILABLE = False
    trace = None
    metrics = None
    baggage = None
    inject = lambda h: h
    extract = lambda h: {}

    class Status:
        def __init__(self, *args, **kwargs):
            pass

    class StatusCode:
        ERROR = "ERROR"
        OK = "OK"


# --- Metric catalogue (#404) ---
# Every metric this process exports, with the complete set of label
# (attribute) names it may carry. Labels must take a small, bounded set of
# values: a label such as repo_name, run_id, a user id, a raw URL path, a Host
# header or an error message creates one time series per distinct value and
# can exhaust the metrics backend. High-cardinality identifiers belong in
# logs and span attributes only. docs/OBSERVABILITY.md documents the values.
#
# ``metric_views`` turns the catalogue into OpenTelemetry Views, so a label
# that is not listed here is dropped when it is recorded, whichever code (ours
# or an instrumentation library) adds it.

# FastAPI/ASGI server metrics. In the default (old) HTTP semantic conventions
# the route template is carried in ``http.target`` ("/runs/{run_id}"); it is
# absent for requests that match no route, never the raw path. The stable
# conventions (OTEL_SEMCONV_STABILITY_OPT_IN=http) use ``http.route``.
# Dropped on purpose: http.server_name (copied from the client's Host header,
# so any caller can mint new values), http.host, net.host.port, http.scheme
# and http.flavor.
_HTTP_SERVER_LABELS = frozenset(
    {
        "http.method",
        "http.status_code",
        "http.target",
        "http.request.method",
        "http.response.status_code",
        "http.route",
    }
)
_HTTP_SERVER_ACTIVE_LABELS = frozenset({"http.method", "http.request.method"})
# httpx/requests client metrics: the peer host is one of the configured
# services (GitHub, Supabase, Groq, ...); full URLs are never labels.
_HTTP_CLIENT_LABELS = frozenset(
    {
        "http.method",
        "http.status_code",
        "net.peer.name",
        "http.request.method",
        "http.response.status_code",
        "server.address",
    }
)

METRIC_LABELS: dict[str, frozenset[str]] = {
    # Custom application metrics (record_* helpers below).
    "agent_runs_total": frozenset({"mode", "outcome"}),
    "agent_run_duration_seconds": frozenset({"mode", "outcome"}),
    "agent_runs_active": frozenset({"mode"}),
    "llm_tokens_total": frozenset({"model"}),
    "llm_request_duration_seconds": frozenset({"model", "outcome"}),
    "github_api_calls_total": frozenset({"operation", "outcome"}),
    "celery_queue_depth": frozenset({"queue"}),
    "celery_task_duration_seconds": frozenset({"task_name", "queue", "outcome"}),
    # Instrumentation-library metrics.
    "http.server.duration": _HTTP_SERVER_LABELS,
    "http.server.request.duration": _HTTP_SERVER_LABELS,
    "http.server.request.size": _HTTP_SERVER_LABELS,
    "http.server.request.body.size": _HTTP_SERVER_LABELS,
    "http.server.response.size": _HTTP_SERVER_LABELS,
    "http.server.response.body.size": _HTTP_SERVER_LABELS,
    "http.server.active_requests": _HTTP_SERVER_ACTIVE_LABELS,
    "http.client.duration": _HTTP_CLIENT_LABELS,
    "http.client.request.duration": _HTTP_CLIENT_LABELS,
}

# Metrics added for Cloud Monitoring dashboards and alerts
# (infra/terraform/monitoring.tf). Kept apart from METRIC_LABELS only so the
# existing catalogue test (test_metric_labels.py) and docs/OBSERVABILITY.md
# can absorb them in a separate change; metric_views() and
# all_metric_labels() treat both catalogues as one allowlist.
ALERTING_METRIC_LABELS: dict[str, frozenset[str]] = {
    "llm_phase_tokens_total": frozenset({"model", "phase", "kind"}),
    "llm_estimated_cost_usd_total": frozenset({"model", "phase"}),
    # One series per worker process; absent when no worker runs.
    "celery_worker_heartbeat": frozenset(),
}


def all_metric_labels() -> dict[str, frozenset[str]]:
    """Every exported metric and its complete label set."""
    return {**METRIC_LABELS, **ALERTING_METRIC_LABELS}


# Bounded values for labels whose input can come from a request.
AGENT_MODES = frozenset({"autonomous", "targeted_issue", "code_review", "refactor"})
RUN_OUTCOMES = frozenset({"completed", "failed", "cancelled"})
# events.AgentPhase values (not imported: this module must stay importable
# without the engine).
LLM_PHASES = frozenset(
    {
        "architect",
        "brainstormer",
        "visionary",
        "pm",
        "reviewer",
        "implementer",
        "maintainer",
        "system",
    }
)
# "total" is a count that was not split into prompt and completion tokens.
LLM_TOKEN_KINDS = frozenset({"input", "output", "total"})
OTHER = "other"
# operation, task_name, queue and model must be code constants or
# configuration (e.g. "get_pull", "agents.run", "llama-3.3-70b-versatile").
# Anything that does not look like one (a path, a sentence, an error message)
# is recorded as "other" rather than becoming a new series.
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")


def _one_of(value, allowed: frozenset[str]) -> str:
    return value if value in allowed else OTHER


def _identifier(value) -> str:
    if isinstance(value, str) and _IDENTIFIER_RE.fullmatch(value):
        return value
    return OTHER


def _outcome(success: bool) -> str:
    return "success" if success else "failure"


def metric_views() -> list:
    """One View per catalogued metric that keeps only its catalogued labels.

    Requires the OpenTelemetry SDK (imported here so the catalogue can be
    used and tested without it).
    """
    from opentelemetry.sdk.metrics.view import (
        ExplicitBucketHistogramAggregation,
        View,
    )

    views = []
    for name, labels in all_metric_labels().items():
        kwargs = {}
        if name == "agent_run_duration_seconds":
            kwargs["aggregation"] = ExplicitBucketHistogramAggregation(
                boundaries=RUN_DURATION_BUCKETS_SECONDS
            )
        views.append(View(instrument_name=name, attribute_keys=set(labels), **kwargs))
    return views


# Agent runs last minutes, up to the 30-minute soft time limit. The SDK's
# default buckets jump from 1000 s to 2500 s, too coarse for a p95 alert.
RUN_DURATION_BUCKETS_SECONDS = (
    10.0,
    30.0,
    60.0,
    120.0,
    300.0,
    600.0,
    900.0,
    1200.0,
    1500.0,
    1800.0,
    2400.0,
    3600.0,
)


# --- Estimated LLM cost (an ESTIMATE, not a bill) ------------------------------
# USD per million tokens. The same figures as evals/e2e/pricing.py: Groq's
# earlier public on-demand list prices, which could not be confirmed on
# 2026-11-18 (the model page showed "Contact sales"). LLM_PRICES_JSON (a JSON
# object of the same shape) adds or replaces entries per model. A model in
# neither table adds tokens but no cost, so the cost metric undercounts
# rather than invents a price.
DEFAULT_LLM_PRICES_PER_MTOK = {
    "llama-3.3-70b-versatile": {"input_per_mtok": 0.59, "output_per_mtok": 0.79},
    "llama-3.1-8b-instant": {"input_per_mtok": 0.05, "output_per_mtok": 0.08},
}
LLM_PRICES_ENV = "LLM_PRICES_JSON"
_PRICE_KEYS = ("input_per_mtok", "output_per_mtok")


def _valid_price(value) -> bool:
    return (
        isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
    )


@lru_cache(maxsize=4)
def _parse_llm_prices(raw: str) -> dict:
    """The default table merged with ``raw``.

    Cached per value, so an invalid override is logged once and then ignored
    (the defaults apply) instead of failing or logging on every LLM call.
    """
    prices = dict(DEFAULT_LLM_PRICES_PER_MTOK)
    if not raw.strip():
        return prices
    try:
        table = json.loads(raw)
        if not isinstance(table, dict):
            raise ValueError("must be a JSON object")
        for model, entry in table.items():
            if not isinstance(entry, dict) or not all(
                _valid_price(entry.get(k)) for k in _PRICE_KEYS
            ):
                raise ValueError(f"{model}: needs {' and '.join(_PRICE_KEYS)} >= 0")
    except ValueError as e:  # json.JSONDecodeError is a ValueError
        logger.warning(f"Ignoring {LLM_PRICES_ENV}: {e}")
        return prices
    prices.update(table)
    return prices


def estimated_llm_cost_usd(model: str, tokens: int, kind: str) -> Optional[float]:
    """Estimated USD cost of ``tokens`` input or output tokens; None if unpriced."""
    if kind not in ("input", "output"):
        return None
    entry = _parse_llm_prices(os.getenv(LLM_PRICES_ENV, "")).get(model)
    if entry is None:
        return None
    return tokens * entry[f"{kind}_per_mtok"] / 1_000_000


# --- Cloud Trace (#527) ---
# OTEL_CLOUD_TRACE_ENABLED=true adds a span exporter that sends the traces
# above to Google Cloud Trace, next to (not instead of) the console and OTLP
# exporters. Off by default. The exporter authenticates with application
# default credentials (on Cloud Run: the service's own service account, which
# needs roles/cloudtrace.agent) and uses GOOGLE_CLOUD_PROJECT when set, else
# the project those credentials belong to. Metrics are not sent to Google.
CLOUD_TRACE_ENV = "OTEL_CLOUD_TRACE_ENABLED"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def cloud_trace_enabled() -> bool:
    """True only when OTEL_CLOUD_TRACE_ENABLED is 1, true, yes or on."""
    return os.getenv(CLOUD_TRACE_ENV, "").strip().lower() in _TRUTHY


def _configure_cloud_trace(tracer_provider) -> bool:
    """Add a batched Cloud Trace span exporter; never raises.

    The exporter package is imported here, not at module import, so a missing
    or broken package disables only this exporter and not the whole
    OpenTelemetry setup (see test_observability_imports.py).
    """
    try:
        from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter

        project_id = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
        kwargs = {"project_id": project_id} if project_id else {}
        tracer_provider.add_span_processor(
            BatchSpanProcessor(CloudTraceSpanExporter(**kwargs))
        )
    except Exception as e:  # ImportError, or no application default credentials
        logger.error(f"Failed to configure the Cloud Trace exporter: {e}")
        return False
    logger.info("Cloud Trace span exporter configured")
    return True


# --- Cloud Monitoring ---
# OTEL_CLOUD_MONITORING_ENABLED=true adds a metric reader that sends the
# metrics above to Google Cloud Monitoring every 60 s, next to Prometheus and
# OTLP: in the API (init_observability) and, once celery_app calls
# install_celery_worker_hooks, in each Celery worker process. Off by default.
# Credentials and project as for Cloud Trace; the account needs
# roles/monitoring.metricWriter. Each metric becomes the metric type
# workload.googleapis.com/<name>, which infra/terraform/monitoring.tf alerts on.
CLOUD_MONITORING_ENV = "OTEL_CLOUD_MONITORING_ENABLED"
CLOUD_MONITORING_METRIC_PREFIX = "workload.googleapis.com"
# Cloud Monitoring accepts one point per series every 5 s at most; 60 s keeps
# the number of points written (billed past the free allotment) low.
CLOUD_MONITORING_EXPORT_INTERVAL_MILLIS = 60_000


def cloud_monitoring_enabled() -> bool:
    """True only when OTEL_CLOUD_MONITORING_ENABLED is 1, true, yes or on."""
    return os.getenv(CLOUD_MONITORING_ENV, "").strip().lower() in _TRUTHY


def _cloud_monitoring_reader():
    """A periodic reader exporting to Cloud Monitoring, or None; never raises.

    The exporter is imported here, like the Cloud Trace one, so a missing
    package or missing credentials disable only this reader (logged once:
    init_observability runs once per process).

    ``add_unique_identifier`` labels each process's series with a random
    ``opentelemetry_id``. Without it, several Cloud Run instances or the
    worker's child processes would write the same series and Cloud
    Monitoring would reject their points. The price: a restarted process
    starts new series, so alerts and charts sum across them.
    """
    try:
        from opentelemetry.exporter.cloud_monitoring import (
            CloudMonitoringMetricsExporter,
        )

        project_id = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
        kwargs = {"project_id": project_id} if project_id else {}
        exporter = CloudMonitoringMetricsExporter(add_unique_identifier=True, **kwargs)
        reader = PeriodicExportingMetricReader(
            exporter,
            export_interval_millis=CLOUD_MONITORING_EXPORT_INTERVAL_MILLIS,
        )
    except Exception as e:  # ImportError, or no application default credentials
        logger.error(f"Failed to configure the Cloud Monitoring exporter: {e}")
        return None
    logger.info("Cloud Monitoring metric exporter configured")
    return reader


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
    enable_cloud_trace: Optional[bool] = None,
    enable_cloud_monitoring: Optional[bool] = None,
) -> None:
    """
    Initialize OpenTelemetry observability stack.

    ``enable_cloud_trace`` defaults to the OTEL_CLOUD_TRACE_ENABLED switch,
    ``enable_cloud_monitoring`` to OTEL_CLOUD_MONITORING_ENABLED.
    """
    global _tracer_provider, _meter_provider, _initialized

    if not OPENTELEMETRY_AVAILABLE:
        logger.warning(
            "OpenTelemetry packages not available; operating in no-op telemetry mode"
        )
        return

    if _initialized:
        logger.warning("Observability already initialized")
        return

    try:
        # Create resource with service metadata
        resource = Resource.create(
            {
                SERVICE_NAME: service_name,
                SERVICE_VERSION: service_version,
                DEPLOYMENT_ENVIRONMENT: environment,
            }
        )

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

        if enable_cloud_trace is None:
            enable_cloud_trace = cloud_trace_enabled()
        if enable_cloud_trace:
            _configure_cloud_trace(_tracer_provider)

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
                otlp_metric_exporter = OTLPMetricExporter(
                    endpoint=otlp_endpoint, insecure=True
                )
                readers.append(
                    PeriodicExportingMetricReader(
                        otlp_metric_exporter, export_interval_millis=30000
                    )
                )
                logger.info(f"OTLP metric exporter configured: {otlp_endpoint}")
            except Exception as e:
                logger.error(f"Failed to configure OTLP metric exporter: {e}")

        if enable_cloud_monitoring is None:
            enable_cloud_monitoring = cloud_monitoring_enabled()
        if enable_cloud_monitoring:
            cloud_reader = _cloud_monitoring_reader()
            if cloud_reader is not None:
                readers.append(cloud_reader)

        if readers:
            _meter_provider = MeterProvider(
                resource=resource, metric_readers=readers, views=metric_views()
            )
            metrics.set_meter_provider(_meter_provider)

        # --- Instrumentations ---
        try:
            HTTPXClientInstrumentor().instrument()
            RequestsInstrumentor().instrument()
            RedisInstrumentor().instrument()
            set_global_textmap(TraceContextTextMapPropagator())
        except Exception as e:
            logger.warning(f"Failed to apply some instrumentations: {e}")

        _initialized = True
        logger.info(
            f"Observability initialized for {service_name} v{service_version} ({environment})"
        )
    except Exception as e:
        logger.error(f"Failed to fully initialize OpenTelemetry: {e}")


def instrument_fastapi(app):
    """Instrument FastAPI application with OpenTelemetry."""
    if not OPENTELEMETRY_AVAILABLE or not _initialized:
        logger.warning(
            "Observability not initialized, skipping FastAPI instrumentation"
        )
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
llm_phase_tokens_total = _NoOpMetric()
llm_estimated_cost_usd_total = _NoOpMetric()

# Module attributes that hold an instrument (create_custom_metrics sets them).
_INSTRUMENT_GLOBALS = (
    "agent_runs_total",
    "agent_run_duration",
    "agent_run_active",
    "llm_tokens_total",
    "llm_request_duration",
    "github_api_calls_total",
    "celery_queue_depth",
    "celery_task_duration",
    "llm_phase_tokens_total",
    "llm_estimated_cost_usd_total",
)


def create_custom_metrics(meter=None):
    """Create custom application metrics (on ``meter``, default the global one)."""
    global agent_runs_total, agent_run_duration, agent_run_active
    global llm_tokens_total, llm_request_duration
    global github_api_calls_total, celery_queue_depth, celery_task_duration
    global llm_phase_tokens_total, llm_estimated_cost_usd_total

    if not OPENTELEMETRY_AVAILABLE or metrics is None:
        return

    try:
        meter = meter or get_custom_meter()

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

        llm_phase_tokens_total = meter.create_counter(
            "llm_phase_tokens_total",
            description="LLM tokens by agent phase and kind (input, output, total)",
            unit="1",
        )

        # "{USD}" is a UCUM annotation: Cloud Monitoring rejects a bare "USD".
        llm_estimated_cost_usd_total = meter.create_counter(
            "llm_estimated_cost_usd_total",
            description=(
                "ESTIMATED LLM cost from a built-in price table "
                "(LLM_PRICES_JSON overrides it); not a bill"
            ),
            unit="{USD}",
        )
    except Exception as e:
        logger.warning(f"Could not create custom metrics: {e}")


# Initialize metrics on import
create_custom_metrics()


# --- Celery worker processes ---
# The worker never runs init_observability on its own: celery_app has to call
# install_celery_worker_hooks() once at import. Each prefork child process
# then sets up its own providers after the fork (a reader's export thread
# does not survive a fork) and flushes them when it exits. The hooks use
# worker_process_init, which only the prefork pool (the default, and what
# the GCP worker runs) sends; a solo or thread pool exports no metrics.
_WORKER_INIT_UID = "automaintainer.observability.worker_process_init"
_WORKER_SHUTDOWN_UID = "automaintainer.observability.worker_process_shutdown"


def _heartbeat_callback(options):
    yield Observation(1)


def register_worker_heartbeat(meter=None):
    """Report celery_worker_heartbeat = 1 at every export of this process.

    The series is absent when no worker process runs, which is what the
    "worker heartbeat absent" alert in monitoring.tf watches.
    """
    if not OPENTELEMETRY_AVAILABLE or metrics is None:
        return None
    try:
        return (meter or get_custom_meter()).create_observable_gauge(
            "celery_worker_heartbeat",
            callbacks=[_heartbeat_callback],
            description="1 per live Celery worker process",
            unit="1",
        )
    except Exception as e:
        logger.warning(f"Could not create the worker heartbeat metric: {e}")
        return None


def init_worker_observability() -> None:
    """init_observability for a Celery worker process, plus its heartbeat.

    No Prometheus reader (a worker serves no /metrics endpoint) and no
    console span exporter; OTLP, Cloud Trace and Cloud Monitoring follow the
    same environment switches as the API.
    """
    init_observability(
        service_name="automaintainer-worker",
        environment=os.getenv("ENVIRONMENT", "development"),
        otlp_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"),
        enable_console_export=False,
        enable_prometheus=False,
    )
    register_worker_heartbeat()


def _on_worker_process_init(**kwargs):
    init_worker_observability()


def _on_worker_process_shutdown(**kwargs):
    shutdown_observability()


def install_celery_worker_hooks() -> bool:
    """Set up observability in every Celery worker child process; idempotent."""
    try:
        from celery.signals import worker_process_init, worker_process_shutdown
    except ImportError:
        return False
    worker_process_init.connect(
        _on_worker_process_init, weak=False, dispatch_uid=_WORKER_INIT_UID
    )
    worker_process_shutdown.connect(
        _on_worker_process_shutdown, weak=False, dispatch_uid=_WORKER_SHUTDOWN_UID
    )
    return True


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
        },
    ) as span:
        labels = {"mode": _one_of(mode, AGENT_MODES)}
        try:
            agent_run_active.add(1, labels)
            yield span
        except Exception as e:
            if hasattr(span, "set_status"):
                span.set_status(Status(StatusCode.ERROR, str(e)))
            if hasattr(span, "record_exception"):
                span.record_exception(e)
            raise
        finally:
            agent_run_active.add(-1, labels)


@contextmanager
def trace_llm_call(model: str, estimated_tokens: int):
    """Context manager for tracing LLM calls."""
    tracer = get_tracer()
    with tracer.start_as_current_span(
        "llm_call",
        attributes={
            "model": model,
            "estimated_tokens": estimated_tokens,
        },
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
        },
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
        },
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


def record_agent_run_complete(mode: str, status: str, duration: float):
    """Record metrics for a finished agent run.

    The run id and repository are not labels (#404): log them, or set them on
    the run's span (``trace_agent_execution`` does).
    """
    labels = {
        "mode": _one_of(mode, AGENT_MODES),
        "outcome": _one_of(status, RUN_OUTCOMES),
    }
    agent_runs_total.add(1, labels)
    agent_run_duration.record(duration, labels)


def record_llm_tokens(
    model: str, tokens: int, phase: Optional[str] = None, kind: str = "total"
):
    """Record LLM token usage (per model, phase and kind, not per run).

    ``phase`` is the calling node's AgentPhase value; ``kind`` is "input"
    (prompt), "output" (completion) or "total" (not split). Input and output
    tokens of a priced model also add their estimated cost.
    """
    model_label = _identifier(model)
    phase_label = _one_of(phase, LLM_PHASES)
    kind_label = _one_of(kind, LLM_TOKEN_KINDS)
    llm_tokens_total.add(tokens, {"model": model_label})
    llm_phase_tokens_total.add(
        tokens, {"model": model_label, "phase": phase_label, "kind": kind_label}
    )
    cost = estimated_llm_cost_usd(model, tokens, kind_label)
    if cost is not None:
        llm_estimated_cost_usd_total.add(
            cost, {"model": model_label, "phase": phase_label}
        )


def record_llm_request(model: str, duration: float, success: bool):
    """Record LLM request duration."""
    llm_request_duration.record(
        duration, {"model": _identifier(model), "outcome": _outcome(success)}
    )


def record_github_api_call(operation: str, success: bool):
    """Record a GitHub API call; ``operation`` is a code constant such as "get_pull"."""
    github_api_calls_total.add(
        1, {"operation": _identifier(operation), "outcome": _outcome(success)}
    )


def set_celery_queue_depth(queue: str, depth: int):
    """Set Celery queue depth gauge."""
    celery_queue_depth.set(depth, {"queue": _identifier(queue)})


def record_celery_task_duration(
    task_name: str, queue: str, duration: float, success: bool
):
    """Record Celery task duration."""
    celery_task_duration.record(
        duration,
        {
            "task_name": _identifier(task_name),
            "queue": _identifier(queue),
            "outcome": _outcome(success),
        },
    )


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
                record.trace_id = format(ctx.trace_id, "032x")
                record.span_id = format(ctx.span_id, "016x")
                return True
        record.trace_id = "0" * 32
        record.span_id = "0" * 16
        return True


STRUCTURED_LOG_FORMAT = (
    "%(asctime)s %(levelname)s [%(name)s] "
    "[trace_id=%(trace_id)s span_id=%(span_id)s] %(message)s"
)
_EMPTY_TRACE_FIELDS = {"trace_id": "0" * 32, "span_id": "0" * 16}


def make_structured_handler(stream=None) -> logging.Handler:
    """Build a handler that stamps trace context on every record it emits.

    The filter lives on the *handler*, not on a logger: logger-level filters
    only run for records created on that exact logger, so records from child
    loggers (``logging.getLogger("x.y")``) that propagate to the root handler
    would skip them and fail to format. The formatter also carries defaults
    for the trace fields so a record can never raise ``KeyError`` here.
    """
    handler = logging.StreamHandler(stream)
    handler.setFormatter(
        logging.Formatter(STRUCTURED_LOG_FORMAT, defaults=_EMPTY_TRACE_FIELDS)
    )
    handler.addFilter(TraceContextFilter())
    handler._automaintainer_structured = True  # marker for idempotent setup
    return handler


def setup_structured_logging(level: int = logging.INFO):
    """Setup structured logging with trace context.

    Idempotent: calling it again (e.g. once per app lifespan) does not stack
    duplicate handlers on the root logger.
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    if not any(
        getattr(h, "_automaintainer_structured", False) for h in root_logger.handlers
    ):
        root_logger.addHandler(make_structured_handler())


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
