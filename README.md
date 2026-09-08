# eduCore

School operations and accountability platform. Multi-tenant SaaS.

The design is authoritative and lives in [`docs/`](docs/README.md) — start with
[the product overview](docs/01-product-overview.md), then the
[ADRs](docs/README.md#architecture-decision-records) for the decisions that
shape the code.

**Status:** Phase 4 backend (platform & insights). See
[the delivery plan](docs/07-delivery-plan.md).

Done: tenancy and isolation, audit chain, token auth, academic structure,
staff presence (signals, confidence, rotating QR, appeals, offline sync),
timetable (versions, conflict detection, instance materialisation, missed-lesson
settlement), lesson delivery (classroom scan, substitutions, coverage and pace),
students (enrolment, guardians, registers, gate events).

Also done: the outbox relay and `comms` — notifications, per-channel delivery
records, channel preferences, SMS fallback for unconfirmed push, and
announcements with late-resolved audience selectors. Assessment covers the full
mark lifecycle, TOTP step-up MFA on release, and report cards rendered to PDF
from a frozen snapshot.

Phase 4 adds the operator side: provisioning that leaves a school usable on
day one, suspension, metering and invoice previews, estate health, validated
CSV import for staff/students/guardians, and the dashboards — today's
snapshot, punctuality, workload, at-risk students, coverage.

The web console (`console/`, Next.js 16) covers the leadership screens: today's
snapshot, the attendance review queue with its evidence bars, coverage, at-risk
students, and mark release behind step-up MFA. Tokens live in httpOnly cookies
and the browser never holds a credential — every call goes through the app's own
`/api/proxy`, which attaches the header server-side and silently rotates an
expired access token.

Not yet built: the mobile app, threaded messaging, and real push/SMS providers
(both channels currently record to the delivery table instead of transmitting —
deliberately, so nothing claims to have sent what it did not).

## Onboarding a school

```bash
.venv/Scripts/python manage.py provision_school "Mbarara Secondary" \
    --plan standard --admin-email head@example.com --admin-name "A Head"
```

Then, as that administrator: create class groups, and `POST` CSVs to
`/api/v1/imports/staff`, `/imports/students`, `/imports/guardians`. Imports
are dry-run by default and refuse the whole file if any row is bad.

## Scheduled work

Configured in `CELERY_BEAT_SCHEDULE` and runnable by hand:

| Command | Cadence | Without it |
|---|---|---|
| `relay_outbox` | every minute | Events queue forever; nobody is ever notified |
| `roll_timetable` | every 5 minutes | No lesson is ever recorded as missed |
| `alert_staff_absences` | every 5 minutes | A staff no-show never reaches the deputy |
| `remind_staff_checkins` | every 5 minutes | Staff get no nudge before they are marked late |
| `sweep_movement` | every 10 minutes | A boarder or trip overdue back on campus is never flagged |
| `verify_audit_chains` | nightly | Tampering goes undetected |
| `estate_report` | nightly | Billing has no usage snapshot; a school that stopped syncing goes unnoticed |

Each fails silently in a different way, which is why they are scheduled rather
than left to a runbook.

## Running it

```bash
py -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt
cp .env.example .env
.venv/Scripts/python manage.py migrate
.venv/Scripts/python manage.py runserver
```

Backing services, once Docker is available:

```bash
docker compose up -d          # PostgreSQL + Redis
```

Without PostgreSQL the project falls back to SQLite and **isolation layer 3
(row-level security) is not exercised**. `pytest` reports this in its skip
reasons; do not read a green SQLite run as proof of tenant isolation.

## Deploying (Railway)

`railway.json` pins the build (`collectstatic`) and start
(`migrate` then `gunicorn config.wsgi`) commands; the Nixpacks builder installs
everything in `requirements.txt`. WhiteNoise serves the collected static files
from the app process.

The `web` service needs these environment variables (the code reads **bare
names** — `django-environ` with no prefix):

| Variable | Value |
|---|---|
| `DJANGO_SETTINGS_MODULE` | `config.settings.production` |
| `SECRET_KEY` | a 50+ char random string |
| `FIELD_ENCRYPTION_KEY` | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `DATABASE_URL` | provided by the Railway Postgres plugin |
| `ALLOWED_HOSTS` | `web-production-xxxx.up.railway.app,app.metocore.dev` |
| `CSRF_TRUSTED_ORIGINS` | `https://web-production-xxxx.up.railway.app,https://app.metocore.dev` |
| `REDIS_URL` | Railway Redis plugin, if the Celery worker/beat run as their own services |
| `CONSOLE_BASE_URL` | the deployed console origin (only used to build invite-email links) |
| `SENTRY_DSN` | optional |

`config/settings/production.py` refuses to boot if `SECRET_KEY` or
`ALLOWED_HOSTS` is missing, or if `DATABASE_URL` is not PostgreSQL — a
misconfigured process fails fast rather than serving with isolation disabled.

The first deploy runs the full migration set, including the row-level-security
policies, against an empty database.

## Checks

```bash
.venv/Scripts/python manage.py verify       # runs everything below, in order
```

Or individually:

```bash
.venv/Scripts/python -m pytest              # tests
.venv/Scripts/python -m ruff check .        # lint
.venv/Scripts/python -m bandit -r educore/  # security scan (SAST)
.venv/Scripts/lint-imports                  # module boundaries (ADR-0005)
.venv/Scripts/python manage.py check --deploy
.venv/Scripts/python manage.py spectacular --file schema.yml   # regenerate contract
.venv/Scripts/python manage.py verify_audit_chains
```

`manage.py verify` also checks `schema.yml` for drift (rather than regenerating
it) and refuses a green result on SQLite for the row-level security battery —
see its `--help` for phase names and `--skip`.

`schema.yml` is committed and CI fails if it drifts from the code. Regenerate
it in the same commit as any API change.

**Regenerate against Postgres, not the SQLite fallback.** drf-spectacular
derives each integer field's OpenAPI `maximum`/`minimum`/`format` from the
database column type Django would actually create --
`PositiveSmallIntegerField`/`PositiveIntegerField` map to real fixed-width
`smallint`/`integer` columns on Postgres, but SQLite has no such native
types, so the same fields introspect as unbounded 64-bit integers instead.
A `schema.yml` regenerated locally against the SQLite fallback will pass
`manage.py spectacular --fail-on-warn` cleanly but still fail CI's "API
schema is current" diff, because CI regenerates against the real Postgres
service container. If you don't have Postgres available locally, treat
`manage.py spectacular`'s local output as a syntax/warning check only, and
verify the actual committed diff against a real CI run before assuming it's
correct.

Interactive API docs run at `/api/v1/docs/` once the server is up.

## Layout

```
config/          settings (base/local/production/test), urls, celery
educore/
  core/          tenancy, identity, RBAC, audit chain, outbox
  academics/     years, terms, levels, class groups, courses
  timetable/     period grids, scheduled lessons, lesson instances
  presence/      staff attendance, signals, confidence
  delivery/      lesson sessions, coverage
  movement/      boarding pass-outs, trips, off-campus tracking
  students/      students, guardians, registers, gate events
  assessment/    assessments, scores, moderation, report cards
  comms/         announcements, threads, deliveries
  insights/      read models, dashboards
  platform/      operator portal, subscriptions
tests/           cross-cutting suites, incl. the tenant isolation battery
docs/            design record and ADRs
```

Modules are layered and the layering is enforced in CI. A module may import
from those below it and never sideways or above; cross-module writes go
through domain events on the outbox.

## Working on this

Every tenant-owned model must inherit `TenantOwnedModel` **and** declare
`class Meta(TenantOwnedModel.Meta)`. Django does not merge a fresh child `Meta`
with its parent's, so a plain `class Meta:` silently drops `base_manager_name`.
The introspective test in `tests/test_tenant_isolation.py` fails the build if
you forget — it is meant to.
