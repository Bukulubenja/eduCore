# eduCore

Multi-tenant school-operations SaaS. Modular monolith: Django 5 + DRF backend
(`educore/`), Next.js console (`console/`). No native mobile app — client
strategy is web-only, including an installable offline-capable PWA for staff
check-in/out (superseding the earlier Flutter plan referenced in
`docs/07-delivery-plan.md`; that doc's "Mobile app" bullets should be read as
"PWA" going forward).
Design is authoritative in [`docs/`](docs/README.md) — read it before making
architectural changes, especially [ADR-0001 tenancy](docs/adr/0001-tenancy-model.md)
and [ADR-0005 modular monolith](docs/adr/0005-modular-monolith.md).

Module layering (`platform → insights → {assessment,delivery,presence} →
students → timetable → academics → core`) is enforced by `lint-imports` in CI.
Never import sideways or upward between `educore/<app>` packages — go through
service functions or the outbox instead.

## Skills to reach for

**Backend / Django** — use for any change under `educore/` or `config/`:
- `django-patterns` — app structure, DRF, ORM, caching, signals, middleware
- `django-security` — auth, CSRF, SQLi/XSS prevention, secure deploy config
- `django-tdd` — pytest-django, factory_boy, TDD workflow
- `django-celery` — Celery task/beat patterns (this repo has `CELERY_BEAT_SCHEDULE`
  jobs: `relay_outbox`, `roll_timetable`, `verify_audit_chains`, `estate_report`)
- `django-verification` — pre-merge check: migrations, lint, tests+coverage,
  security scan (mirrors the `## Checks` section in README.md)

**Data layer** — this project runs Postgres 16 with row-level security and
range-partitioned hot tables (`attendance_event`, `audit_event`, etc.):
- `postgres-patterns` — schema/query design, RLS, partitioning
- `database-migrations` — expand/migrate/contract, lock-safe migrations

**Web console** (`console/`, Next.js 16 + TypeScript + Tailwind):
- `nextjs-turbopack`, `react-patterns`, `react-performance`, `react-testing`
- `frontend-patterns`, `frontend-a11y`

**API contract** — `schema.yml` is committed and CI fails on drift; regenerate
with `manage.py spectacular` in the same commit as any API change:
- `api-design`

**Cross-cutting**:
- `security-review` — before any commit touching auth, tenancy, or user input
- `code-review` — after writing or modifying code
- `graphify` — codebase/architecture questions when a graph query beats manual search
- `git-workflow`, `e2e-testing`, `deployment-patterns` as relevant

## Non-negotiables from the design docs

- Every tenant-owned model inherits `TenantOwnedModel` **and** declares
  `class Meta(TenantOwnedModel.Meta)` — a bare `class Meta:` silently drops
  `base_manager_name`. `tests/test_tenant_isolation.py` enforces this.
- RLS (isolation layer 3) is the layer that must never be bypassed or disabled;
  layers 1–2 (middleware, `TenantManager`) are defense in depth, not the
  guarantee.
- Raw signal/event tables are append-only; everything else is a derived,
  recomputable read model. Don't add mutable summary fields as a shortcut.
