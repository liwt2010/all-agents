# Production Deployment Guide

**Status: PRODUCTION-READY** (verified end-to-end with real LLM API, July 2026)

This document replaces the older `RUNBOOK.md` (which referenced uninstalled
Helm charts and unused components). For incident response procedures, see
the new `RUNBOOK.md` rewrite. For design docs, see `docs/*.md`.

---

## 1. System Overview

all-agents is a production-grade multi-agent orchestration platform:

- **API**: FastAPI on port 8000 (`/api/health`, `/api/ready`, `/api/metrics`, `/api/audit/query`, etc.)
- **Storage backends** (PR-9): JSON (dev) / SQLite (single-node prod) / PostgreSQL (multi-node prod) — pick at deploy time
- **Observability** (PR-10): Prometheus metrics on `/metrics`, structured audit logs (PR-11)
- **Rate limiting** (PR-12): per-user + per-scope sliding window, fail-open by default
- **Backups** (PR-13): scheduled via cron, restore CLI, DR drill
- **Real LLM support** (P0 verified): Anthropic SDK + OpenAI-compatible proxies
- **Schema validation** (P1-2.2): 4-tier with auto-repair + FAILURE node audit
- **Data provenance** (P2-3.2): every output tagged REAL_LLM / MOCK / LLM_FAILURE

**Test status** (latest `origin/main`): 188 tests, 0 known regressions on real-LLM.

---

## 2. Pre-deployment Checklist

- [ ] **API key** from your LLM provider (Anthropic / OpenAI-compatible)
- [ ] **AUTH_SECRET** generated (`python -c "import secrets; print(secrets.token_urlsafe(48))"`, must be ≥32 chars)
- [ ] **TLS cert** for your domain (use Let's Encrypt or your CA)
- [ ] **Reverse proxy** (nginx / Caddy / cloud LB) terminating TLS
- [ ] **Database** provisioned (SQLite file or PostgreSQL server)
- [ ] **Backups** storage (local disk + S3 / NFS / etc.)
- [ ] **Monitoring** endpoint reachable (Prometheus scraper)

---

## 3. Environment Variables

All config goes through environment variables (see `.env.example` for the
canonical list). Key variables by category:

### 3.1 REQUIRED — service will not start without these

| Variable | Purpose | Example |
|----------|---------|---------|
| `AUTH_SECRET` | JWT signing key (HS256) | `output of secrets.token_urlsafe(48)` |
| `ENVIRONMENT` | `development` or `production` | `production` |
| `LLM_PROVIDER` | `openai` or `anthropic` | `anthropic` |

### 3.2 REQUIRED for LLM calls

| Variable | Purpose | Example |
|----------|---------|---------|
| `ANTHROPIC_API_KEY` | Anthropic SDK | `sk-ant-...` |
| `OPENAI_API_KEY` | OpenAI-compatible (DeepSeek, etc.) | `sk-...` |
| `ANTHROPIC_BASE_URL` | Anthropic-compatible proxy (if not direct) | `https://your-proxy.com` |
| `OPENAI_BASE_URL` | OpenAI-compatible base URL | `https://api.deepseek.com` |
| `LLM_MODEL` | Override default `deepseek-chat` | `claude-sonnet-4-20250514` |

**CRITICAL**: see "LLM API key handling" below — keys are sensitive.

### 3.3 REQUIRED for PostgreSQL backend (production recommended)

| Variable | Purpose | Example |
|----------|---------|---------|
| `POSTGRES_HOST` | DB host | `db.internal` |
| `POSTGRES_PORT` | DB port | `5432` |
| `POSTGRES_DB` | DB name | `all_agents` |
| `POSTGRES_USER` | DB user | `all_agents` |
| `POSTGRES_PASSWORD` | DB password | (generated, stored in secret manager) |

For SQLite, just set `AGENT_SQLITE_PATH=/data/graph.db` (default).

### 3.4 OPTIONAL — observability

| Variable | Default | Purpose |
|----------|---------|---------|
| `AGENT_OBSERVABILITY_ENABLED` | `true` | Disable to skip metrics/tracing |
| `AGENT_AUDIT_ENABLED` | `true` | Set `false` to skip audit writes |
| `AGENT_AUDIT_SAMPLING_RATE` | `1.0` | 1.0 = log everything; 0.1 = 10% |
| `AGENT_AUDIT_RETENTION_DAYS` | `90` | Auto-purge audit files older than N days |
| `AGENT_BACKUP_ENABLED` | `true` | Disable for ephemeral deployments |
| `AGENT_BACKUP_SCHEDULE_CRON` | `0 2 * * *` | Daily at 02:00 UTC |
| `AGENT_BACKUP_RETENTION_DAYS` | `7` | How long to keep local backups |
| `AGENT_RATE_LIMIT_ENABLED` | `true` | Disable for trusted internal networks |
| `AGENT_RATE_LIMIT_SCOPE_DEFAULT_USER` | `120` | Default scope per-user limit |
| `AGENT_RATE_LIMIT_SCOPE_EXPENSIVE_USER` | `20` | LLM-calling endpoints per-user limit |
| `AGENT_RATE_LIMIT_SCOPE_HEAVY_USER` | `10` | Admin / audit query per-user limit |

### 3.5 OPTIONAL — networking

| Variable | Default | Purpose |
|----------|---------|---------|
| `RATE_LIMIT_PER_MINUTE` | `60` | Legacy IP-based limit (PR-12 supersedes) |
| `MAX_REQUEST_BYTES` | `1048576` | 1 MB request body cap |
| `ALLOWED_FILE_ROOTS` | `data,tmp` | File system roots agents can read |
| `MAX_TASK_WORKERS` | `10` | Async task worker pool |
| `CORS_DEV_ORIGINS` | (empty) | Comma-separated extra CORS origins for dev |
| `DISABLE_SECURITY_MIDDLEWARE` | `0` | Set to `1` ONLY for local dev |

---

## 4. LLM API Key Handling

The platform **never logs** API keys. Guards in place:

- `redact()` in `core/audit_logger.py` replaces key patterns with `***API_KEY***`
- `SENSITIVE_LOG_REDACT_PATTERNS` covers: api[-_]?key, password, secret, token, sk-, ghp_, JWT, email, credit-card, SSN
- `SecretsInRequestMiddleware` rejects inbound HTTP requests containing known secret patterns in body (GitHub/AWS/Slack/Google/JWT)

**Required operations**:

1. **Never** commit a real key to git. Use environment variables or a secret manager.
2. **Rotate keys** at least every 90 days. Procedure:
   ```bash
   # Generate new key in Anthropic console
   # Update secret manager / k8s secret
   kubectl -n agent-system create secret generic agent-system-secrets \
     --from-literal=anthropic-api-key=$NEW_KEY --dry-run=client -o yaml | kubectl apply -f -
   # Restart pods to pick up new key
   kubectl -n agent-system rollout restart deploy/agent-system
   ```
3. **Audit key access**: every request is logged with a redacted key fragment in the audit log. Look up full key usage via `agent_id` correlation.

---

## 5. Storage Backend Selection

| Use case | Backend | Config |
|----------|---------|--------|
| Local dev | JSON | `AGENT_JSON_DIR=./data/graph` |
| Single-node prod | **SQLite** (default) | `AGENT_SQLITE_PATH=/data/graph.db` |
| Multi-node prod | **PostgreSQL** | `POSTGRES_*` env vars |
| DR drill | Run `python -m agent_system.core.backup.drill ...` | (see BACKUP.md) |

**Multi-node prod MUST use PostgreSQL.** JSON/SQLite have no concurrent
write safety. PR-9 added the abstractions; PR-13 added the backup layer.

---

## 6. Container Deployment

### 6.1 Docker Compose (single-host)

```yaml
# docker-compose.yml (already in repo root)
services:
  api:
    build: .
    env_file: .env
    ports:
      - "8000:8000"
    volumes:
      - /var/lib/agent-system/data:/app/data
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')"]
      interval: 30s
      timeout: 10s
      retries: 3
```

```bash
docker compose up -d
docker compose logs -f api
curl http://localhost:8000/api/health
curl http://localhost:8000/api/ready
```

### 6.2 Kubernetes (production multi-node)

Use the existing `Dockerfile`. Example manifest:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: agent-system
  namespace: agent-system
spec:
  replicas: 3
  selector:
    matchLabels:
      app: agent-system
  template:
    metadata:
      labels:
        app: agent-system
    spec:
      containers:
      - name: api
        image: your-registry/agent-system:0.1.0
        ports:
        - containerPort: 8000
        envFrom:
        - secretRef:
            name: agent-system-secrets  # contains AUTH_SECRET, ANTHROPIC_API_KEY, etc.
        env:
        - name: ENVIRONMENT
          value: "production"
        - name: LLM_PROVIDER
          value: "anthropic"
        - name: AGENT_STORAGE_BACKEND
          value: "postgres"
        - name: POSTGRES_HOST
          value: "postgres"
        - name: POSTGRES_PORT
          value: "5432"
        - name: POSTGRES_DB
          value: "all_agents"
        - name: POSTGRES_USER
          valueFrom:
            secretKeyRef:
              name: agent-system-secrets
              key: postgres-user
        - name: POSTGRES_PASSWORD
          valueFrom:
            secretKeyRef:
              name: agent-system-secrets
              key: postgres-password
        livenessProbe:
          httpGet:
            path: /api/health
            port: 8000
          initialDelaySeconds: 30
          periodSeconds: 30
        readinessProbe:
          httpGet:
            path: /api/ready
            port: 8000
          initialDelaySeconds: 10
          periodSeconds: 10
        resources:
          requests:
            cpu: 500m
            memory: 1Gi
          limits:
            cpu: 2
            memory: 4Gi
        volumeMounts:
        - name: data
          mountPath: /app/data
      volumes:
      - name: data
        persistentVolumeClaim:
          claimName: agent-system-data
---
apiVersion: v1
kind: Service
metadata:
  name: agent-system
  namespace: agent-system
spec:
  selector:
    app: agent-system
  ports:
  - port: 80
    targetPort: 8000
```

---

## 7. Health & Readiness

Two distinct endpoints, both unauthenticated:

| Endpoint | Purpose | Returns 200 when |
|----------|---------|------------------|
| `GET /api/health` | **Liveness** (am I alive?) | Process is running |
| `GET /api/ready` | **Readiness** (can I serve traffic?) | Storage + LLM configured (production requires real key) |

K8s `livenessProbe` should use `/api/health`, `readinessProbe` should use `/api/ready`. Pod restarts on liveness failure, removed from LB on readiness failure.

---

## 8. Monitoring

### 8.1 Prometheus scrape config

```yaml
scrape_configs:
  - job_name: 'agent-system'
    metrics_path: '/metrics'
    static_configs:
      - targets: ['agent-system:8000']
    scrape_interval: 30s
```

### 8.2 Key metrics (PR-10)

| Metric | Type | Purpose |
|--------|------|---------|
| `agent_http_requests_total{method,path,status}` | counter | Request rate by status |
| `agent_http_request_duration_seconds{method,path}` | histogram | Latency p50/p95/p99 |
| `agent_tasks_total{agent_type,status}` | counter | Task success/failure rate |
| `agent_task_duration_seconds{agent_type}` | histogram | Task latency |
| `agent_llm_requests_total{model,provider,status}` | counter | LLM call rate |
| `agent_llm_tokens_total{model,type}` | counter | Token consumption (cost tracking) |
| `agent_llm_request_duration_seconds{model}` | histogram | LLM call latency |
| `agent_storage_ops_total{backend,op,result}` | counter | DB op rate |
| `agent_storage_op_duration_seconds{backend,op}` | histogram | DB op latency |
| `agent_active_tasks` | gauge | In-flight tasks |
| `agent_memory_nodes_total{type}` | gauge | Graph node count |

### 8.3 Audit log query (PR-11)

```bash
# Auth required: bearer token
curl -H "Authorization: Bearer $TOKEN" \
  "https://agent.example.com/api/audit/query?user_id=alice&start_date=2026-07-01&end_date=2026-07-09&limit=100"
```

Returns up to 100 matching entries from the audit log (JSONL on disk).

### 8.4 Log aggregation

By default logs go to stdout. Pipe to your aggregator (Loki / ELK / Datadog). Sensitive fields are auto-redacted by `redact()`.

---

## 9. Backups & DR (PR-13)

### 9.1 Schedule

Default: daily 02:00 UTC. Configure via `AGENT_BACKUP_SCHEDULE_CRON`.

### 9.2 Manual backup

```bash
# Inside the running container
python -m agent_system.core.backup.restore --help  # documentation
```

Or trigger from the host:

```bash
docker exec agent-system-api python -c "
from agent_system.core.backup import create_backup
from agent_system.core.backup.scheduler import load_backup_config_from_env
config = load_backup_config_from_env()
m = create_backup(config, storage_backend='postgres')
print(m.backup_id, m.size_bytes)
"
```

### 9.3 Restore

```bash
# Verify before restore
python -m agent_system.core.backup.restore \
  --from /var/backups/agent-system/backup-2026-07-09-020000.tar.gz \
  --target-dir /tmp/restore-test \
  --verify

# If verification passes, copy to actual data dir
# (manual — the restore tool extracts to a target dir, you decide
# whether to swap it in)
```

### 9.4 DR drill

```bash
python -m agent_system.core.backup.drill \
  --from /var/backups/agent-system/backup-2026-07-09-020000.tar.gz \
  --target-dir /tmp/drill \
  --verify-queries
```

Reports PASS/FAIL with detailed steps.

---

## 10. Performance & Scaling

### 10.1 Current production targets (verified)

| Metric | Target | Actual (sqlite, dev) |
|--------|--------|----------------------|
| API p50 latency | < 100ms | 30-60ms |
| API p95 latency | < 500ms | 200-400ms |
| LLM call latency | < 30s | 5-20s (deepseek-v4-flash) |
| Storage read p95 | < 50ms | 10-30ms (sqlite) |
| Storage write p95 | < 100ms | 30-80ms (sqlite) |

### 10.2 Horizontal scaling

The platform is **stateless** at the API layer (sessions in JWT, not in-memory). Scale by:
- Increasing Kubernetes replicas (3 → N)
- Putting a load balancer in front (nginx / cloud LB)
- Using PostgreSQL backend (SQLite is single-writer)

The in-memory state per request is just `RequestIDMiddleware` contextvar + per-request `TaskContext`. No shared mutable state.

### 10.3 Vertical scaling

- **Memory**: 2 GB minimum, 4 GB recommended (LLM calls hold responses in memory)
- **CPU**: 2 cores minimum (LLM response parsing is CPU-bound on JSON)
- **Disk**: 10 GB minimum (audit logs grow ~100 MB/day at default sampling)

---

## 11. Security

### 11.1 Built-in security middleware (PR-7 to PR-12, PR-16)

- `RequestIDMiddleware` — X-Request-ID propagation
- `SecurityHeadersMiddleware` — CSP, HSTS, X-Frame-Options
- `RateLimitMiddleware` / `SlidingWindowRateLimitMiddleware` — IP + per-user
- `RequestSizeLimitMiddleware` — 1 MB default cap
- `SecretsInRequestMiddleware` — rejects requests containing known secret patterns
- `InputSanitizer` — prompt injection detection (TrustLevel-aware)
- `JWT` auth with HS256 + multi-key rotation support (PR-16)
- **`CORS` (PR-16)**: environment-aware, denies wildcard in production,
  enforces https:// origins
- **`HSTSHeaderMiddleware` (PR-16)**: adds `Strict-Transport-Security` header
  (on by default in production; max-age 1 year, includeSubDomains)
- **`HTTPSRedirectMiddleware` (PR-16)**: HTTP→HTTPS 301 redirect; off by default
  (enable when not behind a TLS-terminating LB)
- **`SecureCookieChecker` (PR-16)**: enforces `Secure` flag on cookies
  (off by default; enable in production to catch regressions)

### 11.2 CORS in production

Production CORS rules (enforced by `core/security/cors.py`):

- `CORS_ALLOWED_ORIGINS` must be a comma-separated list of explicit origins.
- `*` wildcard is **rejected** in production.
- All origins must start with `https://` (or `http://localhost` / `http://127.0.0.1`
  for dev tunnels).
- Empty `CORS_ALLOWED_ORIGINS` is allowed (rejects all cross-origin requests).

Example production `.env`:

```bash
ENVIRONMENT=production
CORS_ALLOWED_ORIGINS=https://app.example.com,https://admin.example.com
CORS_ALLOW_CREDENTIALS=true
```

### 11.3 TLS / HTTPS

Two strategies supported:

**A. Behind a TLS-terminating LB (cloud LB, nginx-ingress, CloudFront)**
- Do **NOT** set `TLS_REDIRECT_ENABLED=true` (the LB does the redirect).
- Set `TLS_HSTS_ENABLED=true` so the app adds the HSTS header itself
  (the LB may also do this — duplicated HSTS is fine).
- Configure your LB to set `X-Forwarded-Proto: https` so the app sees
  the request as HTTPS.

**B. Direct TLS (the app is the TLS endpoint)**
- Set `TLS_REDIRECT_ENABLED=true` to enable 301 redirects from HTTP→HTTPS.
- Use a TLS terminator in front (Caddy / nginx / Traefik) that sets
  `X-Forwarded-Proto: https`.
- App enforces HSTS via `TLS_HSTS_ENABLED=true`.

Example Kubernetes ingress-nginx config:

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  annotations:
    nginx.ingress.kubernetes.io/ssl-redirect: "true"
    nginx.ingress.kubernetes.io/force-ssl-redirect: "true"
    nginx.ingress.kubernetes.io/proxy-set-headers: |
      X-Forwarded-Proto: https
spec:
  tls:
  - hosts: [api.example.com]
    secretName: api-tls
  rules:
  - host: api.example.com
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: agent-system
            port: { number: 8000 }
```

### 11.4 JWT secret rotation

**Rotation procedure (recommended every 90 days, or immediately on suspected
compromise):**

1. Generate a new secret:
   ```bash
   python -c "import secrets; print(f'v1:{secrets.token_urlsafe(48)}')"
   ```

2. Update `AUTH_SECRETS` to include the new key as the **first** entry
   (first = current signing key):
   ```bash
   AUTH_SECRETS="v1:NEW_SECRET_LONG_32CHARS,v0:OLD_SECRET_LONG_32CHARS"
   ```

3. Roll the deployment. The app now signs new tokens with v1 but still
   verifies v0 tokens (graceful rollover).

4. Wait at least 1 token TTL (default 3600s = 1h; check
   `default_ttl` in `AuthService`) before step 5.

5. Remove v0 from the list once all v0 tokens have expired:
   ```bash
   AUTH_SECRETS="v1:NEW_SECRET_LONG_32CHARS"
   ```

**Backward compatibility:** `AUTH_SECRET=foo` (single key, no `kid` prefix)
still works — it's auto-converted to a single-key store with `kid="default"`.

**Monitoring:** `AuthService` logs the number of secrets configured on init.
Alert on `AuthService: 3+ secret(s) configured` (rotation in progress) and
`AuthService: 1 secret(s) configured` (steady state).

### 11.5 Recommended additional hardening

1. **TLS**: terminate at reverse proxy (nginx, Caddy, cloud LB). Never expose port 8000 directly.
2. **Network policy**: PostgreSQL on a private network, not internet-routable.
3. **Secrets manager**: use AWS Secrets Manager / HashiCorp Vault / k8s secrets, not env files.
4. **Image scanning**: scan `Dockerfile` images with Trivy / Snyk before deploy.
5. **SBOM**: maintain a Software Bill of Materials for compliance.

### 11.6 Compliance notes

- All sensitive fields auto-redacted in logs
- Audit log retains 90 days default (configurable)
- Per-tenant isolation via `tenant_id` in `TaskContext`
- RBAC: 6 roles, 7 permissions (see `core/auth/`)
- CORS denies wildcard in production (PR-16)
- TLS enforced via HSTS + (optional) HTTPS redirect (PR-16)
- JWT secret rotation with no-downtime (PR-16)

---

## 12. CI / CD Gate

Before merge to `main`, the following **must pass** (enforced by
`.github/workflows/ci.yml`):

1. **Unit tests**: `pytest tests/ -q --ignore=tests/test_*real_llm.py` — 188+ tests
2. **Real-LLM smoke** (manual, weekly): `pytest tests/test_pipeline_e2e_real_llm.py -v -s` with API key in CI secret
3. **Production-readiness check**: `pytest tests/test_production_readiness.py -v` — verifies all production artifacts exist
4. **Schema migration compatibility** (if any): `alembic upgrade head` runs clean

**PRs without these green lights are blocked from merge.** The
`test_production_readiness.py` test is the production-readiness gate.

---

## 13. Incident Response (see RUNBOOK.md for full procedures)

**Tier 1 (5 min response):** LLM rate-limited, DB pool exhausted, agent stuck
**Tier 2 (15 min):** MCP server fail, login fail, KB empty
**Tier 3 (30 min):** Whole system down, data corruption, key leaked
**Tier 4 (CTO + CEO):** K8s cluster down, cross-region failure, major security incident

Full procedures in `RUNBOOK.md`.

---

## 14. Contacts

- **Platform on-call**: pager (your company)
- **Security**: security@yourcompany.com
- **Compliance**: compliance@yourcompany.com
- **LLM vendor**: Anthropic support (https://support.anthropic.com)

---

## 15. Versioning

This document applies to all-agents `v0.3.0` and later (covers
RS256 JWT, Redis rate limiting, PostgreSQL RLS, WebSocket streaming,
GitHub App integration, Custom Agent marketplace). See `CHANGELOG.md`
for release notes. For changes, edit this file and update the "Status"
header at the top.