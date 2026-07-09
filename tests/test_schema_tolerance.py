"""
Tests for PR-2.2 production schema tolerance:
- Tier 1 STRICT: id, type, created_at, schema_version, created_by
- Tier 2 LENIENT: payload (missing tolerated, partial flagged)
- Tier 3 REPAIR: auto-fill missing fields with warnings
- Tier 4 WARN: missing next_steps/metadata = warning, not error
- FAILURE-node logging: every validation outcome recorded to graph
"""
import asyncio
import os
import time
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from agent_system.core.schema import (
    OutputSchema,
    ValidationResult,
    SchemaValidator,
    FailureNodeLogger,
)


class TestAutoRepair:
    def test_missing_id_auto_repaired(self):
        v = SchemaValidator()
        out = OutputSchema(type="task", created_at=datetime.now(timezone.utc), created_by="x")
        out, result = v.validate_and_repair(out, agent_name="x")
        assert out.id != ""
        assert any("id auto-repaired" in r for r in result.repairs)

    def test_missing_created_at_auto_repaired(self):
        v = SchemaValidator()
        out = OutputSchema(id="x", type="task", created_by="x")
        out, result = v.validate_and_repair(out, agent_name="x")
        assert out.created_at is not None
        assert any("created_at auto-repaired" in r for r in result.repairs)

    def test_missing_created_by_auto_repaired_with_agent_name(self):
        v = SchemaValidator()
        out = OutputSchema(id="x", type="task", created_at=datetime.now(timezone.utc))
        out, result = v.validate_and_repair(out, agent_name="smart_agent")
        assert out.created_by == "smart_agent"
        assert any("created_by auto-repaired" in r for r in result.repairs)

    def test_missing_created_by_without_agent_name_fails(self):
        v = SchemaValidator()
        out = OutputSchema(id="x", type="task", created_at=datetime.now(timezone.utc))
        out, result = v.validate_and_repair(out, agent_name=None)
        assert any("created_by is required" in e for e in result.errors)
        assert not result.valid

    def test_missing_schema_version_repaired_to_1_0(self):
        v = SchemaValidator()
        out = OutputSchema(
            id="x", type="task",
            created_at=datetime.now(timezone.utc),
            created_by="x",
        )
        out.schema_version = ""
        out, result = v.validate_and_repair(out, agent_name="x")
        assert out.schema_version == "1.0"


class TestStrictRequired:
    def test_missing_type_fails(self):
        v = SchemaValidator()
        out = OutputSchema(id="x", created_at=datetime.now(timezone.utc), created_by="x")
        out.type = ""
        out, result = v.validate_and_repair(out, agent_name="x")
        assert any("type is required" in e for e in result.errors)
        assert not result.valid

    def test_valid_output_passes(self):
        v = SchemaValidator()
        out = OutputSchema(
            id="x", type="task",
            created_at=datetime.now(timezone.utc),
            created_by="x",
            payload={"key": "value"},
        )
        out, result = v.validate_and_repair(out, agent_name="x")
        assert result.valid
        assert result.errors == []


class TestLenientPayload:
    def test_empty_payload_warns_but_passes(self):
        v = SchemaValidator()
        out = OutputSchema(
            id="x", type="task",
            created_at=datetime.now(timezone.utc),
            created_by="x",
            payload={},
        )
        out, result = v.validate_and_repair(out, agent_name="x")
        assert result.valid
        assert any("payload is empty" in w for w in result.warnings)

    def test_raw_output_payload_marked_partial(self):
        v = SchemaValidator()
        out = OutputSchema(
            id="x", type="task",
            created_at=datetime.now(timezone.utc),
            created_by="x",
            payload={"raw_output": "I cannot comply with that request."},
        )
        out, result = v.validate_and_repair(out, agent_name="x")
        assert out.partial is True
        assert any("raw_output" in w for w in result.warnings)

    def test_sparse_payload_marked_partial(self):
        """A single-field payload (besides raw_output) is sparse, marked partial."""
        v = SchemaValidator()
        out = OutputSchema(
            id="x", type="task",
            created_at=datetime.now(timezone.utc),
            created_by="x",
            # A 1-field payload (other than raw_output) is below MIN_PAYLOAD_FIELDS=2
            # so it's marked partial. This catches "the LLM produced something
            # but it was too sparse to be useful" cases.
            payload={"only_one": "field"},
        )
        out, result = v.validate_and_repair(out, agent_name="x")
        assert out.partial is True

    def test_empty_payload_warns_but_not_partial(self):
        """Empty payload is a warning (empty result), not 'partial'.

        The 'partial' flag is reserved for the LLM-failed-JSON-parse case
        (raw_output). An empty result is a different concern — the LLM
        succeeded but produced no content.
        """
        v = SchemaValidator()
        out = OutputSchema(
            id="x", type="task",
            created_at=datetime.now(timezone.utc),
            created_by="x",
            payload={},
        )
        out, result = v.validate_and_repair(out, agent_name="x")
        assert out.partial is False
        assert any("payload is empty" in w for w in result.warnings)

    def test_rich_payload_not_marked_partial(self):
        v = SchemaValidator()
        out = OutputSchema(
            id="x", type="task",
            created_at=datetime.now(timezone.utc),
            created_by="x",
            payload={"a": 1, "b": 2, "c": 3, "d": 4, "e": 5},
        )
        out, result = v.validate_and_repair(out, agent_name="x")
        assert out.partial is False


class TestWarnings:
    def test_missing_next_steps_warns(self):
        v = SchemaValidator()
        out = OutputSchema(
            id="x", type="task",
            created_at=datetime.now(timezone.utc),
            created_by="x",
            payload={"a": 1, "b": 2},
            next_steps=[],
        )
        out, result = v.validate_and_repair(out, agent_name="x")
        assert any("next_steps is empty" in w for w in result.warnings)

    def test_missing_metadata_warns(self):
        v = SchemaValidator()
        out = OutputSchema(
            id="x", type="task",
            created_at=datetime.now(timezone.utc),
            created_by="x",
            payload={"a": 1, "b": 2},
            metadata={},
        )
        out, result = v.validate_and_repair(out, agent_name="x")
        assert any("metadata is empty" in w for w in result.warnings)

    def test_next_steps_with_empty_action_errors(self):
        from agent_system.core.schema import NextStep
        v = SchemaValidator()
        out = OutputSchema(
            id="x", type="task",
            created_at=datetime.now(timezone.utc),
            created_by="x",
            payload={"a": 1, "b": 2},
            next_steps=[NextStep(action="", agent="human", description="d")],
        )
        out, result = v.validate_and_repair(out, agent_name="x")
        assert any("action is empty" in e for e in result.errors)
        assert not result.valid


class TestCustomValidator:
    def test_custom_validator_can_add_errors(self):
        v = SchemaValidator()

        def require_specific_type(output):
            r = ValidationResult(valid=True)
            if output.type != "expected_type":
                r.errors.append(f"type must be 'expected_type', got {output.type!r}")
                r.valid = False
            return r

        v.register("expected_type", require_specific_type)
        out = OutputSchema(
            id="x", type="expected_type",
            created_at=datetime.now(timezone.utc),
            created_by="x",
            payload={"a": 1, "b": 2, "c": 3},
        )
        out, result = v.validate_and_repair(out, agent_name="x")
        assert result.valid

    def test_custom_validator_crash_caught(self):
        v = SchemaValidator()

        def broken_validator(output):
            raise RuntimeError("validator crashed")

        v.register("task", broken_validator)
        out = OutputSchema(
            id="x", type="task",
            created_at=datetime.now(timezone.utc),
            created_by="x",
            payload={"a": 1, "b": 2, "c": 3},
        )
        out, result = v.validate_and_repair(out, agent_name="x")
        assert any("custom validator raised" in e for e in result.errors)
        assert not result.valid


class TestFailureNodeLogger:
    def test_records_pass_validation(self):
        out = OutputSchema(
            id="x", type="task",
            created_at=datetime.now(timezone.utc),
            created_by="x",
            payload={"a": 1, "b": 2, "c": 3},
        )
        result = ValidationResult(valid=True)
        from agent_system.memory.graph import reset_graph
        reset_graph()

        node_id = FailureNodeLogger.record_validation(
            task_id="t1", agent_name="test", output=out, result=result,
        )
        assert node_id is not None

        from agent_system.memory.graph import get_graph
        graph = get_graph()
        nodes = graph.find_nodes()
        assert any(n.id == node_id for n in nodes)

    def test_records_failure_validation(self):
        out = OutputSchema(
            id="x", type="task",
            created_at=datetime.now(timezone.utc),
            created_by="x",
        )
        out.type = ""
        result = ValidationResult(
            valid=False, errors=["type is required"], warnings=[],
        )
        from agent_system.memory.graph import reset_graph
        reset_graph()

        node_id = FailureNodeLogger.record_validation(
            task_id="t1", agent_name="test", output=out, result=result,
        )
        assert node_id is not None

        from agent_system.memory.graph import get_graph
        graph = get_graph()
        nodes = graph.find_nodes()
        node = next(n for n in nodes if n.id == node_id)
        assert node.content["action"] == "schema_violation"

    def test_records_partial_output_with_raw_text(self):
        """When output is marked partial (LLM parse failed), logger records
        action='partial_output' regardless of valid flag (as long as no
        tier-1 errors prevent the partial flag from being set).
        """
        v = SchemaValidator()
        out = OutputSchema(
            id="x", type="task",
            created_at=datetime.now(timezone.utc),
            created_by="x",
            payload={"raw_output": "garbage"},
            next_steps=[],  # empty to avoid tier-1 error
        )
        # Run validate first so the partial flag is set
        out, result = v.validate_and_repair(out, agent_name="x")
        assert out.partial is True

        from agent_system.memory.graph import reset_graph
        reset_graph()

        node_id = FailureNodeLogger.record_validation(
            task_id="t1", agent_name="test", output=out, result=result,
            raw_llm_text="garbage from LLM",
        )
        assert node_id is not None

        from agent_system.memory.graph import get_graph
        graph = get_graph()
        nodes = graph.find_nodes()
        node = next(n for n in nodes if n.id == node_id)
        assert node.content["action"] == "partial_output"
        assert "raw_llm_text_preview" in node.content

    def test_write_error_does_not_crash(self):
        out = OutputSchema(
            id="x", type="task",
            created_at=datetime.now(timezone.utc),
            created_by="x",
            payload={"a": 1, "b": 2, "c": 3},
        )
        result = ValidationResult(valid=True)
        from agent_system.memory.graph import get_graph
        with patch.object(get_graph(), "add_node", side_effect=RuntimeError("disk full")):
            node_id = FailureNodeLogger.record_validation(
                task_id="t1", agent_name="test", output=out, result=result,
            )
        assert node_id is None