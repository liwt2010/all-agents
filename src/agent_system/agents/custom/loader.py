"""
Custom Agent YAML loader (PR v0.3.0).

Allows users to define custom agents in a YAML file instead of
hand-writing JSON. The YAML is parsed, validated against
CustomAgentConfig, and registered into the CustomAgentRegistry.

YAML schema (all top-level keys optional except `id`, `name`,
`description`, `system_prompt`):

```yaml
# Required
id: code-reviewer        # unique within tenant, [A-Za-z0-9_-]{1,128}
name: Code Reviewer      # display name
description: Reviews code for style and correctness
system_prompt: |
  You are a senior code reviewer...

# Optional
tools:                    # subset of registered tool names
  - read_file
  - code_search
safety: normal            # strict | normal | autonomous
tenant_id: default        # for multi-tenant deployments
llm_config:               # overrides for the agent's LLM
  model: claude-haiku-4-5-20251001
  temperature: 0.2
  max_tokens: 2048

# Anything else is preserved as extra metadata (the config model
# uses extra='allow'). Useful for tags, owner, ticket-link.
tags:
  - review
  - automation
owner: alice@example.com
```

Loaded configs are validated and (by default) auto-registered
into the global CustomAgentRegistry. Loader errors are surfaced
with file + line context so operators can fix them quickly.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from agent_system.agents.custom.base import CustomAgentConfig
from agent_system.agents.custom.registry import (
    CustomAgentRegistry,
    get_custom_agent_registry,
)

logger = logging.getLogger(__name__)


class CustomAgentLoadError(Exception):
    """Raised when a YAML file fails to load or validate.

    Carries the file path and a one-line hint so the operator can
    fix it without reading a stack trace.
    """

    def __init__(self, path: Path, message: str):
        self.path = path
        super().__init__(f"{path}: {message}")


def load_from_yaml_file(path: str | Path) -> CustomAgentConfig:
    """Parse one YAML file into a validated CustomAgentConfig.

    Raises:
        CustomAgentLoadError: if the file doesn't exist, isn't
            parseable YAML, or fails Pydantic validation.
    """
    p = Path(path)
    if not p.exists():
        raise CustomAgentLoadError(p, "file does not exist")
    try:
        import yaml  # PyYAML — already a runtime dep (pyyaml in pyproject)
    except ImportError as e:
        raise CustomAgentLoadError(p, f"PyYAML not installed: {e}") from e

    try:
        with p.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise CustomAgentLoadError(p, f"invalid YAML: {e}") from e

    if not isinstance(data, dict):
        raise CustomAgentLoadError(
            p, f"top-level must be a mapping, got {type(data).__name__}"
        )

    try:
        return CustomAgentConfig(**data)
    except Exception as e:
        raise CustomAgentLoadError(p, f"schema validation failed: {e}") from e


def load_from_directory(
    directory: str | Path,
    registry: CustomAgentRegistry | None = None,
    *,
    auto_register: bool = True,
) -> list[CustomAgentConfig]:
    """Load all *.yaml / *.yml files under `directory`.

    Files are loaded in alphabetical order for deterministic
    behavior (helps when two files declare the same id — last
    write wins; alphabetical ordering means the order matches
    `ls`).

    Returns the list of successfully loaded configs. Files that
    fail to load are logged and skipped — one bad file shouldn't
    prevent the rest from coming up.

    If `auto_register` is True (default), loaded configs are
    added to the registry via `registry.register(config)`. Use
    `auto_register=False` if you want to inspect the result
    before persisting (e.g. for a dry-run / audit command).
    """
    d = Path(directory)
    if not d.exists():
        raise FileNotFoundError(f"directory not found: {d}")
    if not d.is_dir():
        raise NotADirectoryError(f"not a directory: {d}")

    registry = registry or get_custom_agent_registry()
    loaded: list[CustomAgentConfig] = []
    errors: list[CustomAgentLoadError] = []

    # Sort so behavior is deterministic — files earlier alphabetically
    # are loaded first; later files with the same id win.
    yaml_files = sorted(
        list(d.glob("*.yaml")) + list(d.glob("*.yml")),
        key=lambda p: p.name,
    )

    for path in yaml_files:
        try:
            config = load_from_yaml_file(path)
        except CustomAgentLoadError as e:
            logger.warning(f"Skipping invalid custom agent YAML: {e}")
            errors.append(e)
            continue
        loaded.append(config)
        if auto_register:
            try:
                registry.register(config)
            except Exception as e:
                logger.warning(f"Failed to register {path.name}: {e}")
                errors.append(CustomAgentLoadError(path, str(e)))

    if errors and not loaded:
        # All files failed — surface the first error so the operator
        # sees something useful.
        raise errors[0]

    logger.info(
        f"Loaded {len(loaded)} custom agent config(s) from {d}"
        + (f" ({len(errors)} skipped)" if errors else "")
    )
    return loaded