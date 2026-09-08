# ADR-0007 — Student movement is a first-class domain

**Status:** Accepted
**Date:** 2026-09-08
**Supersedes:** nothing
**Context source:** `SSOMS_Professional_System_Documentation_v2.pdf`, the product
concept this system grew from.

## Context

The founding concept document frames the product as a *"Smart School Operations
and Student Movement Platform"* — and treats **student movement** (boarding
pass-outs, trips, competitions, festivals) as the differentiator, not
attendance alone. The question it puts at the centre is:

> not only "who is absent?" but "who is outside school, why, who approved it,
> who is supervising them, and when are they expected back?"

eduCore already answers the presence half of that well. It had no model for the
movement half. `docs/01` listed neither boarding pass-outs nor trips in scope
*or* out of scope — a silent gap, not a deliberate exclusion.

Two facts made this worth closing now rather than deferring:

1. For a boarding school, an unaccounted student who has left campus is a
   safeguarding incident. A system that records lesson registers but cannot say
   who signed a child out is missing the higher-stakes record.
2. Trips are already a paper *list-and-approval* process in every school. The
   concept document's insight — build the roster *from the student database* so
   it connects to attendance, notification and reporting — is exactly the kind
   of evidence-chain work this product exists to do.

## Decision

**Add a `movement` domain module** covering two workflows:

- **Pass-out** — one boarder authorised to leave for a named reason and return.
- **Trip** — a roster of students taken out together under supervision.

`movement` sits alongside `assessment | delivery | presence` in the module
layering (ADR-0005): a capability built on `students`, imported by nobody in
that tier, readable by `insights` above it. Guardian and leadership
notifications go through the transactional outbox to `comms`, like every other
domain event.

Both workflows are **approval aggregates with a mutable `status`**, following
the `presence.AttendanceException` and `delivery.Substitution` precedent. The
append-only rule (doc 01, principle 5) governs *signal* tables —
`students.GateEvent`, `core.AuditEvent` — not workflow state. Every transition
is written to the audit chain; "overdue" is computed from
`expected_return_at`, never stored as a flag.

We also add the **teacher-facing check-in reminder ladder** the concept
document describes (§6): a staged nudge — *opening soon → due → late* — sent to
the member of staff before the existing `alert_staff_absences` escalation
reaches leadership. This is a `comms` concern layered on `presence`, not a new
attendance status: `AttendanceStatus` stays as ADR-0002 defined it.

## What we are *not* adopting from the concept document

The concept document's later phases propose **predictive analytics, anomaly
detection, and AI-assisted management reports**. `docs/01` lists these as a
permanent non-goal, and `docs/07` requires two full years of clean data before
any predictive feature. That position stands. This ADR adds the movement
*records*; it does not add inference on top of them.

Also unchanged: **no native mobile app** (root `CLAUDE.md` — the client is a
web console plus an installable PWA), and **no visitor management, bus
tracking, or emergency roll-call** (doc 01 out-of-scope, alongside library and
transport).

## Consequences

- New `educore/movement/` app; new `movement_*` tables under RLS with composite
  foreign keys; `pyproject.toml` layering contract widened to include it.
- `docs/01` scope list gains two bullets; `docs/07` gains a movement phase.
- `insights.today()` gains a movement block — students off campus, open
  pass-outs, overdue returns — for the director's 09:30 dashboard.
- Two new scheduled jobs: `sweep_movement` (overdue sweep) and
  `remind_staff_checkins` (the reminder ladder).
- The `students` module is untouched: it still owns *who is in the school*;
  `movement` owns *where an authorised student currently is*.
