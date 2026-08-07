# ADR-0002 — Attendance as a confidence score, not a boolean gate

- **Status:** Accepted
- **Date:** 2026-08-04
- **Supersedes:** the six-layer AND-gate in the original design brief

## Context

The brief specified that staff attendance be accepted only when GPS **and**
Bluetooth beacon **and** face match **and** liveness **and** device binding
**and** Wi-Fi all pass.

Each layer is individually reasonable. Composed with AND, they multiply.

Assume a generous 97% success rate per layer under real conditions — GPS indoors
under iron roofing, Wi-Fi association on a mid-range Android, face matching in
low morning light, a beacon with a flat battery:

```
1 layer:  0.97        = 97.0%   →  3 blocked per 100
3 layers: 0.97³       = 91.3%   →  9 blocked per 100
6 layers: 0.97⁶       = 83.3%   → 17 blocked per 100
```

Seventeen legitimate staff per hundred unable to check in. At a 70-teacher school
that is roughly twelve people queuing at the deputy's office every morning.

The predictable sequence: the school demands a manual override; the override
becomes routine because it is faster than the app; within a term every record is
a manual override and the evidence chain is worthless. The system was not
defeated by fraud. It was abandoned because it was unusable.

There is a second problem. An AND-gate cannot distinguish "this signal says the
teacher is absent" from "this signal is not available". A phone with no Wi-Fi
hardware fails the Wi-Fi layer forever, through no fault of its owner.

## Decision

Attendance verification produces a **confidence score in [0, 100]** and a
**disposition** (`verified` / `provisional` / `rejected`), derived from weighted
signals. Signals report `pass`, `fail`, or `unavailable`. A small set of explicit
hard-fail rules can force rejection independent of score.

**No sensor failure prevents a teacher from working.** Low confidence creates a
supervisor review task; it does not lock a door.

## Scoring

```
confidence = 100 × Σ weight(s) where verdict(s) = pass
                   ────────────────────────────────────
                   Σ weight(s) where verdict(s) ≠ unavailable
```

Unavailable signals are excluded from both numerator and denominator: missing
evidence is not counter-evidence.

A floor prevents a high score from thin evidence: if total available weight is
below `min_evidence_weight` (default 40), the disposition is at best
`provisional`, whatever the ratio. Without this, a single passing low-weight
signal scores 100%.

Default dispositions: `verified` ≥ 75, `provisional` 45–74, `rejected` < 45 or
any hard-fail. Schools tune within platform bounds; the accept threshold cannot
be set below 40.

Weights, thresholds, and rules are a **versioned policy row per school**. Every
`AttendanceRecord` stores the `policy_version` used, so a record can always be
explained in terms of the rules in force when it was made.

## Hard-fail rules

Score-independent, because these indicate deception rather than weak evidence:

1. Unregistered device, or failed platform attestation.
2. QR token expired, reused, or bound to a different campus.
3. Geofence `fail` **with accuracy < 50 m** — a confident fix, confidently
   outside. A low-accuracy fix outside the fence is `unavailable`, not `fail`.
4. Device clock skew > 5 minutes combined with a mock-location flag.
5. Replayed `client_event_id` with differing signal content.

Rule 3 is the one that matters most for fairness. Treating an imprecise GPS fix
as proof of absence manufactures false accusations, and false accusations are
what destroy a workforce's willingness to use the system at all.

## Alternatives considered

**Keep the AND-gate, add an override button.** The override becomes the primary
path, unrecorded and unmeasured. Worse than not gating, because it produces the
appearance of rigour.

**Require any N of M signals.** Simpler, but treats a rotating server-signed QR
as equal to an SSID string. Signals differ enormously in spoof resistance;
weights are how that is expressed.

**Machine-learned anomaly scoring.** No training data, no interpretability, and
an unexplainable decision cannot be appealed. Revisit once two years of labelled
data exist, and then only as an input to review queues, never as the decision.

## Consequences

- Signals are stored raw and immutable, so records are recomputable when policy
  changes. Policy is never retroactively silent.
- A supervisor review queue and appeal workflow are required product surface, not
  optional extras — they are the release valve that keeps the model usable.
- Two health metrics are monitored monthly per school: **provisional rate**
  (> 10% ⇒ miscalibrated weights) and **appeal upheld rate** (> 2% ⇒ the model is
  producing false accusations; recalibrate before anyone is disciplined on it).
- The system is honest about uncertainty rather than projecting false precision.
  A record marked `provisional` is more useful to a head teacher than a `present`
  that everyone knows is unreliable.
