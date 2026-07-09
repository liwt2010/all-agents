"""
PR-15: Generate OpenAPI spec + Python SDK + (optional) TypeScript SDK.

This is the orchestrator script that runs all three steps in sequence.
Used by CI / release scripts: `python -m agent_system.codegen.all`.

Steps:
  1. Dump the OpenAPI spec to openapi/openapi.json + openapi.yaml
  2. Generate the Python SDK into sdks/python/<project_name>/
  3. (Optional) Generate the TypeScript SDK into sdks/typescript/

All three are idempotent — re-running overwrites previous output.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Generate OpenAPI spec + SDKs (all-in-one)")
    parser.add_argument(
        "--openapi-dir", default="./openapi",
        help="Output dir for the OpenAPI spec (default: ./openapi)",
    )
    parser.add_argument(
        "--python-sdk-dir", default="./sdks/python",
        help="Output dir for the Python SDK (default: ./sdks/python)",
    )
    parser.add_argument(
        "--typescript-sdk-dir", default="./sdks/typescript",
        help="Output dir for the TypeScript SDK (default: ./sdks/typescript)",
    )
    parser.add_argument(
        "--skip-typescript", action="store_true",
        help="Skip TypeScript SDK generation (faster, no Node required)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    from agent_system.codegen.openapi_spec import generate_spec, write_spec
    from agent_system.codegen.sdk_generator import generate_python_sdk

    # Step 1: OpenAPI spec
    logger.info("=" * 60)
    logger.info("Step 1/3: Generating OpenAPI spec")
    logger.info("=" * 60)
    spec = generate_spec()
    openapi_dir = Path(args.openapi_dir)
    written = write_spec(spec, openapi_dir, ["json", "yaml"])
    if not written:
        logger.error("OpenAPI spec generation failed")
        return 1
    spec_json = openapi_dir / "openapi.json"

    # Step 2: Python SDK
    logger.info("=" * 60)
    logger.info("Step 2/3: Generating Python SDK")
    logger.info("=" * 60)
    if not generate_python_sdk(spec_json, Path(args.python_sdk_dir)):
        logger.error("Python SDK generation failed")
        return 1

    # Step 3: TypeScript SDK (optional)
    if not args.skip_typescript:
        logger.info("=" * 60)
        logger.info("Step 3/3: Generating TypeScript SDK (optional)")
        logger.info("=" * 60)
        try:
            from agent_system.codegen.ts_sdk_generator import generate_typescript_sdk
            if not generate_typescript_sdk(spec_json, Path(args.typescript_sdk_dir)):
                logger.warning("TypeScript SDK generation skipped or failed (Node missing?)")
        except ImportError:
            logger.warning("ts_sdk_generator not available")
    else:
        logger.info("Step 3/3: TypeScript SDK skipped (--skip-typescript)")

    logger.info("=" * 60)
    logger.info("All done. Outputs:")
    logger.info("  - OpenAPI spec: %s", openapi_dir)
    logger.info("  - Python SDK:   %s", args.python_sdk_dir)
    if not args.skip_typescript:
        logger.info("  - TypeScript SDK: %s", args.typescript_sdk_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
