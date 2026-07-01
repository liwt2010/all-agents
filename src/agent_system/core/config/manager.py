"""
ConfigManager — PLATFORM §27.2, §28.3

4-layer configuration override (PLATFORM §28.3):

  Layer 1: runtime cache (in-memory, fastest)
  Layer 2: environment variables (deployment-config)
  Layer 3: secret store (Vault / AWS Secrets Manager — abstracted)
  Layer 4: file-based (yaml per-tenant, slowest, used in dev)

Each layer overrides the previous. Hot reload via `notify_change` callback.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml
from pydantic import BaseModel, Field

from agent_system.config.settings import get_settings

logger = logging.getLogger(__name__)


# ── Secret store abstraction (PLATFORM §27.3) ──

class SecretStore:
    """
    Abstract secret store. Implementations can wrap Vault, AWS Secrets
    Manager, or a simple file-based store for dev.
    """

    def get(self, key: str) -> Optional[str]:
        raise NotImplementedError

    def set(self, key: str, value: str) -> bool:
        raise NotImplementedError


class FileSecretStore(SecretStore):
    """
    File-based secret store. Reads/writes JSON files at:
      {base_dir}/{key}.secret
    """
    def __init__(self, base_dir: str = "data/secrets"):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def get(self, key: str) -> Optional[str]:
        path = self.base_dir / f"{key}.secret"
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
        return None

    def set(self, key: str, value: str) -> bool:
        path = self.base_dir / f"{key}.secret"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        return True


# ── File-based config layer ──

class FileConfigStore:
    """
    Loads configuration from per-tenant YAML files.

    Layout (PLATFORM §28.2):
      config/tenants/{tenant_id}/
        ├── tenant.yaml              # L2 tenant config
        ├── llm.yaml                 # LLM overrides
        ├── quotas.yaml              # Quota overrides
        ├── groups/{group_id}/
        │   └── group.yaml           # L3 group config
        └── agents/{agent_name}.yaml # L4 agent config
    """

    def __init__(self, base_dir: str = "config"):
        self.base_dir = Path(base_dir)

    def get(self, key: str, tenant_id: str = "default", group_id: str = "", agent_name: str = "") -> Optional[Any]:
        """
        Get a value with the 4-layer override applied.
        Note: this method only handles the file layer (L4). The full
        ConfigManager.get() chains env / secret / file.
        """
        # Agent-specific override
        if agent_name:
            path = self.base_dir / "tenants" / tenant_id / "agents" / f"{agent_name}.yaml"
            if path.exists():
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                if key in data:
                    return data[key]
        # Group-specific
        if group_id:
            path = self.base_dir / "tenants" / tenant_id / "groups" / group_id / "group.yaml"
            if path.exists():
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                if key in data:
                    return data[key]
        # Tenant-level
        path = self.base_dir / "tenants" / tenant_id / "tenant.yaml"
        if path.exists():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if key in data:
                return data[key]
        return None


# ── ConfigManager (PLATFORM §27.2) ──

class ConfigManager:
    """
    4-layer config lookup with hot-reload support.

    Lookup order (first hit wins):
      1. Runtime cache (in-memory)
      2. Environment variable
      3. Secret store (Vault / AWS / File)
      4. File config (per-tenant YAML)

    Subscribers can be notified of changes via on_change().
    """

    def __init__(
        self,
        secret_store: Optional[SecretStore] = None,
        file_store: Optional[FileConfigStore] = None,
    ):
        self.cache: Dict[str, Any] = {}
        self.subscribers: List[Callable[[str, Any], None]] = []
        self.secret_store = secret_store
        self.file_store = file_store or FileConfigStore()

    # ── Read ──

    def get(self, key: str, default: Any = None, tenant_id: str = "default") -> Any:
        """
        4-layer lookup. Returns the first non-None value found.
        """
        # L1: cache
        cache_key = self._cache_key(key, tenant_id)
        if cache_key in self.cache:
            return self.cache[cache_key]

        # L2: env var (uppercased, dotted, with tenant prefix)
        env_key = self._env_key(key, tenant_id)
        if env_key in os.environ:
            value = os.environ[env_key]
            self.cache[cache_key] = value
            return value

        # L3: secret store (optional)
        if self.secret_store:
            secret = self.secret_store.get(env_key)
            if secret is not None:
                self.cache[cache_key] = secret
                return secret

        # L4: file-based (per-tenant yaml)
        file_value = self.file_store.get(key, tenant_id=tenant_id)
        if file_value is not None:
            self.cache[cache_key] = file_value
            return file_value

        return default

    # ── Write ──

    def set(self, key: str, value: Any, persist: bool = False, tenant_id: str = "default") -> None:
        """
        Update the runtime cache. If `persist` is True, also write to
        the secret store (for cross-process persistence).
        """
        cache_key = self._cache_key(key, tenant_id)
        old = self.cache.get(cache_key)
        self.cache[cache_key] = value
        if persist and self.secret_store:
            self.secret_store.set(self._env_key(key, tenant_id), str(value))
        if old != value:
            self._notify(key, value)

    def clear_cache(self) -> None:
        self.cache.clear()
        self._notify_all()

    def on_change(self, callback: Callable[[str, Any], None]) -> None:
        self.subscribers.append(callback)

    # ── Internals ──

    def _cache_key(self, key: str, tenant_id: str) -> str:
        return f"{tenant_id}::{key}" if tenant_id else key

    def _env_key(self, key: str, tenant_id: str) -> str:
        return key.upper().replace(".", "_").replace("-", "_")

    def _notify(self, key: str, value: Any) -> None:
        for sub in self.subscribers:
            try:
                sub(key, value)
            except Exception as e:
                logger.debug(f"Subscriber error: {e}")

    def _notify_all(self) -> None:
        for sub in self.subscribers:
            try:
                sub("*", None)
            except Exception as e:
                logger.debug(f"Subscriber error: {e}")


# Global default
_default_manager: Optional[ConfigManager] = None


def get_config_manager() -> ConfigManager:
    global _default_manager
    if _default_manager is None:
        _default_manager = ConfigManager()
    return _default_manager
