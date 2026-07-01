# Agent System — Production Runbook

## Overview

The Agent System is a multi-tenant enterprise platform with 8 agents
(Product, Tech, Test, Deploy, DevOps, Security, Docs, Review) plus
CEO. It uses FastAPI + React + LangGraph, with optional Postgres /
Redis backends.

## Deployment

### Helm install (production)

```bash
# 1. Create namespace
kubectl create namespace agent-system

# 2. Create secret for the Anthropic API key
kubectl -n agent-system create secret generic agent-system-secrets \
  --from-literal=anthropic-api-key=$ANTHROPIC_API_KEY

# 3. Install chart
helm install agent-system ./deploy/helm \
  --namespace agent-system \
  --set image.tag=0.1.0 \
  --set ingress.hosts[0].host=agent.example.com
```

### Verify deployment

```bash
kubectl -n agent-system get pods
kubectl -n agent-system get svc
kubectl -n agent-system get ingress
```

### Smoke test

```bash
curl https://agent.example.com/api/health
# {"status":"ok","version":"0.1.0","uptime":...}
```

## RTO / RPO targets

- RTO: 4 hours (recovery time objective)
- RPO: 1 hour (max data loss)

## Top 20 incidents (per PLATFORM §19.3)

### Tier 1 — most common

| # | Incident | 1-minute stop the bleeding | Root-cause |
|---|----------|---------------------------|-----------|
| 1 | LLM API rate-limited | Switch to fallback model | Check Anthropic console |
| 2 | DB connection exhausted | Restart connection pool | Check slow queries |
| 3 | Redis down | Restart Redis | Check memory |
| 4 | Agent keeps failing | Pause that agent | Check reflection system |
| 5 | Task stuck | Cancel task | Check trace |

### Tier 2 — common

| # | Incident | 1-minute stop the bleeding | Root-cause |
|---|----------|---------------------------|-----------|
| 6 | MCP server fails | Disable that MCP | Check logs |
| 7 | Login fails | Clear cache | Check Auth logs |
| 8 | KB returns nothing | Rebuild index | Check Chroma status |
| 9 | Dashboard stale | Refresh page | Check WebSocket |
| 10 | Queue backlog | Add workers | Check queue metrics |

### Tier 3 — serious

| # | Incident | 1-minute stop the bleeding | Root-cause |
|---|----------|---------------------------|-----------|
| 11 | Whole system down | Switch to backup env | K8s status |
| 12 | Data corruption | Switch to yesterday's backup | Run data check |
| 13 | Key leaked | Rotate immediately | Check access log |
| 14 | Under attack | Cut off ingress | Start IR |
| 15 | Billing error | Pause charges | Stripe logs |

### Tier 4 — disaster

| # | Incident | Action |
|---|----------|--------|
| 16 | K8s cluster down | Switch to backup cluster |
| 17 | DB primary+replica down | Restore from S3 backup |
| 18 | Cross-region failure | Enable disaster recovery |
| 19 | Compliance: pause service | Shut down immediately, legal |
| 20 | Major security incident | Disconnect, IR process |

## On-call escalation

- Tier 1: Platform engineer (5 min)
- Tier 2: Platform engineer + on-call lead (15 min)
- Tier 3: Engineering manager (30 min)
- Tier 4: CTO + CEO

## Operational commands

### Check service health

```bash
kubectl -n agent-system get pods
curl https://agent.example.com/api/health
```

### Tail logs

```bash
kubectl -n agent-system logs -f -l app=agent-system
```

### Check metrics

```bash
curl https://agent.example.com/api/metrics
```

### Restart a stuck pod

```bash
kubectl -n agent-system delete pod -l app=system --field-selector=status.phase=Failed
```

### Roll back a deploy

```bash
helm history agent-system -n agent-system
helm rollback agent-system 1 -n agent-system
```

### Drain traffic (emergency)

```bash
# Scale to 0
kubectl -n agent-system scale deploy agent-system --replicas=0
```

## Maintenance

### Backup

```bash
# Postgres backup (uses pg_dump)
kubectl -n agent-system exec -it deploy/postgres -- pg_dump -U agent agent > backup-$(date +%F).sql

# Or use a managed snapshot
```

### Restore

```bash
kubectl -n agent-system exec -i deploy/postgres -- psql -U agent agent < backup-2026-06-30.sql
```

### Schema migrations

Alembic is configured under `alembic/`. Run:

```bash
alembic upgrade head
```

## ADR (Architectural Decision Records)

ADRs live in `docs/adr/`. Create a new one as `00XX-name.md` describing
context, decision, and consequences.

## Contacts

- Platform on-call: pager
- Security: security@yourcompany.com
- Compliance: compliance@yourcompany.com
- LLM vendor (Anthropic): support@anthropic.com
