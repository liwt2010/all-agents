"""
Code search tool — search for code patterns in files
"""

import os
import re
from typing import Any, Dict, List

from agent_system.tools.base import Tool, ToolResult, register


@register
class CodeSearchTool(Tool):
    name = "code_search"
    description = "Search for code patterns across files (regex supported)"
    input_schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Search pattern (regex)"},
            "path": {"type": "string", "description": "Root directory to search in"},
            "file_pattern": {"type": "string", "description": "File glob filter (e.g. *.py)"},
        },
        "required": ["pattern", "path"],
    }

    async def execute(self, inputs: dict[str, Any]) -> ToolResult:
        try:
            pattern = inputs["pattern"]
            root = inputs["path"]
            file_pattern = inputs.get("file_pattern", "*")
            results: list[dict] = []

            for dirpath, _, filenames in os.walk(root):
                for fname in filenames:
                    if not self._match_glob(fname, file_pattern):
                        continue
                    fpath = os.path.join(dirpath, fname)
                    try:
                        with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                            for i, line in enumerate(f, 1):
                                if re.search(pattern, line):
                                    results.append({
                                        "file": fpath,
                                        "line": i,
                                        "content": line.rstrip()[:200],
                                    })
                    except Exception:
                        continue

            return ToolResult(success=True, output={
                "matches": results,
                "count": len(results),
                "pattern": pattern,
            })
        except Exception as e:
            return ToolResult(success=False, error=str(e))

    def _match_glob(self, filename: str, pattern: str) -> bool:
        import fnmatch
        return fnmatch.fnmatch(filename, pattern)


@register
class RunTestTool(Tool):
    name = "run_test"
    description = "Run a test file or pytest command and return the results"
    input_schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Test command to run (e.g. pytest tests/)"},
        },
        "required": ["command"],
    }

    async def execute(self, inputs: dict[str, Any]) -> ToolResult:
        import subprocess
        import sys
        try:
            cmd = inputs["command"]
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
            output = result.stdout[-3000:] if len(result.stdout) > 3000 else result.stdout
            if result.stderr:
                output += "\n--- stderr ---\n" + result.stderr[-1000:]
            return ToolResult(success=result.returncode == 0, output=output)
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, error="Test timed out (120s)")
        except Exception as e:
            return ToolResult(success=False, error=str(e))
