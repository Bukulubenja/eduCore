# 07 — Delivery Plan

The original roadmap put multi-tenancy, auth, attendance, students, timetable,
QR verification, and dashboards all in "Phase 1 (MVP)". That is nine months of
work before a single school touches it, and nine months is long enough to build
the wrong thing with total confidence.

This plan reorders around **the earliest point at which a real school can use the
product daily**, then extends. Each phase has an exit criterion that is
observable at a pilot school, not a checklist of merged pull requests.

## Phase 0 — Foundations (4 weeks)

Not shippable. Everything after depends on getting this right, and it is very
expensive to retrofit.

- Repository, CI, environments, docker-compose for local development
- `core`: School, User, Membership, Role, RoleAssignment, Device
- Tenancy: `TenantOwnedModel`, `TenantManager`, middleware, **RLS policies**,
  composite foreign keys, and the cross-tenant isolation test battery
- Audit event model with hash chaining and the nightly verification job
- Auth: tokens, refresh rotation with reuse detection, MFA, `/v1/me`
- Transactional outbox and Celery wiring
- OpenAPI generation, error format, pagination, rate limiting

**Exit:** an automated test proves School A cannot read any School B object
through any endpoint, including with a hand-crafted raw query, and the audit
chain verifier detects a deliberately tampered row.

## Phase 1 — Presence (6 weeks)

The first thing a school actually pays for.

- `academics`: years, terms, levels, class groups, staff import
- `presence`: signal model, confidence evaluator, policy versioning, rotating QR
  service, geofence, device binding, appeal and review queue
- Mobile app: check-in/out, offline queue, sync endpoint, own attendance history
- Web: attendance dashboard, review queue, policy configuration
- Notifications: staff absence alert to the deputy

**Exit:** one pilot school runs staff attendance on eduCore for four consecutive
weeks with paper stopped; provisional rate under 10%; appeals upheld under 2%.

Stopping here is a viable, saleable product. That is the point of stopping here.

## Phase 2 — Delivery (8 weeks)

Presence becomes accountability.

- `timetable`: period grids, versions, scheduled lessons, publish, nightly
  instance materialisation, calendar exceptions, conflict detection
- `delivery`: lesson sessions, classroom QR, substitutions, schemes of work,
  coverage entries, coverage and pace computation, missed-lesson job
- `students`: student records, enrolment, guardian links, lesson registers,
  gate events for day scholars
- Web: DOS dashboard leading with missed lessons and pace; HOD department view
- Parent notification for student absence

**Exit:** DOS at the pilot school uses the coverage dashboard to make a real
scheduling intervention before end of term — not because we asked them to.

## Phase 3 — Assessment & Communication (8 weeks)

- `assessment`: definitions, state machine, score entry, moderation, grading
  scales, report card generation and PDF rendering, release with step-up MFA
- `comms`: announcements, audience selectors, threads, delivery tracking,
  push + SMS fallback, read receipts
- Parent mobile access: attendance, results, homework, announcements
- Student portal

**Exit:** a full term's report cards produced end-to-end in eduCore and issued to
parents, with no parallel spreadsheet anywhere in the process.

## Phase 4 — Scale & Operate (ongoing)

- Platform operator portal: provisioning, subscriptions, usage metering,
  suspension, tenant health
- Self-service school onboarding with bulk import and validation
- Insights: at-risk students, punctuality trends, workload balance
- Performance: partitioning rollout, read-model materialisation, query budgets
- Hardening: penetration test remediation, DPIA, restore drills

**Exit:** a new school is onboarded by a non-engineer in under one day.

## Phase 5+ — Deferred by decision

Revisit only with evidence of demand from paying schools.

| Item | Reconsider when |
|---|---|
| Finance, fees, payroll | Three pilot schools ask, unprompted, in writing |
| Face recognition + liveness | A school has exhausted QR + device + check-out enforcement and still has a measured fraud problem, and a DPIA clears |
| BLE beacons | A school will fund the hardware |
| Predictive/AI features | Two full years of clean data exist to train and validate on |
| Library, hostel, transport | Never, unless it becomes the reason deals are lost |

Building on Phase-1 data with a Phase-4 model is how analytics features come to
produce confident nonsense. The two-year requirement is not conservatism; it is
the minimum for a seasonal signal to be distinguishable from noise.

## Team shape

Minimum viable team for this plan:

| Role | Count | Focus |
|---|---:|---|
| Backend engineer | 2 | Django, domain, API |
| Mobile engineer | 1 | Flutter, offline sync |
| Frontend engineer | 1 | Next.js console |
| Product / implementation | 1 | Pilot school relationship, requirements, training |

The implementation role is not optional. School software fails at rollout far
more often than at build, and a pilot without someone physically present in the
staff room during week one will produce a false negative about the product.

## Principal risks

| Risk | Impact | Mitigation |
|---|---|---|
| **Staff resistance to monitoring** | Fatal | Symmetric visibility, appeal rights, involve staff reps before rollout, never launch attendance and discipline together |
| Verification too strict in practice | High | Confidence model, per-school policy, monitored provisional rate |
| Connectivity worse than assumed | High | Offline-first from Phase 1, not Phase 4 |
| Timetable data entry burden at onboarding | High | Bulk import, conflict detection, and a service offer to do the first term's entry for the school |
| Scope creep into full ERP | High | Documented non-goals; finance requests answered with a roadmap, not a sprint |
| Single-school over-fitting | Medium | Second pilot of a different type (day vs boarding) before Phase 3 |
| Regulatory exposure on biometrics | Medium | Deferred entirely; DPIA gate |

The first row is the one that kills products of this kind. Every other risk is
recoverable. A staff body that has decided the system is an instrument used
against them will defeat it, and no amount of engineering fixes that afterwards.

## Definition of done

A change is done when it has: tests including the tenant-isolation case;
an OpenAPI entry; audit events for privileged actions; a migration reviewed for
lock behaviour; observability for its failure modes; and documentation updated in
this directory where it changes a design decision recorded here.
