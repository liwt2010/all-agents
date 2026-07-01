"""
Tests: ConfigManager 4-layer override
"""

import os
import pytest
from pathlib import Path

from agent_system.core.config.manager import (
    ConfigManager,
    SecretStore,
    FileSecretStore,
    FileConfigStore,
    get_config_manager,
)


class TestConfigManager:
    def test_layer1_cache(self):
        cm = ConfigManager()
        cm.set("foo", "cached")
        assert cm.get("foo") == "cached"

    def test_layer2_env(self, monkeypatch):
        cm = ConfigManager()
        monkeypatch.setenv("LLM_MODEL", "claude-sonnet-4-20250514")
        assert cm.get("llm.model") == "claude-sonnet-4-20250514"

    def test_layer2_overrides_cache(self, monkeypatch):
        cm = ConfigManager()
        cm.set("llm.model", "cached_value")
        monkeypatch.setenv("LLM_MODEL", "env_value")
        # Cache was set first, so it wins
        assert cm.get("llm.model") == "cached_value"
        # But after clearing cache, env wins
        cm.clear_cache()
        assert cm.get("llm.model") == "env_value"

    def test_layer3_secret_store(self, tmp_path, monkeypatch):
        secrets = FileSecretStore(str(tmp_path))
        secrets.set("DEPLOY_TARGET", "staging")
        cm = ConfigManager(secret_store=secrets)
        assert cm.get("deploy.target") == "staging"

    def test_layer4_file_yaml(self, tmp_path):
        # Create a tenant config
        tenant_dir = tmp_path / "tenants" / "acme"
        tenant_dir.mkdir(parents=True)
        (tenant_dir / "tenant.yaml").write_text("max_users: 100\n", encoding="utf-8")

        file_store = FileConfigStore(str(tmp_path))
        cm = ConfigManager(file_store=file_store)
        assert cm.get("max_users", tenant_id="acme") == 100

    def test_layer4_agent_override(self, tmp_path):
        # Create both tenant-level and agent-level config
        tenant_dir = tmp_path / "tenants" / "acme"
        tenant_dir.mkdir(parents=True, exist_ok=True)
        (tenant_dir / "tenant.yaml").write_text("llm_model: claude-sonnet", encoding="utf-8")
        agent_dir = tenant_dir / "agents"
        agent_dir.mkdir(exist_ok=True)
        (agent_dir / "tech_agent.yaml").write_text("llm_model: claude-haiku", encoding="utf-8")

        file_store = FileConfigStore(str(tmp_path))
        assert file_store.get("llm_model", tenant_id="acme") == "claude-sonnet"
        assert file_store.get("llm_model", tenant_id="acme", agent_name="tech_agent") == "claude-haiku"

    def test_layer_precedence(self, tmp_path, monkeypatch):
        # Set all 4 layers
        cm = ConfigManager()
        # L1 cache (must use same tenant scope as get())
        cm.set("key", "L1_cache", tenant_id="acme")
        # L2 env
        monkeypatch.setenv("KEY", "L2_env")
        # L3 secret
        secrets = FileSecretStore(str(tmp_path))
        secrets.set("KEY", "L3_secret")
        cm.secret_store = secrets
        # L4 file
        fdir = tmp_path / "tenants" / "acme"
        fdir.mkdir(parents=True)
        (fdir / "tenant.yaml").write_text("key: L4_file", encoding="utf-8")
        cm.file_store = FileConfigStore(str(tmp_path))

        # L1 wins
        assert cm.get("key", tenant_id="acme") == "L1_cache"
        # After clearing cache, L2 wins
        cm.clear_cache()
        assert cm.get("key", tenant_id="acme") == "L2_env"
        # After clearing env (and cache), L3 wins
        monkeypatch.delenv("KEY")
        cm.clear_cache()
        assert cm.get("key", tenant_id="acme") == "L3_secret"
        # After clearing secret (and cache), L4 wins
        (tmp_path / "KEY.secret").unlink()
        cm.clear_cache()
        assert cm.get("key", tenant_id="acme") == "L4_file"

    def test_set_persists_to_secret_store(self, tmp_path, monkeypatch):
        secrets = FileSecretStore(str(tmp_path))
        cm = ConfigManager(secret_store=secrets)
        cm.set("api_key", "sk-xyz", persist=True)
        # Reading should still find it from secrets
        assert secrets.get("API_KEY") == "sk-xyz"

    def test_default_value(self):
        cm = ConfigManager()
        assert cm.get("missing.key", default="fallback") == "fallback"

    def test_on_change_callback(self, monkeypatch):
        cm = ConfigManager()
        changes = []
        cm.on_change(lambda k, v: changes.append((k, v)))

        cm.set("foo", "v1")
        assert changes == [("foo", "v1")]

        cm.set("foo", "v1")  # same value, no change
        assert changes == [("foo", "v1")]

        cm.set("foo", "v2")  # different value
        assert changes == [("foo", "v1"), ("foo", "v2")]

    def test_tenant_scoping(self, tmp_path, monkeypatch):
        fdir = tmp_path / "tenants" / "acme"
        fdir.mkdir(parents=True)
        (fdir / "tenant.yaml").write_text("model: claude-sonnet", encoding="utf-8")
        fdir2 = tmp_path / "tenants" / "beta"
        fdir2.mkdir(parents=True)
        (fdir2 / "tenant.yaml").write_text("model: claude-haiku", encoding="utf-8")

        cm = ConfigManager(file_store=FileConfigStore(str(tmp_path)))
        assert cm.get("model", tenant_id="acme") == "claude-sonnet"
        assert cm.get("model", tenant_id="beta") == "claude-haiku"
        # Different tenant, no file
        assert cm.get("model", tenant_id="gamma", default="default") == "default"

    def test_env_key_formatting(self, monkeypatch):
        cm = ConfigManager()
        # Dots and dashes become underscores, uppercase
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        assert cm.get("anthropic.api-key") == "sk-test"


class TestSecretStore:
    def test_file_secret_store_round_trip(self, tmp_path):
        store = FileSecretStore(str(tmp_path))
        store.set("MY_KEY", "secret_value")
        assert store.get("MY_KEY") == "secret_value"

    def test_missing_key(self, tmp_path):
        store = FileSecretStore(str(tmp_path))
        assert store.get("NONEXISTENT") is None


class TestSingleton:
    def test_singleton(self):
        m1 = get_config_manager()
        m2 = get_config_manager()
        assert m1 is m2
