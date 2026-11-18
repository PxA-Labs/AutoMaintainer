"""The alerting metrics are recorded by the server, end to end (#527).

monitoring.tf alerts on agent_runs_total (failure rate),
agent_run_duration_seconds (p95), llm_estimated_cost_usd_total (cost per day)
and celery_worker_heartbeat. These tests run a fake agent run through the
real Celery task body (tasks._execute_agent_run) with an in-memory metric
reader and check that each of them is written:

- run count and duration: recorded by tasks.py when the run ends;
- tokens and estimated cost: recorded by telemetry_metrics.MetricsEmitter,
  the server-side consumer of the engine's TelemetryEvents. The SDK wheel
  (backend/automaintainer) never imports observability.

The heartbeat is covered by test_celery_worker_observability.py.
No Redis, Supabase, GitHub or LLM is used.
"""

import asyncio
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("opentelemetry.sdk.metrics")

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import agents  # noqa: E402
import observability  # noqa: E402
import redis_locks  # noqa: E402
import tasks  # noqa: E402
import telemetry_metrics  # noqa: E402
from automaintainer._runtime.events import (  # noqa: E402
    AgentPhase,
    EventEmitter,
    NodeTransitionEvent,
    SupabaseEmitter,
    TelemetryEvent,
)

MODEL = "llama-3.3-70b-versatile"
RUN_KWARGS = dict(
    run_id="run-1",
    repo_name="org/repo",
    org_id="org-1",
    user_id="user-1",
    repository_id=42,
    github_installation_id=99,
)


# --- In-memory metrics -----------------------------------------------------


@pytest.fixture
def sdk(monkeypatch):
    """Point observability's instruments at an in-memory reader with the Views."""
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
    yield reader
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


def _point(reader, name, attributes):
    for metric_name, attrs, point in _points(reader):
        if metric_name == name and attrs == attributes:
            return point
    raise AssertionError((name, attributes, _points(reader)))


def _names(reader):
    return {name for name, _, _ in _points(reader)}


# --- A fake run through the real task body ---------------------------------


class _FakeRedis:
    def __init__(self):
        self.store = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def eval(self, script, numkeys, key, value):
        owned = self.store.get(key) == value
        if script == redis_locks._RELEASE_SCRIPT and owned:
            del self.store[key]
        return 1 if owned else 0

    def exists(self, key):
        return int(key in self.store)


class _Recorder(EventEmitter):
    def __init__(self):
        self.events = []

    async def emit(self, event):
        self.events.append(event)


@pytest.fixture
def fake_run(monkeypatch):
    """tasks._execute_agent_run with DB, Redis and the agent loop faked.

    The fake loop does what the engine does with the ``emitter`` argument
    (agents._run_pipeline binds it to current_emitter) and then emits the
    TelemetryEvents of two LLM calls through agents.emit_event, the call
    the nodes make.
    """
    state = SimpleNamespace(
        statuses={"run-1": "queued"},
        result={"status": "completed", "summary": "ok"},
        exc=None,
        emitters=[],
        clock=[1000.0],
    )
    monkeypatch.setattr(redis_locks, "_redis_client", _FakeRedis())
    monkeypatch.setattr(tasks, "_fetch_run_status", lambda rid: state.statuses[rid])

    async def fake_update(run_id, status, **kwargs):
        state.statuses[run_id] = status

    async def noop(*a, **k):
        return None

    async def fake_loop(repo_name, target_issue=None, run_id=None, **kwargs):
        emitter = kwargs.get("emitter")
        state.emitters.append(emitter)
        token = agents.current_emitter.set(emitter)
        try:
            await agents.emit_event(
                TelemetryEvent(
                    run_id=run_id,
                    phase=AgentPhase.IMPLEMENTER,
                    latency_ms=1200,
                    tokens_used=1_000_000,
                    model_name=MODEL,
                )
            )
            await agents.emit_event(
                TelemetryEvent(
                    run_id=run_id,
                    phase=AgentPhase.MAINTAINER,
                    latency_ms=300,
                    tokens_used=2_000,
                    model_name=MODEL,
                )
            )
            await agents.emit_event(NodeTransitionEvent(run_id=run_id, to_node="pm"))
        finally:
            agents.current_emitter.reset(token)
        state.clock[0] += 125.0  # the run "takes" 125 s
        if state.exc is not None:
            raise state.exc
        return state.result

    monkeypatch.setattr(tasks, "update_run_status", fake_update)
    monkeypatch.setattr(tasks, "record_usage_event", noop)
    monkeypatch.setattr(tasks, "log_run_event", noop)
    monkeypatch.setattr(tasks, "write_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "run_agent_loop", fake_loop)
    monkeypatch.setattr(tasks.time, "monotonic", lambda: state.clock[0])
    # The forwarded events go to the Supabase emitter as before; with no
    # database configured it drops them.
    monkeypatch.setattr(agents, "get_supabase", lambda: None)
    return state


class _Task:
    def __init__(self):
        self.request = SimpleNamespace(id="celery-1", retries=0)

    def retry(self, **kwargs):  # pragma: no cover - never reached here
        raise AssertionError("retried")


def _run(**overrides):
    return tasks._execute_agent_run(_Task(), **{**RUN_KWARGS, **overrides})


def test_a_completed_run_writes_every_alerting_metric(sdk, fake_run):
    reader = sdk
    assert _run()["status"] == "completed"

    run = {"mode": "autonomous", "outcome": "completed"}
    assert _point(reader, "agent_runs_total", run).value == 1
    duration = _point(reader, "agent_run_duration_seconds", run)
    assert duration.count == 1 and duration.sum == 125.0

    # Tokens by the phase carried on each TelemetryEvent. The engine reports
    # one unsplit count per call, hence kind "total".
    implementer = {"model": MODEL, "phase": "implementer"}
    maintainer = {"model": MODEL, "phase": "maintainer"}
    tokens = "llm_phase_tokens_total"
    assert _point(reader, tokens, {**implementer, "kind": "total"}).value == 1_000_000
    assert _point(reader, tokens, {**maintainer, "kind": "total"}).value == 2_000
    assert _point(reader, "llm_tokens_total", {"model": MODEL}).value == 1_002_000

    # Estimated cost: an unsplit count is priced at the model's higher
    # (output) rate, an upper bound, so the cost alert errs towards firing.
    cost = "llm_estimated_cost_usd_total"
    assert _point(reader, cost, implementer).value == pytest.approx(0.79)
    assert _point(reader, cost, maintainer).value == pytest.approx(0.00158)


@pytest.mark.parametrize(
    "result,exc,outcome",
    [
        ({"status": "failed", "error": "boom"}, None, "failed"),
        ({"status": "cancelled"}, None, "cancelled"),
        ("not a dict", None, "failed"),
        (None, RuntimeError("crash"), "failed"),
        (None, tasks.SoftTimeLimitExceeded(), "timeout"),
    ],
)
def test_every_way_a_run_ends_is_counted_once(sdk, fake_run, result, exc, outcome):
    fake_run.result, fake_run.exc = result, exc
    if exc is not None:
        with pytest.raises(type(exc)):
            _run(mode="targeted_issue")
    else:
        _run(mode="targeted_issue")
    runs = [
        (attrs, point.value)
        for name, attrs, point in _points(sdk)
        if name == "agent_runs_total"
    ]
    assert runs == [({"mode": "targeted_issue", "outcome": outcome}, 1)]
    duration = _point(
        sdk,
        "agent_run_duration_seconds",
        {"mode": "targeted_issue", "outcome": outcome},
    )
    assert duration.sum == 125.0


def test_a_skipped_delivery_is_not_a_run(sdk, fake_run):
    fake_run.statuses["run-1"] = "completed"
    assert _run()["skipped"] is True
    assert "agent_runs_total" not in _names(sdk)
    assert fake_run.emitters == []


def test_a_run_that_never_got_the_repository_lock_is_not_counted(
    sdk, fake_run, monkeypatch
):
    # It never started, so it has no duration; the row is marked failed
    # with a "Repository is busy" message instead.
    monkeypatch.setattr(tasks, "acquire_repo_lock", lambda *a: False)
    task = _Task()
    task.request.retries = tasks.LOCK_WAIT_MAX_RETRIES
    assert tasks._execute_agent_run(task, **RUN_KWARGS)["status"] == "failed"
    assert "agent_runs_total" not in _names(sdk)


def test_a_metrics_failure_never_fails_the_run(fake_run, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("exporter down")

    monkeypatch.setattr(tasks, "record_agent_run_complete", broken)
    monkeypatch.setattr(telemetry_metrics, "record_llm_tokens", broken)
    assert _run()["status"] == "completed"
    assert fake_run.statuses["run-1"] == "completed"


def test_the_real_run_agent_loop_accepts_the_emitter():
    # _agent_loop_kwargs drops arguments run_agent_loop does not take; the
    # token metrics depend on the real signature keeping ``emitter``.
    assert tasks.run_agent_loop is agents.run_agent_loop
    emitter = telemetry_metrics.MetricsEmitter()
    kwargs = tasks._agent_loop_kwargs(repo_name="o/r", emitter=emitter)
    assert kwargs["emitter"] is emitter


# --- The TelemetryEvent consumer -------------------------------------------


def test_the_consumer_forwards_every_event_to_the_wrapped_emitter(sdk):
    inner = _Recorder()
    emitter = telemetry_metrics.MetricsEmitter(inner)
    telemetry = TelemetryEvent(
        run_id="r", phase="pm", latency_ms=1, tokens_used=10, model_name=MODEL
    )
    transition = NodeTransitionEvent(run_id="r", to_node="pm")
    asyncio.run(emitter.emit(telemetry))
    asyncio.run(emitter.emit(transition))
    assert inner.events == [telemetry, transition]
    point = _point(
        sdk, "llm_phase_tokens_total", {"model": MODEL, "phase": "pm", "kind": "total"}
    )
    assert point.value == 10


def test_the_default_wrapped_emitter_is_the_supabase_one():
    # What the engine uses when tasks.py passes no emitter, so the run's
    # events still reach the logs table.
    assert isinstance(telemetry_metrics.MetricsEmitter().inner, SupabaseEmitter)


def test_zero_tokens_and_unknown_phases_add_no_series_growth(sdk):
    emitter = telemetry_metrics.MetricsEmitter(_Recorder())
    asyncio.run(
        emitter.emit(
            TelemetryEvent(run_id="r", latency_ms=1, tokens_used=0, model_name=MODEL)
        )
    )
    asyncio.run(
        emitter.emit(
            TelemetryEvent(
                run_id="r",
                phase="a free-text phase",
                latency_ms=1,
                tokens_used=5,
                model_name="unpriced-model",
            )
        )
    )
    tokens = {
        tuple(sorted(attrs.items()))
        for name, attrs, _ in _points(sdk)
        if name == "llm_phase_tokens_total"
    }
    assert tokens == {
        (("kind", "total"), ("model", "unpriced-model"), ("phase", "other"))
    }
    # A model without a price adds tokens but no cost.
    assert "llm_estimated_cost_usd_total" not in _names(sdk)


def test_the_sdk_wheel_never_imports_observability():
    imports = re.compile(
        r"^\s*(?:from\s+(?:observability|telemetry_metrics)\b"
        r"|import\s+(?:observability|telemetry_metrics)\b)",
        re.M,
    )
    package = BACKEND / "automaintainer"
    offenders = [
        str(p.relative_to(BACKEND))
        for p in package.rglob("*.py")
        if imports.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []
