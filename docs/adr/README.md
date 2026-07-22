# ADR Index

Architectural decisions for the Agent System platform. Each entry
links to the corresponding `docs/adr/NNNN-*.md` file and summarizes
the decision in one sentence so you can decide whether to read the
whole ADR.

| # | Title | Status | Date |
|---|---|---|---|
| [0000](0000-adr-template.md) | ADR template | Accepted | 2026-07-22 |
| [0001](0001-rs256-jwt.md) | Choose RS256 over HS256 for JWT signing | Accepted | 2026-07-22 |
| [0002](0002-postgres-rls.md) | Enforce tenant isolation via PostgreSQL Row-Level Security | Accepted | 2026-07-22 |
| [0003](0003-github-webhook.md) | Self-host GitHub webhook receiver | Accepted | 2026-07-22 |

## How to read

1. Skim this table to find the decision you want to understand.
2. Click into the ADR for full context, alternatives, and
   consequences.
3. If the decision affects code, the ADR links to the relevant
   files in `src/`.

## How to write a new ADR

Copy `0000-adr-template.md`, pick the next free `NNNN`, and
add an entry to this index. Keep it short — if you're writing
more than 200 lines, you're recording an essay, not a decision.