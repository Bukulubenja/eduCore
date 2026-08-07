# 05 — API Design

Versioned REST over JSON. Contract published as OpenAPI 3.1, generated from code
by drf-spectacular. See [ADR-0006](adr/0006-api-style.md).

## Conventions

| Aspect | Rule |
|---|---|
| Base path | `/api/v1/` — version in the path, never a header |
| Naming | Plural nouns, `snake_case` fields, kebab-case paths |
| Identifiers | UUIDv7 everywhere. No sequential integers in the API surface |
| Times | RFC 3339 with offset, always UTC on the wire: `2026-08-04T06:12:33Z` |
| Errors | RFC 9457 `application/problem+json` |
| Pagination | Cursor-based. Offset pagination is not offered |
| Filtering | Explicit whitelisted query params. No generic query language |
| Partial update | `PATCH` with a partial object. `PUT` is not supported |
| Mutations | Non-CRUD transitions are sub-resources, not verbs in the path |

### Tenant scoping

The tenant is **never** a URL parameter or a client-supplied header. It is
derived from the access token's Membership claim. A client cannot ask for another
school's data because it has no way to express the request.

Users with several Memberships receive one token per active Membership and
switch explicitly via `POST /v1/auth/switch-school`.

### Errors

```json
{
  "type": "https://docs.educore.app/errors/lesson-window-closed",
  "title": "Lesson window has closed",
  "status": 409,
  "detail": "Lesson instance ended at 10:40; grace period of 10 minutes expired.",
  "instance": "/api/v1/lesson-sessions",
  "request_id": "01J8F2M4Q7ZK5N8P3R6T9V2X4B",
  "errors": [
    { "field": "lesson_instance_id", "code": "window_closed" }
  ]
}
```

`type` is a stable, documented URI. Clients branch on `type` and `errors[].code`,
never on `title` or `detail`, which are human-facing and may be translated.

### Pagination

```
GET /api/v1/students?class_group_id=…&limit=50&cursor=eyJpZCI6…
```

```json
{
  "results": [ … ],
  "next_cursor": "eyJpZCI6IjAxSjhGMk00…",
  "has_more": true
}
```

No total count by default — counting a partitioned multi-million-row table on
every page request is a self-inflicted outage. `?include_count=true` is available
on endpoints where it is bounded and genuinely needed.

## Authentication

- OAuth 2.0 password grant equivalent, issuing JWT access tokens (15 min) and
  opaque refresh tokens (30 days, rotating, single-use).
- Refresh reuse detection: presenting a consumed refresh token revokes the whole
  family and raises a security event. This is the defence against a stolen token
  that has already been rotated.
- Access token claims: `sub` (user), `mbr` (membership), `sch` (school),
  `rls` (role codes), `dev` (device), `jti`, `exp`.
- Roles are claims for *routing and UI*; every authorisation decision is
  re-evaluated server-side against the database. A claim is a hint, never a grant.
- Step-up MFA required for: mark release, role assignment, bulk export, device
  approval, payment configuration.

## Resource map (v1)

Abbreviated. The generated OpenAPI document is authoritative.

### Identity

```
POST   /v1/auth/token                    obtain tokens
POST   /v1/auth/refresh                  rotate
POST   /v1/auth/switch-school            re-scope to another membership
POST   /v1/auth/mfa/challenge            step-up
GET    /v1/me                            profile, memberships, permissions
GET    /v1/me/devices
POST   /v1/me/devices                    register (requires approval)
DELETE /v1/me/devices/{id}
```

### Academic structure

```
GET    /v1/academic-years
GET    /v1/terms  ?status=current
GET    /v1/levels
GET    /v1/class-groups  ?level_id=
GET    /v1/subjects
GET    /v1/courses  ?level_id=&term_id=
GET    /v1/students  ?class_group_id=&status=&q=
POST   /v1/students
GET    /v1/students/{id}
PATCH  /v1/students/{id}
GET    /v1/students/{id}/guardians
GET    /v1/schemes-of-work/{course_id}  ?term_id=
```

### Timetable

```
GET    /v1/timetable-versions
POST   /v1/timetable-versions/{id}/publish
GET    /v1/scheduled-lessons  ?version_id=&teacher_id=&class_group_id=
GET    /v1/lesson-instances   ?date=&teacher_id=&class_group_id=&status=
GET    /v1/me/timetable       ?date=          the teacher's day
```

### Presence

```
POST   /v1/attendance/check-in           idempotent, signal-bearing
POST   /v1/attendance/check-out
GET    /v1/attendance/qr-token           rotating token for a display device
GET    /v1/attendance/records            ?membership_id=&from=&to=
GET    /v1/me/attendance                 ?from=&to=   own history + evidence
POST   /v1/attendance/records/{id}/appeal
GET    /v1/attendance/reviews            supervisor queue
POST   /v1/attendance/reviews/{id}/decide
```

### Lesson delivery

```
POST   /v1/lesson-sessions               open (scan)
POST   /v1/lesson-sessions/{id}/close    requires coverage entries
GET    /v1/lesson-sessions  ?date=&teacher_id=
POST   /v1/substitutions
GET    /v1/coverage  ?course_id=&term_id=      coverage + pace
```

### Student attendance

```
GET    /v1/lesson-sessions/{id}/register       pre-marked roster
PUT    /v1/lesson-sessions/{id}/register       submit exceptions only
POST   /v1/gate-events                         batch, from gate devices
GET    /v1/students/{id}/attendance  ?from=&to=
```

### Assessment

```
GET    /v1/assessments  ?course_id=&term_id=&state=
POST   /v1/assessments
POST   /v1/assessments/{id}/submit
POST   /v1/assessments/{id}/approve
POST   /v1/assessments/{id}/lock
PUT    /v1/assessments/{id}/scores             bulk upsert
POST   /v1/assessments/{id}/moderate
POST   /v1/assessments/{id}/release            step-up MFA
GET    /v1/report-cards  ?student_id=&term_id=
```

### Insights

```
GET    /v1/insights/today                      director dashboard payload
GET    /v1/insights/coverage      ?department_id=&term_id=
GET    /v1/insights/punctuality   ?from=&to=
GET    /v1/insights/at-risk-students  ?term_id=
```

### Sync

```
POST   /v1/sync                                batched offline upload
GET    /v1/sync/bootstrap  ?since=             delta pull for device cache
```

## Offline sync protocol

Non-negotiable for this market. Full rationale in
[ADR-0003](adr/0003-offline-first-capture.md).

### Upload

```http
POST /api/v1/sync
Idempotency-Key: 01J8F2M4Q7ZK5N8P3R6T9V2X4B
Content-Type: application/json
```

```json
{
  "device_id": "01J8F2M4Q7ZK5N8P3R6T9V2X40",
  "device_time": "2026-08-04T06:12:33Z",
  "operations": [
    {
      "client_event_id": "01J8F2M4Q7ZK5N8P3R6T9V2X41",
      "type": "attendance.check_in",
      "captured_at": "2026-08-04T05:58:02Z",
      "payload": {
        "campus_id": "01J8…",
        "signals": [
          { "type": "qr",       "token": "eyJhbGciOi…" },
          { "type": "geofence", "lat": 0.3136, "lon": 32.5811, "accuracy_m": 12 },
          { "type": "wifi",     "bssid_hash": "b1946ac9…" },
          { "type": "device",   "fingerprint": "…", "attestation": "…" }
        ]
      }
    },
    {
      "client_event_id": "01J8F2M4Q7ZK5N8P3R6T9V2X42",
      "type": "lesson.close",
      "captured_at": "2026-08-04T07:38:10Z",
      "payload": {
        "session_id": "01J8…",
        "coverage": [ { "syllabus_unit_id": "01J8…", "completion": "completed" } ]
      }
    }
  ]
}
```

Response is **per operation** — a partial failure must never fail the batch:

```json
{
  "server_time": "2026-08-04T08:02:11Z",
  "clock_skew_seconds": 4,
  "results": [
    { "client_event_id": "…X41", "status": "accepted",
      "resource": { "type": "attendance_record", "id": "01J8…",
                    "disposition": "verified", "confidence": 88 } },
    { "client_event_id": "…X42", "status": "rejected",
      "error": { "type": "…/coverage-unit-not-in-scheme", "code": "invalid_unit" } }
  ]
}
```

### Rules

1. **Client-generated IDs.** UUIDv7 assigned on the device. Records are usable
   locally before the server has ever seen them.
2. **Idempotent absorption.** Re-sending a `client_event_id` returns the original
   result. Flaky networks retry constantly; duplicates must be free.
3. **Two clocks, one trusted.** `captured_at` is the device's claim,
   `received_at` is authoritative. Skew is measured, returned to the client for
   correction, and beyond 5 minutes becomes an attendance signal in its own right.
4. **Facts sync up, derivations sync down.** The device uploads what it observed;
   it never computes a confidence score or an attendance status. Those are
   server-side so that policy changes apply retroactively and consistently.
5. **Bounded queue.** 14 days or 5,000 operations, whichever comes first, then
   oldest-first eviction with a loud in-app warning. Unbounded offline queues
   silently rot.
6. **Bootstrap pull.** `GET /v1/sync/bootstrap?since=` returns the teacher's
   timetable, class rosters, and scheme of work as a delta — everything needed to
   run a full day with no connectivity.

## Rate limiting

Per Membership and per IP, enforced at the gateway with Redis token buckets.

| Endpoint class | Limit |
|---|---|
| `POST /v1/auth/token` | 5 / 15 min per identifier, then exponential backoff |
| `POST /v1/sync` | 60 / min per device |
| Read endpoints | 600 / min per membership |
| Report generation | 10 / hour per membership |
| Bulk export | 3 / day per membership, MFA required, audited |

Per-tenant global ceilings prevent one school degrading another — the principal
cost of a shared-schema deployment, and the price of admission for its benefits.

## Versioning and deprecation

- Additive changes ship in `v1` without notice. Clients must ignore unknown fields.
- Breaking changes require `v2` running alongside `v1`.
- Minimum 6 months' deprecation notice; `Sunset` and `Deprecation` headers on
  every response from a deprecated version.
- Mobile clients send `X-Client-Version`; the server can refuse versions below a
  configured floor with a forced-upgrade problem response. Never silently break
  an app that a teacher depends on to start their day.
