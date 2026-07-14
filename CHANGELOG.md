# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **await audit_logger.log()**: 4 missing `await` on async `BatchAuditLogger.log()` calls
  in `tasks.py` (lines 117, 183, 206) and `notify.py` (line 142).
  Audit records were silently dropped.
- **CI `|| true` removed**: Ruff advisory check no longer swallows exit code.
  Full scan now blocks CI on lint failures.
- **Python 3.11 f-string syntax**: `test_performance_agent.py` f-strings with
  double-quote dict access (`stats["..."]`) fixed to single quotes.
- **8 failing boundary tests repaired**:
  - Memory disabled: mock paths corrected to `agent_system.memory.experience`
  - Tenant isolation: `set_tenant_context()` token pattern, `LinkType.CREATES` → `REFERS_TO`
  - Schema provenance: STRICT mode weak assertion
  - JWT rotation: correct `AuthService`/`TokenPayload` API, raw dict for old tokens
  - LLM errors: correct resolver routing assertions

### Security
- **pip-audit added to CI**: New `security-audit` job in CI pipeline
- **Dependabot config**: `.github/dependabot.yml` for pip, github-actions, docker
- **SECURITY.md**: Vulnerability reporting policy added
- **Pre-commit hooks**: `.pre-commit-config.yaml` + `scripts/check_no_secrets.py`

### Changed
- **Server refactored**: `src/agent_system/api/server.py` reduced from 607 to 203 lines.
  Routes split into `api/routes/` (health, auth, tasks, agents, graph, metrics, audit).
  Shared singletons moved to `api/state.py`.
- **Strict CI lint**: Removed `|| true` from ruff/mypy; core modules fail build on lint errors
- **mypy strict on core modules**: Added overrides for `core/`, `memory/`, `storage/`
- **UP006/UP045 batch fix**: 72 files modernized `List[X]` → `list[X]`
- **Pydantic V2 migration**: `class Config` → `model_config = ConfigDict(...)` in `dataview.py`

## [0.1.0] — 2026-07-09

### Added
- **9 built-in agents**: Product, Tech, Test, Deploy, CEO, Security, Docs, Review, DevOps
- **SmartAgent base class**: Task execution, retry, checkpointing, validation, memory hooks
- **4-way SmartResolver**: SELF / PEER / HUMAN / ESCALATE resolution paths
- **AutoGen PEER upgrade**: `RoundRobinGroupChat` replacing legacy `DiscussionMixin`
- **MultiLinkGraph memory**: 11 node types, 23 link types, experience feedback loop
- **Tiered schema validation**: Auto-repair with FAILURE-node audit trail
- **Provenance tracking**: Real/mock/llm_failure/unknown provenance on every output
- **JWT secret rotation**: Multi-key support via `AUTH_SECRETS` env var
- **Rate limiter**: Per-user/per-scope sliding window (PR-12)
- **Audit logger**: Batch, retention, query API (PR-11)
- **Prometheus metrics**: Request duration, error rate, agent metrics (PR-10)
- **Pluggable storage**: JSON/SQLite/PostgreSQL backends (PR-9)
- **Custom Agent platform**: YAML-defined custom agents (PR-8)
- **AgentRegistry**: Auto-discovery with agent capability lookup (PR-5)
- **OpenAPI spec dump**: Auto-generated Python/TypeScript SDKs (PR-15)
- **OpenTelemetry tracing**: Distributed tracing support (PR-14)
- **Backup subsystem**: Scheduler + restore + DR drill (PR-13)
- **Dataview engine**: Obsidian-Dataview-style SQL query engine (PR-1)
- **API middleware chain**: Auth, CORS, rate-limit, tracing, request-id
- **Boundary tests**: 26 tests for memory, tenant isolation, schema, JWT rotation, LLM errors
- **Performance benchmarks**: Agent execution time (avg/p50/p95/p99), concurrent task handling
- **Dockerfile**: Python 3.11-slim based
- **CI pipeline**: Ruff, mypy, pytest, pip-audit, dependabot

### Fixed
- CI dependency installation (pip/requirements.txt compat)
- PowerShell stderr garbage in CI output
- pywin32 Windows-only dependency removed from Linux CI
- 6 test bugs found during v0.1.0 audit
- PEER resolver crash on non-OpenAI providers
- LLM router `None` usage fields from non-standard proxies
- 8 boundary tests repaired post-v0.1.0

### Docs
- README.md (Chinese Simplified + Traditional translations)
- ARCHITECTURE.md, PRODUCTION.md, CONTRIBUTING.md
- DEFERRED.md, RELEASE_NOTES.md, ROADMAP.md, SECURITY.md
