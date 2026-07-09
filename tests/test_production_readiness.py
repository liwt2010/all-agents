"""
Production-readiness gate.

This test FAILS the build if any of the following are missing or broken:
  - docs/PRODUCTION.md exists and is non-trivial (>5KB)
  - .env.example exists, is non-empty, has all required sections
  - Dockerfile exists, has HEALTHCHECK, listens on 8000
  - .gitignore excludes .env (no secrets in git)
  - All design docs (STORAGE/METRICS/AUDIT/RATE_LIMIT/BACKUP) exist
  - src/agent_system/api/server.py has all required endpoints
  - test_pipeline_e2e_real_llm.py exists (real-LLM integration)
  - test_data_provenance.py exists (P2-3.2 verification)
  - test_schema_tolerance.py exists (P1-2.2 verification)
  - At least 5 production-grade features wired (storage/observability/etc.)

This test runs in CI (no API key required). It enforces the contract
that "this codebase is production-grade" — not just that the code works.
"""
import re
from pathlib import Path

import pytest

# Project root is one level up from tests/
ROOT = Path(__file__).resolve().parent.parent


def _read(path: str) -> str:
    p = ROOT / path
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8", errors="replace")


def _exists(path: str) -> bool:
    return (ROOT / path).exists()


# ── Required documentation ──

class TestRequiredDocumentation:
    def test_production_md_exists(self):
        assert _exists("docs/PRODUCTION.md"), "docs/PRODUCTION.md is missing"

    def test_production_md_is_substantial(self):
        text = _read("docs/PRODUCTION.md")
        # > 5KB of content
        assert len(text) > 5000, f"PRODUCTION.md too short: {len(text)} bytes"

    def test_production_md_covers_key_sections(self):
        text = _read("docs/PRODUCTION.md")
        required_sections = [
            "Pre-deployment Checklist",
            "Environment Variables",
            "LLM API Key Handling",
            "Storage Backend Selection",
            "Health & Readiness",
            "Monitoring",
            "Backups",
            "Security",
            "CI / CD Gate",
        ]
        for section in required_sections:
            assert section in text, f"PRODUCTION.md missing section: {section}"

    def test_all_pr_design_docs_exist(self):
        for doc in ["STORAGE", "METRICS", "AUDIT", "RATE_LIMIT", "BACKUP", "CUSTOM_AGENT", "DATAVIEW"]:
            assert _exists(f"docs/{doc}.md"), f"docs/{doc}.md is missing"

    def test_runbook_md_replaced_or_updated(self):
        # The old RUNBOOK.md is misleading (references uninstalled helm chart).
        # Either it's been replaced by PRODUCTION.md or updated.
        runbook = _read("docs/RUNBOOK.md")
        if runbook:
            # If it still exists, it should not reference uninstalled components
            assert "deploy/helm" not in runbook, (
                "RUNBOOK.md still references uninstalled deploy/helm. "
                "Either update it or delete in favor of PRODUCTION.md."
            )


# ── Environment configuration ──

class TestEnvConfig:
    def test_env_example_exists(self):
        assert _exists(".env.example"), ".env.example is missing"

    def test_env_example_has_required_vars(self):
        text = _read(".env.example")
        required = [
            "AUTH_SECRET",
            "ENVIRONMENT",
            "LLM_PROVIDER",
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "POSTGRES_HOST",
        ]
        for var in required:
            assert var in text, f".env.example missing required var: {var}"

    def test_env_example_documents_sections(self):
        text = _read(".env.example")
        # Should have section headers
        assert "REQUIRED" in text, ".env.example should have REQUIRED section"
        assert "OPTIONAL" in text, ".env.example should have OPTIONAL section"

    def test_gitignore_excludes_env(self):
        gitignore = _read(".gitignore")
        assert ".env" in gitignore, ".env must be in .gitignore (no real secrets in git)"

    def test_real_env_not_committed(self):
        """The actual .env file should never be checked in. If it is,
        fail the build (this should never happen in production CI)."""
        # Check git index, not working copy
        import subprocess
        result = subprocess.run(
            ["git", "ls-files", ".env"],
            cwd=str(ROOT), capture_output=True, text=True,
        )
        tracked = result.stdout.strip()
        assert not tracked, f".env is tracked in git: {tracked}"


# ── Container deployment ──

class TestContainerDeployment:
    def test_dockerfile_exists(self):
        assert _exists("Dockerfile"), "Dockerfile is missing"

    def test_dockerfile_has_healthcheck(self):
        text = _read("Dockerfile")
        assert "HEALTHCHECK" in text, "Dockerfile must have HEALTHCHECK"
        assert "/api/health" in text, "HEALTHCHECK must probe /api/health"

    def test_dockerfile_exposes_port(self):
        text = _read("Dockerfile")
        assert "EXPOSE" in text, "Dockerfile must EXPOSE the API port"
        assert "8000" in text, "Dockerfile must EXPOSE port 8000"

    def test_dockerfile_starts_api(self):
        text = _read("Dockerfile")
        assert "uvicorn" in text or "gunicorn" in text, (
            "Dockerfile must start the API server (uvicorn or gunicorn)"
        )

    def test_dockerfile_creates_data_dirs(self):
        text = _read("Dockerfile")
        assert "mkdir" in text and "/data" in text, (
            "Dockerfile must create /data dirs for persistence"
        )

    def test_docker_compose_exists(self):
        assert _exists("docker-compose.yml"), "docker-compose.yml is missing"


# ── API server completeness ──

class TestAPIServer:
    def test_server_module_loads(self):
        # Smoke test: import the server module
        try:
            from agent_system.api import server
            assert hasattr(server, "app"), "server module missing 'app'"
        except Exception as e:
            pytest.fail(f"Failed to import agent_system.api.server: {e}")

    def test_health_endpoint_exists(self):
        from agent_system.api.server import app
        paths = {route.path for route in app.routes}
        assert "/api/health" in paths, "Missing /api/health endpoint"
        assert "/api/ready" in paths, "Missing /api/ready endpoint"

    def test_metrics_endpoint_exists(self):
        from agent_system.api.server import app
        paths = {route.path for route in app.routes}
        # PR-10 added standard /metrics (Prometheus format)
        assert "/metrics" in paths, "Missing /metrics Prometheus endpoint"

    def test_audit_query_endpoint_exists(self):
        from agent_system.api.server import app
        paths = {route.path for route in app.routes}
        # PR-11 added audit query endpoint
        assert "/api/audit/query" in paths, "Missing /api/audit/query endpoint"


# ── Production-grade features wired ──

class TestProductionFeaturesWired:
    """Verify each major PR was actually committed (not just designed)."""

    def test_storage_backend_has_3_implementations(self):
        assert _exists("src/agent_system/memory/storage/json_backend.py")
        assert _exists("src/agent_system/memory/storage/sqlite_backend.py")
        assert _exists("src/agent_system/memory/storage/postgres_backend.py")

    def test_observability_module_has_metrics_and_tracing(self):
        assert _exists("src/agent_system/observability/metrics.py")
        assert _exists("src/agent_system/observability/tracing.py")

    def test_audit_logger_supports_batch(self):
        # PR-11 added BatchAuditLogger
        text = _read("src/agent_system/core/audit_logger.py")
        assert "BatchAuditLogger" in text, "BatchAuditLogger missing"
        assert "sampling_rate" in text, "audit sampling_rate missing"

    def test_rate_limit_uses_sliding_window(self):
        # PR-12 added SlidingWindowLimiter
        text = _read("src/agent_system/core/rate_limit/sliding_window.py")
        assert "SlidingWindowLimiter" in text

    def test_backup_subsystem_has_manifest(self):
        assert _exists("src/agent_system/core/backup/manifest.py")
        assert _exists("src/agent_system/core/backup/scheduler.py")
        assert _exists("src/agent_system/core/backup/restore.py")

    def test_request_id_middleware_exists(self):
        text = _read("src/agent_system/core/security_middleware.py")
        assert "RequestIDMiddleware" in text
        assert "X-Request-ID" in text

    def test_data_provenance_module(self):
        # P2-3.2
        text = _read("src/agent_system/core/observability.py")
        assert "DataProvenance" in text
        assert "ProvenanceSource" in text

    def test_schema_validator_has_tiered_validation(self):
        # P1-2.2
        text = _read("src/agent_system/core/schema.py")
        assert "validate_and_repair" in text
        assert "FailureNodeLogger" in text
        assert "partial" in text  # partial output flag


# ── Critical tests must exist ──

class TestCriticalTestsExist:
    """Tests that prove production-grade features actually work."""

    def test_storage_tests(self):
        assert _exists("tests/test_storage.py")

    def test_metrics_instrumentation_tests(self):
        assert _exists("tests/test_metrics_instrumentation.py")

    def test_audit_batch_tests(self):
        assert _exists("tests/test_audit_batch.py")

    def test_rate_limit_tests(self):
        assert _exists("tests/test_rate_limit_sliding.py")

    def test_backup_tests(self):
        assert _exists("tests/test_backup.py")

    def test_real_llm_pipeline_tests(self):
        assert _exists("tests/test_pipeline_e2e_real_llm.py")

    def test_real_llm_resolver_tests(self):
        assert _exists("tests/test_resolver_peer_real_llm.py")

    def test_schema_tolerance_tests(self):
        assert _exists("tests/test_schema_tolerance.py")

    def test_data_provenance_tests(self):
        assert _exists("tests/test_data_provenance.py")

    def test_llm_router_none_defense_tests(self):
        # PR fix for None usage fields
        text = _read("tests/test_llm_router.py")
        assert "TestNoneUsageDefense" in text


# ── Forbidden patterns ──

class TestForbiddenPatterns:
    """Catch things that should NEVER end up in production code."""

    def test_no_hardcoded_api_keys(self):
        """No real API keys in source files."""
        # Skip test files and docs (they have examples)
        forbidden_patterns = [
            r"sk-ant-[a-zA-Z0-9-]{20,}",  # Anthropic
            r"sk-[a-zA-Z0-9]{20,}",  # OpenAI / DeepSeek
            r"sk-FFB[a-zA-Z0-9]+",  # The specific test key from the user's session
        ]
        # Scan src/ only
        src_dir = ROOT / "src"
        for py_file in src_dir.rglob("*.py"):
            content = py_file.read_text(encoding="utf-8", errors="replace")
            for pattern in forbidden_patterns:
                if re.search(pattern, content):
                    pytest.fail(
                        f"Hardcoded API key in {py_file.relative_to(ROOT)}: matches {pattern}"
                    )

    def test_no_print_in_src(self):
        """No print() statements in src/ (use logger instead).

        CLI tools (restore.py, migrate.py) that have a `main()` entry point
        and are meant to be invoked from a terminal are allowed to use
        print() for user-facing output. The check below enforces that prints
        only appear in such CLI scripts.
        """
        src_dir = ROOT / "src"
        # Files that are allowed to use print() (CLI entry points)
        allowed_cli_files = {
            "src/agent_system/core/backup/restore.py",
            "src/agent_system/memory/storage/migrate.py",
        }
        for py_file in src_dir.rglob("*.py"):
            rel = str(py_file.relative_to(ROOT)).replace("\\", "/")
            if rel in allowed_cli_files:
                continue
            content = py_file.read_text(encoding="utf-8", errors="replace")
            for i, line in enumerate(content.splitlines(), 1):
                if re.match(r"^\s*print\s*\(", line) and "noqa" not in line:
                    pytest.fail(
                        f"print() in {py_file.relative_to(ROOT)}:{i}. Use logger instead."
                    )

    def test_no_todo_in_critical_paths(self):
        """No TODO/FIXME in critical code paths (production-readiness check)."""
        # Critical paths only
        critical_files = [
            "src/agent_system/api/server.py",
            "src/agent_system/core/agent.py",
            "src/agent_system/core/llm_router.py",
            "src/agent_system/core/resolver.py",
        ]
        for fpath in critical_files:
            content = _read(fpath)
            for i, line in enumerate(content.splitlines(), 1):
                if re.search(r"#\s*(TODO|FIXME|XXX|HACK)\b", line, re.IGNORECASE):
                    pytest.fail(
                        f"Critical file {fpath}:{i} has TODO/FIXME: {line.strip()}"
                    )


# ── Sanity: the test file itself is valid ──

def test_this_test_runs():
    """Sanity: this test file is discoverable and runnable."""
    assert __file__.endswith("test_production_readiness.py")