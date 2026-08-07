"""Check-in flow, QR handling, derivation, and the appeal loop."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

from educore.core.tenancy import TenantContext
from educore.presence import qr, services
from educore.presence.models import (
    AttendanceEvent,
    AttendanceException,
    AttendanceRecord,
    AttendanceSignal,
    AttendanceStatus,
    Disposition,
    SignalType,
)

pytestmark = pytest.mark.django_db


def check_in(teacher, campus, at, signals, kind=AttendanceEvent.Kind.CHECK_IN,
             client_event_id=None):
    with TenantContext.scope(teacher.school_id):
        return services.submit_event(
            membership=teacher, campus=campus, kind=kind, captured_at=at,
            client_event_id=client_event_id or uuid.uuid4(),
            raw_signals=signals,
        )


# -- The happy path ----------------------------------------------------------


def test_good_evidence_verifies_and_records_present(teacher, campus, morning,
                                                    good_signals, policy):
    outcome = check_in(teacher, campus, morning, good_signals(morning))

    assert outcome.created
    assert outcome.event.disposition == Disposition.VERIFIED
    assert outcome.event.confidence >= 75
    assert outcome.record.status == AttendanceStatus.PARTIAL   # no check-out yet


def test_every_signal_is_stored_for_later_recomputation(teacher, campus, morning,
                                                        good_signals, policy):
    outcome = check_in(teacher, campus, morning, good_signals(morning))

    with TenantContext.scope(teacher.school_id):
        stored = {s.signal_type: s for s in
                  AttendanceSignal.objects.filter(event=outcome.event)}

    assert SignalType.QR in stored
    assert stored[SignalType.GEOFENCE].payload["distance_m"] < 50
    # Retained even when they contributed nothing, so a policy revision can be
    # replayed over history without the evidence having been thrown away.
    assert SignalType.BEACON in stored


def test_check_out_completes_the_day(teacher, campus, morning, good_signals, policy):
    check_in(teacher, campus, morning, good_signals(morning))
    evening = morning.replace(hour=16, minute=45)
    outcome = check_in(teacher, campus, evening, good_signals(evening),
                       kind=AttendanceEvent.Kind.CHECK_OUT)

    assert outcome.record.status == AttendanceStatus.PRESENT
    assert outcome.record.minutes_on_site > 500


def test_late_arrival_is_recorded_as_late(teacher, campus, morning, good_signals,
                                          policy):
    late = morning.replace(hour=8, minute=30)
    outcome = check_in(teacher, campus, late, good_signals(late))
    evening = morning.replace(hour=16, minute=0)
    outcome = check_in(teacher, campus, evening, good_signals(evening),
                       kind=AttendanceEvent.Kind.CHECK_OUT)

    assert outcome.record.status == AttendanceStatus.LATE


def test_sign_in_and_leave_is_partial_not_present(teacher, campus, morning,
                                                  good_signals, policy):
    """The pattern the product exists to expose.

    A day with no check-out must never read as `present`; that is precisely
    the failure schools already have with paper.
    """
    outcome = check_in(teacher, campus, morning, good_signals(morning))
    assert outcome.record.status == AttendanceStatus.PARTIAL


# -- Nobody gets locked out --------------------------------------------------


def test_missing_sensors_do_not_block_the_teacher(teacher, campus, morning,
                                                  good_signals, policy):
    """No GPS, no Wi-Fi: still recorded, possibly for review."""
    signals = good_signals(morning)
    del signals[SignalType.GEOFENCE]
    del signals[SignalType.WIFI]

    outcome = check_in(teacher, campus, morning, signals)

    assert outcome.event.is_accepted
    assert outcome.record.status in {AttendanceStatus.PARTIAL, AttendanceStatus.PRESENT}


def test_weak_evidence_is_provisional_and_queued_for_review(teacher, campus,
                                                            morning, policy):
    """No QR, no device: recorded, flagged, not refused."""
    outcome = check_in(teacher, campus, morning, {
        SignalType.GEOFENCE: {"lat": 0.3136, "lon": 32.5811, "accuracy_m": 20},
    })

    assert outcome.event.disposition == Disposition.PROVISIONAL
    assert outcome.record.needs_review


# -- Fraud controls ----------------------------------------------------------


def test_check_in_from_far_away_with_a_confident_fix_is_rejected(
    teacher, campus, morning, good_signals, policy
):
    signals = good_signals(morning)
    signals[SignalType.GEOFENCE] = {"lat": 0.4500, "lon": 32.9000, "accuracy_m": 8}

    outcome = check_in(teacher, campus, morning, signals)

    assert outcome.event.disposition == Disposition.REJECTED
    assert "geofence" in outcome.event.rejection_reason


def test_an_imprecise_fix_outside_the_fence_accuses_nobody(
    teacher, campus, morning, good_signals, policy
):
    """A 500m-accuracy fix is not evidence of absence.

    Treating it as such manufactures false accusations, and those destroy
    trust in the system far faster than fraud does.
    """
    signals = good_signals(morning)
    signals[SignalType.GEOFENCE] = {"lat": 0.4500, "lon": 32.9000, "accuracy_m": 500}

    outcome = check_in(teacher, campus, morning, signals)

    assert outcome.event.is_accepted
    with TenantContext.scope(teacher.school_id):
        geofence = AttendanceSignal.objects.get(event=outcome.event,
                                                signal_type=SignalType.GEOFENCE)
    assert geofence.verdict == "unavailable"


def test_mocked_location_is_rejected(teacher, campus, morning, good_signals, policy):
    signals = good_signals(morning)
    signals[SignalType.GEOFENCE]["mocked"] = True

    outcome = check_in(teacher, campus, morning, signals)
    assert outcome.event.disposition == Disposition.REJECTED


def test_unregistered_device_is_rejected(teacher, campus, morning, good_signals,
                                         policy):
    signals = good_signals(morning)
    signals[SignalType.DEVICE] = {"fingerprint": "some-other-phone"}

    outcome = check_in(teacher, campus, morning, signals)

    assert outcome.event.disposition == Disposition.REJECTED
    assert "not registered" in outcome.event.rejection_reason


def test_another_persons_device_is_rejected(teacher, campus, morning, good_signals,
                                            make_membership, school_a, policy):
    """Defeats "check me in, I'm running late" between colleagues."""
    signals = good_signals(morning)
    colleague = make_membership(school_a, email="colleague@example.com")

    outcome = check_in(colleague, campus, morning, signals)

    assert outcome.event.disposition == Disposition.REJECTED
    assert "another member of staff" in outcome.event.rejection_reason


# -- QR ----------------------------------------------------------------------


def test_expired_code_is_rejected(teacher, campus, morning, good_signals, policy):
    """The photograph-and-reuse attack, which is the dominant real one."""
    signals = good_signals(morning)

    outcome = check_in(teacher, campus, morning + timedelta(minutes=1), signals)

    assert outcome.event.disposition == Disposition.REJECTED
    assert "expired" in outcome.event.rejection_reason


def test_a_code_cannot_be_scanned_before_it_was_minted(teacher, campus, morning,
                                                       good_signals, policy):
    """Closes clock-backdating with a fresh code.

    Expiry alone only catches codes that are too old. A device whose clock is
    set back presents a code that is perfectly current against a captured_at
    from an hour ago, and without a not-before check that verifies happily.
    """
    signals = good_signals(morning)

    outcome = check_in(teacher, campus, morning - timedelta(hours=1), signals)

    assert outcome.event.disposition == Disposition.REJECTED
    assert "not been issued" in outcome.event.rejection_reason


def test_code_from_another_school_does_not_verify(teacher, campus, morning,
                                                  good_signals, school_b, policy):
    signals = good_signals(morning)
    foreign = qr.issue(school_id=school_b.id, campus_id=campus.id, ttl_seconds=30)
    signals[SignalType.QR] = {"token": foreign["token"]}

    outcome = check_in(teacher, campus, morning, signals)
    assert outcome.event.disposition == Disposition.REJECTED


def test_the_same_person_cannot_redeem_one_code_twice(teacher, campus, morning,
                                                      good_signals, policy):
    signals = good_signals(morning)
    check_in(teacher, campus, morning, signals)

    # A different client_event_id, so not an idempotent retry -- a real reuse.
    outcome = check_in(teacher, campus, morning + timedelta(seconds=5), signals)

    assert outcome.event.disposition == Disposition.REJECTED
    assert "already been used" in outcome.event.rejection_reason


def test_one_code_serves_everyone_arriving_in_its_window(
    teacher, campus, morning, good_signals, make_membership, school_a, policy
):
    """A displayed code is scanned by the whole staff room, not one person."""
    from educore.core.models import Device

    signals = good_signals(morning)
    colleague = make_membership(school_a, email="second@example.com")
    with TenantContext.scope(school_a):
        Device.objects.create(school_id=school_a.id, membership=colleague,
                              fingerprint="device-colleague", platform="android",
                              attested=True, status=Device.Status.ACTIVE)

    colleague_signals = dict(signals)
    colleague_signals[SignalType.DEVICE] = {"fingerprint": "device-colleague",
                                            "attestation": "ok"}

    first = check_in(teacher, campus, morning, signals)
    second = check_in(colleague, campus, morning, colleague_signals)

    assert first.event.disposition == Disposition.VERIFIED
    assert second.event.disposition == Disposition.VERIFIED


# -- Offline and idempotency -------------------------------------------------


def test_retrying_the_same_event_is_free(teacher, campus, morning, good_signals,
                                         policy):
    signals = good_signals(morning)
    event_id = uuid.uuid4()

    first = check_in(teacher, campus, morning, signals, client_event_id=event_id)
    second = check_in(teacher, campus, morning, signals, client_event_id=event_id)

    assert first.created is True
    assert second.created is False
    assert first.event.id == second.event.id
    with TenantContext.scope(teacher.school_id):
        assert AttendanceEvent.objects.count() == 1


def test_replaying_an_event_id_with_different_evidence_is_refused(
    teacher, campus, morning, good_signals, policy
):
    event_id = uuid.uuid4()
    check_in(teacher, campus, morning, good_signals(morning),
             client_event_id=event_id)

    with pytest.raises(services.DuplicateEventConflictError):
        check_in(teacher, campus, morning,
                 {SignalType.GEOFENCE: {"lat": 0.9, "lon": 32.0, "accuracy_m": 5}},
                 client_event_id=event_id)


def test_an_event_captured_offline_hours_ago_is_accepted(teacher, campus, morning,
                                                         good_signals, policy):
    """Captured at 07:20, synced at 16:00. Normal, not suspicious."""
    signals = good_signals(morning)
    outcome = check_in(teacher, campus, morning, signals)

    assert outcome.event.is_accepted
    assert outcome.event.clock_skew_seconds != 0


def test_events_older_than_the_queue_limit_are_refused(teacher, campus,
                                                       good_signals, policy):
    ancient = timezone.now() - timedelta(days=20)
    with pytest.raises(services.PresenceError):
        check_in(teacher, campus, ancient, good_signals(ancient))


def test_future_captures_are_refused(teacher, campus, good_signals, policy):
    future = timezone.now() + timedelta(hours=2)
    with pytest.raises(services.PresenceError):
        check_in(teacher, campus, future, good_signals(future))


# -- Derivation and policy versioning ----------------------------------------


def test_records_are_recomputable_from_stored_evidence(teacher, campus, morning,
                                                       good_signals, policy):
    outcome = check_in(teacher, campus, morning, good_signals(morning))

    with TenantContext.scope(teacher.school_id):
        AttendanceRecord.objects.filter(pk=outcome.record.pk).update(
            status=AttendanceStatus.ABSENT, confidence=0
        )
        rebuilt = services.recompute_record(teacher, outcome.record.date)

    assert rebuilt.status == AttendanceStatus.PARTIAL
    assert rebuilt.confidence == outcome.event.confidence


def test_policy_revision_supersedes_rather_than_edits(school_a, policy):
    with TenantContext.scope(school_a):
        revised = services.revise_policy(accept_threshold=80)
        current = services.active_policy(school_a.id)

    assert revised.version == policy.version + 1
    assert current.pk == revised.pk
    assert current.accept_threshold == 80
    # The old version survives so records citing it remain explicable.
    with TenantContext.scope(school_a):
        from educore.presence.models import AttendancePolicy
        assert AttendancePolicy.objects.count() == 2


def test_records_cite_the_policy_that_produced_them(teacher, campus, morning,
                                                    good_signals, policy):
    outcome = check_in(teacher, campus, morning, good_signals(morning))
    assert outcome.record.policy_version == policy.version


# -- Appeals -----------------------------------------------------------------


def test_appeal_upheld_corrects_the_record_without_erasing_evidence(
    teacher, campus, morning, deputy, policy
):
    outcome = check_in(teacher, campus, morning, {})       # no evidence at all

    with TenantContext.scope(teacher.school_id):
        exception = services.raise_appeal(
            record=outcome.record, raised_by=teacher,
            reason_code=AttendanceException.Reason.DEVICE_FAULT,
            narrative="Phone Wi-Fi broken since Monday.",
            requested_status=AttendanceStatus.PRESENT,
        )
        services.decide_appeal(exception=exception, decided_by=deputy,
                               decision=AttendanceException.Decision.UPHELD,
                               note="Confirmed with ICT.")
        record = AttendanceRecord.objects.get(pk=outcome.record.pk)
        events = AttendanceEvent.objects.filter(membership=teacher).count()

    assert record.status == AttendanceStatus.PRESENT
    assert record.resolution == AttendanceRecord.Resolution.OVERRIDDEN
    assert events == 1, "the original evidence must survive the override"


def test_only_one_appeal_may_be_open_per_record(teacher, campus, morning, policy):
    outcome = check_in(teacher, campus, morning, {})

    with TenantContext.scope(teacher.school_id):
        services.raise_appeal(record=outcome.record, raised_by=teacher,
                              reason_code=AttendanceException.Reason.OTHER)
        with pytest.raises(services.PresenceError):
            services.raise_appeal(record=outcome.record, raised_by=teacher,
                                  reason_code=AttendanceException.Reason.SICK)


def test_health_metrics_flag_a_miscalibrated_school(teacher, campus, morning,
                                                    policy):
    """Above 10% provisional means the weights are wrong, not the staff."""
    check_in(teacher, campus, morning, {})

    with TenantContext.scope(teacher.school_id):
        today = timezone.localdate()
        metrics = services.health_metrics(since=today - timedelta(days=1),
                                          until=today)

    assert metrics["records"] == 1
    assert metrics["provisional_rate"] == 1.0
    assert metrics["provisional_rate_exceeded"] is True
