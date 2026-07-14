#!/usr/bin/env python3
"""Block accidental commits of secrets / API keys.

Reads file content from stdin (pre-commit passes the file content via stdin).
Exits non-zero if any secret pattern matches.

Usage (via pre-commit):
    entry: python scripts/check_no_secrets.py

Patterns detected:
    - Anthropic API keys (sk-...)
    - OpenAI project keys (sk-proj-...)
    - GitHub PATs (ghp_...)
    - AWS access keys (AKIA...)
    - Generic api_key= / secret= assignments
"""
import re
import sys

PATTERNS = [
    (r"sk-[a-zA-Z0-9]{20,}", "Anthropic-style API key"),
    (r"sk-proj-[a-zA-Z0-9]{20,}", "OpenAI project key"),
    (r"ghp_[a-zA-Z0-9]{20,}", "GitHub personal access token"),
    (r"AKIA[0-9A-Z]{16}", "AWS access key"),
    (r"(?i)api[_-]?key\s*=\s*[\"'][^\"']{8,}[\"']", "api_key assignment"),
    (r"(?i)secret\s*=\s*[\"'][^\"']{8,}[\"']", "secret assignment"),
]


def main() -> int:
    text = sys.stdin.read()
    hits = []
    for pattern, label in PATTERNS:
        matches = re.findall(pattern, text)
        if matches:
            hits.append(f"{label}: {len(matches)} match(es)")

    if hits:
        print("BLOCKED: detected potential secrets:", file=sys.stderr)
        for hit in hits:
            print(f"  - {hit}", file=sys.stderr)
        print("Rotate any leaked credentials immediately.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
