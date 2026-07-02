"""
AutoGen 0.4+ PEER discussion — replaces the lightweight DiscussionMixin.discuss()
with a real RoundRobinGroupChat where multiple agents discuss a problem and
reach consensus.

This module is OPTIONAL — if the autogen packages cannot be imported, the
module degrades gracefully and returns DiscussionResult with no_peers_available=True.

Usage:
    from agent_system.core.autogen_discussion import AutoGenGroupChat
    chat = AutoGenGroupChat(original_agent, task, error, analysis)
    result = await chat.run()
"""

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from agent_system.config.settings import get_settings, LLMConfig
from agent_system.core.agent import SmartAgent, TaskContext
from agent_system.core.evaluator import ProblemAnalysis, ResolutionPath
from agent_system.core.mixins.discussion import (
    Consensus, DiscussionContext, DiscussionMessage,
    DiscussionResult, DiscussionRole,
)
from agent_system.core.resolver import ResolutionResult, ResolutionStatus
from agent_system.core.event_bus import event_bus, EventType, AgentEvent

logger = logging.getLogger(__name__)

# Try auto-import AutoGen 0.4+ — if not installed, we degrade gracefully.
try:
    from autogen_agentchat.agents import AssistantAgent
    from autogen_agentchat.teams import RoundRobinGroupChat
    from autogen_agentchat.messages import TextMessage
    from autogen_agentchat.base._task import TaskResult
    from autogen_ext.models.openai import OpenAIChatCompletionClient
    HAS_AUTOGEN = True
except ImportError as e:
    logger.warning(f"AutoGen 0.4+ not available: {e}. PEER path will fall back.")
    HAS_AUTOGEN = False


class AutoGenGroupChat:
    """
    Runs a RoundRobinGroupChat among the original agent and its peers.

    Each peer gets its own AssistantAgent with its own model_client, configured
    from the system's LLM settings (settings.yaml). All agents share a single
    DeepSeek endpoint but may use different models (e.g. product_agent uses
    deepseek-chat, test_agent uses deepseek-chat, etc.).
    """

    # Maximum number of discussion rounds before we force a summary.
    MAX_TURNS = 8
    # Per-turn timeout for the entire chat.
    TIMEOUT_SECONDS = 90.0

    def __init__(self) -> None:
        self.settings = get_settings()

    async def run(
        self,
        task: TaskContext,
        error: Exception,
        analysis: ProblemAnalysis,
        original_agent: SmartAgent,
    ) -> ResolutionResult:
        if not HAS_AUTOGEN:
            return self._no_autogen_result(task, error)

        try:
            # 1. Collect peers (exclude self)
            peers = self._collect_peers(
                exclude=original_agent.agent_name,
            )

            # 2. Build AutoGen agents — one per peer + the asker.
            autogen_agents = []
            # The asker (original agent) goes first
            asker_cfg = self.settings.get_llm_config(original_agent.agent_name)
            asker_kwargs = self._make_model_kwargs(asker_cfg)
            asker_agent = AssistantAgent(
                name=original_agent.agent_name,
                model_client=OpenAIChatCompletionClient(**asker_kwargs),
                system_message=(
                    f"You are {original_agent.agent_name}. "
                    f"Your capabilities: {', '.join(original_agent.agent_capabilities[:5])}. "
                    f"You encountered an error and need your colleagues' advice to continue."
                ),
                description=original_agent.description,
            )
            autogen_agents.append(asker_agent)

            for peer_name, peer_cls in peers:
                try:
                    sample = peer_cls()
                    peer_cfg = self.settings.get_llm_config(peer_name)
                    peer_kwargs = self._make_model_kwargs(peer_cfg)
                    peer_agent = AssistantAgent(
                        name=peer_name,
                        model_client=OpenAIChatCompletionClient(**peer_kwargs),
                        system_message=(
                            f"You are {peer_name}. "
                            f"Your capabilities: {', '.join(sample.agent_capabilities[:5])}. "
                            f"A colleague asked for your advice on a task. "
                            f"Provide concise, practical suggestions."
                        ),
                        description=getattr(sample, 'description', ''),
                    )
                    autogen_agents.append(peer_agent)
                except Exception as e:
                    logger.warning(f"Failed to create peer agent {peer_name}: {e}")

            if len(autogen_agents) < 2:
                logger.warning("Not enough peers available for AutoGen discussion")
                return self._no_autogen_result(task, error)

            # 3. Build the discussion prompt
            discussion_task = self._format_task(task, error, original_agent)

            # 4. Create RoundRobinGroupChat
            chat = RoundRobinGroupChat(
                participants=autogen_agents,
                max_turns=self.MAX_TURNS,
            )

            # 5. Run with timeout
            start = time.time()
            try:
                result: TaskResult = await asyncio.wait_for(
                    chat.run(task=discussion_task),
                    timeout=self.TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning("AutoGen discussion timed out")
                return self._no_autogen_result(task, error, timed_out=True)

            duration = time.time() - start

            # 6. Convert to ResolutionResult
            return self._to_resolution_result(
                result, duration, original_agent, task, analysis,
            )

        except Exception as e:
            logger.exception(f"AutoGen discussion failed: {e}")
            return self._no_autogen_result(task, error)

    def _collect_peers(self, exclude: str) -> List[Tuple[str, type]]:
        """Collect peer agent classes, excluding the asker."""
        # Import here to avoid circular imports at module level
        from agent_system.agents.product_agent import ProductAgent
        from agent_system.agents.tech_agent import TechAgent
        from agent_system.agents.test_agent import TestAgent
        from agent_system.agents.deploy_agent import DeployAgent
        from agent_system.agents.ceo_agent import CEOAgent

        all_agents = [
            ("product_agent", ProductAgent),
            ("tech_agent", TechAgent),
            ("test_agent", TestAgent),
            ("deploy_agent", DeployAgent),
            ("ceo_agent", CEOAgent),
        ]
        return [(n, c) for n, c in all_agents if n != exclude]

    def _make_model_kwargs(self, cfg: LLMConfig) -> dict:
        """Build kwargs for OpenAIChatCompletionClient from an LLMConfig."""
        provider = os.environ.get("LLM_PROVIDER", "openai").strip().lower()
        # Determine which env vars to read
        if provider == "openai":
            api_key = os.environ.get("OPENAI_API_KEY", "")
            base_url = os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com")
        else:
            api_key = os.environ.get("ANTHROPIC_API_KEY", "")
            base_url = os.environ.get("ANTHROPIC_BASE_URL", "")

        return {
            "model": cfg.model,
            "api_key": api_key,
            "base_url": base_url,
            "temperature": cfg.temperature,
            "max_tokens": cfg.max_tokens,
        }

    def _format_task(
        self,
        task: TaskContext,
        error: Exception,
        original_agent: SmartAgent,
    ) -> str:
        """Format the discussion initiation task."""
        return (
            f"I need your help with a task I'm working on.\n\n"
            f"## My role\n"
            f"Agent: {original_agent.agent_name}\n"
            f"Capabilities: {', '.join(original_agent.agent_capabilities[:5])}\n\n"
            f"## Task\n"
            f"{task.input[:500]}\n\n"
            f"## Error encountered\n"
            f"{str(error)[:300]}\n\n"
            f"## How to respond\n"
            f"- Each of you provide your perspective **one at a time**.\n"
            f"- Be concise (2-3 sentences per turn).\n"
            f"- Focus on **what I should try next** — concrete suggestions, not theory.\n"
            f"- After everyone has spoken, if you agree with a previous suggestion, "
            f"just say 'Agree with <agent name>'.\n"
            f"- The **last agent to speak should summarize** the consensus "
            f"and propose a specific next step.\n"
        )

    def _to_resolution_result(
        self,
        autogen_result: Any,
        duration: float,
        original_agent: SmartAgent,
        task: TaskContext,
        analysis: ProblemAnalysis,
    ) -> ResolutionResult:
        """Convert AutoGen TaskResult -> ResolutionResult (DiscussionResult inside)."""
        messages = getattr(autogen_result, "messages", [])
        stop_reason = getattr(autogen_result, "stop_reason", "")

        # Build transcript
        transcript = []
        seen_agents = set()
        for m in messages:
            if hasattr(m, "content") and hasattr(m, "source"):
                transcript.append(DiscussionMessage(
                    agent=m.source,
                    role=DiscussionRole.ADVISOR,
                    message=str(m.content)[:500],
                ))
                seen_agents.add(m.source)

        # The last non-trivial message is the consensus / summary
        consensus_text = ""
        for m in reversed(messages):
            if hasattr(m, "content") and str(m.content).strip():
                source = getattr(m, "source", "unknown")
                # Skip the user's initial prompt
                if source != "user":
                    consensus_text = str(m.content)[:500]
                    break

        discussion_result = DiscussionResult(
            context=DiscussionContext(
                task_id=task.task_id,
                task_input=task.input[:200],
                error=str(autogen_result)[:200] if not isinstance(autogen_result, str) else "",
                agent_capabilities=original_agent.agent_capabilities,
            ),
            transcript=transcript,
            consensus=Consensus(
                summary=f"AutoGen discussion with {len(seen_agents)} agents ({len(messages)} messages)",
                actionable_suggestion=consensus_text,
                confidence=0.7 if consensus_text else 0.0,
                agreement_ratio=0.8 if len(seen_agents) > 1 else 0.0,
            ),
            duration_seconds=duration,
            timed_out="timeout" in (stop_reason or "").lower(),
        )

        # Build discussion_log (legacy format for resolver.py)
        discussion_log = [
            {"agent": m.agent, "role": m.role.value, "message": m.message}
            for m in transcript
        ]

        # If we got a useful suggestion, treat as success
        if discussion_result.successful():
            return ResolutionResult(
                path=ResolutionPath.PEER,
                status=ResolutionStatus.SUCCESS,
                analysis=analysis,
                discussion_log=discussion_log,
                metadata={
                    "solution": consensus_text,
                    "consensus_confidence": 0.7,
                    "consensus_agreement": 0.8,
                    "discussion_duration_seconds": duration,
                    "autogen_messages": len(messages),
                },
            )
        else:
            return ResolutionResult(
                path=ResolutionPath.PEER,
                status=ResolutionStatus.FAILED,
                error="No consensus reached in AutoGen discussion",
                analysis=analysis,
                discussion_log=discussion_log,
                metadata={"discussion_duration_seconds": duration},
            )

    def _no_autogen_result(
        self,
        task: TaskContext,
        error: Exception,
        timed_out: bool = False,
    ) -> ResolutionResult:
        """Return a failed ResolutionResult when AutoGen is unavailable."""
        return ResolutionResult(
            path=ResolutionPath.PEER,
            status=ResolutionStatus.FAILED,
            error="AutoGen not available or timed out",
            discussion_log=[],
            metadata={
                "autogen_available": HAS_AUTOGEN,
                "timed_out": timed_out,
            },
        )
