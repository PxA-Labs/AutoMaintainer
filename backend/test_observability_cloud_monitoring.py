"""Cloud Monitoring metrics export switch and the alerting metrics.

``OTEL_CLOUD_MONITORING_ENABLED`` adds a periodic metric reader that sends
the backend's OpenTelemetry metrics to Google Cloud Monitoring, in the API
and in each Celery worker process. It is off unless set, and none of these
tests reaches Google: the exporter is a stand-in module, and emitted metrics
are read back from an in-memory reader.
"""

import json
import random
import string
import sys
import types
from unittest.mock import MagicMock

import pytest

import observability

pytestmark = pytest.mark.skipif(
    not observability.OPENTELEMETRY_AVAILABLE,
    reason="OpenTelemetry SDK not installed in this environment",
)

EXPORTER_MODULE = "opentelemetry.exporter.cloud_monitoring"


# --- The switch ----------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "false", "0", "no", "off", "maybe"])
def test_cloud_monitoring_is_off_unless_explicitly_enabled(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("OTEL_CLOUD_MONITORING_ENABLED", raising=False)
    else:
        monkeypatch.setenv("OTEL_CLOUD_MONITORING_ENABLED", value)
    assert observability.cloud_monitoring_enabled() is False


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on", " true "])
def test_cloud_monitoring_is_on_for_truthy_values(monkeypatch, value):
    monkeypatch.setenv("OTEL_CLOUD_MONITORING_ENABLED", value)
    assert observability.cloud_monitoring_enabled() is True


# --- Exporter enablement (fake exporter module) ------------------------------


class _FakeMetricsExporter:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        _FakeMetricsExporter.instances.append(self)


@pytest.fixture
def fake_exporter_module(monkeypatch):
    _FakeMetricsExporter.instances = []
    module = types.ModuleType(EXPORTER_MODULE)
    module.CloudMonitoringMetricsExporter = _FakeMetricsExporter
    monkeypatch.setitem(sys.modules, EXPORTER_MODULE, module)
    return module


class _FakeReader:
    def __init__(self, exporter, export_interval_millis=None, **kwargs):
        self.exporter = exporter
        self.export_interval_millis = export_interval_millis


@pytest.fixture
def isolated_init(monkeypatch):
    """Run init_observability without touching process-wide OTel state.

    Returns a dict with the MeterProvider calls and the meter provider set.
    """
    monkeypatch.delenv("OTEL_CLOUD_TRACE_ENABLED", raising=False)
    monkeypatch.setattr(observability, "_initialized", False)
    monkeypatch.setattr(observability, "_tracer_provider", None)
    monkeypatch.setattr(observability, "_meter_provider", None)
    monkeypatch.setattr(observability.trace, "set_tracer_provider", MagicMock())
    set_meter_provider = MagicMock()
    monkeypatch.setattr(observability.metrics, "set_meter_provider", set_meter_provider)
    for name in (
        "HTTPXClientInstrumentor",
        "RequestsInstrumentor",
        "RedisInstrumentor",
        "PrometheusMetricReader",
    ):
        monkeypatch.setattr(observability, name, MagicMock(name=name))
    monkeypatch.setattr(observability, "set_global_textmap", MagicMock())
    monkeypatch.setattr(observability, "BatchSpanProcessor", MagicMock())
    monkeypatch.setattr(observability, "PeriodicExportingMetricReader", _FakeReader)
    providers = []

    def _meter_provider(**kwargs):
        providers.append(kwargs)
        return MagicMock(name="MeterProvider")

    monkeypatch.setattr(observability, "MeterProvider", _meter_provider)

    def run(**kwargs):
        kwargs.setdefault("enable_console_export", False)
        kwargs.setdefault("enable_prometheus", False)
        observability.init_observability(**kwargs)
        return {"providers": providers, "set_meter_provider": set_meter_provider}

    return run


def _cloud_readers(state):
    return [
        reader
        for provider in state["providers"]
        for reader in provider["metric_readers"]
        if isinstance(reader, _FakeReader)
    ]


def test_init_adds_no_cloud_monitoring_reader_by_default(
    monkeypatch, fake_exporter_module, isolated_init
):
    monkeypatch.delenv("OTEL_CLOUD_MONITORING_ENABLED", raising=False)
    state = isolated_init()
    assert observability._initialized is True
    assert _FakeMetricsExporter.instances == []
    assert state["providers"] == []


def test_enabling_the_switch_adds_a_periodic_cloud_monitoring_reader(
    monkeypatch, fake_exporter_module, isolated_init
):
    monkeypatch.setenv("OTEL_CLOUD_MONITORING_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "automaintainer-prod")
    state = isolated_init()
    (exporter,) = _FakeMetricsExporter.instances
    # Every process writes its own series: Cloud Run instances and the
    # worker's child processes would otherwise write the same series and
    # Cloud Monitoring rejects points written more often than every 5 s.
    assert exporter.kwargs == {
        "project_id": "automaintainer-prod",
        "add_unique_identifier": True,
    }
    (reader,) = _cloud_readers(state)
    assert reader.exporter is exporter
    assert reader.export_interval_millis == 60_000
    (provider,) = state["providers"]
    assert provider["views"], "the label allowlist must apply to Cloud Monitoring"
    state["set_meter_provider"].assert_called_once()


def test_without_a_project_the_exporter_detects_it_itself(
    monkeypatch, fake_exporter_module, isolated_init
):
    monkeypatch.setenv("OTEL_CLOUD_MONITORING_ENABLED", "1")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    isolated_init()
    (exporter,) = _FakeMetricsExporter.instances
    assert exporter.kwargs == {"add_unique_identifier": True}


def test_cloud_monitoring_runs_alongside_prometheus(
    monkeypatch, fake_exporter_module, isolated_init
):
    monkeypatch.setenv("OTEL_CLOUD_MONITORING_ENABLED", "true")
    state = isolated_init(enable_prometheus=True)
    (provider,) = state["providers"]
    assert len(provider["metric_readers"]) == 2
    assert len(_cloud_readers(state)) == 1


def test_explicit_argument_overrides_the_environment(
    monkeypatch, fake_exporter_module, isolated_init
):
    monkeypatch.setenv("OTEL_CLOUD_MONITORING_ENABLED", "true")
    state = isolated_init(enable_cloud_monitoring=False)
    assert _FakeMetricsExporter.instances == []
    assert state["providers"] == []


def test_a_missing_exporter_package_does_not_break_startup(
    monkeypatch, isolated_init, caplog
):
    monkeypatch.setenv("OTEL_CLOUD_MONITORING_ENABLED", "true")
    # None in sys.modules makes the import raise ImportError.
    monkeypatch.setitem(sys.modules, EXPORTER_MODULE, None)
    state = isolated_init()
    assert observability._initialized is True
    assert state["providers"] == []
    messages = [
        r.getMessage() for r in caplog.records if "Cloud Monitoring" in r.getMessage()
    ]
    assert len(messages) == 1, messages


def test_missing_credentials_do_not_break_startup(
    monkeypatch, fake_exporter_module, isolated_init, caplog
):
    def _no_credentials(**kwargs):
        raise RuntimeError("Could not automatically determine credentials")

    fake_exporter_module.CloudMonitoringMetricsExporter = _no_credentials
    monkeypatch.setenv("OTEL_CLOUD_MONITORING_ENABLED", "true")
    state = isolated_init(enable_prometheus=True)
    assert observability._initialized is True
    (provider,) = state["providers"]  # Prometheus still works
    assert _cloud_readers(state) == []
    assert "Cloud Monitoring" in caplog.text


def test_real_exporter_is_built_from_stubbed_credentials(monkeypatch):
    """The locked exporter package accepts the arguments, offline."""
    pytest.importorskip(EXPORTER_MODULE)
    import google.auth
    from google.auth.credentials import AnonymousCredentials
    from opentelemetry.exporter.cloud_monitoring import (
        CloudMonitoringMetricsExporter,
    )

    monkeypatch.setattr(
        google.auth,
        "default",
        lambda *a, **kw: (AnonymousCredentials(), "automaintainer-prod"),
    )
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "automaintainer-prod")
    monkeypatch.setattr(observability, "PeriodicExportingMetricReader", _FakeReader)

    reader = observability._cloud_monitoring_reader()

    assert isinstance(reader.exporter, CloudMonitoringMetricsExporter)
    assert reader.exporter.project_id == "automaintainer-prod"
    assert reader.exporter.unique_identifier is not None
    assert reader.exporter._prefix == observability.CLOUD_MONITORING_METRIC_PREFIX


# --- Celery worker processes ----------------------------------------------------


def test_worker_init_exports_worker_metrics_without_prometheus(
    monkeypatch, fake_exporter_module, isolated_init
):
    monkeypatch.setenv("OTEL_CLOUD_MONITORING_ENABLED", "true")
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    state = isolated_init()  # sets up the isolation; init runs again below
    state["providers"].clear()
    monkeypatch.setattr(observability, "_initialized", False)
    heartbeat = MagicMock()
    monkeypatch.setattr(observability, "register_worker_heartbeat", heartbeat)
    resources = []
    real_create = observability.Resource.create
    monkeypatch.setattr(
        observability.Resource,
        "create",
        lambda attrs: resources.append(attrs) or real_create(attrs),
    )

    observability.init_worker_observability()

    assert resources[-1][observability.SERVICE_NAME] == "automaintainer-worker"
    assert resources[-1][observability.DEPLOYMENT_ENVIRONMENT] == "production"
    (provider,) = state["providers"]
    # No Prometheus reader: a worker serves no /metrics endpoint.
    assert [type(r) for r in provider["metric_readers"]] == [_FakeReader]
    observability.PrometheusMetricReader.assert_not_called()
    heartbeat.assert_called_once_with()


def test_worker_hooks_initialise_each_child_process(monkeypatch):
    from celery.signals import worker_process_init, worker_process_shutdown

    init = MagicMock()
    shutdown = MagicMock()
    monkeypatch.setattr(observability, "init_worker_observability", init)
    monkeypatch.setattr(observability, "shutdown_observability", shutdown)
    try:
        observability.install_celery_worker_hooks()
        observability.install_celery_worker_hooks()  # idempotent
        worker_process_init.send(sender=None)
        worker_process_shutdown.send(sender=None, pid=1, exitcode=0)
    finally:
        worker_process_init.disconnect(dispatch_uid=observability._WORKER_INIT_UID)
        worker_process_shutdown.disconnect(
            dispatch_uid=observability._WORKER_SHUTDOWN_UID
        )
    init.assert_called_once_with()
    shutdown.assert_called_once_with()


# --- Metrics emitted through the record_* helpers ----------------------------


@pytest.fixture
def sdk(monkeypatch):
    """Point the module's instruments at an in-memory reader with the Views."""
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    provider = MeterProvider(
        metric_readers=[reader], views=observability.metric_views()
    )
    for name in observability._INSTRUMENT_GLOBALS:
        monkeypatch.setattr(observability, name, getattr(observability, name))
    observability.create_custom_metrics(meter=provider.get_meter("test"))
    monkeypatch.delenv("LLM_PRICES_JSON", raising=False)
    yield reader, provider
    provider.shutdown()


def _points(reader):
    data = reader.get_metrics_data()
    out = []
    for rm in data.resource_metrics if data else []:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                for point in metric.data.data_points:
                    out.append((metric.name, dict(point.attributes), point))
    return out


def _value(reader, name, attributes):
    for metric_name, attrs, point in _points(reader):
        if metric_name == name and attrs == attributes:
            return getattr(point, "value", None) if hasattr(point, "value") else point
    raise AssertionError((name, attributes, _points(reader)))


def test_run_outcome_and_duration_by_final_status(sdk):
    reader, _ = sdk
    observability.record_agent_run_complete("autonomous", "failed", 42.0)
    labels = {"mode": "autonomous", "outcome": "failed"}
    assert _value(reader, "agent_runs_total", labels) == 1
    duration = _value(reader, "agent_run_duration_seconds", labels)
    assert duration.count == 1 and duration.sum == 42.0


def test_a_timed_out_run_keeps_its_own_outcome(sdk):
    # tasks.py writes status "failed" for a run stopped by the soft time
    # limit; the metric keeps "timeout" apart instead of folding it into
    # "other", which the failure-rate alert would not count.
    reader, _ = sdk
    observability.record_agent_run_complete("autonomous", "timeout", 1800.0)
    labels = {"mode": "autonomous", "outcome": "timeout"}
    assert _value(reader, "agent_runs_total", labels) == 1
    assert _value(reader, "agent_run_duration_seconds", labels).count == 1


def test_run_duration_buckets_resolve_long_runs(sdk):
    reader, _ = sdk
    observability.record_agent_run_complete("autonomous", "completed", 1700.0)
    point = _value(
        reader,
        "agent_run_duration_seconds",
        {"mode": "autonomous", "outcome": "completed"},
    )
    # A p95 alert at 30 minutes needs a bucket edge near it; the SDK default
    # buckets jump from 1000 s to 2500 s.
    assert 1800 in point.explicit_bounds
    assert 900 in point.explicit_bounds


def test_llm_tokens_by_phase_and_kind_and_estimated_cost(sdk):
    reader, _ = sdk
    model = "llama-3.3-70b-versatile"
    observability.record_llm_tokens(model, 1_000_000, phase="implementer", kind="input")
    observability.record_llm_tokens(model, 500_000, phase="implementer", kind="output")

    # The existing per-model total is unchanged.
    assert _value(reader, "llm_tokens_total", {"model": model}) == 1_500_000
    base = {"model": model, "phase": "implementer"}
    assert (
        _value(reader, "llm_phase_tokens_total", {**base, "kind": "input"}) == 1_000_000
    )
    assert (
        _value(reader, "llm_phase_tokens_total", {**base, "kind": "output"}) == 500_000
    )
    cost = _value(reader, "llm_estimated_cost_usd_total", base)
    assert cost == pytest.approx(0.59 + 0.5 * 0.79)


def test_tokens_without_phase_or_kind_still_count(sdk):
    reader, _ = sdk
    observability.record_llm_tokens("llama-3.1-8b-instant", 10)
    assert (
        _value(
            reader,
            "llm_phase_tokens_total",
            {"model": "llama-3.1-8b-instant", "phase": "other", "kind": "total"},
        )
        == 10
    )
    # An unsplit total has no price: input and output are priced differently.
    names = {name for name, _, _ in _points(reader)}
    assert "llm_estimated_cost_usd_total" not in names


def test_unpriced_models_add_no_cost(sdk):
    reader, _ = sdk
    observability.record_llm_tokens("some-new-model", 1000, phase="pm", kind="input")
    names = {name for name, _, _ in _points(reader)}
    assert "llm_phase_tokens_total" in names
    assert "llm_estimated_cost_usd_total" not in names


def test_price_table_can_be_overridden_from_the_environment(sdk, monkeypatch):
    reader, _ = sdk
    monkeypatch.setenv(
        "LLM_PRICES_JSON",
        json.dumps({"some-new-model": {"input_per_mtok": 2.0, "output_per_mtok": 4.0}}),
    )
    observability.record_llm_tokens(
        "some-new-model", 1_000_000, phase="pm", kind="output"
    )
    # Built-in entries stay unless overridden.
    observability.record_llm_tokens(
        "llama-3.1-8b-instant", 1_000_000, phase="pm", kind="input"
    )
    assert _value(
        reader,
        "llm_estimated_cost_usd_total",
        {"model": "some-new-model", "phase": "pm"},
    ) == pytest.approx(4.0)
    assert _value(
        reader,
        "llm_estimated_cost_usd_total",
        {"model": "llama-3.1-8b-instant", "phase": "pm"},
    ) == pytest.approx(0.05)


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[1, 2]",
        json.dumps({"m": {"input_per_mtok": -1, "output_per_mtok": 1}}),
        json.dumps({"m": {"input_per_mtok": "1"}}),
    ],
)
def test_an_invalid_price_override_is_logged_once_and_ignored(
    sdk, monkeypatch, caplog, raw
):
    reader, _ = sdk
    observability._parse_llm_prices.cache_clear()
    monkeypatch.setenv("LLM_PRICES_JSON", raw)
    for _ in range(3):
        observability.record_llm_tokens(
            "llama-3.1-8b-instant", 1_000_000, phase="pm", kind="input"
        )
    assert _value(
        reader,
        "llm_estimated_cost_usd_total",
        {"model": "llama-3.1-8b-instant", "phase": "pm"},
    ) == pytest.approx(3 * 0.05)
    warnings = [r for r in caplog.records if "LLM_PRICES_JSON" in r.getMessage()]
    assert len(warnings) == 1


def test_worker_heartbeat_reports_one_per_process(sdk):
    reader, provider = sdk
    observability.register_worker_heartbeat(meter=provider.get_meter("hb"))
    assert _value(reader, "celery_worker_heartbeat", {}) == 1


# --- Cardinality: the label allowlist covers the new metrics ----------------


def test_new_metrics_are_catalogued_with_bounded_labels():
    labels = observability.all_metric_labels()
    assert labels["llm_phase_tokens_total"] == {"model", "phase", "kind"}
    assert labels["llm_estimated_cost_usd_total"] == {"model", "phase"}
    assert labels["celery_worker_heartbeat"] == frozenset()
    view_names = {v._instrument_name for v in observability.metric_views()}
    assert set(labels) <= view_names


def _noise(n=12):
    # Always contains "/ ", so it can never pass as an identifier.
    return "/ " + "".join(random.choice(string.printable) for _ in range(n))


def test_unbounded_inputs_cannot_grow_the_series_count(sdk):
    reader, _ = sdk
    rng = random.Random(7)
    random.seed(7)
    phases = ["architect", "implementer", "../../etc", "run 3f1c9a2e failed"]
    kinds = ["input", "output", "total", "prompt; DROP TABLE"]
    for i in range(300):
        observability.record_llm_tokens(
            rng.choice(["llama-3.3-70b-versatile", _noise()]),
            5,
            phase=rng.choice(phases + [_noise()]),
            kind=rng.choice(kinds + [_noise()]),
        )
        observability.record_agent_run_complete(_noise(), _noise(), 1.0)

    allowed = observability.all_metric_labels()
    series = {}
    for name, attrs, _ in _points(reader):
        assert set(attrs) <= allowed[name], (name, attrs)
        series.setdefault(name, set()).add(tuple(sorted(attrs.items())))
        for key, value in attrs.items():
            if key == "phase":
                assert value in observability.LLM_PHASES | {"other"}
            if key == "kind":
                assert value in observability.LLM_TOKEN_KINDS | {"other"}
    # model is "llama-3.3-70b-versatile" or "other" (noise contains spaces or
    # punctuation and fails the identifier rule).
    assert len(series["llm_phase_tokens_total"]) <= 2 * 3 * 4
    assert len(series["llm_estimated_cost_usd_total"]) <= 3
    assert len(series["agent_runs_total"]) == 1
