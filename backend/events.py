"""
Strongly-Typed Domain Event Model and EventEmitter Abstraction.
Part of Epic #157 (GitHub Issue #185).

Defines structured domain events for AutoMaintainer agent execution,
node transitions, plan checkpoints, test results, telemetry, and errors,
completely decoupled from frontend presentation concerns (CSS/HTML).
"""

from abc import ABC, abstractmethod
import asyncio
from datetime import datetime, timezone
from enum import Enum
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union
import uuid

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class AgentPhase(str, Enum):
    """Strongly-typed agent phases matching AutoMaintainer agent workflow."""

    ARCHITECT = "architect"
    BRAINSTORMER = "brainstormer"
    VISIONARY = "visionary"
    PM = "pm"
    REVIEWER = "reviewer"
    IMPLEMENTER = "implementer"
    MAINTAINER = "maintainer"
    SYSTEM = "system"


class BaseEvent(BaseModel):
    """
    Base domain event model for AutoMaintainer.
    All events inherit from BaseEvent and contain unique event identification,
    run correlation ID, creation timestamp, and execution phase.
    """

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    phase: Union[AgentPhase, str] = AgentPhase.SYSTEM
    event_type: str = "BaseEvent"

    model_config = ConfigDict(
        extra="ignore",
        use_enum_values=True,
        validate_assignment=True,
    )


class NodeTransitionEvent(BaseEvent):
    """Emitted when the agent workflow transitions between nodes/states."""

    from_node: Optional[str] = None
    to_node: str
    iteration: int = 0
    event_type: str = "NodeTransitionEvent"


class PlanCheckpointEvent(BaseEvent):
    """Emitted when an architect or planner generates or updates an execution plan."""

    plan_markdown: str
    files_to_modify: List[str] = Field(default_factory=list)
    files_to_create: List[str] = Field(default_factory=list)
    event_type: str = "PlanCheckpointEvent"


class TestResultEvent(BaseEvent):
    """Emitted when test execution finishes within a runner or verification step."""

    __test__ = False  # Instruct pytest not to treat this model as a test case

    exit_code: int
    passed_count: int = 0
    failed_count: int = 0
    tracebacks: List[str] = Field(default_factory=list)
    event_type: str = "TestResultEvent"


class TelemetryEvent(BaseEvent):
    """Emitted to record performance metrics, LLM latency, token counts, and costs."""

    latency_ms: float
    tokens_used: int = 0
    cost_usd: float = 0.0
    model_name: str = ""
    event_type: str = "TelemetryEvent"


class ErrorEvent(BaseEvent):
    """Emitted when an error or exception occurs during workflow execution."""

    error_code: str
    message: str
    fatal: bool = False
    event_type: str = "ErrorEvent"


EVENT_REGISTRY: Dict[str, type[BaseEvent]] = {
    "BaseEvent": BaseEvent,
    "NodeTransitionEvent": NodeTransitionEvent,
    "PlanCheckpointEvent": PlanCheckpointEvent,
    "TestResultEvent": TestResultEvent,
    "TelemetryEvent": TelemetryEvent,
    "ErrorEvent": ErrorEvent,
}


def parse_event(data: Union[Dict[str, Any], str]) -> BaseEvent:
    """
    Parse a dictionary or JSON string into the appropriate concrete BaseEvent subclass.
    """
    import json

    if isinstance(data, str):
        data = json.loads(data)

    event_type = data.get("event_type", "BaseEvent")
    if event_type not in EVENT_REGISTRY:
        raise ValueError(f"Unknown or unregistered event type: '{event_type}'")
    model_cls = EVENT_REGISTRY[event_type]
    return model_cls.model_validate(data)


class EventEmitter(ABC):
    """
    Abstract Base Class for asynchronous domain event listeners and dispatchers.
    Core engine and agent nodes depend on this abstraction rather than concrete sinks.
    """

    @abstractmethod
    async def emit(self, event: BaseEvent) -> None:
        """Asynchronously emit a domain event."""
        pass

    async def close(self) -> None:
        """Optional hook to cleanly close or flush any pending emitter resources."""
        pass


class NullEmitter(EventEmitter):
    """
    Null / No-Op emitter for headless SDK runs, silent CLI mode, and unit tests.
    Performs no external side effects and is safe to await.
    """

    async def emit(self, event: BaseEvent) -> None:
        pass


class StreamingEmitter(EventEmitter):
    """
    EventEmitter for SSE / WebSocket streaming sinks.
    Pushes structured domain events to an asyncio.Queue or an async callback
    without embedding any presentation-specific HTML/CSS.
    """

    def __init__(
        self,
        queue: Optional[asyncio.Queue] = None,
        callback: Optional[Callable[[BaseEvent], Awaitable[None]]] = None,
    ):
        self.queue = queue if queue is not None else asyncio.Queue()
        self.callback = callback

    async def emit(self, event: BaseEvent) -> None:
        try:
            await self.queue.put(event)
            if self.callback is not None:
                await self.callback(event)
        except Exception as e:
            logger.error(f"StreamingEmitter error: {e}")

    async def get_next_event(
        self, timeout: Optional[float] = None
    ) -> Optional[BaseEvent]:
        """Helper for streaming consumers to pull the next event from the queue."""
        if timeout is not None:
            try:
                return await asyncio.wait_for(self.queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                return None
        return await self.queue.get()


class SupabaseEmitter(EventEmitter):
    """
    EventEmitter for persisting domain events and telemetry to Supabase.
    Operates asynchronously without blocking the main agent loop.
    Isolates database schema interactions and handles failures gracefully.
    """

    def __init__(
        self,
        client: Optional[Any] = None,
        table_name: str = "logs",
        org_id: Optional[str] = None,
    ):
        self._client = client
        self.table_name = table_name
        self.org_id = org_id

    def _get_client(self) -> Optional[Any]:
        if self._client is not None:
            return self._client
        try:
            import agents

            if getattr(agents, "supabase", None) is not None:
                return agents.supabase
        except Exception:
            pass
        return None

    async def emit(self, event: BaseEvent) -> None:
        client = self._get_client()
        if client is None:
            return

        try:
            event_payload = event.model_dump(mode="json")

            if self.table_name == "logs":
                log_type = "event"
                if isinstance(event, ErrorEvent):
                    log_type = "error"
                elif isinstance(event, TelemetryEvent):
                    log_type = "metric"

                if isinstance(event, NodeTransitionEvent):
                    msg = f"Transition: {event.from_node or 'start'} -> {event.to_node} (iteration {event.iteration})"
                elif isinstance(event, PlanCheckpointEvent):
                    msg = f"Plan checkpoint: {len(event.files_to_modify)} to modify, {len(event.files_to_create)} to create"
                elif isinstance(event, TestResultEvent):
                    msg = f"Test results: exit code {event.exit_code}, {event.passed_count} passed, {event.failed_count} failed"
                elif isinstance(event, TelemetryEvent):
                    msg = f"Telemetry: {event.latency_ms}ms, {event.tokens_used} tokens, ${event.cost_usd:.6f}"
                elif isinstance(event, ErrorEvent):
                    msg = f"Error [{event.error_code}]: {event.message}"
                else:
                    msg = f"Event: {event.event_type}"

                record: Dict[str, Any] = {
                    "run_id": event.run_id,
                    "agent_name": str(event.phase),
                    "log_type": log_type,
                    "message": msg,
                    "metadata": event_payload,
                }
                if self.org_id:
                    record["org_id"] = self.org_id

                await asyncio.to_thread(
                    lambda: client.table(self.table_name).insert(record).execute()
                )
            else:
                await asyncio.to_thread(
                    lambda: client.table(self.table_name)
                    .insert(event_payload)
                    .execute()
                )
        except Exception as e:
            logger.error(f"SupabaseEmitter failed to persist event: {e}")


class CompositeEmitter(EventEmitter):
    """
    EventEmitter that dispatches events to multiple child emitters concurrently.
    Ensures that slow or failing emitters do not block other sinks or the main loop.
    """

    def __init__(
        self,
        emitters: Optional[List[EventEmitter]] = None,
        child_timeout: Optional[float] = 10.0,
    ):
        self.emitters = list(emitters) if emitters is not None else []
        self.child_timeout = child_timeout

    def add_emitter(self, emitter: EventEmitter) -> None:
        self.emitters.append(emitter)

    async def _emit_child(self, emitter: EventEmitter, event: BaseEvent) -> None:
        if self.child_timeout is not None:
            await asyncio.wait_for(emitter.emit(event), timeout=self.child_timeout)
        else:
            await emitter.emit(event)

    async def emit(self, event: BaseEvent) -> None:
        if not self.emitters:
            return
        results = await asyncio.gather(
            *(self._emit_child(emitter, event) for emitter in self.emitters),
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                logger.error(f"CompositeEmitter child emission error: {r}")

    async def close(self) -> None:
        await asyncio.gather(
            *(emitter.close() for emitter in self.emitters),
            return_exceptions=True,
        )
