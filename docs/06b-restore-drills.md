# 06b — Restore Drills

Companion to [06 — Security & Compliance](06-security-compliance.md), which
lists "Immutable off-site backups, restore drills" as the primary control for
threat 7 (ransomware / destructive insider) and states that "runbooks live in
`ops/runbooks/` and are exercised twice yearly." [02 — Architecture](02-architecture.md)
goes further, with numeric targets: RPO ≤ 5 minutes via WAL archiving, RTO
≤ 4 hours, and "restore is exercised quarterly against a real backup into a
scratch environment." **Neither of those is implemented tooling today.** This
document is the honest state of that gap, plus the concrete steps to run a
restore drill with what actually exists.

## 1. What exists today

Checked directly against this repository, not against the aspiration in doc
02 or doc 06:

- **No backup mechanism.** `docker-compose.yml` defines named Docker volumes
  (`postgres_data`, `redis_data`) for local-development persistence only.
  A Docker volume surviving a `docker compose down` is not a backup — it does
  not survive the volume itself being deleted, the host disk failing, or a
  destructive insider with access to the host, which is precisely the threat
  doc 06 lists this control against.
- **No WAL archiving.** Nothing in `config/settings/` or the deployment
  configuration configures `archive_mode`, a WAL shipping target, or a
  point-in-time-recovery tool (WAL-G, pgBackRest, or a managed provider's
  equivalent). The ≤5-minute RPO in doc 02 has no mechanism behind it yet.
  A repository-wide search for `pg_dump`, `pg_basebackup`, `WAL-G`,
  `pgbackrest`, and `S3` turns up only the two design documents that mention
  them — no script, no Celery task, no CI job.
- **No scheduled purge/retention job either** (see [06a — DPIA](06a-dpia.md#7-retention))
  — related, since a retention policy and a backup policy have to agree on
  what "restore" is allowed to bring back.
- **`ops/runbooks/` does not exist.** Doc 06 references it as the home for
  exercised runbooks; there is no such directory in this repository.
- **What does exist and is directly useful for a restore drill:**
  - `educore.core.audit.verify_chain()` — walks one school's hash-chained
    audit events and reports gaps, broken links, and tampered rows
    (`educore/core/audit.py`).
  - `manage.py verify_audit_chains` — runs `verify_chain()` for every school
    (or one, via `--school`), exits non-zero on any break, and is wired into
    CI (`.github/workflows/ci.yml`, "Audit chain integrity") and
    `CELERY_BEAT_SCHEDULE` (nightly, per README's "Scheduled work" table).
    This is the tool that turns "the restore looks fine" into a falsifiable
    check: if the audit chain verifies clean post-restore, tampering
    happened after the restore point, not before it went undetected.
  - `educore.core.checks.rls_enabled` — a Django deploy check (`manage.py
    check --deploy`) that refuses to pass if row-level security is not
    enabled and forced on every tenant-owned table. A restore that
    reconstructs the schema from a plain data dump without reapplying
    migrations could silently lose RLS; this check is what catches that
    before the restored database is trusted, and `manage.py verify`'s
    `deploy-check` phase runs it locally.
  - `tests/test_tenant_isolation.py` and the `pytest -m postgres` battery —
    exercises cross-tenant isolation against a live PostgreSQL instance.
    Pointing this battery at a restored database is a stronger check than
    the deploy check alone, because it proves isolation *behaves* correctly,
    not just that the policy rows exist.

The gap, stated plainly: this system has the tooling to **verify** a restore
is trustworthy, but not the tooling to **produce** a restore to verify. Both
are needed; only the first exists.

## 2. Restore procedure (manual, against what exists today)

Until scheduled backups and WAL archiving are built, a drill still has
value — it forces the manual procedure to be written down and rehearsed
before the day it is needed under pressure, and it exercises the
verification tooling above, which *is* real. This is the procedure for a
local or staging drill using the PostgreSQL 16 instance defined in
`docker-compose.yml`.

### 2.1 Take a snapshot to restore from

```bash
docker compose up -d postgres
docker compose exec -T postgres pg_dump -U educore -d educore \
    --format=custom --file=/tmp/educore-drill.dump
docker cp $(docker compose ps -q postgres):/tmp/educore-drill.dump ./educore-drill.dump
```

`--format=custom` (not plain SQL) because it is restorable with `pg_restore`
and supports parallel restore for the larger hot tables (`audit_event`,
`presence_attendanceevent`) once this repo's partitioning work lands.

Record, before going further, a baseline to compare the restored database
against:

```bash
.venv/Scripts/python manage.py verify_audit_chains
docker compose exec -T postgres psql -U educore -d educore -c \
    "SELECT count(*) FROM core_auditevent;"
```

### 2.2 Restore into a scratch database

Never restore over the database you just dumped from — the drill proves
nothing if it does, and a real incident restore must never be rehearsed
against production data either.

```bash
docker compose exec -T postgres psql -U educore -d postgres -c \
    "CREATE DATABASE educore_restore_drill;"
docker cp ./educore-drill.dump $(docker compose ps -q postgres):/tmp/
docker compose exec -T postgres pg_restore -U educore -d educore_restore_drill \
    --no-owner --no-privileges /tmp/educore-drill.dump
```

### 2.3 Verify the restore before trusting it

Point the application at the scratch database and run the checks that
matter, in this order — each one catches a different failure mode a restore
can silently introduce:

```bash
DATABASE_URL=postgres://educore:educore@localhost:5432/educore_restore_drill \
    .venv/Scripts/python manage.py migrate --check
```

Confirms the restored schema matches the migration state the application
expects. A restore from an older dump than the running codebase fails here,
loudly, instead of surfacing as a runtime `ProgrammingError` later.

```bash
DATABASE_URL=postgres://educore:educore@localhost:5432/educore_restore_drill \
DJANGO_SETTINGS_MODULE=config.settings.production DEBUG=False \
ALLOWED_HOSTS=localhost SECRET_KEY=drill-only-key \
    .venv/Scripts/python manage.py check --deploy --fail-level WARNING
```

Runs `educore.core.checks.rls_enabled` against the restored database. If RLS
was lost in the dump/restore round trip — a real risk with `--no-owner
--no-privileges` restores against a role that is not the original owner —
this fails with the exact list of unprotected tables (`educore.E004`)
instead of the isolation gap being discovered by an incident.

```bash
DATABASE_URL=postgres://educore:educore@localhost:5432/educore_restore_drill \
    .venv/Scripts/python manage.py verify_audit_chains
```

Every school's hash chain must still verify clean. This is the check that
answers the actual question a restore drill exists to answer: *is the data
that came back intact, or was it tampered with before, during, or after the
backup was taken?* A break here, in a drill, means the dump/restore process
itself is corrupting data — fix that before this procedure is trusted for a
real incident.

```bash
DATABASE_URL=postgres://educore:educore@localhost:5432/educore_restore_drill \
    .venv/Scripts/python -m pytest -m postgres --no-header -q
```

Runs the tenant-isolation battery against the restored database. This is the
strongest check available: it does not just confirm RLS is *enabled*, it
proves cross-tenant reads are actually refused post-restore.

Finally, compare the row count captured in 2.1 against the restored
database, and spot-check a handful of `AuditEvent.sequence` values are
contiguous per school (a gap is exactly what `verify_audit_chains` is
checking for, but a manual second look costs little in a drill).

### 2.4 Tear down

```bash
docker compose exec -T postgres psql -U educore -d postgres -c \
    "DROP DATABASE educore_restore_drill;"
rm educore-drill.dump
```

## 3. What a production-grade version of this needs (not yet built)

Recorded here so the gap has a shape, not just a name:

1. **Scheduled, offsite, immutable backups.** `pg_dump` on a cron job writing
   to the same infrastructure it protects against is not the "immutable
   off-site" control doc 06 specifies. This needs either WAL archiving to
   object storage with a managed PITR tool, or a managed PostgreSQL
   provider's built-in continuous backup — a genuine build-vs-buy decision,
   not something to hand-roll into a Celery task.
2. **Per-tenant restore**, per [ADR-0001](adr/0001-tenancy-model.md)'s
   accepted cost: "a filtered logical export, not a schema dump." The
   procedure above restores the whole database; recovering one school
   without touching others (the scenario doc 06's threat 7 most plausibly
   produces — one compromised or malicious account, not a full-database
   incident) needs a `school_id`-filtered `pg_dump --table` / row-level
   export path that does not exist yet.
3. **Automation**, so the drill is a scheduled job with an alert on failure
   rather than a manual procedure someone has to remember to run. Doc 02
   claims quarterly; doc 06 claims runbooks generally are exercised twice
   yearly — those two cadences are not the same, and should be reconciled
   into one stated cadence for restore specifically once this is automated.
4. **`ops/runbooks/`** should exist and hold the operational (not
   design-record) version of this procedure, with real infrastructure
   details (which object storage bucket, which credentials, who is paged).
   This document is the design-record placeholder for that; it does not
   replace it.

Until items 1–3 exist, doc 02's RPO/RTO numbers are targets, not measured
capability, and should be read that way by anyone relying on them for an
incident-response commitment to a school.
