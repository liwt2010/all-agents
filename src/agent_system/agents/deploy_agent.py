"""
Deploy Agent — PLATFORM §5.1 (5th core agent)

Pipeline stage after Test Agent. Takes tested code and produces a deployment
plan: environment rollout strategy, health checks, rollback procedure,
monitoring hooks. In mock mode the output is structured JSON describing
a safe canary deployment.

In production this would integrate with:
  - Kubernetes (kubectl, ArgoCD)
  - Terraform (infrastructure as code)
  - CI/CD pipelines (GitHub Actions, GitLab CI)
  - Monitoring (Prometheus, Datadog)
"""

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List

from pydantic import BaseModel, Field

from agent_system.core.registry import register_agent
from agent_system.core.agent import SmartAgent, TaskContext
from agent_system.core.schema import OutputSchema, NextStep


class DeployTarget(BaseModel):
    """A single environment to deploy to."""
    environment: str          # dev / staging / prod
    namespace: str = ""      # k8s namespace (e.g. tenant-acme)
    strategy: str = "rolling"  # rolling / canary / blue-green / recreate
    replicas: int = 1
    rollout_percent: int = 100


class HealthCheck(BaseModel):
    """A health/readiness check to run after deploy."""
    name: str
    type: str                # http / tcp / exec / grpc
    target: str              # e.g. "/healthz" or "tcp:5432"
    initial_delay_seconds: int = 30
    period_seconds: int = 10
    timeout_seconds: int = 5
    failure_threshold: int = 3
    success_threshold: int = 1


class RollbackPlan(BaseModel):
    """How to roll back if the deploy fails."""
    auto_rollback: bool = True
    trigger_conditions: List[str] = Field(default_factory=list)  # e.g. "error_rate > 5%"
    steps: List[str] = Field(default_factory=list)
    max_rollback_time_seconds: int = 600


@register_agent
class DeployAgent(SmartAgent):
    """5th core agent: deployment planning and execution."""

    agent_name: str = "deploy_agent"
    agent_capabilities: list = [
        "deployment planning",
        "CI/CD integration",
        "canary rollout",
        "rollback management",
        "infrastructure as code",
        "monitoring setup",
        "environment promotion",
    ]
    description: str = "Plans and executes deployments across environments with safe rollout and rollback"

    def _mock_deploy_plan(self, task: TaskContext, upstream: Dict[str, Any]) -> Dict[str, Any]:
        """Deterministic deploy plan used when no LLM API key is set."""
        env_chain = ["dev", "staging", "prod"]
        targets = [
            {
                "environment": "dev",
                "namespace": "dev",
                "strategy": "recreate",
                "replicas": 1,
                "rollout_percent": 100,
            },
            {
                "environment": "staging",
                "namespace": "staging",
                "strategy": "rolling",
                "replicas": 2,
                "rollout_percent": 100,
            },
            {
                "environment": "prod",
                "namespace": "prod",
                "strategy": "canary",
                "replicas": 6,
                "rollout_percent": 100,
            },
        ]

        health_checks = [
            {
                "name": "http_root",
                "type": "http",
                "target": "/healthz",
                "initial_delay_seconds": 30,
                "period_seconds": 10,
                "timeout_seconds": 5,
                "failure_threshold": 3,
                "success_threshold": 1,
            },
            {
                "name": "http_ready",
                "type": "http",
                "target": "/readyz",
                "initial_delay_seconds": 15,
                "period_seconds": 5,
                "timeout_seconds": 3,
                "failure_threshold": 3,
                "success_threshold": 1,
            },
        ]

        rollback = {
            "auto_rollback": True,
            "trigger_conditions": [
                "error_rate > 5% in 5m window",
                "p99_latency > 2x baseline for 3m",
                "sustained 5xx > 1% for 2m",
            ],
            "steps": [
                "1. Mark canary as failed",
                "2. Re-route traffic to stable pods",
                "3. Roll back image to previous version",
                "4. Verify health checks pass",
                "5. Notify on-call team",
            ],
            "max_rollback_time_seconds": 600,
        }

        # Build a canary schedule
        canary_schedule = [
            {"step": 1, "percent": 5,  "duration_min": 5,  "passes_to_next": "error_rate < 0.5%"},
            {"step": 2, "percent": 25, "duration_min": 10, "passes_to_next": "error_rate < 1%"},
            {"step": 3, "percent": 50, "duration_min": 15, "passes_to_next": "error_rate < 1%"},
            {"step": 4, "percent": 100,"duration_min": 20, "passes_to_next": "stable"},
        ]

        return {
            "summary": f"Deploy plan for: {task.input[:60]}",
            "pipeline": ["code", "test", "deploy:dev", "deploy:staging", "deploy:prod"],
            "current_stage": "deploy:prod",
            "targets": targets,
            "health_checks": health_checks,
            "canary_schedule": canary_schedule,
            "rollback": rollback,
            "estimated_duration_minutes": 60,
            "requires_human_approval": True,
            "approvers": ["tech_lead"],
            "monitoring": {
                "dashboards": ["golden_signals", "error_budget"],
                "alerts": ["deploy_failure", "rollback_triggered"],
            },
        }

    async def do_work(self, task: TaskContext) -> OutputSchema:
        router = self.llm_router
        config = router.get_config(self.agent_name)

        # Use upstream code (from Tech Agent) and test results (from Test Agent)
        upstream = task.upstream_output or {}

        # Mock mode (no API key) — return deterministic plan
        if router.is_mock_mode:
            plan = self._mock_deploy_plan(task, upstream)
            text = json.dumps(plan)
            usage = type("U", (), {"mock": True, "input_tokens": 0, "output_tokens": 0,
                                    "cost_estimate": 0.0, "duration_ms": 0.0,
                                    "num_retries": 0, "cache_read_tokens": 0,
                                    "cache_creation_tokens": 0})()
        else:
            system_prompt = self.get_system_prompt() + """

## Deploy Plan Output Format

Generate a JSON response with:
- summary: 1-line description of the deploy
- pipeline: ordered list of stages (build, test, deploy:dev, deploy:staging, deploy:prod)
- current_stage: which stage is being planned
- targets: list of {environment, namespace, strategy (rolling/canary/blue-green), replicas, rollout_percent}
- health_checks: list of {name, type, target, initial_delay_seconds, period_seconds, timeout_seconds, failure_threshold, success_threshold}
- canary_schedule: list of {step, percent, duration_min, passes_to_next}
- rollback: {auto_rollback, trigger_conditions, steps, max_rollback_time_seconds}
- estimated_duration_minutes: total expected deploy time
- requires_human_approval: bool
- approvers: list of role names that must approve
- monitoring: {dashboards, alerts}

Output ONLY valid JSON, no markdown code blocks.
"""
            messages = [
                {"role": "user", "content": (
                    f"Generate a deployment plan for the following:\n\n"
                    f"Task: {task.input}\n\n"
                    f"Upstream code/test: {json.dumps(upstream.get('payload', {}), ensure_ascii=False)[:500]}"
                )}
            ]
            text, usage = await router.call_llm(
                config, system_prompt, messages,
                _agent_name=self.agent_name, _task_id=task.task_id,
            )

        # Parse JSON
        payload = self._parse_json(text)

        # Track LLM usage
        if hasattr(usage, "input_tokens") and usage.input_tokens:
            self._last_usage = usage
        else:
            self._last_usage = None

        return OutputSchema(
            id=OutputSchema.generate_id("deploy"),
            type="deploy_plan",
            created_at=datetime.now(timezone.utc),
            created_by=self.agent_name,
            schema_version="1.0",
            payload=payload,
            metadata={
                "input": task.input,
                "model": getattr(usage, "model", "mock_mode"),
                "upstream_agent": (upstream.get("created_by", "") if upstream else ""),
            },
            next_steps=[
                NextStep(action="review", agent="human",
                        description="Human review for production deploy (PLATFORM §30)"),
                NextStep(action="execute_deploy", agent="deploy_agent",
                        description="Execute the deploy plan"),
            ],
        )

    def _parse_json(self, text: str) -> dict:
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            text = text.rsplit("```", 1)[0] if "```" in text else text
            text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw_output": text[:2000]}

    def _get_last_usage(self):
        return getattr(self, "_last_usage", None)
