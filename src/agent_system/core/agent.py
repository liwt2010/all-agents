"""
SmartAgentMixin — all Agent base class (iteration 2, with plugin tools + LLM Router)

Subclasses only implement do_work(). Base class provides:
  - execute() loop with retry, validation, event publishing
  - Plugin-based tool registry via auto-discovery
  - LLM Router integration
  - Error classification (L1-L4)
"""

import asyncio
import logging
import traceback
from abc import abstractmethod
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from agent_system.config.settings import get_settings
from agent_system.core.schema import (
    OutputSchema, ValidationResult, validator,
    FailureNodeLogger,
)
from agent_system.tools.base import ToolRegistry, discover_tools, filter_registry
from agent_system.core.llm_router import LLMRouter, router as default_router

logger = logging.getLogger(__name__)


class EventType(str, Enum):
    TASK_STARTED = "agent.task.started"
    TASK_COMPLETED = "agent.task.completed"
    TASK_FAILED = "agent.task.failed"
    TASK_RETRYING = "agent.task.retrying"
    OUTPUT_VALIDATED = "agent.output.validated"
    OUTPUT_INVALID = "agent.output.invalid"
    PEER_REQUESTED = "agent.peer.requested"
    HUMAN_REQUESTED = "agent.human.requested"
    ESCALATED = "agent.escalated"


class AgentEvent(BaseModel):
    event_type: EventType
    agent_name: str
    task_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    data: Dict[str, Any] = Field(default_factory=dict)


class ErrorLevel(str, Enum):
    L1_TEMPORARY = "temporary"
    L2_RECOVERABLE = "recoverable"
    L3_BUSINESS = "business"
    L4_SYSTEM = "system"


class AgentError(BaseModel):
    level: ErrorLevel
    message: str
    details: Dict[str, Any] = Field(default_factory=dict)
    exception: Optional[str] = None


class TaskContext(BaseModel):
    task_id: str
    input: str
    config: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    upstream_output: Optional[Dict[str, Any]] = None  # output from prev agent
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    retry_count: int = 0
    max_retries: int = 3


class EventHandler:
    """Publish/subscribe event bus"""

    def __init__(self):
        self._handlers: Dict[EventType, List[Callable]] = {}

    def subscribe(self, event_type: EventType, handler: Callable):
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)

    async def publish(self, event: AgentEvent):
        handlers = self._handlers.get(event.event_type, [])
        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(event)
                else:
                    handler(event)
            except Exception as e:
                logger.warning(f"Event handler error [{event.event_type}]: {e}")

    def unsubscribe(self, event_type: EventType, handler: Callable):
        handlers = self._handlers.get(event_type, [])
        if handler in handlers:
            handlers.remove(handler)


event_bus = EventHandler()


class SmartAgent(BaseModel):
    """
    Smart Agent Base Class

    Subclasses must define:
        agent_name: str
        agent_capabilities: list
        async def do_work(self, task: TaskContext) -> OutputSchema
    """

    agent_name: str = "base_agent"
    agent_capabilities: list = Field(default_factory=list)
    description: str = "Base Agent"
    tool_registry: ToolRegistry = Field(default_factory=lambda: filter_registry(
        discover_tools(),
        get_settings().tools.enabled,
    ))
    llm_router: LLMRouter = Field(default_factory=LLMRouter)
    max_retries: int = 3
    enable_escalation: bool = False  # Set to True to enable 4-way resolution
    memory_enabled: bool = True  # Set False to disable experience recording

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def enable_resolver(self):
        """Enable the 4-way escalation resolver for this agent"""
        from agent_system.core.resolver import SmartResolver
        resolver = SmartResolver(self)
        object.__setattr__(self, '_resolver', resolver)
        self.enable_escalation = True

    @abstractmethod
    async def do_work(self, task: TaskContext) -> OutputSchema:
        """Subclass implements this — the actual business logic"""
        ...

    def get_system_prompt(self) -> str:
        """Generate system prompt with tools and capabilities"""
        tools_desc = "\n".join(
            f"  - {t.name}: {t.description}"
            for t in self.tool_registry.list_definitions()
        )
        capabilities = ", ".join(self.agent_capabilities)
        return f"""You are {self.agent_name}, {self.description}.

Capabilities: {capabilities}

Available tools:
{tools_desc}

Requirements:
1. Output must conform to the standard schema (id, type, created_at, schema_version, payload, next_steps)
2. Be clear, complete, and actionable
3. If uncertain, state your assumptions clearly

Begin."""

    async def execute(self, task: TaskContext) -> OutputSchema:
        """
        Top-level execution orchestrator. Composes:
          1. _setup_checkpoint  — create and persist TaskCheckpoint
          2. _run_with_retry    — main retry loop; returns OutputSchema or raises
          3. _handle_final_failure — record TASK_FAILED event + persist failed checkpoint
          4. _escalate          — optional 4-way resolver (SELF/PEER/HUMAN/ESCALATE)

        When memory_enabled=True (default), this also:
          - records task start/complete/failure to the experience graph
          - injects relevant past experiences into task.metadata['experiences']
            before _run_with_retry, so do_work() can use them as hints
        """
        # ── Memory hook (P2-3.1) ──────────────────────────────
        if self.memory_enabled:
            from agent_system.memory.experience import (
                record_task_start,
                record_task_complete,
                record_task_failure,
                record_experience,
                get_relevant_experiences,
                get_graph,
            )
            graph = get_graph()
            record_task_start(graph, task.task_id, task.input, self.agent_name)
            # Inject past experiences as hints into task metadata
            try:
                experiences = get_relevant_experiences(graph, task.input, max_results=3)
                if experiences:
                    if task.metadata is None:
                        task.metadata = {}
                    task.metadata["experiences"] = experiences
            except Exception:
                # Don't block task execution if experience lookup fails
                pass

        task.started_at = datetime.now(timezone.utc)
        checkpoint = self._setup_checkpoint(task)
        await self._publish_event(EventType.TASK_STARTED, task, {"input_hash": hash(task.input[:200])})
        try:
            output = await self._run_with_retry(task, checkpoint)
            if self.memory_enabled:
                record_task_complete(graph, task.task_id, output)
                if not task.metadata.get("skip_experience_recording"):
                    record_experience(
                        graph,
                        task.task_id,
                        f"Agent {self.agent_name} completed: {task.input[:100]}",
                        self.agent_name,
                        success=True,
                    )
            return output
        except Exception as e:
            if self.memory_enabled:
                record_task_failure(
                    graph,
                    task.task_id,
                    str(e),
                    self.agent_name,
                    details={"attempts": getattr(task, "retry_count", 0)},
                )
            last_error = self._classify_error(e)
            await self._handle_final_failure(task, checkpoint, last_error)
            if getattr(self, "_resolver", None):
                return await self._escalate(task, last_error)
            raise RuntimeError(
                f"[{last_error.level.value}] {self.agent_name} failed: {last_error.message}"
            )

    # ── Step 1: Setup checkpoint ──────────────────────────────────

    def _setup_checkpoint(self, task: TaskContext) -> Any:
        """Create and persist a TaskCheckpoint for the new task."""
        from agent_system.core.failure_ux import (
            TaskCheckpoint,
            StepRecord,
            CheckpointStore,
            estimate_task_type,
        )
        cp = TaskCheckpoint(
            task_id=task.task_id,
            agent_name=self.agent_name,
            task_type=estimate_task_type(task.input),
            pending_steps=[StepRecord(
                step_id="do_work",
                name=f"Execute {self.agent_name}",
                started_at=datetime.now(timezone.utc),
            )],
            timeout_seconds=getattr(task, 'timeout_seconds', None) or 300,
        )
        cp.start()
        CheckpointStore().save(cp)
        return cp

    # ── Step 2: Main retry loop ──────────────────────────────────

    async def _run_with_retry(self, task: TaskContext, checkpoint: Any) -> OutputSchema:
        """
        Loop up to max_retries+1 times. Returns OutputSchema on success.
        Raises Exception (with .message attribute) on final failure.
        """
        last_error: Optional[AgentError] = None
        for attempt in range(task.max_retries + 1):
            task.retry_count = attempt
            try:
                output = await self.do_work(task)
                # PR-2.2: tiered validation with auto-repair + FAILURE node log
                output, v_result = validator.validate_and_repair(
                    output, agent_name=self.agent_name,
                )
                # Log every validation outcome to the graph for audit
                FailureNodeLogger.record_validation(
                    task_id=task.task_id,
                    agent_name=self.agent_name,
                    output=output,
                    result=v_result,
                )
                if v_result.valid:
                    return await self._on_validation_success(task, checkpoint, output, attempt)
                # Validation failed (tier-1 errors survived) — record + retry
                last_error = AgentError(
                    level=ErrorLevel.L2_RECOVERABLE,
                    message=f"Output validation failed: {', '.join(v_result.errors)}",
                    details={
                        "warnings": v_result.warnings,
                        "repairs": v_result.repairs,
                        "partial": output.partial,
                    },
                )
                if attempt < task.max_retries:
                    await self._publish_event(
                        EventType.TASK_RETRYING, task,
                        {"attempt": attempt, "error": last_error.message},
                    )
            except Exception as e:
                last_error = self._classify_error(e)
                if attempt < task.max_retries and last_error.level == ErrorLevel.L1_TEMPORARY:
                    await self._publish_event(
                        EventType.TASK_RETRYING, task,
                        {"attempt": attempt, "error": str(e)},
                    )
                    continue
                break
        raise RuntimeError(last_error.message if last_error else "Unknown error")

    async def _on_validation_success(
        self, task: TaskContext, checkpoint: Any, output: OutputSchema, attempt: int
    ) -> OutputSchema:
        """Finalize a validated output: stamp metadata, publish TASK_COMPLETED, delete checkpoint."""
        from agent_system.core.failure_ux import CheckpointStore
        from agent_system.core.observability import (
            build_provenance,
            attach_provenance,
            ProvenanceSource,
        )
        output.metadata["retry_count"] = attempt
        output.metadata["agent_name"] = self.agent_name
        task.completed_at = datetime.now(timezone.utc)
        if hasattr(self, "_last_usage") and self._last_usage:
            output.metadata["llm_usage"] = self._last_usage.model_dump()

        # P2-3.2: stamp data provenance so consumers/UI know what they're
        # looking at. Real LLM = source=real_llm, mock = source=mock,
        # LLM-failed-then-fallback = source=llm_failure.
        usage = getattr(self, "_last_usage", None)
        if usage is not None and getattr(usage, "mock", False):
            source = ProvenanceSource.MOCK
        elif usage is not None:
            source = ProvenanceSource.REAL_LLM
        else:
            # No usage recorded (e.g. agent bypassed LLM entirely)
            source = ProvenanceSource.UNKNOWN
        # If the output is marked partial (raw_output fallback), override
        # to LLM_FAILURE so the badge shows the LLM attempted but failed.
        if getattr(output, "partial", False):
            source = ProvenanceSource.LLM_FAILURE
        prov = build_provenance(
            source=source,
            agent_name=self.agent_name,
            task_id=task.task_id,
            usage=usage,
        )
        attach_provenance(output, prov)

        await self._publish_event(
            EventType.TASK_COMPLETED, task,
            {"output_id": output.id, "type": output.type},
        )
        checkpoint.complete_step("do_work", output.model_dump(mode="json"))
        CheckpointStore().delete(task.task_id)
        return output

    # ── Step 3: Final failure ─────────────────────────────────────

    async def _handle_final_failure(
        self, task: TaskContext, checkpoint: Any, last_error: AgentError
    ) -> None:
        """Publish TASK_FAILED and persist the failed checkpoint (kept for debugging)."""
        from agent_system.core.failure_ux import CheckpointStore
        await self._publish_event(
            EventType.TASK_FAILED, task,
            {
                "error": last_error.model_dump(),
                "attempts": task.retry_count,
            },
        )
        checkpoint.fail_step("do_work", last_error.message)
        CheckpointStore().save(checkpoint)

    # ── Step 4: 4-way escalation ─────────────────────────────────

    async def _escalate(self, task: TaskContext, last_error: AgentError) -> OutputSchema:
        """
        Run the 4-way resolver. Returns OutputSchema if a path resolves; raises otherwise.
        The ESCALATE path additionally invokes the CEO Agent for a decision (best-effort).
        """
        from agent_system.core.evaluator import evaluator as default_evaluator
        from agent_system.core.failure_ux import CheckpointStore

        analysis = default_evaluator.evaluate(
            error_message=last_error.message,
            agent_name=self.agent_name,
            agent_capabilities=self.agent_capabilities,
            attempted_action=task.metadata.get("action"),
            task_input=task.input,
            retry_count=task.retry_count,
        )
        resolution = await self._resolver.resolve(
            task, RuntimeError(last_error.message), analysis
        )
        if resolution.status.value == "success" and resolution.output:
            CheckpointStore().delete(task.task_id)
            return resolution.output
        if resolution.path.value == "escalate":
            await self._invoke_ceo(task, last_error, analysis)
        raise RuntimeError(
            f"[{last_error.level.value}] {self.agent_name} failed: {last_error.message}"
        )

    async def _invoke_ceo(self, task: TaskContext, last_error: AgentError, analysis: Any) -> None:
        """Best-effort: spawn a CEO Agent and let it make a decision on this failure."""
        try:
            from agent_system.agents.ceo_agent import CEOAgent
            ceo = CEOAgent()
            escalate_task = TaskContext(
                task_id=f"{task.task_id}-esc",
                input=f"Escalation from {self.agent_name}: {last_error.message}",
                metadata={
                    "is_escalation": True,
                    "escalation_data": {
                        "task_id": task.task_id,
                        "agent": self.agent_name,
                        "severity": analysis.severity.value,
                        "error": last_error.message,
                        "analysis": analysis.reasoning,
                        "capabilities": self.agent_capabilities,
                        "similar_experiences": analysis.similar_experiences,
                    },
                },
                max_retries=1,
            )
            ceo_output = await ceo.execute(escalate_task)
            task.metadata["ceo_decision"] = ceo_output.payload
            logger.info(
                f"CEO made a decision: {ceo_output.payload.get('decision', {}).get('action')}"
            )
        except Exception as e:
            logger.warning(f"CEO escalation failed: {e}")

    # ── Helpers ──────────────────────────────────────────────────

    async def _publish_event(self, event_type: EventType, task: TaskContext, data: Dict[str, Any]) -> None:
        """Thin wrapper around event_bus.publish for type-safe events."""
        await event_bus.publish(AgentEvent(
            event_type=event_type,
            agent_name=self.agent_name,
            task_id=task.task_id,
            data=data,
        ))

    def _classify_error(self, e: Exception) -> AgentError:
        """Map an exception to an AgentError level (L1-L4) by keyword match."""
        error_str = str(e).lower()
        tb = traceback.format_exc()
        if any(kw in error_str for kw in ["timeout", "rate limit", "429", "503", "connection"]):
            return AgentError(level=ErrorLevel.L1_TEMPORARY, message=str(e), exception=tb)
        if any(kw in error_str for kw in ["permission denied", "access denied"]):
            return AgentError(level=ErrorLevel.L4_SYSTEM, message=str(e), exception=tb)
        return AgentError(level=ErrorLevel.L2_RECOVERABLE, message=str(e), exception=tb)
