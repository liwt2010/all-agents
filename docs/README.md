# Documentation map

Each doc has a single audience. If you're not sure which to read,
find your role below and follow the link.

## For users / evaluators

- [README.md](../README.md) — what the platform does, quick start,
  feature inventory, current version status. **Read this first.**
- [README.zh-CN.md](../README.zh-CN.md) / [README.zh-TW.md](../README.zh-TW.md) —
  Simplified Chinese / Traditional Chinese translations.
- [RELEASE_NOTES.md](../RELEASE_NOTES.md) — per-release user-facing
  change log (v0.1.0 / v0.1.1 / v0.2.0 / v0.3.0 / v0.4.0 / v0.5.0 / v0.6.0).

## For deployers / operators

- [PRODUCTION.md](PRODUCTION.md) — 13-section deployment guide.
  Audience: anyone running this in production.
- [RUNBOOK.md](RUNBOOK.md) — incident response. Audience: on-call
  engineer. Cross-linked from PRODUCTION.md §11.

## For storage / data engineers

- [STORAGE.md](STORAGE.md) — JSON / SQLite / PostgreSQL backends.
- [PostgreSQL RLS section](STORAGE.md#11-multi-tenant-isolationpostgresql-row-level-security)
  (v0.2.0) — schema-level tenant isolation.

## For rate-limit / API gateway engineers

- [RATE_LIMIT.md](RATE_LIMIT.md) — sliding window + Redis backend
  (v0.2.0).

## For gRPC / service-to-service integrators

- [GRPC.md](GRPC.md) — the `.proto`-driven gRPC transport
  (v0.5.0). Submit/get/list tasks and stream LLM tokens without
  HTTP. Mirrors the REST API exactly.

## For observability engineers

- [METRICS.md](METRICS.md) — Prometheus metrics catalog (11 metrics).
- [AUDIT.md](AUDIT.md) — audit log retention, query API.
- [BACKUP.md](BACKUP.md) — backup subsystem + DR drill.
- OpenTelemetry exporter is configured in
  [`src/agent_system/observability/otel_exporter.py`](../src/agent_system/observability/otel_exporter.py)
  (see [ADR index](adr/README.md) for the design rationale).

## For agent authors

- [CUSTOM_AGENT.md](CUSTOM_AGENT.md) — how to define a custom agent
  via YAML.
- [DATAVIEW.md](DATAVIEW.md) — memory graph query language.
- [examples/custom-agents/](../examples/custom-agents/) — two
  ready-to-use YAML examples (translator, pr-summarizer).

## For platform engineers / integrators

- [GitHub App setup](#) — registered as a follow-up in
  [docs/TODO.md](TODO.md) (currently inline in `api/routes/github_webhook.py`).
- [WebSocket streaming](#) — see `api/routes/llm_stream.py` +
  `core/llm_router.py::stream_chunks()` (inline docstrings).

## For contributors / maintainers

- [TODO.md](TODO.md) — current backlog (marketplace UI, HL7/FHIR,
  gRPC interceptors, distributed task queue, list_tasks SQL
  pushdown).
- [DEFERRED.md](../DEFERRED.md) — historically deferred items and
  their resolution.
- [CHANGELOG.md](../CHANGELOG.md) — per-PR change log.
- [CONTRIBUTING.md](../CONTRIBUTING.md) — how to land a PR.
- [SECURITY.md](../SECURITY.md) — supported versions, reporting
  vulnerabilities.
- [STATUS.md](../STATUS.md) — what works today, what doesn't, what
  roadmap items are still open (bumped to v0.5.0).

## For the historically curious

- [ARCHITECTURE.md](../ARCHITECTURE.md) — 51KB, frozen at v15.1
  (pre-v0.1.0 design). Top of file explains it's historical;
  current architecture is captured in README + ADRs.
- [adr/](adr/README.md) — Architectural Decision Records.
  Currently 4 ADRs covering RS256 JWT, PostgreSQL RLS, GitHub
  webhook. New ADRs should be added in the standard Nygard format
  (see [0000-adr-template.md](adr/0000-adr-template.md)).

## Conventions

- Every `.md` file should answer "who is this for" in its first
  paragraph. If you can't, it's probably misnamed.
- The English README is the source of truth. Chinese translations
  lag behind by at most one release.
- Long-lived design rationale belongs in [`adr/`](adr/), not in
  code comments — comments rot, ADRs are timestamped.