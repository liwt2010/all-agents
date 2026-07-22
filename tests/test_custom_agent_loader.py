"""
Custom Agent YAML loader + API tests (PR v0.3.0).

Verifies:
  - load_from_yaml_file parses valid YAML into CustomAgentConfig
  - Invalid YAML / invalid schema raises CustomAgentLoadError
  - load_from_directory loads all *.yaml/*.yml files in order
  - Example files (translator, pr-summarizer) load successfully
  - HTTP API:
      - GET /api/custom-agents returns tenant-scoped list
      - GET /api/custom-agents/{id} returns detail (or 404)
      - POST /api/custom-agents/{id}/run executes via LLM Router
      - POST /api/custom-agents:upload (admin) persists YAML
      - DELETE /api/custom-agents/{id} (admin) removes
      - Non-admin upload returns 403
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


# ── YAML loader ──

class TestYAMLLoader:
    def test_load_translator_example(self):
        from agent_system.agents.custom import load_from_yaml_file
        cfg = load_from_yaml_file("examples/custom-agents/translator.yaml")
        assert cfg.id == "translator"
        assert cfg.safety.value == "normal"
        assert cfg.llm_config["model"] == "claude-haiku-4-5-20251001"
        # Extra fields preserved
        assert cfg.model_extra.get("tags") == ["i18n", "translation"]

    def test_load_pr_summarizer_example(self):
        from agent_system.agents.custom import load_from_yaml_file
        cfg = load_from_yaml_file("examples/custom-agents/pr-summarizer.yaml")
        assert cfg.id == "pr-summarizer"
        assert "read_file" in cfg.tools
        assert cfg.safety.value == "autonomous"

    def test_invalid_yaml_raises(self, tmp_path):
        from agent_system.agents.custom import (
            CustomAgentLoadError,
            load_from_yaml_file,
        )
        bad = tmp_path / "bad.yaml"
        bad.write_text("not: [valid: yaml", encoding="utf-8")
        with pytest.raises(CustomAgentLoadError, match="invalid YAML"):
            load_from_yaml_file(bad)

    def test_missing_required_field_raises(self, tmp_path):
        from agent_system.agents.custom import (
            CustomAgentLoadError,
            load_from_yaml_file,
        )
        # Missing 'system_prompt' — required field
        bad = tmp_path / "missing.yaml"
        bad.write_text(
            "id: foo\nname: Foo\ndescription: d\n",
            encoding="utf-8",
        )
        with pytest.raises(CustomAgentLoadError, match="schema validation"):
            load_from_yaml_file(bad)

    def test_top_level_must_be_mapping(self, tmp_path):
        from agent_system.agents.custom import (
            CustomAgentLoadError,
            load_from_yaml_file,
        )
        bad = tmp_path / "list.yaml"
        bad.write_text("- id: foo\n- id: bar\n", encoding="utf-8")
        with pytest.raises(CustomAgentLoadError, match="top-level must be a mapping"):
            load_from_yaml_file(bad)

    def test_missing_file_raises(self):
        from agent_system.agents.custom import (
            CustomAgentLoadError,
            load_from_yaml_file,
        )
        with pytest.raises(CustomAgentLoadError, match="does not exist"):
            load_from_yaml_file("/nonexistent/path.yaml")

    def test_load_directory(self, tmp_path):
        from agent_system.agents.custom import load_from_directory
        (tmp_path / "a.yaml").write_text(
            "id: a\nname: A\ndescription: d\nsystem_prompt: p\n", encoding="utf-8",
        )
        (tmp_path / "b.yml").write_text(
            "id: b\nname: B\ndescription: d\nsystem_prompt: p\n", encoding="utf-8",
        )
        (tmp_path / "ignored.txt").write_text("not yaml", encoding="utf-8")
        loaded = load_from_directory(tmp_path, auto_register=False)
        assert {c.id for c in loaded} == {"a", "b"}

    def test_load_directory_skips_bad_files(self, tmp_path):
        """One bad file shouldn't block the whole directory."""
        from agent_system.agents.custom import load_from_directory
        (tmp_path / "good.yaml").write_text(
            "id: good\nname: G\ndescription: d\nsystem_prompt: p\n", encoding="utf-8",
        )
        (tmp_path / "bad.yaml").write_text(
            "id: bad\nname: B\n",  # missing required fields
            encoding="utf-8",
        )
        loaded = load_from_directory(tmp_path, auto_register=False)
        assert [c.id for c in loaded] == ["good"]


# ── HTTP API ──

@pytest.fixture
def temp_registry(monkeypatch, tmp_path):
    """Point the registry at a tmp dir so tests don't pollute state."""
    monkeypatch.setenv("AGENT_CUSTOM_AGENTS_DIR", str(tmp_path))
    # Reset the global singleton so it picks up the new env
    import agent_system.agents.custom.registry as reg_mod
    monkeypatch.setattr(reg_mod, "_custom_registry", None)
    yield tmp_path


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from agent_system.api.server import app
    return TestClient(app)


@pytest.fixture
def user_token():
    # Match conftest's AUTH_SECRET so issue/verify use the same key.
    from agent_system.core.auth import AuthService
    svc = AuthService(secret="test-only-jwt-secret-32chars-long-enough-for-hs256")
    return svc.issue_token("alice", tenant_id="acme")


@pytest.fixture
def admin_token():
    from agent_system.core.auth import AuthService
    svc = AuthService(secret="test-only-jwt-secret-32chars-long-enough-for-hs256")
    return svc.issue_token("admin-bob", tenant_id="acme", role="tenant_admin")


class TestCustomAgentsAPI:
    def test_list_empty(self, client, user_token):
        r = client.get(
            "/api/custom-agents",
            headers={"Authorization": f"Bearer {user_token}"},
        )
        assert r.status_code == 200
        assert r.json() == []

    def test_upload_requires_admin(self, client, user_token, temp_registry):
        yaml_text = """
id: foo
name: Foo
description: d
system_prompt: p
""".strip()
        r = client.post(
            "/api/custom-agents:upload",
            headers={"Authorization": f"Bearer {user_token}"},
            json={"yaml": yaml_text},
        )
        assert r.status_code == 403

    def test_upload_admin_succeeds(self, client, admin_token, temp_registry):
        yaml_text = """
id: foo
name: Foo
description: d
system_prompt: p
tags: [test]
""".strip()
        r = client.post(
            "/api/custom-agents:upload",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"yaml": yaml_text},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == "foo"
        assert body["tenant_id"] == "acme"

    def test_list_after_upload(self, client, admin_token, temp_registry):
        yaml_text = """
id: listed
name: Listed Agent
description: d
system_prompt: p
""".strip()
        client.post(
            "/api/custom-agents:upload",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"yaml": yaml_text},
        )
        r = client.get(
            "/api/custom-agents",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r.status_code == 200
        ids = [a["id"] for a in r.json()]
        assert "listed" in ids

    def test_get_detail(self, client, admin_token, temp_registry):
        yaml_text = """
id: detailable
name: Detail Agent
description: Detailed description
system_prompt: detailed prompt
""".strip()
        client.post(
            "/api/custom-agents:upload",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"yaml": yaml_text},
        )
        r = client.get(
            "/api/custom-agents/detailable",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["system_prompt"] == "detailed prompt"

    def test_get_404(self, client, user_token, temp_registry):
        r = client.get(
            "/api/custom-agents/nonexistent",
            headers={"Authorization": f"Bearer {user_token}"},
        )
        assert r.status_code == 404

    def test_run_invokes_agent(self, client, admin_token, temp_registry):
        """End-to-end: upload → run → get output.

        The output shape depends on whether a real LLM is configured:
          - mock mode:  payload has 'result', 'agent_name', 'safety_level'
          - real LLM:    payload has 'raw' (the LLM's text response)
        Both are valid — we just assert the agent ID and a non-empty
        output payload."""
        yaml_text = """
id: runnable
name: Runnable
description: d
system_prompt: You are a test agent.
""".strip()
        client.post(
            "/api/custom-agents:upload",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"yaml": yaml_text},
        )
        r = client.post(
            "/api/custom-agents/runnable/run",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"input": "Hello"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["agent_id"] == "runnable"
        assert body["status"] == "custom_result"
        # Either the mock marker or a raw LLM text — both are valid
        assert body["output"]

    def test_run_404(self, client, user_token, temp_registry):
        r = client.post(
            "/api/custom-agents/nonexistent/run",
            headers={"Authorization": f"Bearer {user_token}"},
            json={"input": "x"},
        )
        assert r.status_code == 404

    def test_tenant_isolation_in_list(self, client, admin_token, user_token, temp_registry):
        """Tenant A's admin uploads an agent; tenant B's user can't see it."""
        # Upload as tenant "acme" (default in admin_token)
        yaml_text = """
id: isolated
name: Isolated
description: d
system_prompt: p
""".strip()
        client.post(
            "/api/custom-agents:upload",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"yaml": yaml_text},
        )
        # Tenant B: issue a token with a different tenant_id
        from agent_system.core.auth import AuthService
        svc = AuthService(secret="test-only-jwt-secret-32chars-long-enough-for-hs256")
        other_token = svc.issue_token("eve", tenant_id="other-co")
        r = client.get(
            "/api/custom-agents",
            headers={"Authorization": f"Bearer {other_token}"},
        )
        assert r.status_code == 200
        ids = [a["id"] for a in r.json()]
        assert "isolated" not in ids

    def test_delete_admin(self, client, admin_token, temp_registry):
        yaml_text = """
id: killme
name: Kill Me
description: d
system_prompt: p
""".strip()
        client.post(
            "/api/custom-agents:upload",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"yaml": yaml_text},
        )
        r = client.delete(
            "/api/custom-agents/killme",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r.status_code == 204
        # Subsequent GET returns 404
        r = client.get(
            "/api/custom-agents/killme",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r.status_code == 404

    def test_upload_invalid_yaml_returns_400(self, client, admin_token, temp_registry):
        r = client.post(
            "/api/custom-agents:upload",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"yaml": "not: [valid: yaml"},
        )
        assert r.status_code == 400