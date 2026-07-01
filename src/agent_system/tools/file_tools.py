"""
File system tools — read, write, list files (sandboxed)

PLATFORM §6.3 (security). All paths must resolve under one of the
allowed roots (default: data/, tmp/, current working directory).
Writes to sensitive patterns (.env, *.key, etc.) are blocked.
"""

import fnmatch
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_system.tools.base import Tool, ToolResult, register

logger = logging.getLogger(__name__)

# Sensitive patterns that should never be touched (PLATFORM §27.3 secrets)
SENSITIVE_PATTERNS = [
    "*.env", "*.env.*",          # dotenv files
    "*.key", "*.pem", "*.p12",   # keys
    "*.secret", "*.secrets",
    "id_rsa*", "id_ed25519*",     # SSH keys
    ".aws", ".ssh", ".npmrc",   # sensitive dirs
    "secrets",                    # top-level secrets dir
    "*.kubeconfig",                # K8s config
]

# Default sandbox roots (relative to cwd)
DEFAULT_ALLOWED_ROOTS = ["data", "tmp", "."]


def _resolve_and_validate(path_str: str, allowed_roots: List[str], allow_write: bool) -> Path:
    """
    Resolve a path string and verify it falls under an allowed root.

    Raises PermissionError on rejection (with reason).
    """
    p = Path(path_str)
    # Reject obvious tricks early
    if path_str != str(p) and any(part == ".." for part in p.parts):
        # Path() already normalizes ".."; check for traversal in raw input
        if ".." in path_str.replace("\\", "/"):
            raise PermissionError(f"Path traversal not allowed: {path_str}")

    # Resolve to absolute
    try:
        absolute = p.resolve()
    except (OSError, ValueError) as e:
        raise PermissionError(f"Invalid path: {e}")

    # Check it's under an allowed root
    cwd = Path.cwd().resolve()
    allowed = [cwd / r if not Path(r).is_absolute() else Path(r) for r in allowed_roots]
    allowed = [a.resolve() for a in allowed]

    if not any(_is_within(absolute, root) for root in allowed):
        raise PermissionError(
            f"Path {absolute} is outside allowed roots. "
            f"Allowed: {[str(a) for a in allowed]}"
        )

    # Check sensitive patterns (writes only — reads can be allowed for tools like read_file)
    if allow_write:
        sensitive_match = _match_sensitive(absolute)
        if sensitive_match:
            raise PermissionError(f"Refusing to write sensitive path: {sensitive_match}")

    return absolute


def _match_sensitive(absolute: Path) -> Optional[str]:
    """Check if path matches any sensitive pattern. Returns matched name or None."""
    for pat in SENSITIVE_PATTERNS:
        # Try matching against each path component
        for part in absolute.parts:
            if fnmatch.fnmatch(part, pat.rstrip("/")):
                return f"path component {part!r} matches pattern {pat!r}"
            if fnmatch.fnmatch(part, pat):
                return f"path component {part!r} matches pattern {pat!r}"
    return None


def _is_within(path: Path, root: Path) -> bool:
    """Check if path is the same as or under root."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


# ReadFileTool — reads only, with sandbox
@register
class ReadFileTool(Tool):
    name = "read_file"
    description = "Read file contents from a given path (sandboxed to data/tmp/cwd)"
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path (within sandbox)"},
        },
        "required": ["path"],
    }

    async def execute(self, inputs: Dict[str, Any]) -> ToolResult:
        try:
            path_str = inputs["path"]
            roots = _get_allowed_roots()
            absolute = _resolve_and_validate(path_str, roots, allow_write=False)
            if not absolute.exists():
                return ToolResult(success=False, error=f"File not found: {path_str}")
            if not absolute.is_file():
                return ToolResult(success=False, error=f"Not a file: {absolute}")
            content = absolute.read_text(encoding="utf-8", errors="replace")
            # Cap reads to 1MB to avoid memory issues
            if len(content) > 1_000_000:
                content = content[:1_000_000] + "\n\n[... truncated at 1MB ...]"
            return ToolResult(success=True, output=content)
        except PermissionError as e:
            return ToolResult(success=False, error=str(e))
        except FileNotFoundError:
            return ToolResult(success=False, error=f"File not found: {inputs.get('path')}")
        except Exception as e:
            return ToolResult(success=False, error=str(e))


# WriteFileTool — sandboxed, blocked for sensitive files
@register
class WriteFileTool(Tool):
    name = "write_file"
    description = "Write content to a file at the given path (sandboxed, sensitive files blocked)"
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path within sandbox"},
            "content": {"type": "string", "description": "Content to write"},
        },
        "required": ["path", "content"],
    }

    async def execute(self, inputs: Dict[str, Any]) -> ToolResult:
        try:
            path_str = inputs["path"]
            content = inputs["content"]
            roots = _get_allowed_roots()
            absolute = _resolve_and_validate(path_str, roots, allow_write=True)
            absolute.parent.mkdir(parents=True, exist_ok=True)
            absolute.write_text(content, encoding="utf-8")
            return ToolResult(success=True, output=f"Wrote {len(content)} chars to {absolute}")
        except PermissionError as e:
            return ToolResult(success=False, error=str(e))
        except Exception as e:
            return ToolResult(success=False, error=str(e))


@register
class ListFilesTool(Tool):
    name = "list_files"
    description = "List files and directories in a given path (sandboxed)"
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory path"},
            "pattern": {"type": "string", "description": "Optional glob filter"},
        },
        "required": ["path"],
    }

    async def execute(self, inputs: Dict[str, Any]) -> ToolResult:
        try:
            path_str = inputs["path"]
            pattern = inputs.get("pattern", "*")
            roots = _get_allowed_roots()
            absolute = _resolve_and_validate(path_str, roots, allow_write=False)
            if not absolute.is_dir():
                return ToolResult(success=False, error=f"Not a directory: {absolute}")
            files = []
            for entry in absolute.iterdir():
                if fnmatch.fnmatch(entry.name, pattern):
                    files.append(entry.name)
            return ToolResult(success=True, output={"files": sorted(files), "count": len(files)})
        except PermissionError as e:
            return ToolResult(success=False, error=str(e))
        except Exception as e:
            return ToolResult(success=False, error=str(e))


def _get_allowed_roots() -> List[str]:
    """Read ALLOWED_FILE_ROOTS from env (comma-separated). Default = data/tmp/cwd."""
    env = os.environ.get("ALLOWED_FILE_ROOTS")
    if env:
        return [r.strip() for r in env.split(",") if r.strip()]
    return DEFAULT_ALLOWED_ROOTS
