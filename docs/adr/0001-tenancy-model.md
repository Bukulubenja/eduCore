# ADR-0001 — Shared schema with row-level tenant isolation

- **Status:** Accepted
- **Date:** 2026-08-04
- **Supersedes:** the schema-per-tenant recommendation in the original design brief

## Context

eduCore is multi-tenant: many schools on one platform, with strict isolation.
Three standard options:

1. **Database per tenant** — maximum isolation, maximum operational cost.
2. **Schema per tenant** — one database, one PostgreSQL schema per school
   (`django-tenants`). Recommended in the original brief.
3. **Shared schema** — one set of tables, `school_id` discriminator on every row.

Target scale: 20 schools in year one, 200 by year three, up to ~5,000 staff and
~120,000 students in aggregate. Data volume is dominated by attendance and audit
rows, roughly 8M rows per 1,000 students per year.

## Decision

**Shared database, shared schema, `school_id` discriminator**, with three
independent enforcement layers: request-context filtering, an ORM default
manager, and PostgreSQL Row Level Security as the backstop.

## Rationale

Schema-per-tenant is intuitively safer and is where most teams start. It becomes
expensive in ways that are not visible at three tenants:

**Migrations scale with tenant count.** Every schema migration runs N times.
At 200 schools a migration touching a large table moves from seconds to hours,
and a failure midway leaves the estate in mixed states — some schools on the new
schema, some on the old, with one codebase that must serve both. Deployment
becomes a nightly operation requiring a rollback plan per tenant.

**Connection pooling degrades.** Schema switching uses `SET search_path`, which is
session state. That is incompatible with PgBouncer in transaction pooling mode —
the mode you need to serve hundreds of workers off a modest connection budget.
Falling back to session pooling multiplies required connections by roughly the
worker count.

**Cross-tenant queries become impossible.** Platform-level questions — usage
metering, aggregate health, anonymised benchmarking, "which schools have not
synced today" — require `UNION ALL` across N schemas, regenerated whenever a
school is added. This is a core operator-portal requirement, not an edge case.

**Operational surface grows.** `pg_dump` per schema, catalogue bloat (200 schools
× ~60 tables = 12,000 relations before indexes), slower planning, heavier
autovacuum scheduling.

The claimed benefit — isolation — is achievable without any of that. Postgres RLS
enforces the boundary *in the database*, which is exactly where schema separation
was supposed to enforce it. A forgotten `.filter(school=…)` returns zero rows
under RLS, the same as it would under a wrong `search_path`.

The genuine advantage schema-per-tenant retains is per-tenant restore: recovering
one school to a point in time without touching others. We accept a slower path
for that — logical export filtered by `school_id`, restored into a scratch
database — because it is a rare, planned operation, whereas migrations and
pooling are daily.

## Implementation

```python
class TenantOwnedModel(models.Model):
    school = models.ForeignKey("core.School", on_delete=models.PROTECT,
                               db_index=True, editable=False)
    objects = TenantManager()        # filters by current tenant context
    all_tenants = models.Manager()   # explicit, audited, operator-only

    class Meta:
        abstract = True
        constraints = [
            models.UniqueConstraint(fields=["school", "id"],
                                    name="%(app_label)s_%(class)s_school_id_uniq"),
        ]
```

The `(school_id, id)` unique constraint exists so other tables can reference it
with a **composite** foreign key. A row in School A then cannot reference a row in
School B: the database rejects it. This closes the class of bug where tenancy is
correct on the row but wrong on its relations.

RLS, applied by migration to every tenant-owned table:

```sql
ALTER TABLE presence_attendanceevent ENABLE ROW LEVEL SECURITY;
ALTER TABLE presence_attendanceevent FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON presence_attendanceevent
    USING      (school_id = current_setting('educore.school_id', true)::uuid)
    WITH CHECK (school_id = current_setting('educore.school_id', true)::uuid);
```

`FORCE` matters — without it the table owner bypasses the policy, which is the
role your application probably runs as. The GUC is set at connection checkout
from the request's Membership; a connection with no tenant set reads nothing,
so the failure mode of a bug is empty results, not leakage.

The application role has no `BYPASSRLS`. Operator tooling connects as a separate
role, on a separate hostname, behind MFA and IP allowlist, and every query it
runs is audited.

## Consequences

**Accepted costs**

- Noisy-neighbour risk: one school's heavy report can degrade others. Mitigated
  with per-tenant rate limits, `statement_timeout`, and a separate worker queue
  for report generation.
- Per-tenant restore is a filtered logical export, not a schema dump.
- Every tenant-owned table needs an index leading with `school_id`, and every
  query plan must be checked to use it.
- RLS adds a predicate to every query. Measured overhead is small when indexes
  lead with `school_id`; it is significant when they do not, so this is enforced
  in review.

**Gained**

- One migration per release, regardless of tenant count.
- PgBouncer transaction pooling works.
- Operator analytics are ordinary queries.
- Isolation is enforced by the database, surviving ORM mistakes and raw SQL.

## Revisit if

- A single school's data volume justifies its own database (a very large group).
- A jurisdiction requires physical data separation or in-country residency — then
  a separate deployment per region, still shared-schema within it.
- Noisy-neighbour incidents recur despite quotas.
