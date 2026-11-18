"""Cloud Trace export switch (#527).

``OTEL_CLOUD_TRACE_ENABLED`` turns on a second span exporter that sends the
backend's existing OpenTelemetry traces to Google Cloud Trace. It is off
unless set, and none of these tests reaches Google: the exporter is either a
stand-in module or the real class with stubbed application default
credentials (its gRPC channel is lazy and no span is exported).
"""

import importlib.util
import sys
import types
from unittest.mock import MagicMock

import pytest

import observability


def _installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except ModuleNotFoundError:  # a parent package is missing
        return False


pytestmark = pytest.mark.skipif(
    not observability.OPENTELEMETRY_AVAILABLE,
    reason="OpenTelemetry SDK not installed in this environment",
)


@pytest.mark.parametrize("value", [None, "", "false", "0", "no", "off", "maybe"])
def test_cloud_trace_is_off_unless_explicitly_enabled(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("OTEL_CLOUD_TRACE_ENABLED", raising=False)
    else:
        monkeypatch.setenv("OTEL_CLOUD_TRACE_ENABLED", value)
    assert observability.cloud_trace_enabled() is False


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on", " true "])
def test_cloud_trace_is_on_for_truthy_values(monkeypatch, value):
    monkeypatch.setenv("OTEL_CLOUD_TRACE_ENABLED", value)
    assert observability.cloud_trace_enabled() is True


class _FakeCloudTraceExporter:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        _FakeCloudTraceExporter.instances.append(self)

    def export(self, spans):  # pragma: no cover - never called
        raise AssertionError("no span may be exported in tests")

    def shutdown(self):
        pass


@pytest.fixture
def fake_cloud_trace_module(monkeypatch):
    _FakeCloudTraceExporter.instances = []
    module = types.ModuleType("opentelemetry.exporter.cloud_trace")
    module.CloudTraceSpanExporter = _FakeCloudTraceExporter
    monkeypatch.setitem(sys.modules, "opentelemetry.exporter.cloud_trace", module)
    return module


@pytest.fixture
def isolated_init(monkeypatch):
    """Run init_observability without touching process-wide OTel state.

    Returns the list of exporters handed to BatchSpanProcessor.
    """
    monkeypatch.setattr(observability, "_initialized", False)
    monkeypatch.setattr(observability, "_tracer_provider", None)
    monkeypatch.setattr(observability, "_meter_provider", None)
    monkeypatch.setattr(observability.trace, "set_tracer_provider", MagicMock())
    monkeypatch.setattr(observability.metrics, "set_meter_provider", MagicMock())
    for name in (
        "HTTPXClientInstrumentor",
        "RequestsInstrumentor",
        "RedisInstrumentor",
    ):
        monkeypatch.setattr(observability, name, MagicMock())
    monkeypatch.setattr(observability, "set_global_textmap", MagicMock())

    exporters = []

    def _processor(exporter, *args, **kwargs):
        exporters.append(exporter)
        return MagicMock()

    monkeypatch.setattr(observability, "BatchSpanProcessor", _processor)

    def run(**kwargs):
        observability.init_observability(
            enable_console_export=False, enable_prometheus=False, **kwargs
        )
        return exporters

    return run


def test_init_adds_no_cloud_trace_exporter_by_default(
    monkeypatch, fake_cloud_trace_module, isolated_init
):
    monkeypatch.delenv("OTEL_CLOUD_TRACE_ENABLED", raising=False)
    exporters = isolated_init()
    assert observability._initialized is True
    assert _FakeCloudTraceExporter.instances == []
    assert exporters == []


def test_enabling_the_switch_adds_a_cloud_trace_span_exporter(
    monkeypatch, fake_cloud_trace_module, isolated_init
):
    monkeypatch.setenv("OTEL_CLOUD_TRACE_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "automaintainer-prod")
    exporters = isolated_init()
    (exporter,) = _FakeCloudTraceExporter.instances
    assert exporter.kwargs == {"project_id": "automaintainer-prod"}
    assert exporters == [exporter]


def test_without_a_project_the_exporter_detects_it_itself(
    monkeypatch, fake_cloud_trace_module, isolated_init
):
    monkeypatch.setenv("OTEL_CLOUD_TRACE_ENABLED", "1")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    isolated_init()
    (exporter,) = _FakeCloudTraceExporter.instances
    assert exporter.kwargs == {}


def test_cloud_trace_runs_alongside_otlp(
    monkeypatch, fake_cloud_trace_module, isolated_init
):
    monkeypatch.setenv("OTEL_CLOUD_TRACE_ENABLED", "true")
    otlp = MagicMock(name="otlp-exporter")
    monkeypatch.setattr(observability, "OTLPSpanExporter", lambda **kw: otlp)
    monkeypatch.setattr(observability, "OTLPMetricExporter", MagicMock())
    monkeypatch.setattr(observability, "PeriodicExportingMetricReader", MagicMock())
    monkeypatch.setattr(observability, "MeterProvider", MagicMock())
    exporters = isolated_init(otlp_endpoint="http://collector:4317")
    assert exporters == [otlp, _FakeCloudTraceExporter.instances[0]]


def test_a_missing_exporter_package_does_not_break_startup(
    monkeypatch, isolated_init, caplog
):
    monkeypatch.setenv("OTEL_CLOUD_TRACE_ENABLED", "true")
    # None in sys.modules makes the import raise ImportError.
    monkeypatch.setitem(sys.modules, "opentelemetry.exporter.cloud_trace", None)
    exporters = isolated_init()
    assert observability._initialized is True
    assert exporters == []
    assert "Cloud Trace" in caplog.text


@pytest.mark.skipif(
    not _installed("opentelemetry.exporter.cloud_trace"),
    reason="opentelemetry-exporter-gcp-trace not installed",
)
def test_real_exporter_is_built_from_stubbed_credentials(monkeypatch):
    """The locked exporter package constructs offline with given credentials."""
    import google.auth
    from google.auth.credentials import AnonymousCredentials
    from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter

    monkeypatch.setattr(
        google.auth,
        "default",
        lambda *a, **kw: (AnonymousCredentials(), "automaintainer-prod"),
    )
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "automaintainer-prod")
    exporters = []
    monkeypatch.setattr(
        observability, "BatchSpanProcessor", lambda e: exporters.append(e) or e
    )
    provider = MagicMock()

    assert observability._configure_cloud_trace(provider) is True
    (exporter,) = exporters
    assert isinstance(exporter, CloudTraceSpanExporter)
    assert exporter.project_id == "automaintainer-prod"
    provider.add_span_processor.assert_called_once_with(exporter)
