# Partitioning Plan — `attendance_event` and `audit_event`

Status: **planned, not applied**. This document exists because a feature-completeness
audit found `docs/02-architecture.md` and `CLAUDE.md` asserting range partitioning
as an already-built fact, when no partitioning code existed anywhere in the
repository. Both documents have been corrected to point here. This is the design
for Phase 4 ("Performance: partitioning rollout", `docs/07-delivery-plan.md`).

## What exists today

- `educore.core.db.partition_by_range(table, column, *, using="RANGE")` — a
  migration helper that performs the standard safe retrofit of an *existing*
  Postgres table onto native declarative partitioning: rename the table aside,
  `CREATE TABLE ... (LIKE ... INCLUDING ALL) PARTITION BY {using} (column)`
  under the original name, then `ATTACH PARTITION ... DEFAULT`. All three steps
  are metadata-only — no row is copied — so it is safe to run against a table
  that already holds production data, and it is symmetrically reversible
  (`DETACH PARTITION`, `DROP TABLE`, rename back). Like every other helper in
  `db.py` (`rls()`, `composite_fks()`, `append_only()`), it is a no-op on
  SQLite with the same vendor-check shape.
- `presence_attendanceevent` and `core_auditevent` are, as of this writing,
  ordinary unpartitioned Postgres tables.

## Why it has not been applied yet

`partition_by_range()`'s precondition is that **every PRIMARY KEY and UNIQUE
constraint on the table already includes the partition column**. PostgreSQL
enforces this at `CREATE TABLE ... PARTITION BY` time: a unique index that
does not carry the partition key cannot express uniqueness across partitions,
so the statement is rejected outright. Neither candidate table satisfies this
today, and fixing it is a real schema change, not a one-line migration:

- Every tenant-owned model's primary key is a bare `id` (`UUIDModel.id`,
  `educore/core/tenancy.py`), and every tenant-owned model additionally
  carries a `(school_id, id)` UNIQUE constraint
  (`TenantOwnedModel.tenant_constraints()`) that other tables' composite
  foreign keys point at (`educore.core.db.composite_fks()`). Partitioning
  `presence_attendanceevent` by, say, `received_at` would require widening
  that unique constraint to `(school_id, id, received_at)` — `id` alone can
  no longer be enforced unique by a single Postgres index once the table is
  partitioned by a different column.
- `presence_attendancesignal.event` is a plain, single-column Django
  `ForeignKey` to `presence_attendanceevent.id`
  (`educore/presence/models.py`). A foreign key must reference a column set
  that has an exact matching unique constraint on the parent. Widening
  `presence_attendanceevent`'s key as above invalidates this FK; it would
  need to become a composite FK carrying the partition column too, and the
  `Signal.event` relation would need `received_at` available on every row
  that creates a signal (it doesn't currently store it).
- `core_auditevent` has no inbound foreign key, so it is less constrained,
  but it still carries a `(school, sequence)` UNIQUE constraint
  (`AuditEvent.Meta.constraints`) that the hash-chain verifier
  (`educore/core/audit.py::verify_chain`) relies on for strict per-school
  ordering. That constraint would need the same widening before
  partitioning `occurred_at`.
- `docs/02-architecture.md` also names `attendance_signal` and
  `student_attendance` as partitioning targets. `student_attendance` does not
  exist as a model yet (a documentation-only forward reference); when it is
  built it should be designed with the partition column in its keys from the
  start, which avoids this whole retrofit problem for that table.

In short: retrofitting partitioning onto these two tables is not just "run
`partition_by_range()`" — it is a coordinated schema change touching the
primary key shape, the composite-FK helper's assumptions, one Django model's
foreign key, and the hash-chain verifier's uniqueness guarantee. That is a
multi-migration change that needs review and a staging Postgres to verify
lock behaviour and FK re-validation cost against representative data volumes,
neither of which is available in this environment (SQLite only, locally; CI's
Postgres is not interactively reachable). Forcing it through without that
verification is exactly the risk this plan exists to avoid.

## Proposed rollout (for a future PR, against staging first)

### Step 1 — widen the keys (expand phase, no partitioning yet)

1. Add the future partition column to `presence_attendanceevent` if it does
   not already carry the desired one. `received_at` (server time, `auto_now_add`,
   already indexed) is the right choice over `captured_at` (device-reported,
   attacker-influenceable per the model's own comment) — partition boundaries
   must be server-trustworthy.
2. Migration: drop and recreate `presence_attendanceevent_school_id_uniq` as
   `UNIQUE (school_id, id, received_at)`. Use `state_operations` so Django's
   migration state still thinks the constraint is the old two-column one only
   if code elsewhere depends on the exact name/shape being unchanged;
   otherwise update `TenantOwnedModel.tenant_constraints()` callers and let
   the state track reality — check call sites before deciding which.
3. Add `received_at` to `presence_attendancesignal` (denormalised from
   `event.received_at` at write time, `db_index=True`) and change
   `presence_attendancesignal_event_id_same_school_fk`-equivalent handling:
   `AttendanceSignal.event` needs a composite FK
   `(event_id, event_received_at)` → `(id, received_at)` on
   `presence_attendanceevent`, replacing the plain Django FK's implicit
   single-column one. This is the most invasive part of the whole plan and
   deserves its own review pass.
4. For `core_auditevent`: widen `core_auditevent_school_sequence_uniq` to
   `UNIQUE (school_id, sequence, occurred_at)`. No inbound FK to fix.
5. Ship step 1, verify on staging (constraint creation on a large existing
   table takes a `SHARE` lock and validates existing rows — measure the time
   against a staging copy sized like the largest pilot school's data before
   merging), let it bake.

### Step 2 — retrofit partitioning (this is where `partition_by_range()` runs)

```python
from django.db import migrations
from educore.core.db import partition_by_range

class Migration(migrations.Migration):
    dependencies = [("presence", "00XX_widen_attendanceevent_key")]
    operations = [
        partition_by_range("presence_attendanceevent", "received_at"),
    ]
```

and equivalently for `core_auditevent` on `occurred_at`. Each of these needs
`state_operations` alongside the `RunPython` (mirroring how
`educore/presence/migrations/0002_tenant_isolation.py` and
`educore/core/migrations/0002_tenant_isolation.py` pair a `RunPython` with
plain SQL and leave Django's model state untouched, because the table shape
Django cares about — columns, the model-level PK — does not change) so
`makemigrations --check` does not detect drift afterward. Confirm this by
running `manage.py makemigrations --check --dry-run` immediately after
writing the migration, against a Postgres database (SQLite will not exercise
the `RunPython` body and will falsely appear clean).

All existing rows land in the automatically created DEFAULT partition
(`presence_attendanceevent__unpartitioned`, per the helper's naming). That is
intentional and matches Postgres's own recommended retrofit pattern — no
data movement happens in this step.

### Step 3 — create dated partitions and migrate rows out of DEFAULT

Not covered by `partition_by_range()` on purpose (see its docstring). Needs:

- A migration or Celery beat job (candidate: alongside `roll_timetable`'s
  scheduling pattern) that creates the next 1–2 months of partitions ahead of
  need: `CREATE TABLE presence_attendanceevent_2026_09 PARTITION OF
  presence_attendanceevent FOR VALUES FROM ('2026-09-01') TO ('2026-10-01');`
- A backfill to move existing DEFAULT-partition rows into dated partitions.
  Postgres does not do this automatically; the standard approach is
  `INSERT INTO presence_attendanceevent SELECT * FROM
  presence_attendanceevent__unpartitioned WHERE received_at >= ... AND
  received_at < ...` batched by month, followed by deleting the moved rows
  from the DEFAULT partition — done in bounded batches to avoid a long
  transaction on an append-only table with a live insert rate.
- A retention job that `DROP`s partitions older than the retention window
  (`docs/06-security-compliance.md` already specifies "partition drop plus a
  tombstone in the audit chain" as the deletion mechanism — that tombstone
  write does not exist yet either and should be built alongside this step).

## What to verify on staging before any of this merges

- [ ] `EXPLAIN ANALYZE` on the query patterns in `presence/services.py` and
      `core/audit.py::verify_chain` before and after, on a staging copy sized
      like the largest pilot school (docs cite ~6–8M rows/year for the
      comparable `student_attendance` case).
- [ ] Lock duration of the Step 1 constraint-widening migration and the Step 2
      rename/attach, measured, not estimated.
- [ ] `composite_fks()` targets: confirm every FK referencing
      `presence_attendanceevent` and `core_auditevent` after the key widening
      still validates correctly (`composite_fks()` re-adds constraints with
      `DEFERRABLE INITIALLY DEFERRED`; confirm that still holds under the new
      composite shape).
- [ ] `manage.py sqlmigrate` output for every migration in this plan, reviewed
      by a human, before running against any database holding real rows.
- [ ] The retention "drop partition + audit tombstone" mechanism from
      `docs/06-security-compliance.md`, since it currently does not exist and
      partitioning is a prerequisite for it.

## Non-goal of this document

This is a plan, not an implementation. No migration touching
`presence_attendanceevent` or `core_auditevent` on any real database ships as
part of the change that introduced this document — only the reusable
`partition_by_range()` helper (exercised on SQLite as a no-op, matching the
existing test pattern for `rls()`/`append_only()`) and the corrected
documentation.
