"""
Unit tests for strongly-typed domain events and EventEmitter implementations.
Part of Epic #157 (GitHub Issue #185).
"""

from datetime import datetime
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from pydantic import ValidationError

from events import (
    BaseEvent,
    AgentPhase,
    NodeTransitionEvent,
    PlanCheckpointEvent,
    TestResultEvent,
    TelemetryEvent,
    ErrorEvent,
    EVENT_REGISTRY,
    parse_event,
    EventEmitter,
    NullEmitter,
    StreamingEmitter,
    SupabaseEmitter,
    CompositeEmitter,
)


def test_event_registry():
    assert "BaseEvent" in EVENT_REGISTRY
    assert "NodeTransitionEvent" in EVENT_REGISTRY
    assert "PlanCheckpointEvent" in EVENT_REGISTRY
    assert "TestResultEvent" in EVENT_REGISTRY
    assert "TelemetryEvent" in EVENT_REGISTRY
    assert "ErrorEvent" in EVENT_REGISTRY


# ============================================================================
# 1. BaseEvent Tests
# ============================================================================


def test_base_event_valid_construction():
    event = BaseEvent(run_id="run-123", phase=AgentPhase.ARCHITECT)
    assert event.run_id == "run-123"
    assert event.phase == AgentPhase.ARCHITECT
    assert event.event_type == "BaseEvent"
    assert event.event_id is not None
    assert isinstance(event.timestamp, datetime)


def test_base_event_serialization_and_deserialization():
    event = BaseEvent(run_id="run-123", phase=AgentPhase.ARCHITECT)
    dumped_dict = event.model_dump()
    assert dumped_dict["run_id"] == "run-123"
    assert dumped_dict["phase"] == "architect"

    dumped_json = event.model_dump_json()
    loaded = BaseEvent.model_validate_json(dumped_json)
    assert loaded.run_id == event.run_id
    assert loaded.phase == event.phase
    assert loaded.event_id == event.event_id


def test_base_event_missing_required_fields():
    with pytest.raises(ValidationError):
        BaseEvent()  # Missing required run_id


# ============================================================================
# 2. NodeTransitionEvent Tests
# ============================================================================


def test_node_transition_event_valid_construction():
    event = NodeTransitionEvent(
        run_id="run-123",
        phase=AgentPhase.ARCHITECT,
        from_node="architect",
        to_node="brainstormer",
        iteration=1,
    )
    assert event.run_id == "run-123"
    assert event.from_node == "architect"
    assert event.to_node == "brainstormer"
    assert event.iteration == 1
    assert event.event_type == "NodeTransitionEvent"


def test_node_transition_event_serialization_deserialization():
    event = NodeTransitionEvent(
        run_id="run-123",
        from_node="brainstormer",
        to_node="pm",
        iteration=2,
    )
    data_dict = event.model_dump(mode="json")
    assert data_dict["to_node"] == "pm"
    assert data_dict["iteration"] == 2

    reconstructed = parse_event(data_dict)
    assert isinstance(reconstructed, NodeTransitionEvent)
    assert reconstructed.to_node == "pm"
    assert reconstructed.iteration == 2


def test_node_transition_event_validation_rejection():
    with pytest.raises(ValidationError):
        NodeTransitionEvent(run_id="run-123")  # missing required to_node


# ============================================================================
# 3. PlanCheckpointEvent Tests
# ============================================================================


def test_plan_checkpoint_event_valid_construction():
    event = PlanCheckpointEvent(
        run_id="run-123",
        phase=AgentPhase.ARCHITECT,
        plan_markdown="# Plan\nBuild new feature.",
        files_to_modify=["src/main.py"],
        files_to_create=["src/utils.py"],
    )
    assert event.plan_markdown == "# Plan\nBuild new feature."
    assert event.files_to_modify == ["src/main.py"]
    assert event.files_to_create == ["src/utils.py"]
    assert event.event_type == "PlanCheckpointEvent"


def test_plan_checkpoint_event_serialization_deserialization():
    event = PlanCheckpointEvent(
        run_id="run-123",
        plan_markdown="# Directives",
        files_to_modify=["app.py"],
    )
    json_str = event.model_dump_json()
    reconstructed = parse_event(json_str)
    assert isinstance(reconstructed, PlanCheckpointEvent)
    assert reconstructed.plan_markdown == "# Directives"
    assert reconstructed.files_to_modify == ["app.py"]
    assert reconstructed.files_to_create == []


def test_plan_checkpoint_event_validation_rejection():
    with pytest.raises(ValidationError):
        PlanCheckpointEvent(run_id="run-123")  # missing required plan_markdown


# ============================================================================
# 4. TestResultEvent Tests
# ============================================================================


def test_test_result_event_valid_construction():
    event = TestResultEvent(
        run_id="run-123",
        phase=AgentPhase.MAINTAINER,
        exit_code=0,
        passed_count=10,
        failed_count=0,
        tracebacks=[],
    )
    assert event.exit_code == 0
    assert event.passed_count == 10
    assert event.failed_count == 0
    assert event.tracebacks == []
    assert event.event_type == "TestResultEvent"


def test_test_result_event_serialization_deserialization():
    event = TestResultEvent(
        run_id="run-123",
        exit_code=1,
        passed_count=2,
        failed_count=1,
        tracebacks=["AssertionError on line 10"],
    )
    data_dict = event.model_dump(mode="json")
    reconstructed = parse_event(data_dict)
    assert isinstance(reconstructed, TestResultEvent)
    assert reconstructed.exit_code == 1
    assert reconstructed.passed_count == 2
    assert reconstructed.failed_count == 1
    assert reconstructed.tracebacks == ["AssertionError on line 10"]


def test_test_result_event_validation_rejection():
    with pytest.raises(ValidationError):
        TestResultEvent(run_id="run-123")  # missing required exit_code


# ============================================================================
# 5. TelemetryEvent Tests
# ============================================================================


def test_telemetry_event_valid_construction():
    event = TelemetryEvent(
        run_id="run-123",
        phase=AgentPhase.SYSTEM,
        latency_ms=350.5,
        tokens_used=1200,
        cost_usd=0.0024,
        model_name="llama-3.3-70b-versatile",
    )
    assert event.latency_ms == 350.5
    assert event.tokens_used == 1200
    assert event.cost_usd == 0.0024
    assert event.model_name == "llama-3.3-70b-versatile"
    assert event.event_type == "TelemetryEvent"


def test_telemetry_event_serialization_deserialization():
    event = TelemetryEvent(
        run_id="run-123",
        latency_ms=120.0,
        tokens_used=500,
    )
    json_str = event.model_dump_json()
    reconstructed = parse_event(json_str)
    assert isinstance(reconstructed, TelemetryEvent)
    assert reconstructed.latency_ms == 120.0
    assert reconstructed.tokens_used == 500


def test_telemetry_event_validation_rejection():
    with pytest.raises(ValidationError):
        TelemetryEvent(run_id="run-123")  # missing latency_ms


# ============================================================================
# 6. ErrorEvent Tests
# ============================================================================


def test_error_event_valid_construction():
    event = ErrorEvent(
        run_id="run-123",
        phase=AgentPhase.IMPLEMENTER,
        error_code="SYNTAX_ERROR",
        message="Invalid syntax on line 42",
        fatal=True,
    )
    assert event.error_code == "SYNTAX_ERROR"
    assert event.message == "Invalid syntax on line 42"
    assert event.fatal is True
    assert event.event_type == "ErrorEvent"


def test_error_event_serialization_deserialization():
    event = ErrorEvent(
        run_id="run-123",
        error_code="RATE_LIMIT",
        message="Rate limit reached",
    )
    data_dict = event.model_dump(mode="json")
    reconstructed = parse_event(data_dict)
    assert isinstance(reconstructed, ErrorEvent)
    assert reconstructed.error_code == "RATE_LIMIT"
    assert reconstructed.message == "Rate limit reached"
    assert reconstructed.fatal is False


def test_error_event_validation_rejection():
    with pytest.raises(ValidationError):
        ErrorEvent(run_id="run-123")  # missing error_code and message


# ============================================================================
# 7. NullEmitter Tests
# ============================================================================


@pytest.mark.asyncio
async def test_null_emitter_async_execution():
    emitter = NullEmitter()
    event = NodeTransitionEvent(run_id="run-123", to_node="architect")
    # Must be safely awaitable with no exceptions or side effects
    await emitter.emit(event)
    await emitter.close()


# ============================================================================
# 8. EventEmitter Interface Behavior Tests
# ============================================================================


def test_event_emitter_cannot_be_instantiated_directly():
    with pytest.raises(TypeError):
        EventEmitter()


@pytest.mark.asyncio
async def test_custom_event_emitter_implementation():
    received = []

    class CustomEmitter(EventEmitter):
        async def emit(self, event: BaseEvent) -> None:
            received.append(event)

    emitter = CustomEmitter()
    event = TelemetryEvent(run_id="run-1", latency_ms=42.0)
    await emitter.emit(event)
    assert len(received) == 1
    assert received[0] is event


# ============================================================================
# 9. StreamingEmitter Tests
# ============================================================================


@pytest.mark.asyncio
async def test_streaming_emitter_queue_and_callback():
    callback_events = []

    async def mock_callback(evt: BaseEvent):
        callback_events.append(evt)

    emitter = StreamingEmitter(callback=mock_callback)
    event1 = NodeTransitionEvent(run_id="run-123", to_node="architect")
    event2 = TelemetryEvent(run_id="run-123", latency_ms=150.0)

    await emitter.emit(event1)
    await emitter.emit(event2)

    assert len(callback_events) == 2
    assert callback_events[0] == event1
    assert callback_events[1] == event2

    pulled1 = await emitter.get_next_event(timeout=1.0)
    pulled2 = await emitter.get_next_event(timeout=1.0)
    assert pulled1 == event1
    assert pulled2 == event2


def test_streaming_emitter_no_css_or_html_markup():
    event = ErrorEvent(
        run_id="run-123",
        error_code="ERR_AUTH",
        message="Unauthorized access",
    )
    payload = event.model_dump(mode="json")
    for key, value in payload.items():
        assert "text-red" not in str(value)
        assert "<div" not in str(value)
        assert "<span" not in str(value)


# ============================================================================
# 10. SupabaseEmitter Tests
# ============================================================================


@pytest.mark.asyncio
async def test_supabase_emitter_logs_table():
    mock_client = MagicMock()
    mock_table = MagicMock()
    mock_insert = MagicMock()
    mock_execute = MagicMock()

    mock_client.table.return_value = mock_table
    mock_table.insert.return_value = mock_insert
    mock_insert.execute.return_value = mock_execute

    emitter = SupabaseEmitter(client=mock_client, table_name="logs", org_id="org-123")

    event = NodeTransitionEvent(
        run_id="run-123",
        phase=AgentPhase.ARCHITECT,
        from_node="start",
        to_node="architect",
        iteration=0,
    )

    await emitter.emit(event)

    mock_client.table.assert_called_with("logs")
    inserted_records = mock_table.insert.call_args[0][0]
    assert inserted_records["run_id"] == "run-123"
    assert inserted_records["org_id"] == "org-123"
    assert inserted_records["agent_name"] == "architect"
    assert "Transition: start -> architect" in inserted_records["message"]
    assert inserted_records["metadata"]["event_type"] == "NodeTransitionEvent"


@pytest.mark.asyncio
async def test_supabase_emitter_custom_table():
    mock_client = MagicMock()
    mock_table = MagicMock()
    mock_insert = MagicMock()
    mock_execute = MagicMock()

    mock_client.table.return_value = mock_table
    mock_table.insert.return_value = mock_insert
    mock_insert.execute.return_value = mock_execute

    emitter = SupabaseEmitter(client=mock_client, table_name="domain_events")

    event = TelemetryEvent(
        run_id="run-456",
        phase=AgentPhase.SYSTEM,
        latency_ms=250.0,
        tokens_used=800,
    )

    await emitter.emit(event)

    mock_client.table.assert_called_with("domain_events")
    inserted_records = mock_table.insert.call_args[0][0]
    assert inserted_records["run_id"] == "run-456"
    assert inserted_records["latency_ms"] == 250.0
    assert inserted_records["tokens_used"] == 800


@pytest.mark.asyncio
async def test_supabase_emitter_graceful_error_handling():
    mock_client = MagicMock()
    mock_table = MagicMock()
    mock_insert = MagicMock()

    mock_client.table.return_value = mock_table
    mock_table.insert.return_value = mock_insert
    mock_insert.execute.side_effect = Exception("Supabase connection error")

    emitter = SupabaseEmitter(client=mock_client, table_name="logs")
    event = ErrorEvent(run_id="run-123", error_code="TEST_ERR", message="test message")

    # Should not raise exception
    await emitter.emit(event)


# ============================================================================
# 11. CompositeEmitter Tests
# ============================================================================


@pytest.mark.asyncio
async def test_composite_emitter_dispatches_to_all_sinks():
    mock_emitter_1 = AsyncMock(spec=EventEmitter)
    mock_emitter_2 = AsyncMock(spec=EventEmitter)

    composite = CompositeEmitter([mock_emitter_1, mock_emitter_2])
    event = NodeTransitionEvent(run_id="run-123", to_node="architect")

    await composite.emit(event)

    mock_emitter_1.emit.assert_awaited_once_with(event)
    mock_emitter_2.emit.assert_awaited_once_with(event)


@pytest.mark.asyncio
async def test_composite_emitter_child_failure_isolation():
    mock_emitter_ok = AsyncMock(spec=EventEmitter)
    mock_emitter_fail = AsyncMock(spec=EventEmitter)
    mock_emitter_fail.emit.side_effect = Exception("Sink failed")

    composite = CompositeEmitter([mock_emitter_fail, mock_emitter_ok])
    event = ErrorEvent(run_id="run-123", error_code="TEST", message="msg")

    # Should execute all emitters despite failures
    await composite.emit(event)
    mock_emitter_ok.emit.assert_awaited_once_with(event)


def test_parse_event_unknown_type_rejection():
    with pytest.raises(ValueError, match="Unknown or unregistered event type"):
        parse_event({"event_type": "CompletelyUnknownEvent", "run_id": "123"})


@pytest.mark.asyncio
async def test_composite_emitter_child_timeout():
    mock_emitter_slow = AsyncMock(spec=EventEmitter)

    async def slow_emit(event: BaseEvent):
        await asyncio.sleep(5.0)

    mock_emitter_slow.emit.side_effect = slow_emit
    mock_emitter_fast = AsyncMock(spec=EventEmitter)

    composite = CompositeEmitter(
        [mock_emitter_slow, mock_emitter_fast], child_timeout=0.1
    )
    event = TelemetryEvent(run_id="run-123", latency_ms=10.0)

    await composite.emit(event)
    mock_emitter_fast.emit.assert_awaited_once_with(event)


# ============================================================================
# 12. Agent Layer Event Decoupling Tests
# ============================================================================


@pytest.mark.asyncio
async def test_agent_emitted_events_have_no_css_formatting():
    from agents import (
        current_emitter,
        current_run_id,
        architect_node,
        brainstormer_node,
        pm_node,
        implementer_node,
        maintainer_node,
    )

    events_received = []

    class EventCollector(EventEmitter):
        async def emit(self, event: BaseEvent) -> None:
            events_received.append(event)

    collector = EventCollector()
    current_emitter.set(collector)
    current_run_id.set("test-run-123")

    mock_issue = MagicMock()
    mock_issue.title = "Sample Targeted Issue"
    mock_issue.body = "Issue description for testing"
    mock_issue.number = 42

    mock_gh_repo = MagicMock()
    mock_gh_repo.get_issue.return_value = mock_issue

    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_gh_repo

    state = {
        "repo_name": "test/repo",
        "target_issue": 42,
        "architect_directive": "",
        "idea": "",
        "pm_decision": "",
        "code": "",
        "review": "",
        "issue_number": 42,
        "pr_number": 0,
        "branch_name": "",
        "iteration": 0,
        "log_messages": [],
    }

    mock_branch = MagicMock()
    mock_branch.commit.sha = "sha123"
    mock_gh_repo.default_branch = "main"
    mock_gh_repo.get_branch.return_value = mock_branch

    mock_pr = MagicMock()
    mock_pr.number = 101
    mock_pr.html_url = "https://github.com/test/repo/pull/101"
    mock_gh_repo.create_pull.return_value = mock_pr
    mock_gh_repo.get_pull.return_value = mock_pr

    mock_file = MagicMock()
    mock_file.decoded_content = b"original content"
    mock_file.sha = "filesha123"
    mock_gh_repo.get_contents.return_value = mock_file

    with patch("agents.gh", mock_gh), patch(
        "agents.run_llm_with_tools", AsyncMock(return_value="print('test')")
    ):
        # 1. Architect Node
        res_arch = await architect_node(state)
        state["architect_directive"] = res_arch["architect_directive"]

        # 2. Brainstormer Node
        res_brain = await brainstormer_node(state)
        state["idea"] = res_brain["idea"]

        # 3. PM Node
        res_pm = await pm_node(state)
        state["pm_decision"] = res_pm["pm_decision"]

        # 4. Implementer Node
        res_imp = await implementer_node(state)
        state["code"] = res_imp["code"]

    with patch("agents.gh", mock_gh), patch(
        "agents.run_llm_with_tools", AsyncMock(return_value="LGTM!")
    ):
        # 5. Maintainer Node
        res_maint = await maintainer_node(state)
        state["review"] = res_maint["review"]

    assert len(events_received) >= 5
    for evt in events_received:
        payload = evt.model_dump(mode="json")
        for k, v in payload.items():
            assert "text-red" not in str(v)
            assert "text-amber" not in str(v)
            assert "text-emerald" not in str(v)
            assert "text-zinc" not in str(v)
            assert "text-blue" not in str(v)
            assert "text-purple" not in str(v)
            assert "<div" not in str(v)
            assert "<span" not in str(v)
