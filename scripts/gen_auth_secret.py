"""Helper: generate a strong AUTH_SECRET."""
import secrets
import sys

secret = secrets.token_urlsafe(48)
print(f"AUTH_SECRET={secret}")
if len(sys.argv) > 1 and sys.argv[1] == "--write":
    with open(".env", "a") as f:
        f.write(f"\nAUTH_SECRET={secret}\n")
    print("Written to .env")
