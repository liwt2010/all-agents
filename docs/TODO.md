# TODO — Forward-looking roadmap

> This document tracks **post-v0.6.0 work**. All v0.1.0 through
> v0.6.0 roadmap items are complete; see [CHANGELOG.md](../CHANGELOG.md)
> and [RELEASE_NOTES.md](../RELEASE_NOTES.md) for what shipped.

## Backlog (unprioritized)

- **Function-call / tool-call streaming** — current WebSocket
  streaming only emits text deltas. Extending `stream_chunks()` to
  yield structured tool-use events would let chat UIs render
  intermediate reasoning steps in real time.
- **Custom Agent marketplace UI** — the YAML + HTTP API exists
  (v0.3.0); a web frontend for browsing/uploading would close the
  loop for non-technical operators.
- **HL7 / FHIR adapter** — healthcare data format integration.
  Would let agents ingest patient records and emit
  HL7-formatted outputs.
- **Native gRPC server** alongside REST/WebSocket — for service-to-service
  integrations that want streaming RPC semantics.
- **Distributed task queue** — current `SmartAgent.execute()` runs in
  the request handler. Adding Celery/RQ would let a single API
  replica accept unbounded task submissions and dispatch to a
  worker pool.

## Known technical debt

- **`ARCHITECTURE.md`** (51KB) was last edited before v0.1.0; it
  pre-dates the v0.2.0 / v0.3.0 / v0.5.0 / v0.6.0 modules. The
  header now declares it historical; v0.6.0 added per-section
  "implemented at Task layer" callouts. Either trim to historical-
  only or do a full pass.
- **Flaky tests** — `test_save_1000_nodes_under_5s[json]` and the
  OpenAPI SDK subprocess test occasionally time out under heavy
  parallel load. Investigate once we have CI timing data.

## Housekeeping

- Push `v0.2.0` and `v0.3.0` tags to `origin` (currently local-only,
  following the project's prior convention of waiting for operator
  approval before publishing release tags).
- Rebuild Docker image `liwt2010/all-agents` at `v0.3.0` and push to
  Docker Hub.
- Add `make verify` shortcut to run lint + type-check + production-
  readiness gate + full pytest, suitable as a pre-push hook.

## When to revisit

If a new v0.4.0 milestone is opened, copy this file into
`docs/adr/0004-v040-roadmap.md` and turn the backlog into
prioritized items with effort estimates.