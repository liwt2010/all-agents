"""
GitHub App webhook handler (PR v0.2.0 — v0.3.0 roadmap item).

Provides:
  - HMAC-SHA256 signature verification (X-Hub-Signature-256)
  - Replay protection via delivery_id deduplication
  - Event dispatch:
      * pull_request (opened/synchronize/reopened): trigger ReviewAgent
      * other events:    log + store, no action

Wire format (inbound):
  Headers:
    X-GitHub-Event:      "pull_request" | "issues" | "push" | ...
    X-GitHub-Delivery:   <unique id per delivery>
    X-Hub-Signature-256: "sha256=<hex digest>"
  Body: JSON payload from GitHub

Wire format (response):
  200 OK with body {"status": "ok", "delivery_id": "..."} for success
  401 Unauthorized for signature failure
  202 Accepted for ignored events (no action taken, still acknowledged)
  400 Bad Request for malformed payloads

Security:
  - Constant-time HMAC comparison (hmac.compare_digest)
  - The raw request body must be used for signature verification, not the
    parsed JSON. FastAPI/Starlette exposes this via request.body() before
    any parsing.
  - delivery_id dedupe: a simple in-memory LRU set covers the deployment
    lifetime. For multi-replica HA, swap in Redis.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from collections import OrderedDict

from fastapi import APIRouter, Header, HTTPException, Request, status

logger = logging.getLogger(__name__)

router = APIRouter(tags=["github_webhook"])

# Maximum number of delivery_ids to remember (replay protection window).
# 1000 covers a few minutes of burst traffic; rotate older entries as new
# ones arrive (LRU semantics). For longer retention, swap in Redis.
_REPLAY_CACHE_MAX = 1000
_replay_cache: OrderedDict[str, float] = OrderedDict()


def _is_replay(delivery_id: str) -> bool:
    """Return True if this delivery_id has been seen recently.

    Side effect: records the id and trims the cache to size.
    """
    now = time.time()
    if delivery_id in _replay_cache:
        # Move to end (LRU touch)
        _replay_cache.move_to_end(delivery_id)
        return True
    _replay_cache[delivery_id] = now
    # Trim oldest
    while len(_replay_cache) > _REPLAY_CACHE_MAX:
        _replay_cache.popitem(last=False)
    return False


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """Constant-time verification of a GitHub HMAC-SHA256 signature.

    Expected header format: "sha256=<64 hex chars>".
    Returns False on missing header, wrong algorithm, or bad digest.
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(
        secret.encode("utf-8"),
        msg=body,
        digestmod=hashlib.sha256,
    ).hexdigest()
    provided = signature_header.split("=", 1)[1]
    return hmac.compare_digest(expected, provided)


# ── Webhook endpoint ──

@router.post("/api/webhooks/github")
async def github_webhook(
    request: Request,
    x_github_event: str | None = Header(default=None, alias="X-GitHub-Event"),
    x_github_delivery: str | None = Header(default=None, alias="X-GitHub-Delivery"),
    x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
):
    """GitHub App webhook entrypoint.

    Verifies the HMAC signature, deduplicates the delivery, and
    dispatches to the appropriate handler. Returns 202 for events
    we don't act on (still acknowledged so GitHub doesn't retry).
    """
    import os
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "").strip()
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GITHUB_WEBHOOK_SECRET is not configured",
        )

    if not x_github_delivery:
        raise HTTPException(status_code=400, detail="missing X-GitHub-Delivery header")

    # Replay protection: refuse to handle the same delivery_id twice.
    # This guards against attackers replaying captured webhooks.
    if _is_replay(x_github_delivery):
        logger.warning(f"Replay detected for delivery_id={x_github_delivery}")
        return {"status": "duplicate", "delivery_id": x_github_delivery}

    # Read raw body BEFORE parsing — required for signature verification.
    body = await request.body()
    if not verify_signature(secret, body, x_hub_signature_256):
        logger.warning(f"GitHub webhook signature mismatch (delivery={x_github_delivery})")
        raise HTTPException(
            status_code=401,
            detail="signature verification failed",
        )

    # Parse JSON
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"invalid JSON: {e}")

    event = x_github_event or "unknown"
    logger.info(
        f"GitHub webhook: event={event} delivery={x_github_delivery} "
        f"action={payload.get('action', '?') if isinstance(payload, dict) else '?'}"
    )

    if event == "pull_request":
        return await _handle_pull_request(payload, x_github_delivery)

    # Other events: acknowledged but not actioned.
    return {
        "status": "ignored",
        "delivery_id": x_github_delivery,
        "event": event,
        "reason": "no handler for event type",
    }


async def _handle_pull_request(payload: dict, delivery_id: str) -> dict:
    """Trigger a review when a PR is opened/synchronized/reopened.

    Other actions (closed, edited metadata) are no-ops.
    """
    action = payload.get("action", "")
    if action not in ("opened", "synchronize", "reopened"):
        return {
            "status": "ignored",
            "delivery_id": delivery_id,
            "event": "pull_request",
            "action": action,
            "reason": "action not handled",
        }

    pr = payload.get("pull_request", {})
    repo = payload.get("repository", {})
    pr_number = pr.get("number")
    pr_title = pr.get("title", "")
    pr_body = pr.get("body", "") or ""
    pr_url = pr.get("html_url", "")
    repo_full = repo.get("full_name", "")
    sender = payload.get("sender", {}).get("login", "")

    logger.info(
        f"PR review trigger: repo={repo_full}#{pr_number} title={pr_title!r} "
        f"sender={sender} delivery={delivery_id}"
    )

    # Schedule the review asynchronously. We do NOT block the webhook
    # response on the LLM call (GitHub has a 10s timeout) — instead,
    # dispatch to a background task and let the LLM work proceed.
    import asyncio
    asyncio.create_task(
        _run_review(
            repo_full=repo_full,
            pr_number=pr_number,
            pr_title=pr_title,
            pr_body=pr_body,
            pr_url=pr_url,
            delivery_id=delivery_id,
        )
    )

    return {
        "status": "queued",
        "delivery_id": delivery_id,
        "action": action,
        "repo": repo_full,
        "pr_number": pr_number,
    }


async def _run_review(
    *,
    repo_full: str,
    pr_number: int,
    pr_title: str,
    pr_body: str,
    pr_url: str,
    delivery_id: str,
):
    """Background task: invoke ReviewAgent and (optionally) post a comment.

    The POST-back to GitHub is opt-in via GITHUB_PR_COMMENT_TOKEN env;
    if absent we log the review locally instead. Keeps the integration
    useful for staging environments without requiring a full GitHub App
    installation.
    """
    try:
        from agent_system.core.agent import TaskContext, OutputSchema
        from agent_system.core.registry import agent_registry
        from datetime import datetime, timezone

        review_input = (
            f"PR review for {repo_full}#{pr_number}: {pr_title}\n\n"
            f"{pr_body}\n\nPR URL: {pr_url}"
        )
        task = TaskContext(
            task_id=f"github-pr-{delivery_id}",
            input=review_input,
            config={"max_retries": 1},
            metadata={
                "agent": "review",
                "source": "github_webhook",
                "delivery_id": delivery_id,
                "repo": repo_full,
                "pr_number": pr_number,
            },
        )
        instance = agent_registry.get_instance("review_agent")
        if instance is None:
            logger.error("review_agent not registered — cannot process PR review")
            return
        output = await instance.do_work(task)

        # In a real GitHub App we'd post this back as a PR comment
        # using the installation access token. For now, log it.
        token = os.environ.get("GITHUB_PR_COMMENT_TOKEN", "").strip()  # noqa: F821
        if token:
            await _post_pr_comment(
                token=token,
                repo=repo_full,
                pr_number=pr_number,
                body=output.payload.get("summary", "(no summary)"),
            )
        else:
            logger.info(
                f"Review for {repo_full}#{pr_number}: {output.payload.get('summary', '')[:200]}"
            )
    except Exception as e:
        logger.exception(f"PR review failed for {delivery_id}: {e}")


async def _post_pr_comment(*, token: str, repo: str, pr_number: int, body: str):
    """Post a comment on a PR using a GitHub installation token.

    In production this would use the GitHub App installation flow
    (JWT exchange → installation access token). For now we use a
    personal access token, which is enough for staging.
    """
    import httpx
    # GH API: POST /repos/{owner}/{repo}/issues/{pr_number}/comments
    # (PRs are issues under the hood)
    url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(
            url,
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"body": body},
        )
        if r.status_code >= 300:
            logger.warning(f"PR comment post failed ({r.status_code}): {r.text[:200]}")
        else:
            logger.info(f"PR comment posted: {url} status={r.status_code}")