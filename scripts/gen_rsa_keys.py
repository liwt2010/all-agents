#!/usr/bin/env python3
"""
Generate an RSA key pair for use with RS256 JWTs.

Outputs:
  - private key (PEM, PKCS#8) — signs new tokens. KEEP SECRET.
  - public key (PEM, SPKI)  — verifies tokens. Safe to publish via
    /api/auth/jwks or copy to external verifiers.

Usage:
  python scripts/gen_rsa_keys.py --kid v1
  python scripts/gen_rsa_keys.py --kid v1 --bits 4096
  python scripts/gen_rsa_keys.py --kid v1 --output-dir ./keys
  python scripts/gen_rsa_keys.py --kid v1 --env-file .env.local

The --env-file mode writes AUTH_PRIVATE_KEY= and AUTH_PUBLIC_KEYS=
lines that can be sourced or copied into your environment config.

This is intentionally a standalone script (no app imports) so it can be
run inside a build pipeline or in a sealed environment where the rest
of the codebase isn't installed.
"""
from __future__ import annotations

import argparse
import base64
import sys
from pathlib import Path


def _b64url_uint(n: int) -> str:
    nbytes = (n.bit_length() + 7) // 8
    return base64.urlsafe_b64encode(n.to_bytes(nbytes, "big")).rstrip(b"=").decode("ascii")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate an RSA key pair for RS256 JWT signing."
    )
    parser.add_argument(
        "--kid",
        default="v1",
        help="Key ID (kid) — identifies this key in JWKS and rotation flows (default: v1).",
    )
    parser.add_argument(
        "--bits",
        type=int,
        default=2048,
        choices=[2048, 3072, 4096],
        help="RSA modulus size (default: 2048; 4096 is slower but stronger).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("."),
        help="Where to write private.pem + public.pem (default: current directory).",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="If set, append AUTH_PRIVATE_KEY=... and AUTH_PUBLIC_KEYS=... lines to this file.",
    )
    args = parser.parse_args()

    # Lazy import so the script works without cryptography pre-installed
    # at argparse-failure time.
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
    except ImportError:
        print(
            "ERROR: 'cryptography' is required. pip install cryptography",
            file=sys.stderr,
        )
        return 2

    key = rsa.generate_private_key(public_exponent=65537, key_size=args.bits)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    priv_path = args.output_dir / "private.pem"
    pub_path = args.output_dir / "public.pem"
    priv_path.write_text(private_pem, encoding="utf-8")
    pub_path.write_text(public_pem, encoding="utf-8")
    try:
        priv_path.chmod(0o600)
    except OSError:
        pass  # Windows doesn't honour POSIX bits; ignore.

    # Emit JWKS preview so the operator can sanity-check the key
    pub_numbers = key.public_key().public_numbers()
    jwks_preview = (
        "{\n"
        f'  "kty": "RSA",\n'
        f'  "kid": "{args.kid}",\n'
        f'  "use": "sig",\n'
        f'  "alg": "RS256",\n'
        f'  "n": "{_b64url_uint(pub_numbers.n)}",\n'
        f'  "e": "{_b64url_uint(pub_numbers.e)}"\n'
        "}"
    )

    print(f"Wrote private key: {priv_path}  (mode 0600, KEEP SECRET)")
    print(f"Wrote public  key: {pub_path}")
    print()
    print("JWKS preview:")
    print(jwks_preview)

    if args.env_file:
        # Append (or replace existing AUTH_PUBLIC_KEYS line) so multiple
        # rotations can accumulate via repeated --env-file runs.
        existing = args.env_file.read_text(encoding="utf-8") if args.env_file.exists() else ""
        nl = chr(10)  # f-strings can't contain backslashes
        priv_one_line = private_pem.replace(nl, "\\n")
        pub_one_line = public_pem.replace(nl, "\\n")
        new_lines = [
            f'AUTH_PRIVATE_KEY="{priv_one_line}"',
            f'AUTH_PUBLIC_KEYS="{args.kid}:{pub_one_line}"',
        ]
        # Strip prior AUTH_PRIVATE_KEY / AUTH_PUBLIC_KEYS lines so re-runs
        # don't accumulate conflicting entries.
        kept = [
            line for line in existing.splitlines()
            if not line.startswith("AUTH_PRIVATE_KEY=")
            and not line.startswith("AUTH_PUBLIC_KEYS=")
        ]
        args.env_file.write_text(
            "\n".join(kept + new_lines) + "\n",
            encoding="utf-8",
        )
        print(f"Appended env vars to {args.env_file}")

    return 0


if __name__ == "__main__":
    sys.exit(main())