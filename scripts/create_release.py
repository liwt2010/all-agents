#!/usr/bin/env python3
"""Create or update the v0.4.0 GitHub release.

GitHub Push Protection will block pushes that contain real PATs,
so the token is read from $GITHUB_TOKEN at runtime instead of
embedded in the script.

Usage:
    GITHUB_TOKEN=ghp_xxx python scripts/create_v040_release.py
"""
import json, os, re, sys, urllib.request, urllib.error

if "GITHUB_TOKEN" not in os.environ:
    print("error: GITHUB_TOKEN env var not set", file=sys.stderr)
    sys.exit(2)

token = os.environ["GITHUB_TOKEN"]
notes = open("RELEASE_NOTES.md", encoding="utf-8").read()
m = re.search(r"## v0\.4\.0(.*?)---", notes, re.DOTALL)
body = m.group(0).rstrip() if m else "NOT FOUND"
hdr_line = "## v0.4.0 - 2026-07-22 (Streaming tool-call events)"
full = hdr_line + chr(10) + chr(10) + body

# Try POST first, fall back to PATCH on conflict (release exists).
for method, url in [("POST", "releases"), ("PATCH", "releases/v0.4.0")]:
    data = json.dumps({
        "tag_name": "v0.4.0",
        "name": hdr_line,
        "body": full,
        "draft": False,
        "prerelease": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.github.com/repos/liwt2010/all-agents/releases/{url}",
        data=data, method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json; charset=utf-8",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read())
            print("OK", d.get("html_url"), d.get("name"))
            sys.exit(0)
    except urllib.error.HTTPError as e:
        if e.code == 422 and method == "POST":
            # Release already exists — try PATCH.
            print("release already exists; trying PATCH", file=sys.stderr)
            continue
        print("HTTP", e.code, e.read().decode()[:500])
        sys.exit(1)
