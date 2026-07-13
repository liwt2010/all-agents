"""
DiscussionMixin — PLATFORM.md §5.2, §7.3

Multi-agent deliberation when an agent is "unsure" (low confidence / ambiguous
task). Replaces the simulated peer discussion with a real round-robin protocol
that:

  1. Discovers peer agents based on capability-overlap with the topic
  2. Asks each peer to provide a perspective (bounded, single-turn)
  3. Lets the original agent synthesize a refined approach
  4. Extracts a structured consensus
  5. Times out cleanly so callers can fall back to other paths

Public API:
  DiscussionContext    — input to a discussion round
  DiscussionResult     — output (transcript + consensus)
  DiscussionMixin      — base class Agent can mix in to get the .discuss() method

The mixin is intentionally **standalone** — it doesn't depend on real LLM
backends. It reuses the existing `do_work()` of peer agents, so it works
in mock mode and in production alike.
"""

import asyncio
import logging
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


async def _invoke_peer(provider: "PeerProvider", peer_name: str, peer_ctx) -> Any:
    """
    Invoke a peer provider, supporting both sync and async peer functions.

    The provider's peer_fn is called with (name, ctx). If it returns a
    coroutine, await it; otherwise run in a thread.
    """
    result = provider.invoke(peer_name, peer_ctx)
    if asyncio.iscoroutine(result):
        return await result
    if asyncio.iscoroutinefunction(provider.peer_fn):
        return await provider.peer_fn(peer_name, peer_ctx)
    return await asyncio.to_thread(provider.peer_fn, peer_name, peer_ctx)


# ── Context (what's being discussed) ──

class DiscussionContext(BaseModel):
    """Inputs to a discussion round."""
    task_id: str = "discussion"
    task_input: str
    error: str | None = None
    capability_hint: str | None = None
    agent_capabilities: list[str] = Field(default_factory=list)
    max_participants: int = 3
    max_rounds: int = 1
    timeout_seconds: float = 30.0


# ── Result (what came out) ──

class DiscussionRole(str, Enum):
    ASKER = "asker"
    ADVISOR = "advisor"
    SYNTHESIZER = "synthesizer"
    MODERATOR = "moderator"


class DiscussionMessage(BaseModel):
    """One message in the discussion transcript."""
    agent: str
    role: DiscussionRole
    message: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    duration_ms: float = 0.0
    unavailable: bool = False


class Consensus(BaseModel):
    """Structured consensus extracted from the discussion."""
    summary: str
    actionable_suggestion: str
    confidence: float = 0.0           # 0-1, how clear the consensus is
    agreement_ratio: float = 0.0      # 0-1, how many peers agreed
    dissenting_views: list[str] = Field(default_factory=list)


class DiscussionResult(BaseModel):
    """Output of one discussion round."""
    context: DiscussionContext
    transcript: list[DiscussionMessage] = Field(default_factory=list)
    consensus: Consensus | None = None
    duration_seconds: float = 0.0
    timed_out: bool = False
    no_peers_available: bool = False
    error: str | None = None

    def successful(self) -> bool:
        return self.consensus is not None and self.consensus.confidence > 0


# ── Peer provider protocol ──

# We avoid a hard dependency on agent_system.agents.* so the mixin is
# decoupled. Callers register peer providers that map name -> callable.

class PeerProvider:
    """
    Returns a list of (name, callable) peers.

    Each callable takes a TaskContext-like input and returns a result that
    has a `.payload` attribute (dict) — same shape as a SmartAgent output.
    """

    def __init__(self, peer_fn: Callable[[str, "TaskContextLike"], Any]):
        self.peer_fn = peer_fn

    def invoke(self, peer_name: str, context) -> Any:
        return self.peer_fn(peer_name, context)


class TaskContextLike(BaseModel):
    """Minimal stand-in for TaskContext that the mixin needs."""
    task_id: str
    input: str
    metadata: dict[str, Any] = Field(default_factory=dict)


# ── The Mixin ──

class DiscussionMixin:
    """
    Mixin that gives any agent a `.discuss()` capability.

    Usage:
        class MyAgent(DiscussionMixin, SmartAgent):
            ...

        result = await agent.discuss(ctx)
    """

    # Subclasses can override these
    agent_name: str = "agent"
    agent_capabilities: list[str] = []

    # Default peer registry — populated by SmartAgent or by the application
    _peer_registry: dict[str, PeerProvider] = {}

    def register_peer(self, name: str, provider: PeerProvider, *, _class_level: bool = False):
        """Register a peer provider.

        Instance form: agent.register_peer("name", provider)
        Class form:    StubAgent.register_peer_class("name", provider)
        """
        if _class_level:
            type(self)._peer_registry[name] = provider
        else:
            target_registry = getattr(self, "_instance_peers", None)
            if target_registry is None:
                target_registry = {}
                self._instance_peers = target_registry
            target_registry[name] = provider
            # Also register on the class so future instances see it
            type(self)._peer_registry[name] = provider

    @classmethod
    def register_peer_class(cls, name: str, provider: PeerProvider):
        """Class-level peer registration."""
        cls._peer_registry[name] = provider

    def _all_peers(self) -> dict[str, PeerProvider]:
        merged = dict(type(self)._peer_registry)
        merged.update(getattr(self, "_instance_peers", None) or {})
        return merged

    def _score_peer(self, peer_name: str, peer_provider: PeerProvider, ctx: DiscussionContext) -> int:
        """Score a peer's relevance to the topic. Higher = more relevant."""
        score = 0
        topic = (ctx.task_input + " " + (ctx.error or "") + " " + (ctx.capability_hint or "")).lower()

        # Use the peer name as a soft hint
        if peer_name in topic:
            score += 3

        # Each peer's capabilities is exposed via the provider's metadata
        caps = getattr(peer_provider, "capabilities", None) or []
        for cap in caps:
            for word in cap.lower().split():
                if len(word) > 3 and word in topic:
                    score += 1

        # Boost if the peer has any keyword overlap with the asker's caps
        for cap in ctx.agent_capabilities:
            for word in cap.lower().split():
                if len(word) > 3 and word in topic:
                    score += 1

        return score

    async def discuss(self, ctx: DiscussionContext) -> DiscussionResult:
        """
        Run a real multi-agent discussion round.

        The result is best-effort: if no peers respond, if everything times
        out, or if no consensus can be extracted, the result is still
        returned — the caller is expected to check `.successful()` and
        fall back if needed.
        """
        start = time.time()
        result = DiscussionResult(context=ctx)

        peers = self._select_peers(ctx)
        if not peers:
            result.no_peers_available = True
            result.error = "No peers available for this discussion"
            result.duration_seconds = time.time() - start
            return result

        try:
            # Step 1: asker states the problem
            opening = self._format_opening(ctx)
            result.transcript.append(DiscussionMessage(
                agent=self.agent_name,
                role=DiscussionRole.ASKER,
                message=opening,
            ))

            # Step 2: each peer provides a perspective, bounded by timeout
            advisor_messages = await asyncio.wait_for(
                self._gather_peer_perspectives(peers, ctx, result),
                timeout=ctx.timeout_seconds,
            )

            # Step 3: synthesize
            synthesis = self._synthesize(ctx, advisor_messages)
            result.transcript.append(DiscussionMessage(
                agent=self.agent_name,
                role=DiscussionRole.SYNTHESIZER,
                message=synthesis,
            ))

            # Step 4: extract structured consensus
            result.consensus = self._extract_consensus(advisor_messages, synthesis)

        except asyncio.TimeoutError:
            result.timed_out = True
            result.error = f"Discussion timed out after {ctx.timeout_seconds}s"
        except Exception as e:
            logger.exception("Discussion failed")
            result.error = f"{type(e).__name__}: {e}"

        result.duration_seconds = time.time() - start
        return result

    def _select_peers(self, ctx: DiscussionContext) -> list[tuple[str, PeerProvider]]:
        all_peers = self._all_peers()
        if ctx.max_participants <= 0:
            return []
        scored = [
            (name, provider, self._score_peer(name, provider, ctx))
            for name, provider in all_peers.items()
        ]
        scored.sort(key=lambda x: x[2], reverse=True)
        return [(name, provider) for name, provider, _ in scored[:ctx.max_participants]]

    def _format_opening(self, ctx: DiscussionContext) -> str:
        parts = [f"I'm working on: {ctx.task_input[:300]}"]
        if ctx.error:
            parts.append(f"Error encountered: {ctx.error[:200]}")
        parts.append(f"My capabilities: {', '.join(ctx.agent_capabilities[:5])}")
        parts.append("I'd like your perspective. In 1-2 sentences, what should I try next?")
        return " ".join(parts)

    async def _gather_peer_perspectives(
        self,
        peers: list[tuple[str, PeerProvider]],
        ctx: DiscussionContext,
        result: DiscussionResult,
    ) -> list[DiscussionMessage]:
        """Ask each peer in parallel; each peer gets a bounded turn."""
        async def ask_one(peer_name: str, provider: PeerProvider) -> DiscussionMessage:
            peer_start = time.time()
            peer_prompt = self._format_peer_prompt(peer_name, ctx)
            peer_ctx = TaskContextLike(
                task_id=f"{ctx.task_id}-peer-{peer_name}",
                input=peer_prompt,
                metadata={"discussion": True, "original_agent": self.agent_name},
            )
            try:
                # Peer functions may be sync or async — handle both
                peer_timeout = max(2.0, ctx.timeout_seconds / max(1, len(peers)))
                output = await asyncio.wait_for(
                    _invoke_peer(provider, peer_name, peer_ctx),
                    timeout=peer_timeout,
                )
                msg_text = self._extract_peer_message(output)
            except asyncio.TimeoutError:
                msg_text = "[peer did not respond in time]"
            except Exception as e:
                msg_text = f"[peer error: {type(e).__name__}]"
                logger.warning(f"Peer {peer_name} failed: {e}")

            return DiscussionMessage(
                agent=peer_name,
                role=DiscussionRole.ADVISOR,
                message=msg_text,
                duration_ms=(time.time() - peer_start) * 1000,
                unavailable=msg_text.startswith("["),
            )

        messages = await asyncio.gather(
            *[ask_one(name, provider) for name, provider in peers],
            return_exceptions=False,
        )
        result.transcript.extend(messages)
        # Only return successful messages for consensus
        return [m for m in messages if not m.unavailable]

    def _format_peer_prompt(self, peer_name: str, ctx: DiscussionContext) -> str:
        return (
            f"You are '{peer_name}'. A colleague asked for help with this task:\n\n"
            f"Task: {ctx.task_input[:400]}\n"
            + (f"Error: {ctx.error[:300]}\n" if ctx.error else "")
            + f"\nThe asker has capabilities: {', '.join(ctx.agent_capabilities[:5])}.\n\n"
            f"Reply with a brief (1-2 sentences) practical suggestion. "
            f"Focus on what they should try next, not on restating the problem."
        )

    def _extract_peer_message(self, output: Any) -> str:
        """Pull the human-readable suggestion from a peer's output object."""
        if output is None:
            return "[no output]"
        if isinstance(output, str):
            return output[:500]
        if hasattr(output, "payload") and isinstance(output.payload, dict):
            payload = output.payload
            for key in ("summary", "suggestion", "advice", "message", "response", "answer", "raw_output"):
                if payload.get(key):
                    return str(payload[key])[:500]
            return str(payload)[:500]
        if isinstance(output, dict):
            return str(output.get("summary") or output.get("response") or output)[:500]
        return str(output)[:500]

    def _synthesize(self, ctx: DiscussionContext, advisor_messages: list[DiscussionMessage]) -> str:
        if not advisor_messages:
            return "No actionable input from peers."
        bullets = "\n".join(f"- {m.agent}: {m.message[:200]}" for m in advisor_messages)
        return (
            f"Peer advice received:\n{bullets}\n\n"
            f"Next step: pick the most actionable suggestion above and retry."
        )

    def _extract_consensus(
        self,
        advisor_messages: list[DiscussionMessage],
        synthesis: str,
    ) -> Consensus:
        """Build a structured Consensus from the advisor messages."""
        if not advisor_messages:
            return Consensus(
                summary=synthesis,
                actionable_suggestion="",
                confidence=0.0,
                agreement_ratio=0.0,
            )

        # Crude agreement: how many advisors mention similar keywords?
        # (Real implementation could use embeddings — keeping it simple here.)
        all_text = " ".join(m.message.lower() for m in advisor_messages)
        if not all_text.strip():
            return Consensus(
                summary=synthesis,
                actionable_suggestion="",
                confidence=0.0,
                agreement_ratio=0.0,
            )

        # Pick the longest non-trivial message as the actionable suggestion
        sorted_msgs = sorted(advisor_messages, key=lambda m: len(m.message), reverse=True)
        actionable = sorted_msgs[0].message[:300]

        # Estimate agreement by simple keyword overlap
        words = [w for w in all_text.split() if len(w) > 4]
        unique = set(words)
        overlap = len(words) / max(len(unique), 1)
        agreement = min(1.0, overlap / 2.0)  # tune
        confidence = 0.3 + 0.7 * agreement   # base + agreement bonus

        return Consensus(
            summary=f"Discussed with {len(advisor_messages)} peer(s).",
            actionable_suggestion=actionable,
            confidence=round(confidence, 2),
            agreement_ratio=round(agreement, 2),
            dissenting_views=[],  # Future: extract via contrast detection
        )
