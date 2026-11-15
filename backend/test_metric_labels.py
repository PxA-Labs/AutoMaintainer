"""Metrics carry only bounded labels (#404).

repo_name, run_id, user ids, raw paths, Host headers and error messages each
create one time series per distinct value, so they must never be metric
labels. ``observability.METRIC_LABELS`` is the catalogue of every exported
metric and its complete label set; these tests pin it, check that the record
helpers emit exactly those labels with bounded values, and (with the
OpenTelemetry SDK installed) that the Views drop anything else, including the
labels instrumentation libraries add.
"""

from pathlib import Path

import pytest

import observability

EXPECTED_LABELS = {
    "agent_runs_total": {"mode", "outcome"},
    "agent_run_duration_seconds": {"mode", "outcome"},
    "agent_runs_active": {"mode"},
    "llm_tokens_total": {"model"},
    "llm_request_duration_seconds": {"model", "outcome"},
    "github_api_calls_total": {"operation", "outcome"},
    "celery_queue_depth": {"queue"},
    "celery_task_duration_seconds": {"task_name", "queue", "outcome"},
}
HTTP_SERVER = {
    "http.method",
    "http.status_code",
    "http.target",
    "http.request.method",
    "http.response.status_code",
    "http.route",
}
HTTP_CLIENT = {
    "http.method",
    "http.status_code",
    "net.peer.name",
    "http.request.method",
    "http.response.status_code",
    "server.address",
}
EXPECTED_LABELS.update(
    {
        "http.server.duration": HTTP_SERVER,
        "http.server.request.duration": HTTP_SERVER,
        "http.server.request.size": HTTP_SERVER,
        "http.server.request.body.size": HTTP_SERVER,
        "http.server.response.size": HTTP_SERVER,
        "http.server.response.body.size": HTTP_SERVER,
        "http.server.active_requests": {"http.method", "http.request.method"},
        "http.client.duration": HTTP_CLIENT,
        "http.client.request.duration": HTTP_CLIENT,
    }
)
FORBIDDEN = {
    "repo",
    "repo_name",
    "run_id",
    "user_id",
    "org_id",
    "installation_id",
    "error",
    "message",
    "path",
    "url",
    "http.url",
    "http.server_name",
    "http.host",
}

RUN_ID = "3f1c9a2e-0b7d-4c55-9a51-2f3c1e9d8b11"
REPO = "acme/secret-repo"
NOISE = "Traceback: /repos/acme/secret-repo/pulls/7 failed"


def test_catalogue_is_exactly_the_documented_label_sets():
    actual = {name: set(labels) for name, labels in observability.METRIC_LABELS.items()}
    assert actual == EXPECTED_LABELS
    for name, labels in actual.items():
        assert not labels & FORBIDDEN, name


def test_catalogue_is_documented():
    doc = (
        Path(__file__).resolve().parent.parent / "docs" / "OBSERVABILITY.md"
    ).read_text(encoding="utf-8")
    for name, labels in observability.METRIC_LABELS.items():
        assert f"`{name}`" in doc, name
        for label in labels:
            assert f"`{label}`" in doc, (name, label)


class _Recorder:
    def __init__(self, name, calls):
        self.name, self.calls = name, calls

    def _rec(self, value, attributes=None):
        self.calls.append((self.name, dict(attributes or {})))

    add = record = set = _rec


# observability module attribute -> exported metric name
CUSTOM_METRICS = {
    "agent_runs_total": "agent_runs_total",
    "agent_run_duration": "agent_run_duration_seconds",
    "agent_run_active": "agent_runs_active",
    "llm_tokens_total": "llm_tokens_total",
    "llm_request_duration": "llm_request_duration_seconds",
    "github_api_calls_total": "github_api_calls_total",
    "celery_queue_depth": "celery_queue_depth",
    "celery_task_duration": "celery_task_duration_seconds",
}


@pytest.fixture
def recorded(monkeypatch):
    calls = []
    for attr, name in CUSTOM_METRICS.items():
        monkeypatch.setattr(observability, attr, _Recorder(name, calls))
    return calls


def _emit_everything(mode, status, model, operation, queue, task):
    observability.record_agent_run_complete(mode, status, 1.5)
    observability.record_llm_tokens(model, 10)
    observability.record_llm_request(model, 0.2, False)
    observability.record_github_api_call(operation, True)
    observability.set_celery_queue_depth(queue, 3)
    observability.record_celery_task_duration(task, queue, 0.1, True)
    with observability.trace_agent_execution(mode, REPO, run_id=RUN_ID):
        pass


def test_helpers_emit_exactly_the_catalogued_labels(recorded):
    _emit_everything(
        "targeted_issue", "completed", "llama-3.3-70b", "get_pull", "agents", "run"
    )
    assert {name for name, _ in recorded} == set(CUSTOM_METRICS.values())
    for name, attributes in recorded:
        assert set(attributes) == EXPECTED_LABELS[name], name
    assert ("agent_runs_total", {"mode": "targeted_issue", "outcome": "completed"}) in (
        recorded
    )
    assert (
        "github_api_calls_total",
        {"operation": "get_pull", "outcome": "success"},
    ) in (recorded)


def test_unbounded_inputs_collapse_to_other(recorded):
    # Request-controlled or free-text values must not become new series.
    _emit_everything(RUN_ID + "-mode", NOISE, NOISE, "/repos/acme/x", NOISE, NOISE)
    values = {v for _, attributes in recorded for v in attributes.values()}
    assert values <= {"other", "success", "failure"}
    for _, attributes in recorded:
        for value in attributes.values():
            assert REPO not in value and RUN_ID not in value


# --- With the OpenTelemetry SDK: Views enforce the catalogue -----------------


@pytest.fixture
def sdk_reader():
    pytest.importorskip("opentelemetry.sdk.metrics")
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    provider = MeterProvider(
        metric_readers=[reader], views=observability.metric_views()
    )
    yield reader, provider
    provider.shutdown()


def _points(reader):
    data = reader.get_metrics_data()
    return [
        (metric.name, dict(point.attributes))
        for rm in (data.resource_metrics if data else [])
        for sm in rm.scope_metrics
        for metric in sm.metrics
        for point in metric.data.data_points
    ]


def test_views_drop_uncatalogued_labels(sdk_reader):
    reader, provider = sdk_reader
    counter = provider.get_meter("t").create_counter("agent_runs_total")
    counter.add(
        1,
        {
            "mode": "autonomous",
            "outcome": "failed",
            "repo_name": REPO,
            "run_id": RUN_ID,
        },
    )
    assert _points(reader) == [
        ("agent_runs_total", {"mode": "autonomous", "outcome": "failed"})
    ]


def test_http_server_metrics_use_route_templates_only(sdk_reader):
    pytest.importorskip("opentelemetry.instrumentation.fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    reader, provider = sdk_reader
    app = FastAPI()

    @app.get("/runs/{run_id}")
    def get_run(run_id: str):
        return {"id": run_id}

    FastAPIInstrumentor.instrument_app(app, meter_provider=provider)
    try:
        client = TestClient(app)
        client.get(f"/runs/{RUN_ID}?token=t", headers={"Host": "attacker-1.example"})
        client.get(f"/no/such/{RUN_ID}", headers={"Host": "attacker-2.example"})
    finally:
        FastAPIInstrumentor.uninstrument_app(app)

    points = _points(reader)
    assert {name for name, _ in points} >= {
        "http.server.duration",
        "http.server.active_requests",
    }
    targets = set()
    for name, attributes in points:
        assert name in observability.METRIC_LABELS, name
        assert set(attributes) <= observability.METRIC_LABELS[name], name
        for value in attributes.values():
            assert RUN_ID not in str(value) and "attacker" not in str(value)
        for key in ("http.target", "http.route"):
            if key in attributes:
                targets.add(attributes[key])
    # The matched request is labelled with its template; the unmatched one
    # carries no route label at all (never its raw path).
    assert targets == {"/runs/{run_id}"}
