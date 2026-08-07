# ADR-0005 — Modular monolith over microservices

- **Status:** Accepted
- **Date:** 2026-08-04

## Context

The brief listed Kubernetes, Prometheus, Grafana, and a broad service inventory,
implying a distributed architecture. The team is small (2 backend engineers at
Phase 1), the domain boundaries are not yet proven by usage, and the product has
zero production tenants.

## Decision

A **modular monolith**: one Django deployable with enforced internal module
boundaries, plus Celery workers for asynchronous work. Extraction to separate
services is deferred until a specific, evidenced need appears.

## Rationale

Microservice boundaries drawn before the domain is understood in production are
almost always wrong, and a wrong boundary in a distributed system is far more
expensive to move than a wrong boundary inside a monolith. Inside one codebase, a
misplaced boundary is a refactor; across services it is a migration, a versioned
contract, a data move, and a coordinated deployment.

The specific costs at this stage:

- Distributed transactions where a single database transaction would do. The
  lesson-session close writes a session, coverage entries, a student register,
  and a domain event. In one database that is atomic. Across four services it is
  a saga with compensating actions — and a partially applied lesson record is
  exactly the kind of corruption this product cannot tolerate.
- Cross-service authorisation and tenant-context propagation, multiplied by the
  service count, each an opportunity for a leak.
- Local development requiring the whole estate running.
- Debugging that needs distributed tracing to answer questions a stack trace
  answered before.

None of that buys anything at our scale. Independent deployability and
independent scaling are real benefits — of no value with two backend engineers
and uniform load.

Enforced module boundaries capture most of the organisational benefit at none of
the operational cost. If extraction is needed later, a module with clean
boundaries is genuinely extractable; the discipline is the prerequisite either way.

## Enforcement

Boundaries are checked mechanically, because conventions that are not checked
decay. `import-linter` runs in CI with a layered contract:

```
platform → insights → assessment, delivery, presence, students
                          → timetable → academics → core
```

Rules:

- A module may import from layers strictly below it, never above or sideways.
- Cross-module reads go through a published service function, never another
  module's models or queryset internals.
- Cross-module writes are asynchronous: emit a domain event via the transactional
  outbox; the consuming module subscribes.
- `comms` is a leaf. It consumes events and depends only on `core`. Nothing
  imports it, so notification concerns cannot spread through the domain.
- Each module owns its tables. No other module writes them.

A violation fails the build. This is the whole mechanism by which "modular" stays
true rather than becoming an aspiration in a README.

## Scaling path, in order

1. Vertical scaling and query optimisation. Almost always sufficient longest.
2. Read replicas for dashboards and reports.
3. Separate Celery queues and worker pools by workload class — already in the
   design (`default`, `notify`, `reports`).
4. Split the *deployment* by workload, not by domain: same image, different
   process roles (API, sync-ingest, workers). Lets us scale the check-in rush
   independently without splitting the codebase.
5. Only then extract a module into a service — and only one with a demonstrated,
   measured need, a stable interface, and low transactional coupling.

Step 4 is the one that gets skipped. It handles most real scaling pressure and
costs a deployment configuration rather than an architecture.

## Consequences

- Faster delivery, simpler local setup, atomic transactions across the domain.
- One deployment unit: a bad release affects everything, so blue/green,
  automated rollback, and feature flags are mandatory rather than nice to have.
- Module discipline must be enforced from the first commit. Retrofitting
  boundaries onto a tangled monolith is the expensive failure this ADR exists to
  prevent, and it is the failure mode people cite when they say monoliths do not
  work.
- Kubernetes is deferred until we exceed roughly 40 schools or three deployable
  units. Before that it costs more operational attention than it returns.

## Revisit if

- A module develops genuinely different scaling characteristics (sync ingest is
  the likely first candidate).
- Team size passes ~8 backend engineers and merge contention is measurable.
- A compliance requirement demands physical separation of a data domain.
