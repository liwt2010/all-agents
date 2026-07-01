# Agent System — Iteration Plan (post-PLATFORM.md review)

> Generated 2026-06-30 after reviewing `D:\ClaudeCode\my-agent\docs\PLATFORM.md`
> (32 chapters, 2656 lines, the actual product design).

## Current state vs PLATFORM.md

The existing 7-iteration roadmap (`ARCHITECTURE.md` based) implemented:
- 4 Agents (product, tech, test, ceo) — **missing deploy agent**
- 1 Mixin (SmartAgentMixin) — **missing DiscussionMixin, BaseMixin, GroupIsolationMixin**
- Memory (MultiLinkGraph) ✓
- SmartResolver 4-way (PEER is mocked) ⚠️
- Quota, cost, 9 metrics ✓
- FastAPI + React UI ✓
- LLM Router supports real + mock ✓ (not live-verified with API key)

PLATFORM.md asks for ~30 additional capabilities (custom Agent, audit queries,
FTUE, migration tool, time/calendar, distributed locks, secrets, K8s, etc.).

## 3-Round Plan

| Round | Theme | Files | Tests | Demo |
|-------|-------|-------|-------|------|
| 1 | Foundation | ~20 | ~45 | Real PEER + multi-tenancy |
| 2 | High-value product | ~30 | ~60 | Custom Agent + FTUE |
| 3 | Production infra | ~16 | ~25 | Deployable to prod |

Total: ~66 files, ~130 new tests. Existing 222 tests must stay green.

---

## Round 1 — Foundation (Weeks 1-2)

**Goal**: Make PEER real and multi-tenancy meaningful.

| Capability | File | Source |
|---|---|---|
| DiscussionMixin (real AutoGen-style) | `core/mixins/discussion.py` | PLATFORM §5.2, §7.3 |
| BaseMixin (event bus + memory) | `core/mixins/base.py` | PLATFORM §5.2 |
| GroupIsolationMixin | `core/mixins/group_isolation.py` | PLATFORM §5.2, §7.7 |
| DeployAgent (5th agent) | `agents/deploy.py` | PLATFORM §5.1 |
| User/Tenant/Group/Permission | `core/auth/models.py` | PLATFORM §7.7, §28 |
| TenantContext (contextvars) | `core/auth/context.py` | PLATFORM §12.B |
| RBAC matrix | `core/auth/rbac.py` | PLATFORM §7.7 |
| AuditQuery (14 methods) | `core/audit/query.py` | PLATFORM §13.3 |
| Audit alerts (5 rules) | `core/audit/alerts.py` | PLATFORM §13.4 |
| TimezoneHandler | `core/time/timezone.py` | PLATFORM §31.2 |
| WorkingHours | `core/time/working_hours.py` | PLATFORM §31.3 |
| Holiday calendar | `core/time/calendar.py` | PLATFORM §31.4 |

**Why first**: DiscussionMixin unblocks SmartResolver PEER. Tenant/User/Permission
are prereqs for every later round (custom Agents, audit, migration all need isolation).
Time/calendar are needed for HUMAN path + escalation timing.

---

## Round 2 — High-Value Product (Weeks 3-4)

**Goal**: Differentiation + conversion.

| Capability | File | Source |
|---|---|---|
| Custom Agent platform | `agents/custom/` | PLATFORM §14 |
| Failure UX stage 5 (notifications + resume) | `core/failure_ux/notify.py` | PLATFORM §30.1 |
| FTUE flow | `web/src/pages/Onboarding.tsx` | PLATFORM §17 |
| Data migration engine | `core/migration/engine.py` | PLATFORM §15 |
| ConfigManager 4-layer override | `core/config/manager.py` | PLATFORM §27.2, §28.3 |
| Distributed ResourceLock | `core/concurrency/lock.py` | PLATFORM §32.2 |
| Audit alert evaluator | `core/audit/evaluator.py` | PLATFORM §13.4 |

**Why second**: Custom Agent + Failure UX stage 5 drive differentiation and retention.
FTUE drives first impression. Migration enables onboarding from competitors.
ConfigManager enables tenant-tier differentiation. Lock enables concurrent Agent work.

---

## Round 3 — Production Infrastructure (Weeks 5-6)

**Goal**: Ready to sell.

| Capability | File | Source |
|---|---|---|
| Real MCP server integration | `tools/mcp_client.py` | PLATFORM §5.3, §6 |
| Anthropic API live verification | `tests/integration/test_real_api.py` | (production) |
| Postgres + Redis backends | `core/storage/` | PLATFORM §8 (Postgres, Redis) |
| CI/CD pipeline | `.github/workflows/` | PLATFORM §21.6 |
| OTel + Prometheus + Grafana | `infra/observability/` | PLATFORM §5.5 |
| K8s manifests + Helm chart | `deploy/helm/` | PLATFORM §21.3 |
| Secrets manager (Vault / AWS) | `core/secrets/` | PLATFORM §27.3 |

**Why last**: Everything earlier assumes real infra. Without Postgres/Redis the
lock and audit break; without real MCP the tool story is partial; without CI/CD
and K8s the platform doesn't deploy past dev.

---

## Invariants (preserved across all rounds)

- 222 existing tests stay green
- All new code lives under `src/agent_system/core/{mixins,auth,audit,time,concurrency,migration,config,storage,secrets}/`
- Each round ends with a self-contained "demo-able" milestone
- No breaking changes to existing public APIs

## Demo per round

- **R1 done**: Submit a task → real PEER discussion between 3 agents → consensus → result
- **R2 done**: New user signs up → FTUE → creates custom Agent → uses it
- **R3 done**: Push to main → CI green → canary deploy → Prometheus dashboard live
