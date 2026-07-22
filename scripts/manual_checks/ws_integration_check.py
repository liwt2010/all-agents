#!/usr/bin/env python3
"""
Manual integration check — WebSocket endpoints + GitHub webhook.

Why this exists:
  The TestClient.websocket_connect fixture in starlette 1.x has
  a known incompatibility with anyio 4.x + httpx 0.28 (close code
  1008). The WS endpoint itself works correctly when served by real
  uvicorn — this script proves it.

What it does:
  1. Starts the Agent System API in a subprocess (uvicorn, port 8765)
  2. Waits for /api/health to return 200
  3. Mints a JWT for a test user
  4. Connects to /api/ws/llm/stream and reads chunk / done messages
  5. POSTs a fake pull_request event to /api/webhooks/github and
     verifies the 200 response + delivery ID echo
  6. Stops the server, prints PASS/FAIL summary

Run:
  PYTHONPATH=src .venv/Scripts/python.exe scripts/manual_checks/ws_integration_check.py
  # or with the system Python if it has uvicorn:
  PYTHONPATH=src python3 scripts/manual_checks/ws_integration_check.py

Exit code 0 on success, 1 on any failure.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

# Make `src/` importable when run from repo root
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402
import websockets  # noqa: E402


HOST = "127.0.0.1"
PORT = 8765
BASE = f"http://{HOST}:{PORT}"
WS_BASE = f"ws://{HOST}:{PORT}"
STARTUP_TIMEOUT = 30.0
LLM_STREAM_TIMEOUT = 20.0
WEBHOOK_TIMEOUT = 10.0


def log(msg: str) -> None:
    print(f"[ws-check] {msg}", flush=True)


def wait_for_port(host: str, port: int, timeout: float) -> bool:
    """Poll TCP until the port accepts connections or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def kill_port_holder(port: int) -> None:
    """On Windows, terminate any process still bound to `port`.

    Background: a previous run's uvicorn may survive `terminate()` on
    Windows (signal handling quirks), holding the port. The next run's
    subprocess then fails to bind but the TCP probe reports "open"
    because the orphan is still there — leading to confusing 503s from
    a server that doesn't have our env.
    """
    import subprocess
    try:
        out = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return
    for line in out.stdout.splitlines():
        if f":{port}" in line and "LISTENING" in line:
            parts = line.split()
            try:
                pid = int(parts[-1])
            except (ValueError, IndexError):
                continue
            if pid <= 0:
                continue
            # Skip our own process and parent
            if pid in (os.getpid(), os.getppid()):
                continue
            try:
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid)],
                    capture_output=True, timeout=5,
                )
                log(f"killed stale port-holder pid={pid}")
            except Exception:
                pass


def wait_for_health(timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"{BASE}/api/health", timeout=1.0)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def mint_token() -> str:
    """Issue a JWT using the same secret/issuer the server expects."""
    os.environ.setdefault(
        "AUTH_SECRET", "test-only-jwt-secret-32chars-long-enough-for-hs256"
    )
    from agent_system.core.auth.jwt import AuthService
    return AuthService(secret=os.environ["AUTH_SECRET"]).issue_token(
        "ws-check", tenant_id="acme"
    )


async def check_llm_stream(token: str) -> tuple[bool, str]:
    """Connect to /api/ws/llm/stream, read messages until 'done'."""
    url = f"{WS_BASE}/api/ws/llm/stream?token={token}&prompt=say+hi"
    try:
        async with websockets.connect(url, open_timeout=LLM_STREAM_TIMEOUT) as ws:
            chunks: list[str] = []
            done: dict | None = None
            for _ in range(50):  # safety cap
                msg_raw = await asyncio.wait_for(ws.recv(), timeout=LLM_STREAM_TIMEOUT)
                msg = json.loads(msg_raw)
                kind = msg.get("type")
                if kind == "chunk":
                    chunks.append(msg.get("data", ""))
                elif kind == "done":
                    done = msg.get("data", {})
                    break
                elif kind == "error":
                    return False, f"server error: {msg.get('data')!r}"
                # 'ping' messages are allowed; ignore them.
            if done is None:
                return False, "no 'done' message received"
            joined = "".join(chunks)
            if not joined:
                return False, "no 'chunk' messages received"
            return True, (
                f"received {len(chunks)} chunks totalling {len(joined)} chars, "
                f"done payload keys: {sorted(done.keys())}"
            )
    except Exception as e:
        return False, f"WS error: {type(e).__name__}: {e}"


def check_github_webhook(secret: str) -> tuple[bool, str]:
    """POST a fake pull_request event and verify 200 + delivery echo."""
    body = json.dumps({
        "action": "opened",
        "pull_request": {
            "number": 9999,
            "title": "[ws-check] test PR",
            "body": "synthetic payload from ws_integration_check.py",
            "html_url": "https://github.com/test/repo/pull/9999",
        },
        "repository": {"full_name": "test/repo"},
        "sender": {"login": "ws-check"},
    }).encode("utf-8")
    # `secret` here is the GITHUB_WEBHOOK_SECRET the server uses, NOT
    # the JWT secret. We pass both into main() so the server reads the
    # same one we sign with here.
    sig = "sha256=" + hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    try:
        r = httpx.post(
            f"{BASE}/api/webhooks/github",
            content=body,
            headers={
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "ws-check-delivery-001",
                "X-Hub-Signature-256": sig,
                "Content-Type": "application/json",
            },
            timeout=WEBHOOK_TIMEOUT,
        )
    except Exception as e:
        return False, f"HTTP error: {type(e).__name__}: {e}"
    if r.status_code != 200:
        return False, f"webhook returned {r.status_code}: {r.text[:200]}"
    body = r.json()
    if body.get("status") != "queued":
        return False, f"unexpected webhook response: {body}"
    return True, f"webhook queued PR #{body.get('pr_number')} (delivery {body.get('delivery_id')})"


def check_webhook_missing_secret() -> tuple[bool, str]:
    """When GITHUB_WEBHOOK_SECRET is unset, the endpoint should return 503."""
    # We can't unset the env from the child (it inherits ours), so we just
    # verify that the running server *would* 503 — instead we check the
    # error response when an obviously wrong signature is sent.
    body = json.dumps({"action": "opened"}).encode("utf-8")
    r = httpx.post(
        f"{BASE}/api/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "ping",
            "X-GitHub-Delivery": "ws-check-bad-sig",
            "X-Hub-Signature-256": "sha256=deadbeef",
            "Content-Type": "application/json",
        },
        timeout=WEBHOOK_TIMEOUT,
    )
    if r.status_code != 401:
        return False, f"expected 401 on bad signature, got {r.status_code}"
    return True, "bad signature correctly rejected with 401"


def main() -> int:
    # Ensure the dev AUTH_SECRET is set so the server uses HS256 in mock mode
    secret = "test-only-jwt-secret-32chars-long-enough-for-hs256"
    os.environ.setdefault("AUTH_SECRET", secret)
    # Empty keys so the LLM router falls back to mock mode
    os.environ.setdefault("ANTHROPIC_API_KEY", "")
    os.environ.setdefault("OPENAI_API_KEY", "")
    # Set GitHub webhook secret so the endpoint returns 200 (not 503)
    os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "ws-check-webhook-secret")

    # Pre-flight: kill any process still holding the port from a previous
    # run. Without this, the new subprocess fails to bind, but our TCP
    # poll happily reports "port open" (the orphan holds it) and we waste
    # time talking to a stale server with the wrong env.
    kill_port_holder(PORT)
    time.sleep(0.3)

    log("starting uvicorn in subprocess...")
    env = os.environ.copy()
    log(f"subprocess env: GITHUB_WEBHOOK_SECRET={env.get('GITHUB_WEBHOOK_SECRET', 'MISSING')!r}")
    proc = subprocess.Popen(
        [
            sys.executable, "-c",
            "import os, sys; "
            "from agent_system.api.server import app; "
            "import uvicorn; uvicorn.run(app, host='" + HOST + "', port=" + str(PORT) + ", log_level='warning')",
        ],
        env=env,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        if not wait_for_port(HOST, PORT, STARTUP_TIMEOUT):
            log(f"ERROR: port {PORT} never opened within {STARTUP_TIMEOUT}s")
            return 1
        log(f"port {PORT} is open")
        if not wait_for_health(STARTUP_TIMEOUT):
            log(f"ERROR: /api/health never returned 200 within {STARTUP_TIMEOUT}s")
            return 1
        log("/api/health is 200 OK")

        token = mint_token()
        log(f"minted JWT (truncated to {token[:30]}...)")

        log("--- 1/2: WebSocket LLM streaming")
        ok, msg = asyncio.run(check_llm_stream(token))
        log(f"  {'PASS' if ok else 'FAIL'}: {msg}")
        ws_ok = ok

        log("--- 2/2: GitHub App webhook")
        webhook_secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
        ok, msg = check_github_webhook(webhook_secret)
        log(f"  {'PASS' if ok else 'FAIL'}: {msg}")
        gh_ok = ok

        log("--- bonus: webhook rejects bad signature with 401")
        ok, msg = check_webhook_missing_secret()
        log(f"  {'PASS' if ok else 'FAIL'}: {msg}")
        bad_sig_ok = ok

        all_ok = ws_ok and gh_ok and bad_sig_ok
        log(f"summary: {'ALL PASS' if all_ok else 'FAILURES'}")
        return 0 if all_ok else 1
    finally:
        log("stopping uvicorn...")
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        except Exception as e:
            log(f"warning: failed to stop uvicorn cleanly: {e}")
        # Dump captured output for debugging if it failed
        try:
            out, _ = proc.communicate(timeout=2)
            log("--- subprocess output (all) ---")
            for line in (out or b"").decode("utf-8", errors="replace").splitlines()[-30:]:
                log(f"  {line}")
        except Exception:
            pass
        log(f"uvicorn exited with code {proc.returncode}")


if __name__ == "__main__":
    sys.exit(main())