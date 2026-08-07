# ADR-0003 — Offline-first capture with idempotent sync

- **Status:** Accepted
- **Date:** 2026-08-04
- **Amends:** the original brief, which placed offline sync in Phase 4

## Context

The brief listed "offline synchronization" in Phase 4, after AI features. That
ordering assumes connectivity is normally present and offline is an edge case.

In the target market it is the reverse. Classrooms are frequently at the edge of
coverage or inside structures that block it; school Wi-Fi covers the
administration block and not the science wing; mobile data is metered and staff
ration it; power cuts take the router down for hours.

The two most important capture moments — the 07:00 check-in rush and the moment a
lesson starts — are precisely when the network is most likely to be congested or
absent. A product whose core ritual fails without connectivity will be judged
broken in its first week, regardless of what it does when the network is up.

## Decision

Capture is **offline-first from Phase 1**. The mobile client is fully functional
with no connectivity for a complete school day. Synchronisation is a background
concern with an idempotent, batched, per-operation-result protocol.

## Protocol

**Client-generated identifiers.** Every operation gets a UUIDv7 on the device
(`client_event_id`). Time-ordered, so it indexes well; generated locally, so
records exist and are usable before any server round trip.

**Idempotent absorption.** `client_event_id` is unique per school. Re-submitting
returns the original result rather than creating a duplicate or erroring. Flaky
networks retry constantly; retries must be free, or clients will avoid retrying
and lose data.

**Per-operation results.** A batch returns a result array. One rejected operation
never fails the batch — otherwise a single malformed record blocks a device's
entire queue indefinitely, and the queue then grows until it is evicted.

**Two clocks, one trusted.** `captured_at` comes from the device and is
attacker-controlled: a teacher can set the phone clock back and claim an earlier
arrival. `received_at` is server time and is authoritative. Skew is computed,
returned so the client can correct its offset, and beyond 5 minutes becomes an
attendance signal in its own right.

**Facts up, derivations down.** The device uploads what it observed — signals,
scans, coverage entries. It never computes confidence or attendance status.
Those are server-side, so policy changes apply uniformly and retroactively, and
so a modified client cannot assert its own verdict.

**Bootstrap pull.** `GET /v1/sync/bootstrap?since=` returns a delta of the
teacher's timetable, rosters, and scheme of work — everything needed to run a
day offline. Delta, not full snapshot, because a full pull on a metered
connection is a cost the user notices.

**Bounded queue.** 14 days or 5,000 operations, then oldest-first eviction with a
prominent in-app warning from day 7. Unbounded offline queues fill storage and
rot silently; a bounded one with a visible warning fails loudly, which is better.

## Conflict handling

Captured events are immutable facts and do not conflict — two check-ins are two
events, and the evaluator decides what they mean together.

Genuine conflicts arise only on mutable state, principally the lesson register.
Rule: **last-write-wins by `captured_at`, scoped to the field**, with both
versions retained in the audit trail. If a class teacher marks a student absent
offline and the DOS marks them excused online, the later capture wins and the
earlier remains visible and attributable.

Where the correct resolution is genuinely ambiguous the record is flagged for
human resolution rather than silently resolved. Silent resolution of an
attendance conflict is how a child ends up marked present on a day they were
missing.

## Alternatives considered

**Online-only with retry.** Simplest, and fails at exactly the moments that
matter. Rejected.

**Full CRDT replication.** Correct conflict resolution for arbitrary state, at
the cost of a data model most of the team would have to learn, on a problem that
is overwhelmingly append-only. Disproportionate.

**Server-generated IDs with a client correlation token.** Works, but the record
cannot be referenced locally before sync — the app cannot show a teacher their
own check-in until the network returns, which defeats the purpose.

## Consequences

- Mobile carries real complexity from Phase 1: a local database (Drift/SQLite),
  an outbox, a sync engine, and conflict-flag UI. Budgeted accordingly.
- Every write endpoint must be idempotent, not only `/v1/sync`.
- Testing must include simulated offline periods, clock skew, duplicate delivery,
  out-of-order arrival, and queue overflow. These are ordinary test cases here,
  not exotic ones.
- The server must accept events with `captured_at` up to 14 days in the past, and
  the evaluator must therefore be able to run against historical policy versions.
