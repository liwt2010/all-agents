"""
ProblemEvaluator — problem assessment engine (ARCHITECTURE.md 5.4)

Evaluates 3 dimensions:
- Severity (LOW/MEDIUM/HIGH/CRITICAL)
- Can self-solve? (checks experience store + capability assessment)
- Confidence (0-1)

Also detects irreversible/compliance actions -> routes directly to HUMAN.
"""

from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from agent_system.memory.graph import (
    NodeType,
    get_graph,
)
from agent_system.memory.experience import find_similar_failures


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ResolutionPath(str, Enum):
    SELF = "self"          # Retry with modifications
    PEER = "peer"          # Discuss with peer agents
    HUMAN = "human"        # Direct human approval (irreversible/compliance)
    ESCALATE = "escalate"  # Escalate to CEO Agent


class ActionCategory(str, Enum):
    NORMAL = "normal"
    IRREVERSIBLE = "irreversible"   # Delete data, deploy to prod, modify config
    COMPLIANCE = "compliance"       # Legal/regulatory
    HIGH_IMPACT = "high_impact"     # Affects >100 users or core system
    DANGEROUS = "dangerous"         # Delete data, production deploy


class ProblemAnalysis(BaseModel):
    """Structured analysis of a problem"""
    severity: Severity = Severity.LOW
    confidence: float = 0.0
    can_self_solve: bool = False
    needs_peer_help: bool = False
    action_category: ActionCategory = ActionCategory.NORMAL
    suggested_path: ResolutionPath = ResolutionPath.SELF
    similar_experiences: list[dict[str, Any]] = Field(default_factory=list)
    reasoning: str = ""
    error_summary: str = ""

    @property
    def should_route_direct_to_human(self) -> bool:
        return self.action_category in (
            ActionCategory.IRREVERSIBLE,
            ActionCategory.COMPLIANCE,
            ActionCategory.DANGEROUS,
        )


# Known irreversible/dangerous action patterns
IRREVERSIBLE_PATTERNS = [
    "delete", "drop", "truncate", "remove", "destroy",
    "deploy", "release", "publish", "production",
    "modify config", "change setting", "update config",
    "overwrite", "replace all", "mass update",
    "reset password", "change permission", "grant admin",
]

COMPLIANCE_PATTERNS = [
    "gdpr", "pii", "personal data", "compliance",
    "legal", "regulatory", "audit", "sensitive",
    "financial", "payment", "credit card", "hipaa",
    "soc2", "iso", "certification", "license",
]

HIGH_IMPACT_PATTERNS = [
    "all users", "mass", "bulk", "entire", "global",
    "production", "system-wide", "company-wide",
]


class ProblemEvaluator:
    """Assesses problems and determines the best resolution path"""

    def __init__(self):
        self.graph = get_graph()

    def evaluate(
        self,
        error_message: str,
        agent_name: str,
        agent_capabilities: list[str],
        attempted_action: str | None = None,
        task_input: str | None = None,
        retry_count: int = 0,
    ) -> ProblemAnalysis:
        """Evaluate a problem and recommend resolution path"""
        error_lower = error_message.lower()
        action_lower = (attempted_action or "").lower()
        input_lower = (task_input or "").lower()

        # 1. Determine action category
        action_cat = self._classify_action(action_lower, input_lower)

        # 2. If irreversible/compliance -> direct to human, skip further analysis
        if action_cat in (ActionCategory.IRREVERSIBLE, ActionCategory.COMPLIANCE, ActionCategory.DANGEROUS):
            return ProblemAnalysis(
                severity=Severity.HIGH if action_cat != ActionCategory.COMPLIANCE else Severity.MEDIUM,
                confidence=1.0,
                can_self_solve=False,
                needs_peer_help=False,
                action_category=action_cat,
                suggested_path=ResolutionPath.HUMAN,
                reasoning=f"Action classified as {action_cat.value}: requires human approval",
                error_summary=error_message[:500],
            )

        # 3. Analyze severity
        severity = self._assess_severity(error_lower, action_lower)

        # 4. Check past experiences for similar problems
        similar = find_similar_failures(self.graph, error_message, max_results=3)
        similar_data = [
            {
                "id": node.id,
                "summary": node.content.get("summary", "")[:200],
                "success": node.content.get("success", False),
            }
            for node, score in similar
        ]

        # 5. Assess self-solve capability
        can_self_solve, confidence = self._assess_self_solve(
            error_lower, agent_capabilities, similar, retry_count
        )

        # 6. Determine if peer help would be useful
        needs_peer = self._assess_peer_needed(
            severity, can_self_solve, confidence, error_lower, retry_count
        )

        # 7. Suggest the resolution path
        path = self._decide_path(
            action_cat, can_self_solve, confidence, needs_peer, severity, retry_count
        )

        reasoning_parts = []
        reasoning_parts.append(f"Severity: {severity.value}")
        reasoning_parts.append(f"Self-solve: {'yes' if can_self_solve else 'no'} (confidence={confidence:.2f})")
        reasoning_parts.append(f"Peer help needed: {needs_peer}")
        reasoning_parts.append(f"Similar past experiences: {len(similar)}")
        reasoning_parts.append(f"Suggested path: {path.value}")

        return ProblemAnalysis(
            severity=severity,
            confidence=confidence,
            can_self_solve=can_self_solve,
            needs_peer_help=needs_peer,
            action_category=action_cat,
            suggested_path=path,
            similar_experiences=similar_data,
            reasoning="; ".join(reasoning_parts),
            error_summary=error_message[:500],
        )

    def _classify_action(self, action_lower: str, input_lower: str) -> ActionCategory:
        """Classify the action being attempted"""
        combined = f"{action_lower} {input_lower}"

        if any(p in combined for p in IRREVERSIBLE_PATTERNS):
            if any(p in combined for p in ["delete", "drop", "truncate", "remove", "destroy"]):
                return ActionCategory.DANGEROUS
            return ActionCategory.IRREVERSIBLE

        if any(p in combined for p in COMPLIANCE_PATTERNS):
            return ActionCategory.COMPLIANCE

        if any(p in combined for p in HIGH_IMPACT_PATTERNS):
            return ActionCategory.HIGH_IMPACT

        return ActionCategory.NORMAL

    def _assess_severity(self, error_lower: str, action_lower: str) -> Severity:
        """Assess problem severity from error message"""
        critical_patterns = [
            "data loss", "security", "breach", "crash", "down",
            "corrupt", "integrity", "vulnerability",
        ]
        high_patterns = [
            "permission denied", "access denied", "timeout",
            "unavailable", "exception", "failed",
        ]
        medium_patterns = [
            "invalid", "not found", "bad request", "validation",
            "conflict", "limit", "quota",
        ]

        combined = f"{error_lower} {action_lower}"
        if any(p in combined for p in critical_patterns):
            return Severity.CRITICAL
        if any(p in combined for p in high_patterns):
            return Severity.HIGH
        if any(p in combined for p in medium_patterns):
            return Severity.MEDIUM
        return Severity.LOW

    def _assess_self_solve(
        self,
        error_lower: str,
        capabilities: list[str],
        similar_experiences: list[tuple],
        retry_count: int,
    ) -> tuple[bool, float]:
        """Can the agent solve this on its own?"""
        # Already retried too many times
        if retry_count >= 2:
            return False, 0.1

        # If similar past experiences exist with high success rate, more confident
        experience_boost = 0.0
        for node, score in similar_experiences:
            if node.content.get("success", False):
                experience_boost += 0.2 * score

        # Check if error type is self-solvable
        self_solvable_patterns = [
            "timeout", "rate limit", "429", "503", "temporary",
            "retry", "busy", "throttl",
        ]
        is_temporary = any(p in error_lower for p in self_solvable_patterns)

        if is_temporary:
            return True, min(0.8 + experience_boost, 0.95)

        # Capability overlap check (simple heuristic)
        capability_match = any(
            cap.lower() in error_lower for cap in capabilities
        )

        if capability_match:
            return True, min(0.6 + experience_boost, 0.9)
        else:
            return False, min(0.3 + experience_boost, 0.5)

    def _assess_peer_needed(
        self,
        severity: Severity,
        can_self_solve: bool,
        confidence: float,
        error_lower: str,
        retry_count: int,
    ) -> bool:
        """Would peer discussion help?"""
        if can_self_solve and confidence > 0.7:
            return False

        peer_helpful_patterns = [
            "complex", "ambiguous", "unclear", "multiple",
            "integration", "depend", "conflict",
        ]
        if any(p in error_lower for p in peer_helpful_patterns):
            return True

        if severity in (Severity.HIGH, Severity.CRITICAL) and confidence < 0.5:
            return True

        return False

    def _decide_path(
        self,
        action_cat: ActionCategory,
        can_self_solve: bool,
        confidence: float,
        needs_peer: bool,
        severity: Severity,
        retry_count: int,
    ) -> ResolutionPath:
        """Decide which resolution path to take"""
        # Direct to human for dangerous/compliance/irreversible
        if action_cat in (ActionCategory.IRREVERSIBLE, ActionCategory.COMPLIANCE, ActionCategory.DANGEROUS):
            return ResolutionPath.HUMAN

        # High impact -> human
        if action_cat == ActionCategory.HIGH_IMPACT and confidence < 0.8:
            return ResolutionPath.HUMAN

        # Can self-solve with high confidence
        if can_self_solve and confidence > 0.7:
            return ResolutionPath.SELF

        # Can self-solve but low confidence -> try SELF once, then PEER
        if can_self_solve and confidence > 0.4:
            if retry_count == 0:
                return ResolutionPath.SELF
            else:
                return ResolutionPath.PEER

        # Peer discussion useful
        if needs_peer:
            return ResolutionPath.PEER

        # Default: escalate to CEO
        return ResolutionPath.ESCALATE


# Global evaluator
evaluator = ProblemEvaluator()
