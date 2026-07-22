# ADR-0002: Enforce tenant isolation via PostgreSQL Row-Level Security

**Status**: Accepted
**Date**: 2026-07-22
**Deciders**: Platform team

## Context

Tenant isolation in v0.1.x is enforced at the API layer: every
storage query includes `WHERE tenant_id = ?` in the SQL string.
This works but is fragile:

1. **One missing `WHERE` clause leaks data across tenants.** With
   ~20 endpoints and 5 storage operations, the audit surface
   is large. Code review alone isn't enough.
2. **Test coverage can't prove the absence of bugs** — every new
   endpoint has to remember to add the filter.
3. **PostgreSQL admins with direct `psql` access bypass the API
   entirely** and can read all tenants' data.

We also need this to work with existing async code paths and
the connection pool — the GUC must be set on every connection
checkout, not just at pool init time.

## Decision

We use **PostgreSQL Row-Level Security (RLS)** to enforce tenant
isolation at the database schema level, with a per-connection
GUC (`app.current_tenant`) that the application sets before every
query.

Concretely:

1. `graph_nodes` and `graph_links` get a `tenant_id TEXT NOT NULL
   DEFAULT 'default'` column. Existing rows (created before the
   migration) remain valid with the default value.
2. RLS is enabled on both tables. Policies:

   ```sql
   CREATE POLICY tenant_isolation_nodes ON graph_nodes
       USING (tenant_id = current_setting('app.current_tenant', true))
       WITH CHECK (tenant_id = current_setting('app.current_tenant', true));
   ```

   `current_setting(..., true)` returns NULL if the GUC is unset,
   which fails the `tenant_id = NULL` comparison — so the default
   behavior is **fail-closed**: unconfigured connections see no
   rows.
3. `PostgresBackend.set_tenant_id(tenant_id)` validates the input
   (1-128 chars) and stores it on the backend instance.
4. `_conn_with_tenant()` is a context manager that wraps every
   pooled connection checkout with
   `SELECT set_config('app.current_tenant', ?, true)`. The third
   arg `true` is `is_local` — the GUC resets at transaction end,
   so a stale tenant value can't leak into the next checkout.
5. All data operations (`save_node`, `save_link`, `save_graph`,
   `load_*`, `list_*`, `delete_*`) now go through
   `_conn_with_tenant()`. The INSERT statements include the
   `tenant_id` from `self._current_tenant`.
6. Cross-tenant admin operations (migrations, batch reports)
   bypass RLS via a role with the `BYPASSRLS` attribute.

The migration (`RLS_MIGRATION_SQL`) is idempotent: `ADD COLUMN
IF NOT EXISTS`, `DROP POLICY IF EXISTS` + `CREATE POLICY` pairs.
Safe to re-run on every `init()`.

## Alternatives considered

### Stay with API-layer `WHERE tenant_id = ?` filtering
Pros: simple, works on every backend (PG, SQLite, JSON).
Cons: one forgotten clause is a data leak; doesn't protect
against direct `psql` access; test coverage can't prove
absence of bugs.

We keep this for the **SQLite and JSON backends** — they have
no RLS concept and the threat model there is "single developer
on a laptop". For production Postgres, RLS is mandatory.

### Use a separate schema per tenant (`SET search_path = tenant_<id>`)
Pros: cleaner separation; easier per-tenant backups.
Cons: requires DDL per tenant (slow at scale); migration tooling
is significantly more complex; harder to share infrastructure
across tenants for cross-tenant analytics.

Worth revisiting if a customer demands hard per-tenant data
isolation for compliance reasons.

### Use a managed row-level security product (e.g. Citus, Atlas)
Pros: offloads complexity.
Cons: another dependency, real cost, and our scale (10k–100k
nodes per tenant) is well within vanilla Postgres range.

## Consequences

### Positive
- **Defense in depth.** Even if a future endpoint forgets to
  set `tenant_id` in its INSERT, the CHECK constraint on the
  RLS policy rejects the row (WITH CHECK is mandatory for
  INSERT/UPDATE in our policy).
- **psql admins are constrained.** A misconfigured DBA running
  raw queries can't accidentally read across tenants unless
  they have `BYPASSRLS`.
- **Fail-closed by default.** Unset `app.current_tenant` → zero
  rows. Catches "I forgot to wire up auth" bugs at runtime,
  not in production.
- **Test coverage proves absence.** RLS makes the contract
  structural — there's no "you forgot the WHERE clause" bug
  to write tests for.

### Negative
- **PG only.** SQLite and JSON backends still rely on API-layer
  filtering. The contract differs by backend, which is a
  documentation hazard.
- **Slight overhead.** The RLS predicate adds a per-row check
  to every query; negligible at our scale (10s of ms vs
  sub-ms), but documented for completeness.
- **More state to manage.** `set_tenant_id` must be called
  before any data operation; tests must respect this. Mitigation:
  `_conn_with_tenant()` makes the GUC set automatic once
  `set_tenant_id` has been called, so most code paths just work.
- **Cross-tenant admin needs a separate role.** Operators
  must understand `BYPASSRLS` semantics.

## References

- Source: `src/agent_system/memory/storage/postgres_backend.py`
  (search for `RLS_MIGRATION_SQL`)
- Tests: `tests/test_storage_rls.py` (15 tests)
- Documentation: `docs/STORAGE.md` §"Multi-tenant isolation"