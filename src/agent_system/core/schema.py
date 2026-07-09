"""
产出物 Schema 标准化
参考架构文档 4.2: 5 个必填字段 + Pydantic 校验

PR-2.2 (production schema tolerance):
- Tier 1 STRICT: id, type, created_at, schema_version, created_by (5 required)
- Tier 2 LENIENT: payload (missing fields tolerated, missing payload -> {})
- Tier 3 REPAIR: auto-fill missing created_at/created_by/id (warning logged)
- Tier 4 WARN: missing next_steps/metadata (warning, not error)
- FAILURE node: log every validation outcome to MultiLinkGraph for audit
- LLM raw_output (parse failure): mark payload as partial, NOT silent success
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, field_validator
import uuid

logger = logging.getLogger(__name__)


class NextStep(BaseModel):
    """下一步行动"""
    action: str
    agent: str
    description: Optional[str] = None


# Fields that MUST be present and non-empty (Tier 1 STRICT)
# The other fields are either auto-repairable or warning-only.
STRICT_REQUIRED_FIELDS = ("id", "type", "created_at", "schema_version", "created_by")

# Fields that can be auto-repaired if missing
AUTO_REPAIRABLE_FIELDS = ("created_at", "created_by", "id", "schema_version")

# Fields that are LENIENT — missing or empty is OK, just a warning
LENIENT_FIELDS = ("payload", "metadata", "next_steps")


class OutputSchema(BaseModel):
    """产出物标准 Schema — 5 个必填字段 + 可选 auto-repair"""
    id: str = ""                              # tier 1 — auto-repairable if empty
    type: str = ""                            # tier 1 — STRICT (LLM must provide)
    created_at: Optional[datetime] = None     # tier 1 — auto-repair to now()
    created_by: str = ""                      # tier 1 — auto-repair to agent_name
    schema_version: str = "1.0"              # tier 1 — defaults to 1.0
    payload: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    next_steps: List[NextStep] = Field(default_factory=list)

    # PR-2.2: indicates whether the payload came from a successful JSON parse
    # or is a raw_output fallback. Used by downstream agents to decide
    # whether to treat the result as authoritative.
    partial: bool = False
    validation_warnings: List[str] = Field(default_factory=list)

    @field_validator("id", mode="before")
    @classmethod
    def validate_id(cls, v):
        # Allow empty string (will be auto-repaired) but not None
        if v is None:
            return ""
        return str(v).strip()

    @field_validator("created_at", mode="before")
    @classmethod
    def parse_datetime(cls, v):
        if v is None:
            return None
        if isinstance(v, str):
            try:
                return datetime.fromisoformat(v)
            except (ValueError, TypeError):
                return None  # trigger auto-repair
        return v

    def model_dump_json(self, *args, **kwargs) -> str:
        """确保 created_at 序列化为 ISO 格式"""
        return super().model_dump_json(*args, **kwargs)

    @classmethod
    def generate_id(cls, prefix: str = "task") -> str:
        """生成带前缀的唯一 ID"""
        short_id = uuid.uuid4().hex[:8]
        now = datetime.now(timezone.utc)
        return f"{prefix}-{now.strftime('%Y%m%d')}-{short_id}"


class ValidationResult(BaseModel):
    """校验结果"""
    valid: bool
    errors: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    repairs: List[str] = Field(default_factory=list)  # auto-repairs applied


class SchemaValidator:
    """
    Schema 校验门 — tiered (STRICT / LENIENT / REPAIR) per PR-2.2.

    Tier 1 STRICT: id, type, created_at, schema_version, created_by.
        Missing -> error. (But created_at/created_by/id are auto-repairable
        when possible, with a warning.)

    Tier 2 LENIENT: payload. Missing payload -> empty dict (warning).
        Payload containing 'raw_output' -> mark output as partial.

    Tier 3 REPAIR: Auto-fill missing auto-repairable fields. Logs warning.

    Tier 4 WARN: Missing optional fields (next_steps/metadata) -> warning.

    Custom validators: registered by output_type. May add errors or warnings.
    """

    # Minimum number of payload fields required for a non-partial output.
    # Below this, the LLM response is considered too sparse to be useful
    # (e.g. {"title": "x"} is technically valid JSON but missing context).
    # Set to 2: a 1-field payload (other than raw_output) is suspicious
    # enough to be flagged.
    MIN_PAYLOAD_FIELDS = 2

    def __init__(self):
        self._validators: Dict[str, callable] = {}

    def register(self, output_type: str, validator: callable):
        self._validators[output_type] = validator

    def validate_and_repair(
        self,
        output: OutputSchema,
        agent_name: Optional[str] = None,
    ) -> tuple[OutputSchema, ValidationResult]:
        """
        Tiered validation + auto-repair.

        Returns (possibly-repaired output, ValidationResult).
        The output is mutated in-place AND returned for convenience.
        """
        repairs: List[str] = []
        warnings: List[str] = []
        errors: List[str] = []

        # ── Tier 3: auto-repair missing fields ──
        if not output.id:
            output.id = OutputSchema.generate_id("auto")
            repairs.append(f"id auto-repaired to {output.id!r}")

        if not output.schema_version:
            output.schema_version = "1.0"
            repairs.append("schema_version auto-repaired to '1.0'")

        if output.created_at is None:
            output.created_at = datetime.now(timezone.utc)
            repairs.append("created_at auto-repaired to now()")

        if not output.created_by:
            if agent_name:
                output.created_by = agent_name
                repairs.append(f"created_by auto-repaired to agent_name={agent_name!r}")
            else:
                # We don't have an agent name — this is a tier-1 error.
                errors.append("created_by is required and could not be auto-repaired")

        # ── Tier 1: STRICT required check on type (not auto-repairable) ──
        if not output.type:
            errors.append("type is required (cannot auto-repair)")

        # ── Tier 2: LENIENT payload check ──
        if not output.payload:
            warnings.append("payload is empty; treating as empty result")
        else:
            # Check for partial output (LLM JSON parse failed)
            if "raw_output" in output.payload and len(output.payload) == 1:
                output.partial = True
                warnings.append(
                    "payload contains only raw_output; LLM did not return valid JSON. "
                    "Marked as partial output."
                )
            elif len(output.payload) < self.MIN_PAYLOAD_FIELDS:
                output.partial = True
                warnings.append(
                    f"payload has only {len(output.payload)} fields (min {self.MIN_PAYLOAD_FIELDS}); "
                    "marked as partial output"
                )

        # ── Tier 4: WARN for optional fields ──
        if not output.metadata:
            warnings.append("metadata is empty")
        if not output.next_steps:
            warnings.append("next_steps is empty (no follow-up actions specified)")

        # ── Custom validators (per output type) ──
        if output.type in self._validators:
            try:
                result = self._validators[output.type](output)
                if isinstance(result, ValidationResult):
                    errors.extend(result.errors)
                    warnings.extend(result.warnings)
            except Exception as e:
                errors.append(f"custom validator raised: {type(e).__name__}: {e}")

        # ── next_steps sanity ──
        for i, step in enumerate(output.next_steps):
            if not step.action:
                errors.append(f"next_steps[{i}].action is empty")
            if not step.agent:
                errors.append(f"next_steps[{i}].agent is empty")

        # Persist warnings onto the output so downstream can see them
        output.validation_warnings = warnings

        result = ValidationResult(
            valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
            repairs=repairs,
        )
        return output, result

    # Backwards-compatible alias
    def validate(self, output: OutputSchema) -> ValidationResult:
        """Legacy single-arg validate — no auto-repair, no agent name context.

        Prefer validate_and_repair() for new callers.
        """
        _, result = self.validate_and_repair(output)
        return result


# 全局校验器实例
validator = SchemaValidator()


# ── FAILURE-node logger (PR-2.2) ──

class FailureNodeLogger:
    """
    Persists validation outcomes to MultiLinkGraph for audit.

    On every validation, writes a FAILURE-type node (even on success) so
    audit trail is complete. Records: agent_name, validation result,
    raw text (if LLM parse failed), timestamp.

    Failures are non-fatal: a write error here must NOT take down the
    agent execution. The audit trail is best-effort.
    """

    @staticmethod
    def record_validation(
        task_id: str,
        agent_name: str,
        output: OutputSchema,
        result: ValidationResult,
        raw_llm_text: Optional[str] = None,
    ) -> Optional[str]:
        """
        Write a validation record to the graph. Returns the node id on success,
        None on failure (logged but not raised).

        On Tier-2 failure (LLM didn't return valid JSON), records a
        FAILURE node with action='schema_violation' and the raw text.
        """
        try:
            from agent_system.memory.graph import get_graph, NodeType
            from agent_system.core.schema import OutputSchema as OS
            graph = get_graph()

            node_id = OS.generate_id("validation")
            payload: Dict[str, Any] = {
                "task_id": task_id,
                "agent_name": agent_name,
                "valid": result.valid,
                "errors": result.errors,
                "warnings": result.warnings,
                "repairs": result.repairs,
                "partial": output.partial,
                "output_id": output.id,
                "output_type": output.type,
            }
            if raw_llm_text:
                # Cap at 2KB to avoid blowing up the graph
                payload["raw_llm_text_preview"] = raw_llm_text[:2000]

            # Choose node type based on outcome
            if not result.valid:
                node_type = NodeType.FAILURE
                payload["action"] = "schema_violation"
            elif output.partial:
                node_type = NodeType.FAILURE
                payload["action"] = "partial_output"
            else:
                node_type = NodeType.OUTPUT
                payload["action"] = "validation_passed"

            from agent_system.memory.graph import GraphNode
            node = GraphNode(
                id=node_id,
                type=node_type,
                content=payload,
                metadata={"source": "schema_validator", "task_id": task_id},
            )
            graph.add_node(node)
            return node_id
        except Exception as e:
            logger.warning(f"FailureNodeLogger.record_validation failed: {e}")
            return None
