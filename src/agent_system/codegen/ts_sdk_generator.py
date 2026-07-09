"""
Generate a TypeScript SDK from the OpenAPI spec using openapi-typescript-codegen.

This is a thin wrapper that calls the openapi-typescript-codegen CLI
(must be installed separately via `npm i -D openapi-typescript-codegen`).

Usage:
    python -m agent_system.codegen.ts_sdk_generator
    python -m agent_system.codegen.ts_sdk_generator --spec ./openapi/openapi.json --output-dir ./sdks/typescript

The output is a TypeScript package with:
    sdks/typescript/
        index.ts
        models/
        services/
        core/
        ...

Why a separate Python wrapper:
  - Keeps the codegen entry point in one place (agent_system/codegen/*)
  - Reuses the same OpenAPI spec artifact as the Python SDK
  - Lets CI / release scripts invoke all codegen from a single make target
"""
from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def generate_typescript_sdk(
    spec_path: Path,
    output_dir: Path,
) -> bool:
    """
    Run openapi-typescript-codegen via npx.

    Returns True on success. The caller should ensure Node.js + npx
    are available in PATH.
    """
    if not spec_path.exists():
        logger.error("OpenAPI spec not found: %s", spec_path)
        return False

    if not shutil.which("npx"):
        logger.error(
            "npx not found in PATH. Install Node.js >= 18 to use the TypeScript SDK generator."
        )
        return False

    output_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "npx", "--yes", "openapi-typescript-codegen@0.25.0",
        "--input", str(spec_path),
        "--output", str(output_dir),
        "--client", "fetch",
    ]
    logger.info("Running: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        logger.error("TypeScript SDK generation timed out after 300s")
        return False
    except Exception as e:
        logger.error("TypeScript SDK generation failed: %s", e)
        return False

    if result.returncode != 0:
        logger.error("openapi-typescript-codegen failed:\n%s", result.stderr[-1500:])
        return False

    if not (output_dir / "index.ts").exists():
        logger.error("Generation did not create %s/index.ts. stdout: %s", output_dir, result.stdout[-500:])
        return False

    logger.info("TypeScript SDK generated: %s", output_dir)
    return True


def main():
    parser = argparse.ArgumentParser(description="Generate a TypeScript SDK from the OpenAPI spec")
    parser.add_argument(
        "--spec", default="./openapi/openapi.json",
        help="Path to openapi.json (default: ./openapi/openapi.json)",
    )
    parser.add_argument(
        "--output-dir", default="./sdks/typescript",
        help="Output directory (default: ./sdks/typescript)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    spec_path = Path(args.spec)
    output_dir = Path(args.output_dir)
    ok = generate_typescript_sdk(spec_path, output_dir)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
