"""
OpenTelemetry-style tracing — PLATFORM §5.5, §6.5

Lightweight in-process tracing that can be:
  - Exported to a real OTel collector
  - Or just kept in memory for now
"""

import logging
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class Span:
    name: str
    trace_id: str
    span_id: str
    parent_id: str | None = None
    start_time: float = 0.0
    end_time: float = 0.0
    attributes: dict[str, Any] = field(default_factory=dict)
    status: str = "ok"  # ok / error
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def duration_ms(self) -> float:
        if self.end_time and self.start_time:
            return (self.end_time - self.start_time) * 1000
        return 0.0


class Tracer:
    """
    In-process OTel-compatible tracer. Spans can be exported to
    Jaeger, Tempo, etc. via OTel SDK — this is the in-memory shim.
    """

    def __init__(self):
        self._spans: list[Span] = []
        self._current: list[Span] = []  # stack of active spans
        self._max_spans = 1000

    def _new_span(self, name: str, attributes: dict | None = None) -> Span:
        parent_id = self._current[-1].span_id if self._current else None
        trace_id = self._current[0].trace_id if self._current else uuid.uuid4().hex
        span_id = uuid.uuid4().hex[:16]
        return Span(
            name=name,
            trace_id=trace_id,
            span_id=span_id,
            parent_id=parent_id,
            start_time=time.time(),
            attributes=attributes or {},
        )

    @contextmanager
    def start_span(self, name: str, attributes: dict | None = None):
        """Synchronous span context manager."""
        span = self._new_span(name, attributes)
        self._current.append(span)
        try:
            yield span
        except Exception as e:
            span.status = "error"
            span.attributes["error.message"] = str(e)
            raise
        finally:
            span.end_time = time.time()
            self._spans.append(span)
            self._current.pop()
            if len(self._spans) > self._max_spans:
                self._spans = self._spans[-self._max_spans:]

    @asynccontextmanager
    async def astart_span(self, name: str, attributes: dict | None = None):
        """Async span context manager."""
        span = self._new_span(name, attributes)
        self._current.append(span)
        try:
            yield span
        except Exception as e:
            span.status = "error"
            span.attributes["error.message"] = str(e)
            raise
        finally:
            span.end_time = time.time()
            self._spans.append(span)
            self._current.pop()
            if len(self._spans) > self._max_spans:
                self._spans = self._spans[-self._max_spans:]

    def get_recent_spans(self, limit: int = 100) -> list[Span]:
        return self._spans[-limit:]

    def get_spans_by_trace(self, trace_id: str) -> list[Span]:
        return [s for s in self._spans if s.trace_id == trace_id]

    def stats(self) -> dict[str, Any]:
        durations = [s.duration_ms for s in self._spans if s.duration_ms]
        errors = sum(1 for s in self._spans if s.status == "error")
        return {
            "total_spans": len(self._spans),
            "active_spans": len(self._current),
            "error_count": errors,
            "avg_duration_ms": sum(durations) / len(durations) if durations else 0,
        }

    def clear(self):
        self._spans = []


# Singleton tracer
_tracer: Tracer | None = None


def get_tracer() -> Tracer:
    global _tracer
    if _tracer is None:
        _tracer = Tracer()
    return _tracer


def reset_tracer():
    global _tracer
    if _tracer is not None:
        _tracer.clear()
    _tracer = None
