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
from agent_system.core.schema import OutputSchema, ValidationResult, validator
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
        Full execution loop:
        1. Start -> publish event + create checkpoint
        2. Execute do_work() -> catch errors
        3. Validate output -> pass or retry
        4. Failure -> retry / escalate
        5. Final -> checkpoint persists + closed
        """
        task.started_at = datetime.now(timezone.utc)

        # Create a checkpoint for this task (ARCHITECTURE.md 12.2)
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
        _checkpoint_store = CheckpointStore()
        _checkpoint_store.save(cp)

        await event_bus.publish(AgentEvent(
            event_type=EventType.TASK_STARTED,
            agent_name=self.agent_name,
            task_id=task.task_id,
            data={"input_hash": hash(task.input[:200])},
        ))

        last_error: Optional[AgentError] = None

        for attempt in range(task.max_retries + 1):
            try:
                task.retry_count = attempt
                output = await self.do_work(task)

                v_result = validator.validate(output)

                if v_result.valid:
                    output.metadata["retry_count"] = attempt
                    output.metadata["agent_name"] = self.agent_name
                    task.completed_at = datetime.now(timezone.utc)

                    # Track LLM usage if available
                    if hasattr(self, '_last_usage') and self._last_usage:
                        output.metadata["llm_usage"] = self._last_usage.model_dump()

                    await event_bus.publish(AgentEvent(
                        event_type=EventType.TASK_COMPLETED,
                        agent_name=self.agent_name,
                        task_id=task.task_id,
                        data={"output_id": output.id, "type": output.type},
                    ))
                    # Mark checkpoint complete + delete (no resume needed)
                    cp.complete_step("do_work", output.model_dump(mode="json"))
                    _checkpoint_store.delete(task.task_id)
                    return output
                else:
                    error_msg = f"Output validation failed: {', '.join(v_result.errors)}"
                    last_error = AgentError(
                        level=ErrorLevel.L2_RECOVERABLE,
                        message=error_msg,
                        details={"warnings": v_result.warnings},
                    )
                    if attempt < task.max_retries:
                        await event_bus.publish(AgentEvent(
                            event_type=EventType.TASK_RETRYING,
                            agent_name=self.agent_name,
                            task_id=task.task_id,
                            data={"attempt": attempt, "error": error_msg},
                        ))

            except Exception as e:
                last_error = self._classify_error(e)
                if attempt < task.max_retries and last_error.level == ErrorLevel.L1_TEMPORARY:
                    await event_bus.publish(AgentEvent(
                        event_type=EventType.TASK_RETRYING,
                        agent_name=self.agent_name,
                        task_id=task.task_id,
                        data={"attempt": attempt, "error": str(e)},
                    ))
                    continue
                else:
                    break

        await event_bus.publish(AgentEvent(
            event_type=EventType.TASK_FAILED,
            agent_name=self.agent_name,
            task_id=task.task_id,
            data={
                "error": last_error.model_dump() if last_error else None,
                "attempts": task.retry_count,
            },
        ))

        # Save checkpoint as failed (keeps resume context for debugging)
        if last_error:
            cp.fail_step("do_work", last_error.message)
            _checkpoint_store.save(cp)

        # ---- 4-way escalation (Iteration 4) ----
        if hasattr(self, '_resolver') and self._resolver:
            from agent_system.core.evaluator import evaluator as default_evaluator
            analysis = default_evaluator.evaluate(
                error_message=last_error.message if last_error else "Unknown error",
                agent_name=self.agent_name,
                agent_capabilities=self.agent_capabilities,
                attempted_action=task.metadata.get("action"),
                task_input=task.input,
                retry_count=task.retry_count,
            )
            resolution = await self._resolver.resolve(task, RuntimeError(last_error.message if last_error else "Unknown"), analysis)
            if resolution.status.value == "success" and resolution.output:
                _checkpoint_store.delete(task.task_id)
                return resolution.output

            # ESCALATE path: actually invoke the CEO Agent for a decision
            if resolution.path.value == "escalate":
                try:
                    from agent_system.agents.ceo_agent import CEOAgent
                    from agent_system.core.agent import TaskContext
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
                    logger.info(f"CEO made a decision: {ceo_output.payload.get('decision', {}).get('action')}")
                except Exception as e:
                    logger.warning(f"CEO escalation failed: {e}")

        raise RuntimeError(
            f"[{last_error.level.value}] {self.agent_name} failed: "
            f"{last_error.message if last_error else 'Unknown error'}"
        )

    def _classify_error(self, e: Exception) -> AgentError:
        error_str = str(e).lower()
        tb = traceback.format_exc()

        if any(kw in error_str for kw in ["timeout", "rate limit", "429", "503", "connection"]):
            return AgentError(level=ErrorLevel.L1_TEMPORARY, message=str(e), exception=tb)
        if any(kw in error_str for kw in ["permission denied", "access denied"]):
            return AgentError(level=ErrorLevel.L4_SYSTEM, message=str(e), exception=tb)
        return AgentError(level=ErrorLevel.L2_RECOVERABLE, message=str(e), exception=tb)
