"""
SmartResolver — 4-way escalation dispatcher (ARCHITECTURE.md 5.2)

SELF      -> Retry do_work() (up to 2 times with modification)
PEER      -> Discuss with peer agents via group chat
HUMAN     -> Direct human approval (irreversible/compliance)
ESCALATE  -> Escalate to CEO Agent for coordination

Decision flow:
  ProblemEvaluator -> SmartResolver -> ResolutionResult
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from agent_system.core.evaluator import (
    ProblemAnalysis,
    ProblemEvaluator,
    ResolutionPath,
    Severity,
    evaluator as default_evaluator,
)
from agent_system.core.agent import (
    SmartAgent,
    TaskContext,
    OutputSchema,
    event_bus,
    EventType,
    AgentEvent,
)
from agent_system.core.schema import NextStep
from agent_system.core.mixins.discussion import (
    DiscussionResult,
)
from agent_system.memory.graph import get_graph
from agent_system.memory.experience import (
    record_task_failure,
    record_experience,
)

logger = logging.getLogger(__name__)


class ResolutionStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    PENDING = "pending"
    CANCELLED = "cancelled"


class ResolutionResult(BaseModel):
    """Result of a resolution attempt"""
    path: ResolutionPath
    status: ResolutionStatus
    output: OutputSchema | None = None
    error: str | None = None
    analysis: ProblemAnalysis | None = None
    discussion_log: list[dict[str, Any]] = Field(default_factory=list)
    human_request: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PeerDiscussionMessage(BaseModel):
    """Message in a peer discussion"""
    agent: str
    content: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SmartResolver:
    """
    4-way escalation dispatcher.

    Usage:
        resolver = SmartResolver(agent)
        result = await resolver.resolve(task, error, analysis)
    """

    def __init__(
        self,
        agent: SmartAgent,
        evaluator: ProblemEvaluator | None = None,
    ):
        self.agent = agent
        self.evaluator = evaluator or default_evaluator

    async def resolve(
        self,
        task: TaskContext,
        error: Exception,
        analysis: ProblemAnalysis | None = None,
    ) -> ResolutionResult:
        """Run the full resolution pipeline: evaluate -> execute path"""
        # 1. Analyze if not provided
        if analysis is None:
            attempted_action = task.metadata.get("action", None)
            analysis = self.evaluator.evaluate(
                error_message=str(error),
                agent_name=self.agent.agent_name,
                agent_capabilities=self.agent.agent_capabilities,
                attempted_action=attempted_action,
                task_input=task.input,
                retry_count=task.retry_count,
            )

        logger.info(
            f"Resolver: {self.agent.agent_name} task={task.task_id} "
            f"path={analysis.suggested_path.value} "
            f"confidence={analysis.confidence:.2f}"
        )

        # 2. Route to the chosen path
        path = analysis.suggested_path

        if path == ResolutionPath.SELF:
            return await self._resolve_self(task, error, analysis)
        elif path == ResolutionPath.PEER:
            return await self._resolve_peer(task, error, analysis)
        elif path == ResolutionPath.HUMAN:
            return await self._resolve_human(task, error, analysis)
        elif path == ResolutionPath.ESCALATE:
            return await self._resolve_escalate(task, error, analysis)
        else:
            return ResolutionResult(
                path=path,
                status=ResolutionStatus.FAILED,
                error=f"Unknown resolution path: {path}",
                analysis=analysis,
            )

    # ── Path A: SELF ──

    async def _resolve_self(
        self,
        task: TaskContext,
        error: Exception,
        analysis: ProblemAnalysis,
    ) -> ResolutionResult:
        """Retry do_work() with modifications"""
        modified_input = self._enrich_with_experience(task.input, analysis)

        # Retry do_work
        retry_task = TaskContext(
            task_id=task.task_id,
            input=modified_input,
            config=task.config,
            metadata={**task.metadata, "resolution_path": "self"},
            retry_count=task.retry_count + 1,
            max_retries=task.max_retries,
        )

        try:
            output = await self.agent.do_work(retry_task)

            # Record the successful self-resolution
            await event_bus.publish(AgentEvent(
                event_type=EventType.TASK_COMPLETED,
                agent_name=self.agent.agent_name,
                task_id=task.task_id,
                data={"resolution": "self", "output_id": output.id},
            ))

            return ResolutionResult(
                path=ResolutionPath.SELF,
                status=ResolutionStatus.SUCCESS,
                output=output,
                analysis=analysis,
                metadata={"modified_input": True, "retry_count": retry_task.retry_count},
            )

        except Exception as e:
            logger.warning(f"Self-resolution failed: {e}")

            # Self failed -> fall through to PEER or ESCALATE
            fallback_analysis = self.evaluator.evaluate(
                error_message=str(e),
                agent_name=self.agent.agent_name,
                agent_capabilities=self.agent.agent_capabilities,
                attempted_action=task.metadata.get("action"),
                task_input=task.input,
                retry_count=retry_task.retry_count,
            )

            if fallback_analysis.suggested_path == ResolutionPath.PEER:
                return await self._resolve_peer(retry_task, e, fallback_analysis)
            else:
                return await self._resolve_escalate(retry_task, e, fallback_analysis)

    # ── Path B: PEER ──

    async def _resolve_peer(
        self,
        task: TaskContext,
        error: Exception,
        analysis: ProblemAnalysis,
    ) -> ResolutionResult:
        """Real PEER path: discuss with peer agents via AutoGen or DiscussionMixin."""
        adapter = _PeerDiscussionAdapter(self.agent)
        discussion_result = await adapter.run_peer_discussion(task, error, analysis)

        # Build a legacy discussion dict from the DiscussionResult for
        # compatibility with the existing return shape.
        discussion_dicts = []
        for m in discussion_result.transcript:
            entry = {"agent": m.agent, "message": m.message}
            # role may be a DiscussionRole enum; convert to string
            role_val = m.role.value if hasattr(m.role, "value") else str(m.role)
            entry["role"] = role_val
            discussion_dicts.append(entry)

        if not discussion_result.successful():
            # No usable consensus — escalate to CEO
            return await self._resolve_escalate(task, error, analysis)

        solution = discussion_result.consensus.actionable_suggestion

        # Try with the peer-suggested approach
        enriched_input = f"{task.input}\n\n[Peer discussion insight]\n{solution}"
        retry_task = TaskContext(
            task_id=task.task_id,
            input=enriched_input,
            config=task.config,
            metadata={
                **task.metadata,
                "resolution_path": "peer",
                "peer_confidence": discussion_result.consensus.confidence,
                "peer_agreement": discussion_result.consensus.agreement_ratio,
            },
            retry_count=task.retry_count + 1,
            max_retries=task.max_retries,
        )

        try:
            output = await self.agent.do_work(retry_task)

            await event_bus.publish(AgentEvent(
                event_type=EventType.TASK_COMPLETED,
                agent_name=self.agent.agent_name,
                task_id=task.task_id,
                data={"resolution": "peer_discussion", "output_id": output.id},
            ))

            return ResolutionResult(
                path=ResolutionPath.PEER,
                status=ResolutionStatus.SUCCESS,
                output=output,
                analysis=analysis,
                discussion_log=discussion_dicts,
                metadata={
                    "solution": solution,
                    "consensus_confidence": discussion_result.consensus.confidence,
                    "consensus_agreement": discussion_result.consensus.agreement_ratio,
                    "discussion_duration_seconds": discussion_result.duration_seconds,
                },
            )
        except Exception:
            pass

        # Peer also failed -> escalate to CEO
        return await self._resolve_escalate(task, error, analysis)

    # ── Path C: HUMAN ──

    async def _resolve_human(
        self,
        task: TaskContext,
        error: Exception,
        analysis: ProblemAnalysis,
    ) -> ResolutionResult:
        """Generate an approval request for human review"""
        human_request = {
            "title": f"Approval Required: {self.agent.agent_name}",
            "task_id": task.task_id,
            "agent": self.agent.agent_name,
            "action": task.metadata.get("action", task.input[:200]),
            "reason": analysis.reasoning,
            "severity": analysis.severity.value,
            "risk_assessment": self._assess_risk(analysis),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "context": {
                "task_input": task.input[:500],
                "error": str(error)[:500],
                "capabilities": self.agent.agent_capabilities,
            },
            "options": [
                {"id": "approve", "label": "Approve", "description": "Allow the agent to proceed"},
                {"id": "reject", "label": "Reject", "description": "Cancel this operation"},
                {"id": "modify", "label": "Modify & Proceed", "description": "Adjust the approach and continue"},
            ],
        }

        await event_bus.publish(AgentEvent(
            event_type=EventType.HUMAN_REQUESTED,
            agent_name=self.agent.agent_name,
            task_id=task.task_id,
            data=human_request,
        ))

        logger.info(f"Human approval requested for task {task.task_id}")

        return ResolutionResult(
            path=ResolutionPath.HUMAN,
            status=ResolutionStatus.PENDING,
            analysis=analysis,
            human_request=human_request,
        )

    # ── Path D: ESCALATE ──

    async def _resolve_escalate(
        self,
        task: TaskContext,
        error: Exception,
        analysis: ProblemAnalysis,
    ) -> ResolutionResult:
        """Escalate to CEO Agent"""
        escalate_data = {
            "task_id": task.task_id,
            "agent": self.agent.agent_name,
            "severity": analysis.severity.value,
            "error": str(error)[:500],
            "analysis": analysis.reasoning,
            "capabilities": self.agent.agent_capabilities,
            "similar_experiences": analysis.similar_experiences,
        }

        await event_bus.publish(AgentEvent(
            event_type=EventType.ESCALATED,
            agent_name=self.agent.agent_name,
            task_id=task.task_id,
            data=escalate_data,
        ))

        logger.info(f"Escalated to CEO: task={task.task_id} severity={analysis.severity.value}")

        # Record the failure in the graph
        graph = get_graph()
        record_task_failure(graph, task.task_id, str(error), self.agent.agent_name)

        return ResolutionResult(
            path=ResolutionPath.ESCALATE,
            status=ResolutionStatus.FAILED,
            error=str(error),
            analysis=analysis,
            metadata={"escalated_to": "ceo_agent"},
        )

    # ── Utility methods ──

    def _enrich_with_experience(self, task_input: str, analysis: ProblemAnalysis) -> str:
        """Enrich task input with relevant past experiences for self-retry"""
        if not analysis.similar_experiences:
            return task_input

        enrichment = "\n\n[Relevant past experiences]\n"
        for exp in analysis.similar_experiences[:2]:
            enrichment += f"- {exp.get('summary', '')}\n"

        return task_input + enrichment

    async def _peer_round_robin(
        self,
        task: TaskContext,
        error: Exception,
        analysis: ProblemAnalysis,
        max_rounds: int = 3,
    ) -> list[dict[str, Any]]:
        """
        Real peer discussion using round-robin protocol.

        Each peer agent is asked for its perspective. The original agent then
        produces a refined approach based on peer input. Returns the full
        discussion log.
        """
        discussion = []

        # 1. Original agent: state the problem
        opening = (
            f"I'm stuck on task '{task.task_id}': {str(error)[:200]}. "
            f"Severity: {analysis.severity.value}, "
            f"action category: {analysis.action_category.value}."
        )
        discussion.append({
            "agent": self.agent.agent_name,
            "role": "asker",
            "message": opening,
        })

        # 2. Discover relevant peers (excluding self)
        peer_agents = self._discover_peers(analysis)
        if not peer_agents:
            discussion.append({
                "agent": "system",
                "role": "moderator",
                "message": "No relevant peers found for this problem.",
            })
            return discussion

        # 3. Each peer provides perspective
        for peer_name, peer_agent in peer_agents:
            try:
                perspective_prompt = (
                    f"You're a peer agent with capabilities: {peer_agent.agent_capabilities}. "
                    f"A colleague '{self.agent.agent_name}' is facing this error:\n\n"
                    f"Error: {str(error)[:300]}\n\n"
                    f"Context: {task.input[:200]}\n\n"
                    f"Give a brief (1-2 sentences) practical suggestion to help them resolve it."
                )
                peer_task = TaskContext(
                    task_id=f"{task.task_id}-peer-{peer_name}",
                    input=perspective_prompt,
                    max_retries=1,
                )
                peer_output = await peer_agent.do_work(peer_task)
                discussion.append({
                    "agent": peer_name,
                    "role": "advisor",
                    "message": peer_output.payload.get("summary") or peer_output.payload.get("raw_output", "")[:200],
                })
            except Exception as e:
                logger.warning(f"Peer {peer_name} could not contribute: {e}")
                discussion.append({
                    "agent": peer_name,
                    "role": "advisor",
                    "message": f"[unavailable: {type(e).__name__}]",
                })

        # 4. Original agent synthesizes the advice
        peer_inputs = "\n".join(
            f"- {d['agent']}: {d['message']}" for d in discussion
            if d.get("role") == "advisor"
        )
        synthesis_prompt = (
            f"Based on peer advice:\n{peer_inputs}\n\n"
            f"What is your refined approach to fix the original error?\n"
            f"Original error: {str(error)[:200]}"
        )
        discussion.append({
            "agent": self.agent.agent_name,
            "role": "synthesizer",
            "message": synthesis_prompt,
        })

        return discussion

    def _discover_peers(self, analysis: ProblemAnalysis) -> list[tuple[str, SmartAgent]]:
        """Find peer agents relevant to this problem.

        Selection strategy:
          - All @register_agent decorated agents (via AgentRegistry), excluding self
          - Rank by capability-overlap with the error text
          - Return at most 3 peers (cost-conscious)

        Adding a new agent class no longer requires editing this method.
        """
        from agent_system.core.registry import agent_registry

        # Discover peers via the global registry — no hardcoded agent list.
        peer_names = agent_registry.names_excluding(self.agent.agent_name)
        peers: list[tuple[str, SmartAgent]] = []
        for name in peer_names:
            instance = agent_registry.get_instance(name)
            if instance is not None:
                peers.append((name, instance))

        # Rank by capability-overlap with error text
        error_lower = analysis.error_summary.lower()

        def relevance_score(peer_agent: SmartAgent) -> int:
            score = 0
            for cap in (peer_agent.agent_capabilities or []):
                for word in cap.lower().split():
                    if len(word) > 4 and word in error_lower:
                        score += 1
            return score

        peers.sort(key=lambda p: relevance_score(p[1]), reverse=True)
        return peers[:3]  # Top 3 peers (was 2; registry opens more peers)

    def _extract_solution_from_discussion(self, discussion: list[dict[str, Any]]) -> str:
        """Extract the original agent's refined approach as the solution."""
        for msg in reversed(discussion):
            if msg.get("role") == "synthesizer":
                return msg["message"]
        for msg in discussion:
            if msg.get("role") == "advisor":
                return msg["message"]
        return "no_solution"

    def _assess_risk(self, analysis: ProblemAnalysis) -> dict[str, Any]:
        """Assess risk level for human approval request"""
        risk_map = {
            Severity.CRITICAL: {"level": "critical", "description": "May cause system-wide impact"},
            Severity.HIGH: {"level": "high", "description": "May cause significant impact"},
            Severity.MEDIUM: {"level": "medium", "description": "May cause moderate impact"},
            Severity.LOW: {"level": "low", "description": "Minimal impact expected"},
        }
        return risk_map.get(analysis.severity, {"level": "unknown", "description": "Unknown risk"})


# ── Bridge: SmartResolver -> DiscussionMixin / AutoGen ──

class _PeerDiscussionAdapter:
    """
    Wraps a SmartAgent and runs a multi-agent discussion using
    AutoGen 0.4+ RoundRobinGroupChat (preferred) or falls back to the
    legacy DiscussionMixin when AutoGen is not available.

    The adapter hides the implementation detail from SmartResolver: it
    always returns a DiscussionResult.
    """

    def __init__(self, original_agent: SmartAgent):
        self.agent = original_agent
        # Lazy import: try AutoGen first, fall back to DiscussionMixin
        self._use_autogen = True
        self._has_autogen = False
        try:
            from agent_system.core.autogen_discussion import AutoGenGroupChat, HAS_AUTOGEN
            # Only use AutoGen if the package is installed AND an OpenAI-compatible
            # key is configured. AutoGen uses OpenAIChatCompletionClient which only
            # works with OpenAI models (or models registered in its model_info DB).
            # For anthropic provider we always go straight to legacy DiscussionMixin.
            provider = os.environ.get("LLM_PROVIDER", "openai").strip().lower()
            openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
            self._has_autogen = (
                HAS_AUTOGEN and bool(openai_key) and provider == "openai"
            )
        except ImportError:
            self._has_autogen = False
        # ALWAYS init legacy mixin so the autogen->legacy fallback path has
        # `discuss` / `_all_peers` available. Previously the legacy init
        # only ran when _has_autogen=False, which left the adapter broken
        # when autogen was enabled but failed at runtime (e.g. model_info
        # missing for non-OpenAI models).
        self._init_legacy_mixin(original_agent)

    def _init_legacy_mixin(self, original_agent: SmartAgent):
        """Fallback: use the old DiscussionMixin (single-round parallel)."""
        from agent_system.core.mixins.discussion import DiscussionMixin, PeerProvider
        # Patch in the mixin's methods
        for attr in ("discuss", "_select_peers", "_format_opening",
                     "_gather_peer_perspectives", "_synthesize",
                     "_extract_consensus", "register_peer", "_all_peers",
                     "_score_peer", "_format_peer_prompt", "_extract_peer_message"):
            if hasattr(DiscussionMixin, attr):
                setattr(self, attr, getattr(DiscussionMixin, attr).__get__(self, _PeerDiscussionAdapter))
        # Set mixin attributes
        object.__setattr__(self, "agent_name", original_agent.agent_name)
        object.__setattr__(self, "agent_capabilities", list(original_agent.agent_capabilities))
        object.__setattr__(self, "_instance_peers", None)
        object.__setattr__(self, "_peer_registry", {})  # instance level
        # Also set on class so DiscussionMixin.register_peer's type(self) path works
        _PeerDiscussionAdapter._peer_registry = {}
        self._setup_default_peers(original_agent)

    def _setup_default_peers(self, original_agent: SmartAgent):
        """Register all @register_agent-decorated agents as default peers, except self."""
        from agent_system.core.mixins.discussion import PeerProvider
        from agent_system.core.registry import agent_registry

        # Auto-discover via the global registry — no hardcoded agent list.
        for name in agent_registry.names_excluding(original_agent.agent_name):
            cls = agent_registry.get_class(name)
            if cls is None:
                continue
            try:
                sample = cls()
            except Exception:
                continue
            caps = list(sample.agent_capabilities or [])
            provider = self._make_peer_provider(name, cls, caps)
            self.register_peer(name, provider)

    def _make_peer_provider(self, name: str, agent_cls, caps):
        """Build a PeerProvider whose peer_fn calls the agent's do_work()."""
        from agent_system.core.mixins.discussion import PeerProvider
        def make_peer_fn():
            async def peer_fn(peer_name, ctx):
                peer = agent_cls()
                prompt = (
                    f"You're a peer agent with capabilities: {peer.agent_capabilities}. "
                    f"A colleague '{self.agent_name}' is facing this issue:\n\n"
                    f"Context: {ctx.input[:300]}\n\n"
                    f"Reply with a brief (1-2 sentences) practical suggestion."
                )
                peer_ctx = TaskContext(
                    task_id=ctx.task_id,
                    input=prompt,
                    metadata={"peer_discussion": True, "original_agent": self.agent_name},
                    max_retries=1,
                )
                return await peer.do_work(peer_ctx)
            return peer_fn
        provider = PeerProvider(peer_fn=make_peer_fn())
        provider.capabilities = list(caps)
        return provider

    async def run_peer_discussion(
        self,
        task: TaskContext,
        error: Exception,
        analysis: ProblemAnalysis,
    ) -> DiscussionResult:
        """Run the discussion round — AutoGen first, legacy fallback."""
        # Try AutoGen path
        if self._has_autogen:
            try:
                from agent_system.core.autogen_discussion import AutoGenGroupChat
                chat = AutoGenGroupChat()
                result: ResolutionResult = await chat.run(task, error, analysis, self.agent)
                if result.status == "success":
                    # Convert ResolutionResult -> DiscussionResult (legacy format)
                    msg = result.discussion_log[-1]["message"] if result.discussion_log else ""
                    return DiscussionResult(
                        context=DiscussionContext(
                            task_id=task.task_id,
                            task_input=task.input[:200],
                            error=str(error)[:200],
                            agent_capabilities=list(self.agent.agent_capabilities),
                        ),
                        transcript=[],  # resolver._resolve_peer doesn't use transcript directly
                        consensus=Consensus(
                            summary=result.metadata.get("solution", "AutoGen discussion completed"),
                            actionable_suggestion=result.metadata.get("solution", msg),
                            confidence=result.metadata.get("consensus_confidence", 0.7),
                            agreement_ratio=result.metadata.get("consensus_agreement", 0.8),
                        ),
                        duration_seconds=result.metadata.get("discussion_duration_seconds", 0.0),
                    )
                # Otherwise, AutoGen failed — fall through to legacy
            except Exception as e:
                logger.warning(f"AutoGen discussion failed, falling back: {e}")

        # Legacy fallback
        from agent_system.core.mixins.discussion import DiscussionContext
        ctx = DiscussionContext(
            task_id=task.task_id,
            task_input=task.input,
            error=str(error),
            capability_hint=analysis.error_summary[:100] if analysis.error_summary else "",
            agent_capabilities=list(self.agent.agent_capabilities),
            max_participants=2,
            timeout_seconds=20.0,
        )
        return await self.discuss(ctx)
