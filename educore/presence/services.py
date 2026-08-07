"""Presence use cases.

The one flow that matters: a device reports a check-in, the server gathers
evidence, scores it, records the fact, and derives the day's record. Nothing
here can refuse a teacher entry to a classroom -- the worst outcome is a
review task for a supervisor.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from educore.core import audit
from educore.core.models import Campus, Device, Membership, OutboxMessage
from educore.core.tenancy import TenantContext

from . import qr
from .confidence import score
from .evaluators import EvaluationContext, evaluate_all, payload_digest
from .models import (
    AttendanceEvent,
    AttendanceException,
    AttendancePolicy,
    AttendanceRecord,
    AttendanceSignal,
    AttendanceStatus,
    Disposition,
    QrRedemption,
    SignalType,
    default_weights,
)


class PresenceError(Exception):
    """A request that cannot be processed at all, as opposed to one that scores badly."""


class DuplicateEventConflictError(PresenceError):
    """Same client_event_id, different evidence -- a replay, not a retry."""


@dataclass
class CheckInOutcome:
    event: AttendanceEvent
    record: AttendanceRecord
    created: bool                # False when this was an idempotent replay


# -- Policy ------------------------------------------------------------------


def active_policy(school_id=None) -> AttendancePolicy:
    """The school's current rules, creating platform defaults on first use."""
    school_id = school_id or TenantContext.require()
    policy = AttendancePolicy.objects.filter(is_active=True).first()
    if policy is not None:
        return policy
    return AttendancePolicy.objects.create(
        school_id=school_id, version=1, is_active=True, weights=default_weights()
    )


def revise_policy(**changes) -> AttendancePolicy:
    """Supersede the active policy with a new version.

    Never an in-place edit: an AttendanceRecord cites the policy version that
    produced it, and that citation is worthless if the rules can change
    underneath it.
    """
    current = active_policy()
    fields = {
        f.name: getattr(current, f.name)
        for f in AttendancePolicy._meta.fields
        if f.name not in {"id", "school", "version", "is_active",
                          "created_at", "updated_at", "created_by"}
    }
    fields.update(changes)

    with transaction.atomic():
        AttendancePolicy.objects.filter(pk=current.pk).update(is_active=False)
        revised = AttendancePolicy.objects.create(
            school_id=current.school_id, version=current.version + 1,
            is_active=True, **fields,
        )
        audit.record(action="presence.policy.revised",
                     object_type="presence.AttendancePolicy",
                     object_id=revised.id,
                     before={"version": current.version},
                     after={"version": revised.version, **{
                         k: str(v) for k, v in changes.items()}})
    return revised


# -- Check in / out ----------------------------------------------------------

def _resolve_device(membership, raw_signals) -> tuple[Device | None, str]:
    raw = raw_signals.get(SignalType.DEVICE)
    if not raw:
        return None, ""

    fingerprint = raw.get("fingerprint")
    if not fingerprint:
        return None, "no device fingerprint supplied"

    device = Device.objects.filter(fingerprint=fingerprint).first()
    if device is None:
        return None, "device is not registered"
    if device.membership_id != membership.pk:
        return device, "device is registered to another member of staff"
    if device.status != Device.Status.ACTIVE:
        return device, f"device is {device.get_status_display().lower()}"
    return device, ""


def _resolve_qr(membership, campus, raw_signals, captured_at) -> tuple[str, str, dict]:
    raw = raw_signals.get(SignalType.QR)
    if not raw:
        return "", "", {}

    token = raw.get("token", "")
    try:
        payload = qr.verify(token, school_id=membership.school_id,
                            campus_id=campus.id, at=captured_at)
    except qr.QrError as exc:
        return "", str(exc), {}

    # Reuse has to be detected here, before scoring, or a replayed code sails
    # through as valid evidence and is only noticed when we try to record the
    # redemption -- by which point the event has already been accepted.
    #
    # This is a read, with the write still deferred until the event is
    # accepted. The gap that leaves is two simultaneous scans of one code by
    # one account, which is device sharing, and device binding already covers
    # that. Burning the nonce before scoring would be worse: a check-in
    # rejected for some unrelated reason would leave the teacher unable to
    # retry with a code that is still perfectly valid.
    if QrRedemption.objects.filter(nonce=payload["non"],
                                   membership=membership).exists():
        return "", "this code has already been used by you", {}

    return payload["non"], "", payload


@transaction.atomic
def submit_event(
    *,
    membership: Membership,
    campus: Campus,
    kind: str,
    captured_at,
    client_event_id: uuid.UUID,
    raw_signals: dict,
    actor_membership: Membership | None = None,
) -> CheckInOutcome:
    """Record one check-in or check-out and derive the day's record."""
    received_at = timezone.now()
    policy = active_policy(membership.school_id)
    digest = payload_digest(raw_signals)

    existing = AttendanceEvent.objects.filter(client_event_id=client_event_id).first()
    if existing is not None:
        # Idempotent absorption: flaky networks retry constantly, and a retry
        # must cost nothing (ADR-0003).
        if existing.payload_digest != digest:
            raise DuplicateEventConflictError(
                "This event id has already been used with different evidence."
            )
        return CheckInOutcome(existing, _record_for(membership, existing), created=False)

    max_backdate = timezone.now() - timedelta(days=_max_backdate_days())
    if captured_at < max_backdate:
        raise PresenceError("Captured too long ago to accept; queue limit exceeded.")
    if captured_at > received_at + timedelta(minutes=5):
        raise PresenceError("Captured in the future; check the device clock.")

    device, device_error = _resolve_device(membership, raw_signals)
    nonce, qr_error, _payload = _resolve_qr(membership, campus, raw_signals, captured_at)

    ctx = EvaluationContext(
        school_id=membership.school_id,
        membership=membership,
        campus=campus,
        policy=policy,
        kind=kind,
        captured_at=captured_at,
        received_at=received_at,
        raw_signals=raw_signals,
        device=device,
        device_error=device_error,
        qr_error=qr_error,
        qr_nonce=nonce,
    )
    results = evaluate_all(ctx)
    assessment = score(results, policy)

    skew = ctx.clock_skew_seconds
    if abs(skew) > policy.max_clock_skew_seconds and _mock_location(raw_signals):
        assessment.disposition = Disposition.REJECTED
        assessment.rejection_reason = "implausible device clock with mocked location"

    event = AttendanceEvent(
        school_id=membership.school_id,
        membership=membership,
        campus=campus,
        device=device if device_error == "" else None,
        kind=kind,
        captured_at=captured_at,
        clock_skew_seconds=skew,
        client_event_id=client_event_id,
        payload_digest=digest,
        disposition=assessment.disposition,
        confidence=assessment.confidence,
        rejection_reason=assessment.rejection_reason[:64],
        policy_version=policy.version,
    )
    try:
        event.save()
    except IntegrityError as exc:                       # concurrent duplicate
        raise DuplicateEventConflictError("Event already recorded.") from exc

    AttendanceSignal.objects.bulk_create([
        AttendanceSignal(
            school_id=membership.school_id,
            event=event,
            signal_type=r.signal_type,
            verdict=r.verdict,
            weight_applied=policy.weight_for(r.signal_type),
            hard_fail=r.hard_fail,
            detail=r.detail[:200],
            payload=r.payload,
        )
        for r in results
    ])

    # Consume the code only once the event is safely written, so a failure
    # later in this transaction does not burn a nonce the teacher could
    # legitimately reuse on retry.
    if nonce and assessment.accepted:
        try:
            qr.redeem({"non": nonce}, membership=membership)
        except qr.QrAlreadyRedeemedError:
            pass        # Already recorded by an earlier attempt; harmless here.

    record = recompute_record(membership, timezone.localtime(captured_at).date())

    OutboxMessage.objects.create(
        school_id=membership.school_id,
        topic="presence.event.recorded",
        payload={
            "event_id": str(event.id),
            "membership_id": str(membership.pk),
            "kind": kind,
            "disposition": event.disposition,
            "confidence": event.confidence,
            "date": str(record.date),
        },
    )
    audit.record(
        action=f"presence.{kind}",
        object_type="presence.AttendanceEvent",
        object_id=event.id,
        actor_membership=actor_membership or membership,
        after={"disposition": event.disposition, "confidence": event.confidence,
               "campus": str(campus.id)},
        device=event.device,
    )
    return CheckInOutcome(event, record, created=True)


def _max_backdate_days() -> int:
    from django.conf import settings
    return getattr(settings, "SYNC_MAX_BACKDATE_DAYS", 14)


def _mock_location(raw_signals) -> bool:
    return bool((raw_signals.get(SignalType.GEOFENCE) or {}).get("mocked"))


def _record_for(membership, event) -> AttendanceRecord:
    return AttendanceRecord.objects.get(
        membership=membership, date=timezone.localtime(event.captured_at).date()
    )


# -- Derivation --------------------------------------------------------------


def recompute_record(membership: Membership, date) -> AttendanceRecord:
    """Rebuild one person-day from its events. Idempotent by construction.

    Everything a dashboard shows comes from here, and here comes only from
    stored facts -- so a policy revision can be replayed over history without
    anyone editing a record by hand.
    """
    policy = active_policy(membership.school_id)
    events = list(
        AttendanceEvent.objects.filter(membership=membership,
                                       captured_at__date=date)
        .order_by("captured_at")
    )
    accepted = [e for e in events if e.is_accepted]

    check_ins = [e for e in accepted if e.kind == AttendanceEvent.Kind.CHECK_IN]
    check_outs = [e for e in accepted if e.kind == AttendanceEvent.Kind.CHECK_OUT]

    first_in = check_ins[0].captured_at if check_ins else None
    last_out = check_outs[-1].captured_at if check_outs else None

    if first_in is None:
        status = AttendanceStatus.ABSENT
        disposition = Disposition.REJECTED if events else Disposition.PROVISIONAL
        confidence = 0
    else:
        confidence = check_ins[0].confidence
        disposition = check_ins[0].disposition
        local_in = timezone.localtime(first_in).time()
        late_cutoff = _late_cutoff(policy, membership, first_in)
        if last_out is None:
            # Signed in and never signed out. Not "present": this is exactly
            # the sign-in-and-leave pattern, and calling it present is the
            # failure the product exists to correct.
            status = AttendanceStatus.PARTIAL
        elif local_in > late_cutoff:
            status = AttendanceStatus.LATE
        else:
            status = AttendanceStatus.PRESENT

    minutes = 0
    if first_in and last_out and last_out > first_in:
        minutes = int((last_out - first_in).total_seconds() // 60)

    record, _created = AttendanceRecord.objects.update_or_create(
        membership=membership,
        date=date,
        defaults={
            "school_id": membership.school_id,
            "status": status,
            "disposition": disposition,
            "confidence": confidence,
            "first_in_at": first_in,
            "last_out_at": last_out,
            "minutes_on_site": minutes,
            "policy_version": policy.version,
        },
    )
    return record


def _late_cutoff(policy, membership, at):
    from datetime import datetime

    from .models import DutySchedule

    local = timezone.localtime(at)
    schedule = DutySchedule.objects.filter(membership=membership,
                                           weekday=local.weekday()).first()
    starts_at = schedule.starts_at if schedule else policy.day_starts_at
    return (datetime.combine(local.date(), starts_at)
            + timedelta(minutes=policy.late_grace_minutes)).time()


# -- Review and appeal -------------------------------------------------------


def raise_appeal(*, record: AttendanceRecord, raised_by: Membership,
                 reason_code: str, narrative: str = "",
                 requested_status: str = "") -> AttendanceException:
    if AttendanceException.objects.filter(
        record=record, decision=AttendanceException.Decision.PENDING
    ).exists():
        raise PresenceError("An appeal on this record is already open.")

    exception = AttendanceException.objects.create(
        school_id=record.school_id, record=record, raised_by=raised_by,
        reason_code=reason_code, narrative=narrative,
        requested_status=requested_status,
    )
    audit.record(action="presence.appeal.raised",
                 object_type="presence.AttendanceException",
                 object_id=exception.id, actor_membership=raised_by,
                 after={"record": str(record.id), "reason": reason_code})
    OutboxMessage.objects.create(
        school_id=record.school_id, topic="presence.appeal.raised",
        payload={"exception_id": str(exception.id), "record_id": str(record.id)},
    )
    return exception


@transaction.atomic
def decide_appeal(*, exception: AttendanceException, decided_by: Membership,
                  decision: str, note: str = "") -> AttendanceException:
    """Resolve an appeal, adjusting the record without erasing the evidence."""
    if exception.decision != AttendanceException.Decision.PENDING:
        raise PresenceError("This appeal has already been decided.")

    record = exception.record
    before = {"status": record.status, "disposition": record.disposition,
              "resolution": record.resolution}

    exception.decision = decision
    exception.decided_by = decided_by
    exception.decided_at = timezone.now()
    exception.decision_note = note
    exception.save(update_fields=["decision", "decided_by", "decided_at",
                                  "decision_note", "updated_at"])

    if decision == AttendanceException.Decision.UPHELD:
        if exception.requested_status:
            record.status = exception.requested_status
        record.disposition = Disposition.VERIFIED
        record.resolution = AttendanceRecord.Resolution.OVERRIDDEN
    else:
        record.resolution = AttendanceRecord.Resolution.REVIEWED
    record.save(update_fields=["status", "disposition", "resolution", "updated_at"])

    audit.record(action="presence.appeal.decided",
                 object_type="presence.AttendanceException",
                 object_id=exception.id, actor_membership=decided_by,
                 before=before,
                 after={"decision": decision, "status": record.status,
                        "resolution": record.resolution, "note": note})
    return exception


# -- Health metrics ----------------------------------------------------------


def health_metrics(*, since, until) -> dict:
    """The two numbers that govern whether this subsystem can be trusted.

    Provisional rate above 10% means the weights are miscalibrated for this
    school. Appeals upheld above 2% means the model is producing false
    accusations, and nobody should be disciplined on its output until that is
    fixed (ADR-0002).
    """
    records = AttendanceRecord.objects.filter(date__gte=since, date__lte=until)
    total = records.count()
    provisional = records.filter(disposition=Disposition.PROVISIONAL).count()

    appeals = AttendanceException.objects.filter(record__date__gte=since,
                                                 record__date__lte=until)
    upheld = appeals.filter(decision=AttendanceException.Decision.UPHELD).count()

    return {
        "records": total,
        "provisional_rate": round(provisional / total, 4) if total else 0.0,
        "provisional_rate_exceeded": bool(total and provisional / total > 0.10),
        "appeals": appeals.count(),
        "appeals_upheld": upheld,
        "upheld_rate": round(upheld / total, 4) if total else 0.0,
        "upheld_rate_exceeded": bool(total and upheld / total > 0.02),
    }
