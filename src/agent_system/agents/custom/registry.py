"""
Custom Agent Registry — persistent storage for CustomAgentConfigs.

PR 8 / agents/custom/registry.py

Stores configs as JSON files under <storage_path>/<tenant_id>/<id>.json.
In-memory cache for fast access. Singleton via get_custom_agent_registry().
"""

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

from agent_system.agents.custom.base import CustomAgent, CustomAgentConfig

logger = logging.getLogger(__name__)


def _safe_id(value: str) -> str:
    """Sanitize an id — only allow [A-Za-z0-9_-], max 128 chars."""
    value = (value or "").strip()
    if not value or len(value) > 128:
        raise ValueError(f"Invalid id: {value!r}")
    if not all(c.isalnum() or c in ("_", "-") for c in value):
        raise ValueError(f"Invalid id (must be [A-Za-z0-9_-]): {value!r}")
    return value


class CustomAgentRegistry:
    """Persistent registry of CustomAgentConfigs.

    Storage layout: <storage_path>/<tenant_id>/<id>.json
    """

    def __init__(self, storage_path: Optional[str] = None):
        self.storage_path = Path(storage_path) if storage_path else Path(tempfile.gettempdir()) / "agent_custom_agents"
        self._configs: Dict[str, CustomAgentConfig] = {}  # key = f"{tenant_id}:{id}"
        self._load_from_disk()

    @staticmethod
    def _key(tenant_id: str, agent_id: str) -> str:
        return f"{tenant_id or 'default'}:{agent_id}"

    def _config_path(self, tenant_id: str, agent_id: str) -> Path:
        safe_tenant = _safe_id(tenant_id or "default")
        safe_id = _safe_id(agent_id)
        # Path.resolve() rejects '..' — but defense in depth: also check string
        p = (self.storage_path / safe_tenant / f"{safe_id}.json").resolve()
        # Prevent path traversal
        if not str(p).startswith(str(self.storage_path.resolve())):
            raise ValueError(f"Path traversal detected: {p}")
        return p

    def _load_from_disk(self) -> None:
        """Scan storage_path and load all *.json configs into memory."""
        if not self.storage_path.exists():
            return
        for tenant_dir in self.storage_path.iterdir():
            if not tenant_dir.is_dir():
                continue
            tenant_id = tenant_dir.name
            for json_file in tenant_dir.glob("*.json"):
                try:
                    with json_file.open("r", encoding="utf-8") as f:
                        data = json.load(f)
                    cfg = CustomAgentConfig(**data)
                    self._configs[self._key(cfg.tenant_id or "default", cfg.id)] = cfg
                except Exception as e:
                    logger.warning(f"Failed to load custom agent config {json_file}: {e}")

    def _save_to_disk(self, config: CustomAgentConfig) -> None:
        path = self._config_path(config.tenant_id or "default", config.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(config.model_dump(mode="json"), f, indent=2, ensure_ascii=False)

    def register(self, config: CustomAgentConfig) -> None:
        """Add or replace a config. Persists to disk."""
        _safe_id(config.id)
        tenant_id = config.tenant_id or "default"
        self._configs[self._key(tenant_id, config.id)] = config
        self._save_to_disk(config)
        logger.info(f"Registered custom agent: {tenant_id}/{config.id}")

    def get(self, agent_id: str, tenant_id: str = "default") -> Optional[CustomAgentConfig]:
        """Load by (tenant_id, id). Returns None if not found."""
        return self._configs.get(self._key(tenant_id, agent_id))

    def list(self, tenant_id: str = "default") -> List[CustomAgentConfig]:
        """List all configs for a tenant."""
        return [
            c for k, c in self._configs.items()
            if k.startswith(f"{tenant_id}:") or k.startswith(f"{tenant_id or 'default'}:")
        ]

    def delete(self, agent_id: str, tenant_id: str = "default") -> bool:
        """Remove + persist. Returns True if existed."""
        key = self._key(tenant_id, agent_id)
        if key not in self._configs:
            return False
        del self._configs[key]
        path = self._config_path(tenant_id, agent_id)
        if path.exists():
            path.unlink()
        return True

    def instantiate(self, agent_id: str, tenant_id: str = "default") -> Optional[CustomAgent]:
        """Get config + create CustomAgent runtime instance. Returns None if not found."""
        config = self.get(agent_id, tenant_id)
        if config is None:
            return None
        return CustomAgent(config)


# ── Singleton ──

_custom_registry: Optional[CustomAgentRegistry] = None


def get_custom_agent_registry() -> CustomAgentRegistry:
    """Return the global CustomAgentRegistry singleton.

    Default storage path is `<tmp>/agent_custom_agents/`. Override via the
    AGENT_CUSTOM_AGENTS_DIR env var if needed.
    """
    global _custom_registry
    if _custom_registry is None:
        path = os.environ.get("AGENT_CUSTOM_AGENTS_DIR")
        _custom_registry = CustomAgentRegistry(storage_path=path)
    return _custom_registry