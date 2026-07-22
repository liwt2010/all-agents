# ADR-0003: Self-host the GitHub webhook receiver

**Status**: Accepted
**Date**: 2026-07-22
**Deciders**: Platform team

## Context

When we register Agent System as a GitHub App, GitHub sends
webhooks (push, pull_request, issues, ...) to a URL we provide.
GitHub requires:

1. **HTTPS** with a valid certificate (no self-signed, no IP addresses)
2. **HMAC-SHA256 signature verification** via a shared secret
   configured in the GitHub App settings
3. **Fast 2xx response** — GitHub retries on timeout (10s default)
4. **Reliable delivery** — GitHub will retry 5x with backoff over
   ~24h

The webhook payload then triggers downstream work: in our case,
`pull_request` events invoke `ReviewAgent` and (optionally) post
a comment back via the GitHub REST API.

We had two main architectural choices:

- **A. Self-host the receiver** — our existing FastAPI server
  exposes `POST /api/webhooks/github` directly.
- **B. Forward via a third-party tunnel** — `smee.io`,
  `ngrok`, or a Cloudflare Tunnel sits in front of our server
  and provides HTTPS.

## Decision

**A. Self-host.** Production deployments terminate TLS at a
load balancer (nginx, ALB, Cloudflare) in front of the Agent
System service. The webhook URL is
`https://api.agentsystem.example.com/api/webhooks/github`.

We rely on the existing FastAPI server's middleware stack:
- `RequestSizeLimitMiddleware` caps payload size (GitHub sends
  ≤25 MB for `push` events with many commits).
- `RateLimitMiddleware` (sliding window) prevents a misbehaving
  GitHub App installation from monopolizing the API.
- `OTel FastAPIInstrumentor` automatically produces a span named
  `POST /api/webhooks/github` per delivery — useful for debugging
  delivery failures.
- Authentication is the HMAC signature, not a JWT — so we
  intentionally *skip* `AuthMiddleware` for this path (GitHub
  doesn't have an Agent System JWT).

Security choices baked in:

- `verify_signature()` uses `hmac.compare_digest` (constant-time)
  to prevent timing attacks.
- Replay protection via `X-GitHub-Delivery` cache (LRU 1000
  entries); duplicate deliveries return `{"status": "duplicate"}`.
- `GITHUB_WEBHOOK_SECRET` is required — endpoint returns 503
  if unset, so misconfiguration is loud, not silent.

## Alternatives considered

### Use a tunneling service (smee.io, ngrok, Cloudflare Tunnel)
Pros:
- No need for our own TLS endpoint (cheaper for staging).
- Works behind NAT / corporate firewalls without IT involvement.

Cons:
- Adds a third party to every webhook delivery — uptime,
  latency, and security depend on them.
- TLS termination happens at the tunnel, not at our edge;
  we lose visibility into real client IPs and cert chains.
- Free tiers have rate limits; paid tiers add real cost.
- Long-term production doesn't want a tunnel — we'd just
  configure a load balancer anyway.

We may still recommend a tunnel for **local development**:
GitHub App settings allow per-environment webhook URLs, so
`ngrok http 8000` gives a developer a working webhook without
deploying. Document this in the README; don't make it part
of the production architecture.

### Use a dedicated webhook receiver service (Svix, Hookdeck)
Pros:
- Replay protection, dead-letter queue, retries, monitoring
  all out of the box.
- Offloads reliability engineering.

Cons:
- Another SaaS dependency for a feature that has well-known
  implementation patterns (~150 lines of code in our case).
- Per-event pricing at scale.

### Use GitHub Actions as the webhook receiver
Pros:
- Already in the GitHub ecosystem.
- Easy to fan out to other Actions.

Cons:
- Forces a per-repo `.github/workflows/` file — Agent System
  becomes a per-repo install rather than a per-org service.
  That's a product model we don't want.

## Consequences

### Positive
- **Zero new dependencies.** No third-party service for
  production deployments.
- **End-to-end visibility.** Webhook deliveries show up in
  the same OTel traces as the rest of the system.
- **Self-contained for staging.** A developer with `ngrok`
  can run the full webhook flow locally without involving
  ops.
- **Cheap to test.** The 18 webhook tests run in <2s and
  don't require any external service.

### Negative
- **Operators must configure HTTPS termination.** Most
  deployments already have this; new users have a setup step.
- **Replay protection is in-memory.** Multi-replica deployments
  share state via Redis (we built the rate-limit Redis
  backend already; reusing it here is on the v0.3.x roadmap).
- **No DLQ.** If the LLM call fails after we've returned 200
  to GitHub, the review is silently lost. We log loudly but
  don't retry; GitHub's automatic retries won't help here
  because we'd handle the same delivery_id as a duplicate.
  Future work: persist `delivery_id → task_id` and offer a
  manual "replay" admin endpoint.

## References

- Source: `src/agent_system/api/routes/github_webhook.py`
- Tests: `tests/test_github_webhook.py` (18 tests)
- Documentation: `docs/PRODUCTION.md` (link TBD — add when
  GitHub App setup guide is written)