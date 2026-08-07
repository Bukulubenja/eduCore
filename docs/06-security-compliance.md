# 06 — Security & Compliance

The system holds data about children, staff performance records that affect
employment, and in some configurations biometric templates. The security posture
is proportionate to that, not to the size of the codebase.

## Threat model

Ranked by likelihood × impact for this product, which is not the same ranking a
generic checklist would give.

| # | Threat | Likelihood | Impact | Primary control |
|---|---|---|---|---|
| 1 | Cross-tenant data exposure | Medium | Catastrophic | RLS + composite FKs + tenant tests |
| 2 | Insider mark tampering | High | High | Immutable audit chain, state machine, step-up MFA |
| 3 | Attendance fraud by staff | High | Medium | Signal model ([04](04-attendance-verification.md)) |
| 4 | Credential sharing among staff | High | Medium | Device binding, concurrent-session detection |
| 5 | Child data exposure to wrong guardian | Medium | Severe | Scoped RBAC, guardian link verification |
| 6 | Account takeover (phishing) | Medium | High | MFA for privileged roles, refresh reuse detection |
| 7 | Ransomware / destructive insider | Low | Catastrophic | Immutable off-site backups, restore drills |
| 8 | Malicious file upload | Medium | Medium | Type allowlist, AV scan, isolated origin |
| 9 | Biometric template theft | Low | Severe | Opt-in only, templates not images, encrypted |
| 10 | DoS / abusive tenant | Medium | Medium | Per-tenant quotas, query timeouts |

Threat 5 deserves emphasis because it is the one teams overlook. Showing a
child's grades, location, and attendance to a person not entitled to them —
a non-custodial parent, for instance — is a safeguarding incident, not a bug.
Guardian links carry an explicit verification state and are never inferred from
a surname or a shared phone number.

## Tenant isolation

Three independent layers, described in [02](02-architecture.md#tenancy). The
engineering commitments:

- Every tenant-owned model inherits `TenantOwnedModel`; a CI check fails the
  build on any model with a `school` FK that does not.
- Cross-table foreign keys are **composite** — `(school_id, id)` referencing
  `(school_id, id)`. The database itself then makes a cross-tenant reference
  impossible to write, rather than merely unlikely.
- RLS policies are created by migration and asserted present by a startup check.
  The application role has no `BYPASSRLS`.
- The test suite includes an isolation battery: for every read endpoint, a
  request authenticated as School A must return 404 (never 403 — a 403 confirms
  the object exists) for every School B object. New endpoints are added to this
  battery automatically by introspection, so it cannot be forgotten.

## Authentication

| Control | Requirement |
|---|---|
| Password hashing | Argon2id, `m=64MiB, t=3, p=4`. Django default upgraded |
| Password policy | Minimum 12 characters, checked against a breach corpus. No composition rules, no forced rotation — both demonstrably worsen real-world password quality |
| MFA | Mandatory: director, head teacher, deputy, DOS, bursar, ICT admin, platform operator. Optional but encouraged: teachers |
| MFA methods | TOTP; WebAuthn for platform operators. SMS OTP only as a last-resort fallback, never as the sole factor |
| Sessions | 15 min access, 30 day rotating refresh, reuse detection |
| Lockout | Exponential backoff per identifier and per IP. Never permanent lockout — that is a denial-of-service against a head teacher on results day |
| Device binding | Staff attendance requires a registered, attested device |

Parents and students authenticate by phone number with OTP where email adoption
is low. That is a deliberate usability trade in this market, and it is why parent
accounts see only their own children and hold no write access to anything
consequential.

## Authorisation

RBAC with object scoping, evaluated server-side on every request.

```
permission granted ⟺
    ∃ RoleAssignment ra on the request's Membership
      where ra.role grants the required permission
        and ra is valid at now()
        and (ra.scope is null  ∨  object ∈ scope(ra))
```

Scope examples: an HOD's permissions apply only within their department; a class
teacher may mark attendance only for their class group; a parent's read scope is
the set of students they have a verified `GuardianLink` to.

Rules:

- Default deny. A permission that is not explicitly granted does not exist.
- Field-level restrictions where needed: a teacher reads a student's academic
  record but not the guardian's financial status.
- Every privileged action writes an `AuditEvent` before returning success.
- Permission changes take effect immediately — they are read from the database,
  not from the token, precisely so that revocation is instant.

## Data protection

### Classification

| Class | Examples | Handling |
|---|---|---|
| **Restricted** | Biometric templates, credentials, safeguarding notes | Encrypted at field level, access always audited, never logged, never in lower environments |
| **Sensitive** | Student records, health notes, marks, staff attendance | Encrypted at rest, RBAC-scoped, audited on export |
| **Internal** | Timetables, schemes of work, announcements | RBAC-scoped |
| **Public** | School name, term dates | Unrestricted |

### Controls

- TLS 1.3 in transit; HSTS with preload; certificates automated.
- Encryption at rest at the volume level, plus field-level encryption for
  Restricted data using envelope encryption with a KMS-held key.
- No PII in logs, error messages, URLs, or analytics. Sentry scrubbing is
  configured with an allowlist, not a denylist.
- Uploads: extension and content-type allowlist, magic-byte verification, size
  caps, AV scan before the file becomes downloadable, served from a separate
  origin with `Content-Disposition: attachment`.
- Exports: MFA-gated, rate-limited, watermarked with the requesting user and
  timestamp, and audited. Bulk export is the single most likely vector for a
  quiet, large-scale data loss.

### Retention

| Data | Retention | Basis |
|---|---|---|
| Audit events | 7 years | Legal/dispute defence |
| Academic records, report cards | Life of tenancy + 7 years | Statutory in most jurisdictions |
| Raw attendance signals | 2 years | Operational recalculation only |
| Derived attendance records | Life of tenancy + 1 year | Employment records |
| Biometric templates | Until consent withdrawal or membership ends, whichever first | Minimisation |
| Message content | 3 years | Safeguarding investigations |
| Application logs | 90 days | Operational |

Deletion is implemented as partition drop plus a tombstone in the audit chain
recording that deletion occurred, what class of data, and under what policy.

### Legal framework

Target markets — East Africa, expanding — bring at least these into scope:

- **Uganda:** Data Protection and Privacy Act, 2019 (biometric data is sensitive
  personal data; registration with the PDPO required).
- **Kenya:** Data Protection Act, 2019 (DPIA required for biometric processing;
  registration with the ODPC).
- **Tanzania:** Personal Data Protection Act, 2022.
- **GDPR** where any EU-resident data subject is involved, and as the design
  baseline regardless, since it is the strictest.

Obligations we design for from the start rather than retrofit:

1. Lawful basis recorded per processing purpose; consent captured with version
   and timestamp where consent is the basis.
2. Data subject rights: access, rectification, erasure, portability — implemented
   as product features, not manual database work.
3. A DPIA completed before any biometric processing is enabled for any school.
4. Data Processing Agreements with each school, who is the controller; we are the
   processor. This distinction determines who answers a parent's request, and
   getting it wrong exposes both parties.
5. Breach notification: 72 hours to the regulator, with a rehearsed runbook.
6. Children's data minimisation — collect what the school needs to operate,
   nothing acquired speculatively for future analytics.

> Legal review by counsel qualified in each target jurisdiction is required before
> the first paying school goes live, and again before biometrics are enabled
> anywhere. The list above is engineering's working understanding, not advice.

## Secure development

| Practice | Implementation |
|---|---|
| Dependency scanning | `pip-audit` and `npm audit` in CI; build fails on high severity |
| SAST | `bandit`, `semgrep` with Django rulesets, on every PR |
| Secrets | Never in the repo. `gitleaks` pre-commit and CI. Runtime secrets from a manager, rotated quarterly |
| Code review | Two approvals for anything touching auth, tenancy, or audit |
| Migrations | Reviewed for lock behaviour; no `ALTER` that takes `ACCESS EXCLUSIVE` on a large table during business hours |
| Penetration test | Before first production school, then annually |
| Secure defaults | `DEBUG=False` asserted at boot; `SECURE_*` settings enforced by a startup check that refuses to start if misconfigured |

Standard Django protections — CSRF for session-authenticated views, ORM
parameterisation, template auto-escaping, `SecurityMiddleware`, CSP with nonces,
`X-Frame-Options: DENY` — are assumed, configured, and covered by tests that
fail if disabled. They are not achievements; they are the floor.

## Incident response

1. **Detect** — alerting on: RLS policy violations, audit chain breaks, anomalous
   export volume, authentication spikes, error-rate SLO breach.
2. **Triage** — severity within 30 minutes. Sev-1 is any suspected cross-tenant
   exposure or child-data disclosure.
3. **Contain** — documented actions: revoke token families, disable a membership,
   suspend a tenant, roll back a release.
4. **Notify** — affected schools within 24 hours of confirmation; regulators
   within 72; a named person is accountable for the notification.
5. **Review** — blameless post-incident review within 5 working days, with
   remediation items tracked to closure like any other work.

Runbooks live in `ops/runbooks/` and are exercised twice yearly. An untested
runbook is a document, not a capability.
