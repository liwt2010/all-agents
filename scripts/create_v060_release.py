#!/usr/bin/env python3
"""Create or update the v0.6.0 GitHub release.

Reads the v0.6.0 section from RELEASE_NOTES.md and POSTs to
api.github.com/repos/liwt2010/all-agents/releases. Falls back to
PATCH on 422 (release already exists) by looking up the release
by tag and patching it.

GitHub Push Protection blocks pushes that contain real PATs, so
the token is read from $GITHUB_TOKEN at runtime.

Usage:
    GITHUB_TOKEN=ghp_xxx python scripts/create_v060_release.py
"""
import json, os, re, sys, urllib.request, urllib.error

TAG = "v0.6.0"
TITLE = "v0.6.0 — 2026-07-24 (Task collaboration primitives)"
REPO = "liwt2010/all-agents"

if "GITHUB_TOKEN" not in os.environ:
    print("error: GITHUB_TOKEN env var not set", file=sys.stderr)
    sys.exit(2)

token = os.environ["GITHUB_TOKEN"]
notes = open("RELEASE_NOTES.md", encoding="utf-8").read()
m = re.search(
    r"## v0\.6\.0.*?(?=\n## v\d+\.\d+\.\d+|\n---\s*\n)",
    notes,
    re.DOTALL,
)
if not m:
    print("error: could not find v0.6.0 section in RELEASE_NOTES.md",
          file=sys.stderr)
    sys.exit(2)
body = m.group(0).rstrip()

API = f"https://api.github.com/repos/{REPO}"
HEADERS = {
    "Authorization": f"Bearer {token}",
    "Accept": "application/vnd.github+json",
    "Content-Type": "application/json; charset=utf-8",
}


def _request(method: str, path: str, body_obj: dict | None = None):
    data = json.dumps(body_obj).encode("utf-8") if body_obj else None
    req = urllib.request.Request(
        f"{API}{path}", data=data, method=method, headers=HEADERS,
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return ("http_error", e.code, e.read().decode()[:500])


# Step 1: try POST.
res = _request("POST", "/releases", {
    "tag_name": TAG,
    "name": TITLE,
    "body": body,
    "draft": False,
    "prerelease": False,
})
if isinstance(res, dict):
    print("OK (created)", res.get("html_url"), res.get("name"))
    sys.exit(0)

code = res[1] if res[0] == "http_error" else None
if code == 422:
    # Release already exists. Look up by tag and PATCH it.
    print("release already exists; looking up id by tag", file=sys.stderr)
    lookup = _request("GET", f"/releases/tags/{TAG}")
    if isinstance(lookup, dict) and lookup.get("id"):
        rel_id = lookup["id"]
        res2 = _request("PATCH", f"/releases/{rel_id}", {
            "tag_name": TAG,
            "name": TITLE,
            "body": body,
            "draft": False,
            "prerelease": False,
        })
        if isinstance(res2, dict):
            print("OK (updated)", res2.get("html_url"), res2.get("name"))
            sys.exit(0)
        print("HTTP", res2[1], res2[2])
        sys.exit(1)
    print("could not look up release by tag:", lookup)
    sys.exit(1)

print("HTTP", code, res[2] if res[0] == "http_error" else res)
sys.exit(1)