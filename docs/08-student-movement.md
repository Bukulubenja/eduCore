# 08 — Student Movement

> Authoritative for the `movement` module. See [ADR-0007](adr/0007-student-movement.md)
> for why this is in scope, and [doc 04](04-attendance-verification.md) for the
> presence model this sits beside.

## The record we are keeping

A lesson register says a child was not in class. It does not say the child left
campus at 14:10 with their aunt, that the bursar approved it, that a text went
to the mother, and that they are due back at 18:00. For a boarding school the
second record is the one that matters, and nobody was keeping it.

**Student Movement Management** is the record of every *authorised* absence
from campus:

| Movement | Shape |
|---|---|
| **Pass-out** | one boarder, one reason, out and back |
| **Trip** | a roster of students, supervised, out and back together |

Ordinary lesson-by-lesson attendance stays where it is, in `students`. This
module is only about students who are legitimately *off site*.

## Principles (inherited from doc 01)

- **Evidence over assertion.** Every transition records who did it and when, in
  the audit chain.
- **Immutable facts, derived views.** The gate scan is the fact
  (`students.GateEvent`, append-only). A pass-out's `status` is a workflow
  aggregate, not a fact table — but "overdue" is never stored; it is computed
  from `expected_return_at` every time it is read.
- **The roster comes from the database.** A trip list is built by selecting
  enrolled students, not by typing names. That is what connects it to
  attendance, guardian notification and reporting (concept doc §9).

## Pass-out state machine

```
requested ──approve──▶ approved ──depart──▶ departed ──return──▶ returned
    │                     │
   deny                 cancel
    ▼                     ▼
  denied              cancelled
```

| Transition | Who | Emits (→ recipients) |
|---|---|---|
| `request_pass_out` | office / class teacher | `movement.pass_out.requested` → leadership |
| `decide_pass_out(approved=True)` | leadership **or bursar** | `movement.pass_out.approved` → requester + verified guardians |
| `decide_pass_out(approved=False)` | leadership or bursar | `movement.pass_out.denied` → requester |
| `record_departure` | gate / office | `movement.pass_out.departed` → guardians |
| `record_return` | gate / office | `movement.pass_out.returned` → guardians |
| `cancel_pass_out` | any staff (pre-departure) | — |
| *sweep* while `departed` and past `expected_return_at` | `sweep_movement` job | `movement.pass_out.overdue` → leadership + guardians |

**Invariant:** at most one pass-out per student in an open state
(`requested`, `approved`, `departed`) — a partial unique constraint, not a
service check that can be raced.

The bursar approves pass-outs (concept doc §8). Trips are leadership-only.

## Trip state machine

```
draft ──submit──▶ submitted ──approve──▶ approved ──depart──▶ departed ──return──▶ returned
  │                   │                     │
  └──── cancel ───────┴────── cancel ───────┘        reject ▼
                                                    rejected
```

| Transition | Who | Notes |
|---|---|---|
| `create_trip` | any staff | starts in `draft` |
| `set_trip_roster` | creator | replace participants + supervisors; **only while `draft` or `submitted`**; every id validated against this tenant's active records |
| `submit_trip` | creator | requires ≥1 participant and ≥1 supervisor; → `movement.trip.submitted` (leadership) |
| `decide_trip(approved=True)` | leadership | **roster is frozen**; → `movement.trip.approved` (guardians of participants) |
| `decide_trip(approved=False)` | leadership | → `movement.trip.rejected` (creator) |
| `record_trip_departure` | supervisor | stamps `departure_recorded_at` per present participant; → `movement.trip.departed` (guardians) |
| `record_trip_return` | supervisor | stamps `return_recorded_at`; a participant left un-stamped is surfaced as unreturned; → `movement.trip.returned` |
| *sweep* while `departed` past `returns_at` | `sweep_movement` job | `movement.trip.overdue` → leadership |

## Notifications

Every event above is an `OutboxMessage`. `comms` subscribers turn them into
notifications. Guardian recipients are resolved **in `movement.services`**,
filtered for `GuardianLink.verified` and `receives_notifications` — deciding who
may be told about a child is this module's business, not `comms`'s (doc 03).
Bodies never carry the destination or reason: a lock-screen preview is not a
private channel.

Dedupe keys carry the record id, so the overdue sweep can run every ten minutes
and a guardian still gets one message per overdue pass-out, not a stream.

## Read models

- **`students_off_campus()`** — the union of departed-not-returned pass-outs and
  participants on a departed trip. Two queries, independent of volume.
- **`insights.today()["movement"]`** — students off campus, open pass-outs,
  trips out, overdue returns. The "students currently out" line on the
  director's 09:30 dashboard (concept doc §15).

## Check-in reminder ladder (presence, not movement)

The concept document's §6 reminder engine has two halves. eduCore already had
the escalation half — `alert_staff_absences` tells leadership about a no-show
once the grace window is spent. This adds the teacher-facing half:

| Stage | Window | Channel |
|---|---|---|
| `opening_soon` | `[day_start − reminder_lead, day_start)` | push + in-app, routine |
| `due` | `[day_start, day_start + late_grace)` | push + in-app, routine |
| `late` | `[day_start + late_grace, + escalation grace)` | push + in-app, important |

`reminder_lead` is `AttendancePolicy.checkin_reminder_lead_minutes` (default
45). After the `late` window, `alert_staff_absences` takes over. The ladder
skips anyone already checked in, on approved leave for the day, off a
`DutySchedule` weekday, or on a school-closed day. Nothing is stored — the
stage is recomputed each run from the roster, the policy and the day's events,
and `comms` dedupes on `(member, date, stage)`.

This is a notification concern. It adds no new `AttendanceStatus` value —
ADR-0002's confidence model stands.

## Boundaries

- `movement` imports `students`, `academics`, `core`. Nothing in the
  `assessment | delivery | presence` tier imports it. `insights` may read it.
- `movement` does **not** write `students.StudentAttendance`. A trip that
  removes students from lessons is reconciled by `students`/`insights` reading
  the movement records, not by `movement` reaching sideways.
- Staff off-site duty for trip supervisors is **not** wired into
  `presence.AttendanceException` yet. Supervisors are recorded on the trip; a
  future change may raise an off-site-duty exception automatically.
