# Agent System — Incident Response Runbook

> **Note**: The deployment / install / config procedures are in
> **[docs/PRODUCTION.md](PRODUCTION.md)** (replaces the older Helm-based
> instructions in this file). This runbook covers **incident response
> only** — what to do when things break.

## Overview

The Agent System is a multi-tenant enterprise platform with 9 built-in
agents (Product, Tech, Test, Deploy, CEO, Security, Docs, Review, DevOps)
plus a Custom Agent marketplace. It uses FastAPI + Pydantic v2 +
SQLAlchemy 2.0, with optional Postgres / Redis backends. The default
deployment is container-based (Docker / k8s), see PRODUCTION.md for
installation.

## RTO / RPO targets

- RTO: 4 hours (recovery time objective)
- RPO: 1 hour (max data loss — enforced by PR-13 backup schedule)

## Top 20 incidents

### Tier 1 — most common

| # | Incident | 1-minute stop the bleeding | Root-cause |
|---|----------|---------------------------|-----------|
| 1 | LLM API rate-limited | Switch to fallback model | Check LLM provider console |
| 2 | DB connection exhausted | Restart connection pool | Check slow queries |
| 3 | Storage backend down | Restart container | Check disk + logs |
| 4 | Agent keeps failing | Pause that agent | Check reflection system |
| 5 | Task stuck | Cancel task | Check trace |

### Tier 2 — common

| # | Incident | 1-minute stop the bleeding | Root-cause |
|---|----------|---------------------------|-----------|
| 6 | MCP server fails | Disable that MCP | Check logs |
| 7 | Login fails | Check AUTH_SECRET | Check Auth logs |
| 8 | KB returns nothing | Rebuild index | Check storage status |
| 9 | Dashboard stale | Refresh page | Check WebSocket |
| 10 | Rate-limited legitimately | Increase per-user scope | Check `/metrics` |

### Tier 3 — serious

| # | Incident | 1-minute stop the bleeding | Root-cause |
|---|----------|---------------------------|-----------|
| 11 | Whole system down | Switch to backup env | Container status |
| 12 | Data corruption | Switch to last backup | Run data check |
| 13 | API key leaked | Rotate immediately | Check access log |
| 14 | Under attack | Cut off ingress | Start IR |
| 15 | Billing error | Pause charges | LLM provider logs |

### Tier 4 — disaster

| # | Incident | Action |
|---|----------|--------|
| 16 | Cluster down | Switch to backup cluster |
| 17 | DB primary+replica down | Restore from backup (PR-13) |
| 18 | Cross-region failure | Enable DR runbook |
| 19 | Compliance: pause service | Shut down immediately, legal |
| 20 | Major security incident | Disconnect, IR process |

## On-call escalation

- Tier 1: Platform engineer (5 min)
- Tier 2: Platform engineer + on-call lead (15 min)
- Tier 3: Engineering manager (30 min)
- Tier 4: CTO + CEO

## Diagnostic commands

### Check service health

```bash
# Liveness (process running)
curl https://agent.example.com/api/health

# Readiness (can serve traffic — checks LLM key + storage)
curl https://agent.example.com/api/ready
```

### Check metrics (Prometheus)

```bash
# Public scrape endpoint (no auth)
curl https://agent.example.com/metrics

# Auth-required metrics view
curl -H "Authorization: Bearer $TOKEN" https://agent.example.com/api/metrics
```

### Query audit log

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "https://agent.example.com/api/audit/query?start_date=2026-07-09&limit=50"
```

### List recent backups

```bash
ls -la /var/lib/agent-system/data/backup/
# Or remote:
aws s3 ls s3://your-bucket/agent-system-backups/
```

### Verify backup integrity (DR drill)

```bash
python -m agent_system.core.backup.drill \
  --from /var/backups/agent-system/backup-20260709-020000.tar.gz \
  --target-dir /tmp/drill \
  --verify-queries
```

### Container commands (docker compose)

```bash
docker compose ps
docker compose logs -f api
docker compose restart api
docker compose down && docker compose up -d
```

### Container commands (k8s)

```bash
kubectl -n agent-system get pods
kubectl -n agent-system logs -f -l app=agent-system
kubectl -n agent-system rollout restart deploy/agent-system
kubectl -n agent-system scale deploy agent-system --replicas=0  # emergency drain
```

## Maintenance

### Manual backup

```bash
docker exec agent-system-api python -c "
from agent_system.core.backup import create_backup
from agent_system.core.backup.scheduler import load_backup_config_from_env
config = load_backup_config_from_env()
m = create_backup(config, storage_backend='postgres')
print(m.backup_id, m.size_bytes)
"
```

### Restore from backup

See PRODUCTION.md §9.3 — manual verification before swapping data dir.

## ADR (Architectural Decision Records)

ADRs live in `docs/adr/`. Create a new one as `00XX-name.md` describing
context, decision, and consequences.

## Contacts

- Platform on-call: pager (your company)
- Security: security@yourcompany.com
- Compliance: compliance@yourcompany.com
- LLM vendor (Anthropic): support@anthropic.com
