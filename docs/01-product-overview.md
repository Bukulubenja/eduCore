# 01 — Product Overview

## The problem

Schools do not lack records. They lack *trustworthy* records.

A head teacher can produce an attendance register for any day last term and still
be unable to answer: did that lesson actually happen, who taught it, what was
covered, and how far behind is that class now. The register is a signature on
paper. It is evidence of a signature, not evidence of teaching.

Every downstream failure follows from that gap:

| Observed failure | Underlying cause |
|---|---|
| Teacher signs in, leaves campus | Presence recorded at a single moment, unverified |
| Teacher present but not in class | Nothing links presence to lesson delivery |
| Syllabus incomplete, discovered at exams | Coverage measured retrospectively, by hand |
| Marks disputes, altered scores | No immutable record of who changed what |
| Parents uninformed until visitation day | Communication lives in ungoverned WhatsApp groups |
| Departments underperform unnoticed | No comparable metrics across staff or subjects |

## Product thesis

> eduCore produces a **verifiable chain of evidence for the school day**, and makes
> that evidence legible to the people accountable for it.

Presence → lesson delivery → curriculum coverage → student attendance →
assessment → parent notification. Each link is captured at the moment it happens,
carries the evidence that supports it, and is immutable afterwards.

This is the product. Student registration, fee collection, and report cards are
table stakes we must also do well — they are not why anyone switches to us.

### What this is not

We are not a Student Information System competing on feature count. There are
thousands, they are cheap, and we will lose that fight. We compete on whether a
director of studies can trust the number on the screen.

## Design principles

1. **Evidence over assertion.** Every consequential record stores what supported it.
2. **Degrade, don't block.** A failed sensor must never prevent a teacher from
   teaching. It lowers confidence and raises a review — it does not lock a door.
   (See [ADR-0002](adr/0002-attendance-confidence-model.md).)
3. **The network is optional.** Capture works offline; sync is a background
   concern. (See [ADR-0003](adr/0003-offline-first-capture.md).)
4. **Isolation is structural, not disciplinary.** Cross-tenant leakage must be
   prevented by the database, not by remembering to filter.
   (See [ADR-0001](adr/0001-tenancy-model.md).)
5. **Immutable facts, derived views.** Raw events are append-only. Everything a
   dashboard shows is derived and recomputable from those events.
6. **Accountability cuts both ways.** Staff are measured; so is the system.
   A teacher can see and dispute every record held about them.

Principle 6 is not decoration. A surveillance tool that staff experience as
one-directional will be sabotaged, and the data will become worthless. Visible
dispute rights are what make the measurements survive contact with a staff room.

## Personas and jobs

| Persona | Primary job | Success looks like |
|---|---|---|
| **Platform Operator** (us) | Run many schools safely | Onboard a school in under a day; no cross-tenant incident, ever |
| **School Director / Owner** | Know the school is running | One dashboard answers "is today going well?" |
| **Head Teacher** | Act on problems early | Alerted to a pattern in week 3, not at exams |
| **Director of Studies (DOS)** | Curriculum delivered on time | Sees pace per class and per teacher, live |
| **Head of Department** | Staff performing | Comparable metrics within department |
| **Bursar** | Money reconciled | Every payment traceable to a receipt and a student |
| **Teacher** | Teach with minimal admin | Check-in and lesson log take under 20 seconds combined |
| **Parent** | Know how my child is doing | Push notification the day something happens |
| **Student** | Know what is expected | Timetable, homework, results in one place |

The teacher's 20 seconds is a hard requirement, not an aspiration. Every
verification layer we add spends part of that budget. If the daily ritual is
slow, staff will find ways around it and the evidence chain breaks at its first
link.

## Scope

### In scope — v1.0

- Multi-tenant platform with operator portal
- Identity, membership, role-based access control
- Academic structure: years, terms, levels, class groups, subjects, courses
- Timetable authoring and versioning
- Staff attendance with layered verification and exception handling
- Lesson delivery verification and substitution handling
- Curriculum coverage and pace tracking
- Student attendance (per-lesson and gate/day-scholar)
- Assessment lifecycle through to released report cards
- Announcements and notifications (push, SMS fallback, email)
- Dashboards for director, head, DOS, HOD
- Immutable audit trail
- Android and iOS app with offline capture

### Explicitly out of scope — v1.0

Deferred deliberately. Each has a real cost and none is load-bearing for the thesis.

- Finance, fees, payroll, procurement — large domain, different buyer, slower sales cycle
- Library, hostel, transport, inventory
- Learning content delivery / LMS (assignments yes, courseware no)
- Face recognition and liveness (see [ADR-0004](adr/0004-biometrics-opt-in.md))
- Predictive AI and machine learning features
- Real-time chat (announcements and threads only — chat is a moderation liability)
- Public parent-facing web portal (mobile app first)

### Non-goals — permanent

- Being cheapest.
- Supporting every national curriculum out of the box. We support a configurable
  grading and curriculum model; each new country is a deliberate investment.
- Offline-only deployments with no eventual connectivity. We require the school
  to sync at least daily.

## Success metrics

Product is working when, at a reference school after one term:

| Metric | Target |
|---|---|
| Staff check-ins completed via app | ≥ 95% of expected |
| Check-ins resolved without manual review | ≥ 92% |
| Lesson sessions opened within 10 min of scheduled start | ≥ 85% |
| Coverage data present for delivered lessons | ≥ 90% |
| Median teacher daily interaction time | ≤ 45 seconds |
| Parent notification delivery (push or SMS) | ≥ 98% within 15 min |
| Disputes upheld (evidence was wrong) | ≤ 2% of records |

That last row is the honesty metric. If it climbs, the evidence model is
producing false accusations and must be recalibrated before it is trusted for
anything consequential.

## Commercial shape

Per-school subscription, priced per active staff member per term, with tiers
gated on modules rather than seats. Students are not billable units — pricing on
enrolment punishes growth and invites under-reporting, which corrupts the data we
depend on.
