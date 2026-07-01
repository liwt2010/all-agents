"""
Event Bus — enterprise event system (ARCHITECTURE.md 7.2)

3 event categories:
  - agent.events    : task start/complete/fail/retry
  - agent.escalation: SELF/PEER/HUMAN/ESCALATE
  - output.validation: output validated/invalid

Sync + async pub/sub, event logging, CEO subscriptions.
"""

import asyncio
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypeVar, Union

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class EventCategory(str, Enum):
    AGENT = "agent"
    ESCALATION = "escalation"
    VALIDATION = "validation"
    SYSTEM = "system"


class EventSeverity(str, Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class Event(BaseModel):
    """Universal event model"""
    id: str = ""
    category: EventCategory
    name: str
    source: str  # agent name or system component
    severity: EventSeverity = EventSeverity.INFO
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    data: Dict[str, Any] = Field(default_factory=dict)
    trace_id: str = ""
    tenant_id: str = "default"

    def to_log_line(self) -> str:
        return (f"[{self.timestamp.isoformat()}] "
                f"[{self.category.value}.{self.severity.value}] "
                f"{self.source}: {self.name} "
                f"{json.dumps(self.data, ensure_ascii=False)[:200]}")


class EventSubscription(BaseModel):
    """A registered event handler"""
    id: str
    event_name: Optional[str] = None  # None = all events
    category: Optional[EventCategory] = None
    source: Optional[str] = None
    severity_min: Optional[EventSeverity] = None
    handler: Any = None  # Callable[[Event], None] or Callable[[Event], Awaitable[None]]


class EventLogWriter:
    """Persists events to a log file"""

    def __init__(self, log_dir: str = "data/events"):
        self.log_dir = Path(log_dir)

    async def write(self, event: Event):
        self.log_dir.mkdir(parents=True, exist_ok=True)
        date_str = event.timestamp.strftime("%Y-%m-%d")
        log_file = self.log_dir / f"{date_str}.jsonl"
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(event.model_dump_json() + "\n")
        except Exception as e:
            logger.warning(f"Failed to write event log: {e}")


class EventBus:
    """
    Central event bus with sync and async pub/sub.

    Usage:
        bus = EventBus()
        bus.subscribe("agent.task.*", my_handler)
        await bus.publish(Event(category="agent", name="task.started", ...))
    """

    def __init__(self):
        self._subscriptions: List[EventSubscription] = []
        self._sub_counter: int = 0
        self._log_writer: Optional[EventLogWriter] = None

    def enable_logging(self, log_dir: str = "data/events"):
        """Enable event persistence to disk"""
        self._log_writer = EventLogWriter(log_dir)

    def subscribe(
        self,
        handler: Callable,
        event_name: Optional[str] = None,
        category: Optional[EventCategory] = None,
        source: Optional[str] = None,
        severity_min: Optional[EventSeverity] = None,
    ) -> str:
        """Register an event handler. Returns subscription ID for unsubscribe."""
        self._sub_counter += 1
        sub_id = f"sub-{self._sub_counter}"
        self._subscriptions.append(EventSubscription(
            id=sub_id,
            event_name=event_name,
            category=category,
            source=source,
            severity_min=severity_min,
            handler=handler,
        ))
        return sub_id

    def unsubscribe(self, sub_id: str) -> bool:
        """Remove a subscription by ID"""
        before = len(self._subscriptions)
        self._subscriptions = [s for s in self._subscriptions if s.id != sub_id]
        return len(self._subscriptions) < before

    def _matches(self, event: Event, sub: EventSubscription) -> bool:
        """Check if an event matches a subscription filter"""
        if sub.event_name and not self._wildcard_match(event.name, sub.event_name):
            return False
        if sub.category and event.category != sub.category:
            return False
        if sub.source and event.source != sub.source:
            return False
        if sub.severity_min:
            levels = list(EventSeverity)
            if levels.index(event.severity) < levels.index(sub.severity_min):
                return False
        return True

    def _wildcard_match(self, value: str, pattern: str) -> bool:
        """Simple wildcard match (* matches anything)"""
        if pattern.endswith("*"):
            return value.startswith(pattern[:-1])
        if pattern.startswith("*"):
            return value.endswith(pattern[1:])
        return value == pattern

    async def publish(self, event: Event):
        """Publish an event to all matching subscribers"""
        # Log to disk if enabled
        if self._log_writer:
            await self._log_writer.write(event)

        # Dispatch to subscribers
        for sub in self._subscriptions:
            if self._matches(event, sub):
                try:
                    handler = sub.handler
                    if asyncio.iscoroutinefunction(handler):
                        await handler(event)
                    else:
                        if asyncio.iscoroutine(handler):
                            await handler
                        else:
                            handler(event)
                except Exception as e:
                    logger.warning(f"Event handler error [{sub.id}]: {e}")

    def count_subscribers(self) -> int:
        return len(self._subscriptions)

    def clear(self):
        self._subscriptions.clear()


# Global event bus instance
event_bus = EventBus()


def subscribe_to_agent_events(
    handler: Callable,
    agent_name: Optional[str] = None,
) -> str:
    """Convenience: subscribe to all agent lifecycle events"""
    return event_bus.subscribe(
        handler=handler,
        category=EventCategory.AGENT,
        source=agent_name,
    )


def subscribe_to_escalations(
    handler: Callable,
    severity_min: EventSeverity = EventSeverity.WARNING,
) -> str:
    """Convenience: subscribe to all escalation events"""
    return event_bus.subscribe(
        handler=handler,
        category=EventCategory.ESCALATION,
        severity_min=severity_min,
    )


def subscribe_to_validations(
    handler: Callable,
) -> str:
    """Convenience: subscribe to all validation events"""
    return event_bus.subscribe(
        handler=handler,
        category=EventCategory.VALIDATION,
    )


def make_event(
    category: EventCategory,
    name: str,
    source: str,
    data: Optional[Dict[str, Any]] = None,
    severity: EventSeverity = EventSeverity.INFO,
    trace_id: str = "",
    tenant_id: str = "default",
) -> Event:
    """Factory: create a properly structured event"""
    import uuid
    return Event(
        id=f"evt-{uuid.uuid4().hex[:8]}",
        category=category,
        name=name,
        source=source,
        severity=severity,
        data=data or {},
        trace_id=trace_id or f"trace-{uuid.uuid4().hex[:8]}",
        tenant_id=tenant_id,
    )
