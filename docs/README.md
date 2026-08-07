# eduCore — System Design

School operations and accountability platform. Multi-tenant SaaS.

This directory is the authoritative design record. Code follows these documents;
where they disagree, either the code is wrong or the document needs an update —
never leave the two silently diverged.

## Documents

| # | Document | Purpose |
|---|----------|---------|
| 01 | [Product Overview](01-product-overview.md) | Problem, thesis, personas, scope, non-goals |
| 02 | [Architecture](02-architecture.md) | Components, tenancy, deployment, runtime |
| 03 | [Domain Model](03-domain-model.md) | Entities, relationships, invariants, state machines |
| 04 | [Attendance & Verification](04-attendance-verification.md) | The evidence model — core differentiator |
| 05 | [API Design](05-api-design.md) | Conventions, resources, offline sync protocol |
| 06 | [Security & Compliance](06-security-compliance.md) | AuthN/Z, tenant isolation, data protection |
| 07 | [Delivery Plan](07-delivery-plan.md) | Phases with exit criteria, team shape, risks |

## Architecture Decision Records

ADRs are immutable once accepted. To change a decision, write a new ADR that
supersedes the old one — do not edit history.

| ADR | Decision | Status |
|-----|----------|--------|
| [0001](adr/0001-tenancy-model.md) | Shared schema with row-level tenant isolation | Accepted |
| [0002](adr/0002-attendance-confidence-model.md) | Attendance as confidence score, not boolean gate | Accepted |
| [0003](adr/0003-offline-first-capture.md) | Offline-first capture with idempotent sync | Accepted |
| [0004](adr/0004-biometrics-opt-in.md) | Biometrics opt-in, template-only, never sole gate | Accepted |
| [0005](adr/0005-modular-monolith.md) | Modular monolith over microservices | Accepted |
| [0006](adr/0006-api-style.md) | Versioned REST + OpenAPI 3.1 | Accepted |

## Glossary (ubiquitous language)

Use these terms exactly — in code, in the API, in the UI, in conversation.

| Term | Meaning |
|------|---------|
| **School** | The tenant. Top-level isolation boundary. |
| **Campus** | A physical site belonging to a School. Owns the geofence. |
| **Membership** | A User's role-bearing link to one School. A User may hold several. |
| **Class Group** | A persistent group of students (stream / form class), e.g. "S4 Blue". |
| **Course** | A Subject taught to a Level in an Academic Year. |
| **Scheduled Lesson** | A recurring timetable entry (template). Has no date. |
| **Lesson Instance** | One scheduled lesson on one date. The unit attendance attaches to. |
| **Lesson Session** | The *actual delivery* of a Lesson Instance. May be absent (lesson missed). |
| **Signal** | One piece of evidence about presence (GPS, Wi-Fi, QR, device, …). |
| **Confidence** | Weighted score derived from Signals, in `[0, 100]`. |
| **Coverage** | Proportion of a scheme of work delivered, weighted by planned periods. |
| **Pace** | Coverage measured against what *should* be covered by today's date. |

## Conventions

- Times stored UTC, rendered in the School's timezone. Never store naive datetimes.
- Money in minor units as integers. Never floats.
- All identifiers UUIDv7 (time-ordered, index-friendly, safe to generate client-side).
- `snake_case` in the database and API, `camelCase` nowhere.
