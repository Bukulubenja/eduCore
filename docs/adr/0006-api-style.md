# ADR-0006 — Versioned REST with generated OpenAPI 3.1

- **Status:** Accepted
- **Date:** 2026-08-04

## Context

Three client types consume the API: a Flutter mobile app with offline sync, a
Next.js web console, and an operator portal. Third-party integrations (SMS
gateways, ministry reporting, accounting) are expected later.

## Decision

**REST over JSON, version in the URL path (`/api/v1/`), contract published as
OpenAPI 3.1 generated from code** by drf-spectacular. Errors follow RFC 9457
(`application/problem+json`). Pagination is cursor-based.

## Rationale

**REST over GraphQL.** GraphQL solves over-fetching across many client shapes.
That is not our problem — we have three known clients and can design endpoints
for them. What GraphQL brings instead is per-query cost unpredictability, which
is genuinely dangerous in a shared-schema multi-tenant system where one school's
deeply nested query becomes another school's latency. Caching, rate limiting, and
audit logging are all harder per-field than per-endpoint. Rejected on cost
control, not on fashion.

**Version in the path, not a header.** Visible in logs, in error reports, in
support conversations, and trivially routable at the load balancer. Header
versioning is more architecturally pure and worse in every operational moment
that matters.

**Generated, not hand-written, OpenAPI.** A hand-written specification is wrong
within a month and is then actively harmful — clients trust it. Generation from
serializers keeps it true, and a CI check fails the build if the committed schema
differs from the generated one.

**Cursor pagination only.** Offset pagination on a partitioned, multi-million-row
attendance table degrades sharply with depth and produces duplicates and gaps
when rows are inserted during iteration — which, for attendance, is continuously.
Totals are omitted by default; `?include_count=true` exists only where the count
is bounded.

**RFC 9457 problem details.** A single machine-readable error shape across every
endpoint. Clients branch on the stable `type` URI and `errors[].code`, never on
prose that may be translated.

## Conventions

| Rule | Detail |
|---|---|
| Identifiers | UUIDv7 in every public surface. No sequential integers — they leak tenant volume and invite enumeration |
| Tenant scope | Derived from the token's Membership claim. Never a path parameter, never a client header |
| Times | RFC 3339, UTC on the wire, rendered in school timezone by clients |
| Field names | `snake_case`, matching the database and the domain language |
| Transitions | Sub-resources (`POST /assessments/{id}/release`), not verbs in paths |
| Partial update | `PATCH` only. `PUT` invites accidental field clearing |
| Idempotency | `Idempotency-Key` supported on all writes, required on `/v1/sync` |
| Unknown fields | Clients must ignore them; this is what makes additive change safe |

## Deprecation policy

- Additive changes ship within `v1` without notice.
- Breaking changes require `v2` alongside `v1`, minimum 6 months' overlap.
- `Deprecation` and `Sunset` headers on every response from a deprecated version.
- Mobile clients send `X-Client-Version`; the server may refuse versions below a
  configured floor with a forced-upgrade problem response — but never silently.
  A teacher whose app stops working at 07:00 without explanation is a support
  incident and a trust loss.

## Consequences

- Endpoint count grows with client needs; accepted, and cheap.
- The generated schema is the contract; the SDK for Flutter is generated from it,
  eliminating a class of client/server drift.
- Every endpoint must declare its permissions, its rate-limit class, and its
  audit behaviour in the view — checked by a CI lint, so none can be omitted.
- Introducing GraphQL later remains possible as an additive gateway if a partner
  integration genuinely demands it. Nothing here forecloses that.
