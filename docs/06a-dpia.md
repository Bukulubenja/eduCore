# 06a — Data Protection Impact Assessment

Companion to [06 — Security & Compliance](06-security-compliance.md), which
promises this document at line 148 ("A DPIA completed before any biometric
processing is enabled for any school") and in the legal-framework obligations
list. This is that document, scoped to what the system actually processes
today.

> This is engineering's working assessment, not legal advice. Doc 06's own
> caveat applies here too: legal review by counsel qualified in each target
> jurisdiction is required before the first paying school goes live, and again
> before any biometric processing is enabled.

## 1. Scope

This DPIA covers the processing implemented in this repository as of the
Phase 4 backend described in [07 — Delivery Plan](07-delivery-plan.md):
tenancy and identity, staff presence (signals, confidence scoring, QR),
timetable and lesson delivery, students (enrolment, guardians, registers,
gate events), assessment (scores, moderation, report cards), and comms
(announcements, threads).

**Explicitly out of scope, because it is not built:** face recognition,
liveness detection, or any other biometric processing. [ADR-0004](adr/0004-biometrics-opt-in.md)
excludes this from v1 entirely, and the `presence.SignalType.FACE` choice
exists in the schema at weight `0` — a placeholder for a future signal, not a
live capability (`educore/presence/models.py`, `default_weights()`). **A
biometric-processing DPIA does not exist and cannot be retrofitted from this
one.** Per ADR-0004 decision 6 and the "Regulatory exposure on biometrics"
row in doc 07's risk table, any future enablement is gated on: a school
demonstrating a measured fraud problem that survives QR + device binding +
check-out enforcement, a completed DPIA specific to that processing, and a
new ADR. This document is not that gate; it is the reason the gate exists —
the assessment below shows the current system already reaches its confidence
targets without biometrics, which is the evidence the gate is asking for.

Also out of scope because it is not built: finance/fees/payroll (deferred per
doc 07's Phase 5+ table) and health/medical notes (referenced as a data
*class* in doc 06's classification table as forward-looking guidance; no
health-record model exists in `educore/students/` or elsewhere today).

## 2. Roles

The school is the data controller; eduCore (the platform operator) is the
processor, per doc 06's legal-framework obligation 4. This determines who is
the accountable party for a parent's subject-access request and who signs
the Data Processing Agreement — a DPA per school is a prerequisite for
production use and is tracked outside this repository (commercial/legal
process, not code).

## 3. Description of processing

### 3.1 Data inventory

| Category | Fields (model, module) | Data subject | Special category? |
|---|---|---|---|
| Identity | `User.email`, `phone_e164`, `full_name` (`core/models.py`) | Staff, guardians, students with portal access | No |
| Employment | `Membership.staff_number`, `started_on`/`ended_on`, `Role`/`RoleAssignment` (`core/models.py`) | Staff | No |
| Staff attendance signals | `AttendanceEvent.captured_at`, `received_at`, GPS/Wi-Fi/QR evidence in `AttendanceSignal.payload`, `disposition`, `confidence` (`presence/models.py`) | Staff | No, but see 4.3 — location data warrants care even though it is not a GDPR special category |
| Staff attendance outcomes | `AttendanceRecord.status`, `minutes_on_site`, `AttendanceException.narrative` (an employee's own account of an absence) (`presence/models.py`) | Staff | No |
| Device data | `Device.fingerprint`, `platform`, `push_token` (`core/models.py`) | Staff | No |
| Student identity | `Student.full_name`, `date_of_birth`, `admission_number`, `scan_code`, `residency`, `status` (`students/models.py`) | Students, most of them children | **Yes — children's data** (heightened protection under all four frameworks in doc 06; not itself a GDPR Art. 9 special category, but Uganda/Kenya/Tanzania law treats a child's data with additional care) |
| Guardian relationship | `GuardianLink.relationship`, `is_primary_contact`, `verified`, `receives_notifications` (`students/models.py`) | Guardians, indirectly students | No |
| Student attendance | `StudentAttendance.status`, `method`, `note`; `GateEvent.direction`, `occurred_at` (`students/models.py`) | Students | Location/movement pattern of a child — treated as sensitive in practice even where not a formal special category |
| Assessment results | `Assessment`/scores/`GradeBand` state machine, released report cards (`assessment/models.py`) | Students | No, but doc 06 classifies it **Sensitive** — affects a child's academic record |
| Communications | `Message.body`, `Thread`, announcement content (`comms/models.py`) | Staff, guardians, students | Content is free text and may incidentally contain anything a sender writes, including safeguarding-relevant disclosures — handled as **Restricted** by policy even though the schema cannot enforce that |
| Authentication secrets | `User.mfa_secret` (TOTP secret), hashed passwords, hashed refresh tokens (`core/models.py`) | All authenticated users | No, but doc 06 classifies **Restricted** |
| Audit trail | `AuditEvent.before`/`after` snapshots, `ip_address`, `actor_membership` (`core/models.py`) | Whoever the audited action concerns | Inherits the sensitivity of what it snapshots |

### 3.2 Purposes and lawful basis

| Purpose | Lawful basis (GDPR terms; the closest equivalent applies under the Uganda/Kenya/Tanzania Acts) |
|---|---|
| Staff attendance and payroll-adjacent records | Performance of the employment contract / legal obligation (labour law record-keeping) |
| Student attendance and safeguarding | Legitimate interest of the school (duty of care) balanced against the child's rights; in most cases also a legal/statutory obligation on the school as controller |
| Assessment and report cards | Performance of the school's educational mandate; typically a legal/contractual basis between school and guardian |
| Guardian notifications | Legitimate interest (keeping a guardian informed) plus the guardian's own consent to a communication channel where phone/SMS is used |
| Communications (comms app) | Legitimate interest, scoped by RBAC to the audience the sender is entitled to reach |
| Audit logging | Legal obligation / legitimate interest — necessary to investigate disputes and detect tampering, and itself required by several of the retention rows in doc 06 |

Doc 06 obligation 1 requires lawful basis to be recorded **per processing
purpose**, with consent versioned and timestamped where consent is the
basis. That recording is a school-onboarding/DPA responsibility, not
something this codebase currently models as data (no `ConsentRecord` or
similar exists). **Gap:** if consent becomes the operative basis for a
specific purpose (e.g., an SMS notification channel), it needs a first-class
model with a version and timestamp, not an assumption baked into a checkbox
during onboarding.

## 4. Necessity and proportionality

### 4.1 Staff presence

[ADR-0002](adr/0002-attendance-confidence-model.md) and doc 04 justify why
attendance requires multiple signals rather than one authoritative source:
GPS alone is spoofable, a single QR scan alone is replayable. The signal set
(QR, device, geofence, Wi-Fi, time window — see `presence.default_weights()`)
is the minimum needed to reach the `verified`/`provisional` thresholds
without biometrics, which is exactly the point ADR-0004 makes: the system
hits its confidence targets without processing the most invasive category of
data available. Nothing here is collected "because we might want it later" —
each signal type maps to a specific weight in a documented scoring function.

### 4.2 Guardian and student data

`GuardianLink.verified` exists specifically to prevent the over-collection
failure mode doc 06 calls out: inferring a parent-child relationship from a
shared surname or phone number and giving that inferred party access. A link
is either verified by a specific action (recorded as an `AuditEvent`, since
every privileged action does) or it is not, and unverified links do not
unlock read access to a child's record. This is a minimisation control, not
just an access control — it stops speculative linkage from becoming a data
disclosure.

### 4.3 Location and movement data

Attendance signals include GPS coordinates (via geofence evaluation) and
Wi-Fi BSSID hashes. `Campus.wifi_bssid_hashes` stores **hashes**, not raw
SSIDs or BSSIDs, specifically so a stolen database does not hand out a map of
the school's network (see the model docstring). Raw location fixes live in
`AttendanceSignal.payload` (a JSON blob) — this is retained under the "Raw
attendance signals: 2 years" row in doc 06's retention table, on the basis
that recomputation needs it, not for any purpose beyond that.

## 5. Risk assessment

This inherits doc 06's threat-model table (section "Threat model") rather
than duplicating it; the risks with the highest data-protection relevance are
threats 5 ("Child data exposure to wrong guardian"), 9 ("Biometric template
theft" — mitigated by not processing biometrics at all, see scope above),
and 7 ("Ransomware / destructive insider", addressed by
[06b — Restore Drills](06b-restore-drills.md)).

Additional risks specific to this assessment:

| Risk | Likelihood | Impact | Mitigation, as actually implemented |
|---|---|---|---|
| Cross-tenant exposure of any category above | Medium | Catastrophic | Three independent isolation layers: middleware context, `TenantManager` default-manager filtering, and PostgreSQL RLS with `FORCE ROW LEVEL SECURITY` as the backstop that survives ORM mistakes (`educore/core/tenancy.py`, `educore/core/db.py`, ADR-0001). RLS presence is asserted by a deploy-time check (`educore/core/checks.py:rls_enabled`) that refuses to start if a tenant table is unprotected. |
| Undetected tampering with a score, attendance record, or audit trail itself | High impact if it happens, currently low likelihood | High | Hash-chained, append-only `AuditEvent` (`educore/core/audit.py`) — every event links to the previous via `prev_hash`, and `verify_chain()` walks the chain looking for gaps, broken links, and recomputed-hash mismatches. Enforced nightly via `verify_audit_chains` and now in CI (see `.github/workflows/ci.yml`, "Audit chain integrity"). |
| MFA/authentication secret exposure | Low likelihood, severe impact | High | **Gap, not a mitigation.** `User.mfa_secret` is a plain `CharField` (`educore/core/models.py`). The model's own comment states this field is "Restricted data... field-level encrypted before this reaches production," but no field-level encryption is implemented anywhere in this codebase — there is no envelope-encryption utility, no KMS integration, nothing under `educore/core/` that encrypts a field at write time. Password hashes (Argon2id) and refresh tokens (stored as SHA-256 hashes, `RefreshToken.token_hash`) are correctly one-way; the TOTP secret is not, because a TOTP secret must be reversible to generate the next code. This is the single largest gap between what doc 06 promises for Restricted data and what the code does. **Recommendation: block production go-live on either implementing field-level encryption for `mfa_secret`, or an explicit, reviewed risk acceptance if the row-level and volume-level (disk) encryption controls are judged sufficient in the interim.** |
| Communications content (`comms.Message.body`) carrying an unintended safeguarding disclosure | Medium | High | RBAC scoping restricts who can read a thread (`ThreadParticipant`), and delivery is tracked per channel. There is no content classification or DLP on message bodies — doc 06's "Restricted" handling for this class is a policy statement, not an enforced control, since free text cannot be reliably classified automatically. Mitigation is procedural (staff training, the appeal/incident-response process in doc 06) rather than technical. |
| Export of bulk student/guardian data | Medium | Severe | Doc 06 specifies exports must be MFA-gated, rate-limited, watermarked, and audited. `educore/platform/importers.py` exists for **import**; a symmetric bulk-export path was not located in this codebase at the time of writing. **Gap:** if a bulk export endpoint exists elsewhere or is added later, it must implement all four controls doc 06 lists before shipping — none of them are generic Django/DRF defaults. |

## 6. Data subject rights

Doc 06 obligation 2 requires access, rectification, erasure, and portability
"as product features, not manual database work." As implemented today:

- **Rectification** — ordinary authenticated writes through the existing
  CRUD endpoints, subject to RBAC.
- **Erasure** — partial. Deletion is designed as "partition drop plus a
  tombstone in the audit chain" per doc 06's retention section, but this
  DPIA did not find a dedicated erasure/right-to-be-forgotten endpoint or
  management command in `educore/`. `AuditEvent` is append-only by
  construction (`save()` raises on update), which is correct for audit
  integrity but means an erasure request must be satisfied by a
  documented, tombstoned deletion of the *underlying* record — that
  procedure is a gap to close before it is needed under regulatory
  deadline pressure, not something to design for the first time when a
  request arrives.
- **Access and portability** — no self-service export was found. A guardian
  or staff member's own data is visible through the ordinary API surface
  (`/v1/me`, attendance/assessment endpoints scoped to what they are
  authorised to see), which satisfies access in substance but not a
  structured portability export.

## 7. Retention

Inherited unchanged from doc 06's retention table — this DPIA does not
introduce new retention periods, it confirms the table is the operative
policy and notes where enforcement is mechanical versus manual:

- **Mechanical:** the append-only, hash-chained `AuditEvent` table makes
  *tampering* with retained audit data detectable; it does not by itself
  enforce the 7-year retention *period* (nothing in this repo currently
  purges audit events older than 7 years — partitioning for `audit_event` is
  listed as Phase 4 scope in doc 07 but a scheduled purge job was not found).
- **Manual/policy:** every other row in doc 06's retention table (raw
  attendance signals at 2 years, biometric templates on consent withdrawal,
  message content at 3 years) currently depends on an operational process
  rather than a Celery Beat job. `CELERY_BEAT_SCHEDULE` (see README's
  "Scheduled work" table) runs `relay_outbox`, `roll_timetable`,
  `verify_audit_chains`, and `estate_report` — no retention/purge job is
  among them today.

## 8. Consultation

Doc 07's principal-risks table names "Staff resistance to monitoring" as the
risk most likely to be fatal to the product, and its mitigation explicitly
includes involving staff representatives before rollout. That consultation
is an implementation-phase (per-school) activity, not something this
document can complete on the schools' behalf — it is recorded here as a
required step, to be evidenced per school before staff attendance monitoring
goes live there.

## 9. Conclusion and residual risk

For the processing actually implemented (staff presence without biometrics,
student records, guardian links, assessment, communications), the
architectural controls — RLS-backed tenant isolation, the hash-chained audit
trail, RBAC with object scoping, guardian-link verification — are
proportionate to the risk and match what doc 06 commits to, with two
material exceptions carried forward as open items:

1. `User.mfa_secret` is not field-level encrypted despite being classified
   Restricted (section 5).
2. No scheduled purge job enforces the retention *periods* in doc 06's
   table; only tampering-detection is enforced today (section 7).

Neither exception blocks continued development. Both should be closed, or
formally risk-accepted by whoever holds that authority for this product,
before the first paying school's data is processed in production — which is
also the point at which doc 06's required legal review and DPO/regulator
registration steps apply.

**This DPIA does not clear biometric processing.** Any move on the Phase 5+
face-recognition item requires its own DPIA addressing template storage,
on-device matching, and the discrimination/accuracy concerns ADR-0004
documents in detail — this document's conclusion does not transfer to that
processing.
