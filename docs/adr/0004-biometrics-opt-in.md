# ADR-0004 — Biometrics opt-in, template-only, never the sole gate

- **Status:** Accepted
- **Date:** 2026-08-04
- **Amends:** the original brief, which made face verification and liveness
  mandatory layers of staff check-in

## Context

The brief made face verification plus liveness detection required layers for
every staff check-in, and proposed face recognition at school gates for students.

Face recognition is the most legally hazardous, most operationally fragile, and
most expensive of the available verification signals — and it is not the one that
stops the dominant real-world attack.

**Legal.** Face templates are sensitive personal data under Uganda's Data
Protection and Privacy Act 2019, Kenya's Data Protection Act 2019, Tanzania's
Personal Data Protection Act 2022, and GDPR Article 9. Processing requires an
explicit lawful basis, a Data Protection Impact Assessment, and in several
jurisdictions regulator registration. Applying it to **children** raises the bar
further, and in some jurisdictions makes it effectively unavailable.

**Employment law.** Mandatory biometric processing as a condition of employment
is contested in most jurisdictions, because consent given under threat of losing
a job is not freely given and therefore is not valid consent.

**Accuracy.** Face matching on mid-range devices, in variable light, with
head coverings, glasses, masks, or facial hair changes, degrades badly. Published
error rates vary substantially across demographic groups. A verification layer
that fails more often for some staff than others is a discrimination problem
sitting inside an attendance system that affects pay.

**Breach severity.** A leaked password is rotated. A leaked face template is
permanent. The blast radius is not comparable to any other data we hold.

**And it does not solve the main problem.** The dominant attack is not
impersonation at the gate; it is checking in remotely, and photographing a static
QR to reuse later. Device binding with attestation defeats the first. Rotating
server-signed QR defeats the second. Both are free, legally unremarkable, and
already in the design.

## Decision

Biometric verification is:

1. **Excluded from v1 entirely.** Not built, not shipped, not sold.
2. **Opt-in per school and per individual** if later enabled. A staff member who
   declines uses the standard signal set and is not disadvantaged in any way that
   affects their record.
3. **Never a hard requirement.** Maximum weight 20 of ~110 available. It can
   raise confidence; its absence or failure alone can never reject a check-in.
4. **Template-only.** Irreversible feature vectors, encrypted at field level with
   a KMS-held key. Raw images are never stored — not in the database, not in
   object storage, not in logs, not in Sentry.
5. **On-device matching preferred**, with only the match verdict and a quality
   score transmitted, so the template never leaves the enrolled device.
6. **Gated on a completed DPIA** and jurisdiction-specific legal review before
   enablement for any school.
7. **Never applied to students.** No face recognition at gates or anywhere else.
   Students use QR, NFC, or barcode ID.
8. **Deletable on demand.** Withdrawal of consent deletes the template
   immediately, and enrolment ends automatically when the membership ends.

## Alternatives considered

**Mandatory face + liveness as specified.** Highest legal exposure, worst failure
modes, discriminatory error profile, and it does not address the actual fraud
vectors. Rejected.

**Fingerprint instead of face.** Same legal classification, plus shared-device
hygiene concerns and poor performance on manual-labour-worn fingertips. No better.

**Voice.** Worse accuracy, worse in noisy environments, same legal class.

**Behavioural biometrics** (gait, typing). Same legal exposure, far weaker signal.

## Consequences

- v1 relies on QR + device attestation + geofence + Wi-Fi + time window. Modelled
  confidence for the common configuration reaches `verified` without biometrics,
  which is the design target.
- If a specific school demonstrates a measured fraud problem that survives
  rotating QR, device binding, and check-out enforcement, biometrics become
  available as a **school-level opt-in**, subject to the gates above.
- We can state plainly to schools and to parents that we do not perform facial
  recognition on children. That is a commercial asset in this market, not only a
  compliance position.
- Any future enablement is a new ADR, not a configuration change.
