"""
Generate a Python SDK from the OpenAPI spec using openapi-python-client.

Usage:
    python -m agent_system.codegen.sdk_generator
    python -m agent_system.codegen.sdk_generator --spec ./openapi/openapi.json --output-dir ./sdks/python

The output is a fully-typed Python package with sync + async clients:
    sdks/python/agent_system_client/
        __init__.py
        api/
        models/
        client.py     # SyncClient
        async_client.py  # AsyncClient
        ...
    sdks/python/README.md
    sdks/python/pyproject.toml

Idempotent — overwrites previous SDK.

Why openapi-python-client:
  - Pure Python (no Java/JVM dependency like openapi-generator)
  - Generates modern, typed, async-first code
  - Pydantic v2 models (matches our backend)
  - ~5-10s generation time vs minutes for openapi-generator
"""
from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def generate_python_sdk(
    spec_path: Path,
    output_dir: Path,
    project_name: str = "agent-system-client",
    config_path: Optional[Path] = None,
) -> bool:
    """
    Run openapi-python-client to generate a Python SDK from the spec.

    Returns True on success.
    """
    if not spec_path.exists():
        logger.error("OpenAPI spec not found: %s", spec_path)
        return False

    output_dir.mkdir(parents=True, exist_ok=True)

    # openapi-python-client CLI:
    #   openapi-python-client generate --path <spec.json> --output-path <dir>
    # It creates <dir>/<project_name>_api_client/ (note: auto-adds _api_client suffix)
    target_dir = output_dir / project_name
    cmd = [
        sys.executable, "-m", "openapi_python_client",
        "generate",
        "--path", str(spec_path),
        "--output-path", str(target_dir),
    ]
    if config_path and config_path.exists():
        cmd.extend(["--config", str(config_path)])

    logger.info("Running: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        logger.error("SDK generation timed out after 120s")
        return False
    except Exception as e:
        logger.error("SDK generation failed: %s", e)
        return False

    if result.returncode != 0:
        logger.error("openapi-python-client failed:\n%s\n%s", result.stdout[-1000:], result.stderr[-1000:])
        return False

    # Verify output exists
    if not target_dir.exists():
        # Tool may have created a suffixed dir
        candidates = list(output_dir.glob("*-client"))
        if candidates:
            target_dir = candidates[0]
        else:
            logger.error("Generation did not create %s. stdout: %s", target_dir, result.stdout[-500:])
            return False

    logger.info("SDK generated: %s", target_dir)
    # Print a brief summary
    if result.stdout:
        for line in result.stdout.splitlines()[-10:]:
            logger.debug("  %s", line)
    return True


def main():
    parser = argparse.ArgumentParser(description="Generate a Python SDK from the OpenAPI spec")
    parser.add_argument(
        "--spec", default="./openapi/openapi.json",
        help="Path to openapi.json (default: ./openapi/openapi.json)",
    )
    parser.add_argument(
        "--output-dir", default="./sdks/python",
        help="Output directory (default: ./sdks/python)",
    )
    parser.add_argument(
        "--project-name", default="agent-system-client",
        help="Project name (default: agent-system-client)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    spec_path = Path(args.spec)
    output_dir = Path(args.output_dir)
    ok = generate_python_sdk(spec_path, output_dir, args.project_name)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
