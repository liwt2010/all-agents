"""
CEO Agent — escalation handler (iteration 4)

Receives ESCALATE events and decides:
1. Handle directly (if within capability)
2. Assign expert agent
3. Change rules
4. Call human (as last resort)
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from agent_system.core.registry import register_agent
from agent_system.core.agent import SmartAgent, TaskContext, event_bus, EventType, AgentEvent
from agent_system.core.schema import OutputSchema, NextStep
from agent_system.core.resolver import ResolutionPath, ResolutionStatus
from agent_system.memory.graph import get_graph
from agent_system.memory.experience import record_experience, record_task_failure

logger = logging.getLogger(__name__)


class EscalationDecision(BaseModel):
    """CEO's decision on an escalation"""
    action: str  # handle / assign / change_rules / call_human
    assigned_to: str | None = None
    instructions: str = ""
    reason: str = ""


class CEOEscalationHandler:
    """Handles escalation events for the CEO Agent"""

    def __init__(self, ceo_agent: "CEOAgent"):
        self.ceo = ceo_agent

    async def handle_escalation(
        self,
        task_id: str,
        agent_name: str,
        error: str,
        severity: str,
        analysis: str,
        capabilities: list[str],
        similar_experiences: list[dict],
    ) -> EscalationDecision:
        """Make a decision on an escalated task"""
        logger.info(f"CEO handling escalation: task={task_id} agent={agent_name} severity={severity}")

        # 1. Try to handle directly (if CEO understands the problem)
        if self._can_handle_directly(error, capabilities):
            return EscalationDecision(
                action="handle",
                instructions=f"Provide guidance to {agent_name} on resolving: {error[:200]}",
                reason="CEO has relevant context to provide guidance",
            )

        # 2. Assign to an expert agent
        expert = self._find_expert(error, capabilities)
        if expert:
            return EscalationDecision(
                action="assign",
                assigned_to=expert,
                instructions=f"Please help resolve this issue for {agent_name}: {error[:200]}",
                reason=f"Assigning to expert agent: {expert}",
            )

        # 3. Change rules / modify approach
        if severity in ("high", "critical"):
            return EscalationDecision(
                action="change_rules",
                instructions=f"Adjust the approach for task {task_id}. Current error: {error[:200]}",
                reason="High severity issue requires approach adjustment",
            )

        # 4. Last resort: call human
        return EscalationDecision(
            action="call_human",
            instructions=f"Escalating to human: task={task_id} agent={agent_name} error={error[:300]}",
            reason="CEO cannot resolve this automatically",
        )

    def _can_handle_directly(self, error: str, capabilities: list[str]) -> bool:
        """CEO tries to determine if it can provide direct guidance"""
        resolvable_patterns = [
            "resource constraint", "quota", "limit",
            "approach", "strategy", "coordination",
            "priority", "conflict",
        ]
        error_lower = error.lower()
        return any(p in error_lower for p in resolvable_patterns)

    def _find_expert(self, error: str, excluded_capabilities: list[str]) -> str | None:
        """Find the best expert agent for this problem"""
        error_lower = error.lower()

        expert_map = [
            ("tech_agent", ["code", "implement", "compile", "build", "dependency", "api"]),
            ("test_agent", ["test", "coverage", "assertion", "mock", "fixture"]),
            ("product_agent", ["requirement", "spec", "prd", "feature", "user story"]),
        ]

        for expert_name, keywords in expert_map:
            if any(kw in error_lower for kw in keywords):
                # Don't assign to the agent that already failed
                if expert_name not in excluded_capabilities:
                    return expert_name

        return None


@register_agent
class CEOAgent(SmartAgent):
    """CEO Agent — orchestrates multi-agent workflows and handles escalations"""

    agent_name: str = "ceo_agent"
    agent_capabilities: list = [
        "task orchestration",
        "resource allocation",
        "quality review",
        "escalation handling",
        "expert assignment",
    ]
    description: str = "CEO Agent — coordinates multi-agent workflows and handles escalations"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        object.__setattr__(self, '_escalation_handler', CEOEscalationHandler(self))
        # Subscribe to escalation events
        event_bus.subscribe(EventType.ESCALATED, self._on_escalation)

    async def _on_escalation(self, event: AgentEvent):
        """Handle escalation events"""
        logger.info(f"CEO received escalation: {event.task_id} from {event.agent_name}")
        # The actual decision happens in do_work when the CEO runs
        # This subscription is for logging/monitoring

    async def do_work(self, task: TaskContext) -> OutputSchema:
        """CEO decides on the best course of action"""
        # Check if this is an escalation request
        is_escalation = task.metadata.get("is_escalation", False)

        if is_escalation:
            return await self._handle_escalation_task(task)

        # Default: run the standard pipeline
        return await self._run_pipeline(task)

    async def _handle_escalation_task(self, task: TaskContext) -> OutputSchema:
        """Handle an escalated task"""
        escalation_data = task.metadata.get("escalation_data", {})

        decision = await self._escalation_handler.handle_escalation(
            task_id=task.task_id,
            agent_name=escalation_data.get("agent", "unknown"),
            error=escalation_data.get("error", "Unknown error"),
            severity=escalation_data.get("severity", "low"),
            analysis=escalation_data.get("analysis", ""),
            capabilities=escalation_data.get("capabilities", []),
            similar_experiences=escalation_data.get("similar_experiences", []),
        )

        # Record the decision as experience
        graph = get_graph()
        record_experience(
            graph,
            task.task_id,
            f"CEO escalation decision: {decision.action} - {decision.reason}",
            self.agent_name,
            success=decision.action != "call_human",
        )

        return OutputSchema(
            id=OutputSchema.generate_id("ceo-decision"),
            type="decision",
            created_at=datetime.now(timezone.utc),
            created_by=self.agent_name,
            schema_version="1.0",
            payload={
                "decision": decision.model_dump(),
                "original_task_id": escalation_data.get("task_id"),
                "original_agent": escalation_data.get("agent"),
                "severity": escalation_data.get("severity"),
            },
            metadata={
                "task_input": task.input,
                "is_escalation": True,
            },
            next_steps=[
                NextStep(action=decision.action, agent=decision.assigned_to or "human",
                        description=decision.instructions),
            ],
        )

    async def _run_pipeline(self, task: TaskContext) -> OutputSchema:
        """Run the standard product development pipeline"""
        from agent_system.agents.product_agent import ProductAgent
        from agent_system.agents.tech_agent import TechAgent
        from agent_system.agents.test_agent import TestAgent

        steps = []

        # Step 1: Product Agent
        await event_bus.publish(AgentEvent(
            event_type=EventType.TASK_STARTED, agent_name=self.agent_name,
            task_id=task.task_id, data={"step": "product_agent"},
        ))
        try:
            product = ProductAgent()
            prd_task = TaskContext(
                task_id=f"{task.task_id}-prd",
                input=f"Write a detailed PRD for: {task.input}",
                max_retries=2,
            )
            prd_output = await product.execute(prd_task)
            steps.append({"agent": "product_agent", "status": "completed", "output_id": prd_output.id})
        except Exception as e:
            steps.append({"agent": "product_agent", "status": "failed", "error": str(e)})
            return self._pipeline_result(task, steps, f"Product Agent failed: {e}")

        # Step 2: Tech Agent
        try:
            tech = TechAgent()
            code_task = TaskContext(
                task_id=f"{task.task_id}-code",
                input=f"Implement code based on the PRD for: {task.input}",
                upstream_output=prd_output.model_dump(mode="json"),
                max_retries=2,
            )
            code_output = await tech.execute(code_task)
            steps.append({"agent": "tech_agent", "status": "completed", "output_id": code_output.id})
        except Exception as e:
            steps.append({"agent": "tech_agent", "status": "failed", "error": str(e)})
            return self._pipeline_result(task, steps, f"Tech Agent failed: {e}")

        # Step 3: Test Agent
        try:
            test = TestAgent()
            test_task = TaskContext(
                task_id=f"{task.task_id}-test",
                input=f"Generate tests for: {task.input}",
                upstream_output=code_output.model_dump(mode="json"),
                max_retries=2,
            )
            test_output = await test.execute(test_task)
            steps.append({"agent": "test_agent", "status": "completed", "output_id": test_output.id})
        except Exception as e:
            steps.append({"agent": "test_agent", "status": "failed", "error": str(e)})
            return self._pipeline_result(task, steps, f"Test Agent failed: {e}")

        # Step 4: Deploy Agent (PLATFORM §5.1 — 5 core agents)
        try:
            from agent_system.agents.deploy_agent import DeployAgent
            deploy = DeployAgent()
            deploy_task = TaskContext(
                task_id=f"{task.task_id}-deploy",
                input=f"Plan deployment for: {task.input}",
                upstream_output=test_output.model_dump(mode="json"),
                max_retries=1,
            )
            deploy_output = await deploy.execute(deploy_task)
            steps.append({"agent": "deploy_agent", "status": "completed", "output_id": deploy_output.id})
        except Exception as e:
            steps.append({"agent": "deploy_agent", "status": "failed", "error": str(e)})
            # Don't fail the whole pipeline on deploy-plan failure; the
            # plan itself is informational at this stage.
            logger.warning(f"Deploy Agent failed: {e}")

        return self._pipeline_result(task, steps)

    def _pipeline_result(self, task: TaskContext, steps: list, error: str | None = None) -> OutputSchema:
        return OutputSchema(
            id=OutputSchema.generate_id("pipeline"),
            type="pipeline_result",
            created_at=datetime.now(timezone.utc),
            created_by=self.agent_name,
            schema_version="1.0",
            payload={
                "pipeline_status": "failed" if error else "completed",
                "steps": steps,
                "summary": {
                    "total_steps": len(steps),
                    "completed": sum(1 for s in steps if s["status"] == "completed"),
                    "failed": sum(1 for s in steps if s["status"] == "failed"),
                },
                "error": error,
            },
            metadata={"input": task.input},
            next_steps=[NextStep(action="review", agent="human", description="Review pipeline results")] if not error else [],
        )
