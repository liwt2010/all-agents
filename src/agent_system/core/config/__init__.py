"""Config package — 4-layer override (PLATFORM §27.2, §28.3)."""
from agent_system.core.config.manager import (
    ConfigManager,
    SecretStore,
    FileSecretStore,
    FileConfigStore,
    get_config_manager,
)

__all__ = [
    "ConfigManager", "SecretStore", "FileSecretStore", "FileConfigStore",
    "get_config_manager",
]
