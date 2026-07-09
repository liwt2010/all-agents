"""
Generate the OpenAPI spec (JSON + YAML) from the FastAPI app.

Usage:
    python -m agent_system.codegen.openapi_spec --output-dir ./openapi
    python -m agent_system.codegen.openapi_spec --output-dir ./openapi --format json
    python -m agent_system.codegen.openapi_spec --output-dir ./openapi --format yaml

The output files:
    openapi.json      — machine-readable OpenAPI 3.1 spec (for SDK generators)
    openapi.yaml      — human-readable YAML version
    openapi.html      — Swagger UI for browsing (optional, via redoc-cli or similar)

Idempotent — overwrites previous output.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def generate_spec() -> dict:
    """
    Import the FastAPI app and return its openapi() dict.

    The app may pull in DB / LLM clients at import time, so we wrap imports
    in a try/except to allow the spec dump to run in any environment.
    """
    try:
        from agent_system.api.server import app
    except Exception as e:
        logger.error("Failed to import FastAPI app: %s", e)
        raise
    return app.openapi()


def write_spec(spec: dict, output_dir: Path, formats: list[str]) -> list[Path]:
    """Write the spec to disk in the requested formats. Returns the files written."""
    output_dir.mkdir(parents=True, exist_ok=True)
    written = []
    if "json" in formats:
        p = output_dir / "openapi.json"
        p.write_text(json.dumps(spec, indent=2, sort_keys=False), encoding="utf-8")
        written.append(p)
        logger.info("Wrote %s (%d bytes)", p, p.stat().st_size)
    if "yaml" in formats:
        try:
            import yaml
        except ImportError:
            logger.error("PyYAML not installed; cannot write YAML. pip install pyyaml")
            return written
        p = output_dir / "openapi.yaml"
        p.write_text(
            yaml.safe_dump(spec, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        written.append(p)
        logger.info("Wrote %s (%d bytes)", p, p.stat().st_size)
    return written


def main():
    parser = argparse.ArgumentParser(description="Dump the OpenAPI spec from the FastAPI app")
    parser.add_argument(
        "--output-dir", default="./openapi",
        help="Directory to write openapi.json / openapi.yaml (default: ./openapi)",
    )
    parser.add_argument(
        "--format", choices=["json", "yaml", "both"], default="both",
        help="Output format (default: both)",
    )
    parser.add_argument(
        "--print", action="store_true",
        help="Print the spec summary to stdout (paths, count of routes, etc.)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    spec = generate_spec()
    out = Path(args.output_dir)

    formats = ["json", "yaml"] if args.format == "both" else [args.format]
    written = write_spec(spec, out, formats)

    if args.print:
        paths = spec.get("paths", {})
        route_count = sum(len([m for m in v if m in ("get", "post", "put", "delete", "patch")]) for v in paths.values())
        print(f"\nOpenAPI spec summary:")
        print(f"  title:   {spec.get('info', {}).get('title')}")
        print(f"  version: {spec.get('info', {}).get('version')}")
        print(f"  paths:   {len(paths)}")
        print(f"  routes:  {route_count}")
        print(f"  tags:    {len(spec.get('tags', []))}")
        print(f"  servers: {len(spec.get('servers', []))}")
        print(f"  output:  {[str(p) for p in written]}")

    return 0 if written else 1


if __name__ == "__main__":
    sys.exit(main())
