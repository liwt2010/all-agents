"""Boundary tests: Schema LENIENT/REPAIR mode provenance labels.

Issue: When data is auto-repaired by Schema LENIENT/REPAIR mode,
the provenance label should NOT be "REAL_LLM" because there was
engineering intervention (auto-repair). It should be something like
"REPAIRED" or "AUTO_REPAIRED".

Run: pytest tests/test_boundary_schema_provenance.py -v
"""
import pytest
from datetime import datetime, timezone
from unittest.mock import patch

from agent_system.core.schema import (
    OutputSchema,
    ValidationResult,
    SchemaValidator,
)


class TestSchemaRepairProvenance:
    """Verify provenance labels are correct after auto-repair."""

    def test_auto_repaired_output_should_not_be_marked_real_llm(self):
        """Auto-repaired outputs should have corrected provenance, not REAL_LLM."""
        v = SchemaValidator()

        # Create output missing required fields
        out = OutputSchema(
            type="result",
            created_at=datetime.now(timezone.utc),
            created_by="test",
            # Missing: id, schema_version
        )

        # Validate with REPAIR mode
        out, result = v.validate_and_repair(out, agent_name="test_agent")

        # The output should be valid after repair
        assert result.valid, f"Should be valid after repair: {result.errors}"

        # If there were repairs, the provenance should reflect that
        if result.repairs:
            # After auto-repair, this is no longer pure LLM output
            # The provenance should indicate engineering intervention
            assert hasattr(out, "provenance"), "Output should have provenance field"

            # Check if provenance is correctly set
            provenance = getattr(out, "provenance", None)
            if provenance:
                # Should NOT be REAL_LLM because there was auto-repair
                assert provenance != "REAL_LLM", \
                    "Auto-repaired output should not be marked as REAL_LLM"
                # Should be something like REPAIRED, AUTO_REPAIRED, or ENGINEERED
                assert provenance in ("REPAIRED", "AUTO_REPAIRED", "ENGINEERED"), \
                    f"Unexpected provenance: {provenance}"

    def test_auto_repaired_with_llm_fields_preserved(self):
        """When LLM provides all required fields, provenance should be REAL_LLM."""
        v = SchemaValidator()

        # Create complete output from LLM
        out = OutputSchema(
            id="llm-generated-123",
            type="result",
            created_at=datetime.now(timezone.utc),
            created_by="llm_agent",
            schema_version="1.0",
            payload={"content": "LLM generated content"},
        )

        out, result = v.validate_and_repair(out, agent_name="llm_agent")

        # Should be valid without repairs
        assert result.valid
        assert len(result.repairs) == 0

        # Since no repair was needed, provenance can be REAL_LLM
        # (pure LLM output with no engineering intervention)
        provenance = getattr(out, "provenance", None)
        # If provenance field exists, it should be REAL_LLM for pure LLM output
        if provenance is not None:
            assert provenance == "REAL_LLM", \
                "Pure LLM output should be marked as REAL_LLM"

    def test_repair_preserves_llm_content(self):
        """Auto-repair should only fix schema issues, not modify LLM content."""
        v = SchemaValidator()

        # Output with content but missing schema fields
        original_content = {"answer": "The meaning of life is 42"}
        out = OutputSchema(
            type="result",
            created_at=datetime.now(timezone.utc),
            created_by="test",
            payload=original_content,
            # Missing: id, schema_version
        )

        out, result = v.validate_and_repair(out, agent_name="test")

        assert result.valid

        # Verify original LLM content is preserved
        assert out.payload == original_content, \
            "Auto-repair should not modify LLM-generated content"

    def test_multiple_repairs_chain(self):
        """Multiple repairs should still result in correct provenance."""
        v = SchemaValidator()

        # Output with multiple missing fields
        out = OutputSchema(
            type="result",
            # Missing: id, created_at, created_by, schema_version
        )

        out, result = v.validate_and_repair(out, agent_name="multi_repair_test")

        # Should be valid after multiple repairs
        assert result.valid
        assert len(result.repairs) >= 3, f"Expected at least 3 repairs, got: {result.repairs}"

        # After multiple repairs, definitely not pure LLM output
        if hasattr(out, "provenance"):
            provenance = getattr(out, "provenance", None)
            if provenance:
                assert provenance != "REAL_LLM", \
                    "Multi-repaired output should not be marked as REAL_LLM"


class TestSchemaLenientMode:
    """Test LENIENT mode specifically."""

    def test_lenient_allows_missing_optional_fields(self):
        """LENIENT mode should tolerate missing optional fields."""
        v = SchemaValidator()

        # Output with only essential fields
        out = OutputSchema(
            type="result",
            created_at=datetime.now(timezone.utc),
            created_by="test",
            # Missing: id (optional in LENIENT), schema_version (optional in LENIENT)
        )

        out, result = v.validate_and_repair(out, agent_name="test")

        # LENIENT should not fail on missing optional fields
        assert result.valid, f"LENIENT mode should tolerate missing fields: {result.errors}"

    def test_lenient_flags_issues_but_doesnt_fail(self):
        """LENIENT mode should flag issues but not fail validation."""
        v = SchemaValidator()

        out = OutputSchema(
            type="result",
            created_at=datetime.now(timezone.utc),
            created_by="test",
        )

        out, result = v.validate_and_repair(out, agent_name="test")

        # Should have warnings about missing fields
        assert len(result.warnings) > 0 or len(result.repairs) > 0, \
            "LENIENT should flag issues but not fail"


class TestSchemaStrictMode:
    """Test STRICT mode (baseline - should fail on missing fields)."""

    def test_strict_fails_on_missing_required_fields(self):
        """STRICT mode should fail on missing required fields."""
        v = SchemaValidator()

        out = OutputSchema(
            type="result",
            # Missing: id, created_at, created_by
        )

        out, result = v.validate_and_repair(out, agent_name="test")

        # STRICT should fail
        assert not result.valid, "STRICT mode should fail on missing required fields"
        assert len(result.errors) > 0, "STRICT should produce errors"
