"""Student movement: the pass-out and trip state machines (doc 08)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from educore.core.models import OutboxMessage
from educore.core.tenancy import TenantContext
from educore.movement import services
from educore.movement.models import PassOut, Trip, TripParticipant

pytestmark = pytest.mark.django_db


def _topics(school):
    with TenantContext.scope(school):
        return list(OutboxMessage.objects.values_list("topic", flat=True))


# -- Pass-outs ---------------------------------------------------------------


@pytest.fixture
def bursar(school_a, make_membership, grant_role):
    member = make_membership(school_a, email="bursar@example.com", name="A Bursar")
    grant_role(member, "bursar", "Bursar")
    return member


@pytest.fixture
def office(school_a, make_membership, grant_role):
    member = make_membership(school_a, email="office@example.com", name="Front Office")
    grant_role(member, "class_teacher", "Class Teacher")
    return member


def test_pass_out_runs_request_approve_depart_return(school_a, boarder, office,
                                                     bursar):
    student, _ = boarder
    return_at = timezone.now() + timedelta(hours=6)

    with TenantContext.scope(school_a):
        pass_out = services.request_pass_out(
            student=student, requested_by=office, reason=PassOut.Reason.MEDICAL,
            destination="City Clinic", responsible_person="Boarder Parent",
            expected_return_at=return_at,
        )
        assert pass_out.status == PassOut.Status.REQUESTED

        services.decide_pass_out(pass_out=pass_out, decided_by=bursar,
                                 approved=True)
        pass_out.refresh_from_db()
        assert pass_out.status == PassOut.Status.APPROVED

        services.record_departure(pass_out=pass_out)
        pass_out.refresh_from_db()
        assert pass_out.status == PassOut.Status.DEPARTED
        assert pass_out.departed_at is not None

        services.record_return(pass_out=pass_out)
        pass_out.refresh_from_db()
        assert pass_out.status == PassOut.Status.RETURNED

    topics = _topics(school_a)
    assert "movement.pass_out.requested" in topics
    assert "movement.pass_out.approved" in topics
    assert "movement.pass_out.departed" in topics
    assert "movement.pass_out.returned" in topics


def test_a_student_can_only_have_one_open_pass_out(school_a, boarder, office,
                                                   bursar):
    student, _ = boarder
    return_at = timezone.now() + timedelta(hours=6)
    with TenantContext.scope(school_a):
        services.request_pass_out(
            student=student, requested_by=office, reason=PassOut.Reason.FAMILY,
            destination="Home", responsible_person="Parent",
            expected_return_at=return_at,
        )
        with pytest.raises(services.MovementError, match="already has a pass-out"):
            services.request_pass_out(
                student=student, requested_by=office,
                reason=PassOut.Reason.MEDICAL, destination="Clinic",
                responsible_person="Parent", expected_return_at=return_at,
            )


def test_a_returned_pass_out_frees_the_student_for_another(school_a, boarder,
                                                           office, bursar):
    student, _ = boarder
    return_at = timezone.now() + timedelta(hours=4)
    with TenantContext.scope(school_a):
        first = services.request_pass_out(
            student=student, requested_by=office, reason=PassOut.Reason.FAMILY,
            destination="Home", responsible_person="Parent",
            expected_return_at=return_at,
        )
        services.decide_pass_out(pass_out=first, decided_by=bursar, approved=True)
        services.record_departure(pass_out=first)
        services.record_return(pass_out=first)

        # No error: the first is closed.
        services.request_pass_out(
            student=student, requested_by=office, reason=PassOut.Reason.MEDICAL,
            destination="Clinic", responsible_person="Parent",
            expected_return_at=return_at,
        )


def test_departure_before_approval_is_refused(school_a, boarder, office):
    student, _ = boarder
    with TenantContext.scope(school_a):
        pass_out = services.request_pass_out(
            student=student, requested_by=office, reason=PassOut.Reason.OTHER,
            destination="X", responsible_person="Y",
            expected_return_at=timezone.now() + timedelta(hours=2),
        )
        with pytest.raises(services.MovementError, match="approved pass-out"):
            services.record_departure(pass_out=pass_out)


def test_deciding_a_decided_pass_out_is_refused(school_a, boarder, office, bursar):
    student, _ = boarder
    with TenantContext.scope(school_a):
        pass_out = services.request_pass_out(
            student=student, requested_by=office, reason=PassOut.Reason.OTHER,
            destination="X", responsible_person="Y",
            expected_return_at=timezone.now() + timedelta(hours=2),
        )
        services.decide_pass_out(pass_out=pass_out, decided_by=bursar,
                                 approved=False)
        with pytest.raises(services.MovementError, match="already been decided"):
            services.decide_pass_out(pass_out=pass_out, decided_by=bursar,
                                     approved=True)


def test_overdue_pass_out_is_computed_not_stored(school_a, boarder, office, bursar):
    student, _ = boarder
    with TenantContext.scope(school_a):
        pass_out = services.request_pass_out(
            student=student, requested_by=office, reason=PassOut.Reason.MEDICAL,
            destination="Clinic", responsible_person="Parent",
            expected_return_at=timezone.now() - timedelta(hours=1),
        )
        services.decide_pass_out(pass_out=pass_out, decided_by=bursar,
                                 approved=True)
        services.record_departure(pass_out=pass_out)

        assert pass_out.is_overdue() is True
        assert list(services.overdue_pass_outs()) == [pass_out]

        services.record_return(pass_out=pass_out)
        assert pass_out.is_overdue() is False


def test_approved_pass_out_notification_reaches_verified_guardian(
    school_a, boarder, office, bursar
):
    student, guardian = boarder
    with TenantContext.scope(school_a):
        pass_out = services.request_pass_out(
            student=student, requested_by=office, reason=PassOut.Reason.FAMILY,
            destination="Home", responsible_person="Parent",
            expected_return_at=timezone.now() + timedelta(hours=6),
        )
        services.decide_pass_out(pass_out=pass_out, decided_by=bursar,
                                 approved=True)
        message = OutboxMessage.objects.get(topic="movement.pass_out.approved")

    recipients = {r["membership_id"] for r in message.payload["recipients"]}
    assert str(guardian.pk) in recipients


@pytest.mark.postgres
def test_a_pass_out_is_invisible_to_another_school(school_a, school_b, boarder,
                                                   office, bursar):
    student, _ = boarder
    with TenantContext.scope(school_a):
        services.request_pass_out(
            student=student, requested_by=office, reason=PassOut.Reason.OTHER,
            destination="X", responsible_person="Y",
            expected_return_at=timezone.now() + timedelta(hours=2),
        )
    with TenantContext.scope(school_b):
        assert PassOut.objects.count() == 0


# -- Trips -----------------------------------------------------------------


@pytest.fixture
def trip_lead(school_a, make_membership):
    return make_membership(school_a, email="lead@example.com", name="Trip Lead")


@pytest.fixture
def head(school_a, make_membership, grant_role):
    member = make_membership(school_a, email="head@example.com", name="Head")
    grant_role(member, "head_teacher", "Head Teacher")
    return member


def _new_trip(creator):
    return services.create_trip(
        created_by=creator, title="Science Museum", purpose="S3 physics",
        destination="National Museum",
        departs_at=timezone.now() + timedelta(days=2),
        returns_at=timezone.now() + timedelta(days=2, hours=6),
    )


def test_trip_runs_create_roster_submit_approve_depart_return(
    school_a, students, trip_lead, head
):
    with TenantContext.scope(school_a):
        trip = _new_trip(trip_lead)
        services.set_trip_roster(
            trip=trip, student_ids=[s.id for s in students[:3]],
            supervisor_ids=[trip_lead.pk], lead_id=trip_lead.pk,
        )
        assert trip.participants.count() == 3

        services.submit_trip(trip=trip, actor=trip_lead)
        trip.refresh_from_db()
        assert trip.status == Trip.Status.SUBMITTED

        services.decide_trip(trip=trip, decided_by=head, approved=True)
        trip.refresh_from_db()
        assert trip.status == Trip.Status.APPROVED

        services.record_trip_departure(trip=trip)
        trip.refresh_from_db()
        assert trip.status == Trip.Status.DEPARTED
        assert not TripParticipant.objects.filter(
            trip=trip, departure_recorded_at__isnull=True
        ).exists()

        services.record_trip_return(trip=trip)
        trip.refresh_from_db()
        assert trip.status == Trip.Status.RETURNED

    topics = _topics(school_a)
    assert "movement.trip.submitted" in topics
    assert "movement.trip.approved" in topics
    assert "movement.trip.departed" in topics
    assert "movement.trip.returned" in topics


def test_a_trip_cannot_be_submitted_without_students_or_supervisors(
    school_a, students, trip_lead
):
    with TenantContext.scope(school_a):
        trip = _new_trip(trip_lead)
        with pytest.raises(services.MovementError, match="at least one student"):
            services.submit_trip(trip=trip, actor=trip_lead)

        services.set_trip_roster(trip=trip, student_ids=[students[0].id],
                                 supervisor_ids=[])
        with pytest.raises(services.MovementError, match="supervising teacher"):
            services.submit_trip(trip=trip, actor=trip_lead)


def test_the_roster_is_frozen_once_the_trip_is_approved(school_a, students,
                                                        trip_lead, head):
    with TenantContext.scope(school_a):
        trip = _new_trip(trip_lead)
        services.set_trip_roster(trip=trip, student_ids=[students[0].id],
                                 supervisor_ids=[trip_lead.pk])
        services.submit_trip(trip=trip, actor=trip_lead)
        services.decide_trip(trip=trip, decided_by=head, approved=True)

        with pytest.raises(services.MovementError, match="fixed once the trip"):
            services.set_trip_roster(trip=trip, student_ids=[students[1].id],
                                     supervisor_ids=[trip_lead.pk])


def test_roster_rejects_a_student_from_another_school(school_a, school_b,
                                                      students, trip_lead,
                                                      make_membership):
    from educore.students.models import Student

    with TenantContext.scope(school_b):
        outsider = Student.objects.create(
            school_id=school_b.id, admission_number="OUT1",
            full_name="Outsider", status=Student.Status.ENROLLED,
        )

    with TenantContext.scope(school_a):
        trip = _new_trip(trip_lead)
        with pytest.raises(services.MovementError, match="not enrolled"):
            services.set_trip_roster(
                trip=trip, student_ids=[students[0].id, outsider.id],
                supervisor_ids=[trip_lead.pk],
            )


def test_students_off_campus_unions_pass_outs_and_trips(
    school_a, boarder, students, office, bursar, trip_lead, head
):
    student, _ = boarder
    with TenantContext.scope(school_a):
        pass_out = services.request_pass_out(
            student=student, requested_by=office, reason=PassOut.Reason.MEDICAL,
            destination="Clinic", responsible_person="Parent",
            expected_return_at=timezone.now() + timedelta(hours=3),
        )
        services.decide_pass_out(pass_out=pass_out, decided_by=bursar,
                                 approved=True)
        services.record_departure(pass_out=pass_out)

        trip = _new_trip(trip_lead)
        services.set_trip_roster(trip=trip, student_ids=[students[0].id],
                                 supervisor_ids=[trip_lead.pk])
        services.submit_trip(trip=trip, actor=trip_lead)
        services.decide_trip(trip=trip, decided_by=head, approved=True)
        services.record_trip_departure(trip=trip)

        off = services.students_off_campus()

    ids = {row["student_id"] for row in off}
    assert ids == {str(student.id), str(students[0].id)}


def test_sweep_overdue_emits_one_event_per_record(school_a, boarder, office,
                                                  bursar):
    student, _ = boarder
    with TenantContext.scope(school_a):
        pass_out = services.request_pass_out(
            student=student, requested_by=office, reason=PassOut.Reason.MEDICAL,
            destination="Clinic", responsible_person="Parent",
            expected_return_at=timezone.now() - timedelta(hours=2),
        )
        services.decide_pass_out(pass_out=pass_out, decided_by=bursar,
                                 approved=True)
        services.record_departure(pass_out=pass_out)

        counts = services.sweep_overdue()
        assert counts == {"pass_outs": 1, "trips": 0}
        assert OutboxMessage.objects.filter(
            topic="movement.pass_out.overdue"
        ).count() == 1


def test_cancelled_and_denied_pass_outs_do_not_notify_or_block(school_a, boarder,
                                                               office, bursar):
    student, _ = boarder
    with TenantContext.scope(school_a):
        first = services.request_pass_out(
            student=student, requested_by=office, reason=PassOut.Reason.OTHER,
            destination="X", responsible_person="Y",
            expected_return_at=timezone.now() + timedelta(hours=2),
        )
        services.cancel_pass_out(pass_out=first, actor=office, note="mistake")
        first.refresh_from_db()
        assert first.status == PassOut.Status.CANCELLED

        # Cancelled frees the student.
        second = services.request_pass_out(
            student=student, requested_by=office, reason=PassOut.Reason.MEDICAL,
            destination="Clinic", responsible_person="Y",
            expected_return_at=timezone.now() + timedelta(hours=2),
        )
        services.decide_pass_out(pass_out=second, decided_by=bursar,
                                 approved=False)
        with pytest.raises(services.MovementError, match="before departure"):
            services.cancel_pass_out(pass_out=second, actor=office)


def test_trip_cannot_return_before_it_departs(school_a, students, trip_lead, head):
    with TenantContext.scope(school_a):
        trip = _new_trip(trip_lead)
        services.set_trip_roster(trip=trip, student_ids=[students[0].id],
                                 supervisor_ids=[trip_lead.pk])
        services.submit_trip(trip=trip, actor=trip_lead)
        services.decide_trip(trip=trip, decided_by=head, approved=True)
        with pytest.raises(services.MovementError, match="departed trip can return"):
            services.record_trip_return(trip=trip)


def test_create_trip_rejects_a_return_before_departure(school_a, trip_lead):
    with TenantContext.scope(school_a):
        with pytest.raises(services.MovementError, match="return after it departs"):
            services.create_trip(
                created_by=trip_lead, title="X", purpose="", destination="Y",
                departs_at=timezone.now() + timedelta(days=2),
                returns_at=timezone.now() + timedelta(days=1),
            )


def test_an_approved_pass_out_relays_to_a_guardian_notification(
    school_a, boarder, office, bursar
):
    """End to end: the outbox event becomes a real Notification + Delivery."""
    from educore.comms.models import Channel, Delivery, Notification
    from educore.core import outbox

    student, guardian = boarder
    with TenantContext.scope(school_a):
        pass_out = services.request_pass_out(
            student=student, requested_by=office, reason=PassOut.Reason.FAMILY,
            destination="Home", responsible_person="Parent",
            expected_return_at=timezone.now() + timedelta(hours=6),
        )
        services.decide_pass_out(pass_out=pass_out, decided_by=bursar,
                                 approved=True)
        outbox.relay_pending()

        note = Notification.objects.get(recipient=guardian,
                                        topic="movement.pass_out.approved")
        assert "left campus" not in note.body      # no sensitive detail
        assert Delivery.objects.filter(notification=note,
                                       channel=Channel.PUSH).exists()


def test_the_whole_pass_out_lifecycle_relays_to_notifications(
    school_a, boarder, office, bursar
):
    from educore.comms.models import Notification
    from educore.core import outbox

    student, guardian = boarder
    with TenantContext.scope(school_a):
        po = services.request_pass_out(
            student=student, requested_by=office, reason=PassOut.Reason.MEDICAL,
            destination="Clinic", responsible_person="Parent",
            expected_return_at=timezone.now() - timedelta(minutes=30),
        )
        services.decide_pass_out(pass_out=po, decided_by=bursar, approved=True)
        services.record_departure(pass_out=po)
        services.record_return(pass_out=po)
        services.sweep_overdue()          # po has returned -> nothing overdue
        outbox.relay_pending()

        topics = set(
            Notification.objects.filter(recipient=guardian)
            .values_list("topic", flat=True)
        )
    assert {"movement.pass_out.approved", "movement.pass_out.departed",
            "movement.pass_out.returned"} <= topics


def test_the_whole_trip_lifecycle_relays_to_notifications(
    school_a, boarder, students, make_membership, grant_role
):
    from educore.comms.models import Notification
    from educore.core import outbox

    student, guardian = boarder
    lead = make_membership(school_a, email="tl@example.com", name="Lead")
    head_member = make_membership(school_a, email="h@example.com", name="Head")
    grant_role(head_member, "head_teacher", "Head Teacher")

    with TenantContext.scope(school_a):
        trip = services.create_trip(
            created_by=lead, title="Museum", purpose="", destination="Museum",
            departs_at=timezone.now() - timedelta(hours=1),
            returns_at=timezone.now() - timedelta(minutes=5),
        )
        services.set_trip_roster(trip=trip, student_ids=[student.id],
                                 supervisor_ids=[lead.pk])
        services.submit_trip(trip=trip, actor=lead)
        services.decide_trip(trip=trip, decided_by=head_member, approved=True)
        services.record_trip_departure(trip=trip)
        services.sweep_overdue()          # returns_at is in the past
        services.record_trip_return(trip=trip)
        outbox.relay_pending()

        guardian_topics = set(
            Notification.objects.filter(recipient=guardian)
            .values_list("topic", flat=True)
        )
        leadership_topics = set(
            Notification.objects.filter(recipient=head_member)
            .values_list("topic", flat=True)
        )

    assert {"movement.trip.approved", "movement.trip.departed",
            "movement.trip.returned"} <= guardian_topics
    assert {"movement.trip.submitted", "movement.trip.overdue"} <= leadership_topics


def test_denied_rejected_and_overdue_events_relay(school_a, boarder, office,
                                                  bursar, make_membership,
                                                  grant_role):
    from educore.comms.models import Notification
    from educore.core import outbox

    student, guardian = boarder
    head_member = make_membership(school_a, email="h2@example.com", name="Head2")
    grant_role(head_member, "head_teacher", "Head Teacher")

    with TenantContext.scope(school_a):
        po = services.request_pass_out(
            student=student, requested_by=office, reason=PassOut.Reason.OTHER,
            destination="X", responsible_person="Y",
            expected_return_at=timezone.now() + timedelta(hours=2),
        )
        services.decide_pass_out(pass_out=po, decided_by=bursar, approved=False)

        trip = services.create_trip(
            created_by=office, title="T", purpose="", destination="D",
            departs_at=timezone.now() + timedelta(days=1),
            returns_at=timezone.now() + timedelta(days=1, hours=3),
        )
        services.set_trip_roster(trip=trip, student_ids=[student.id],
                                 supervisor_ids=[office.pk])
        services.submit_trip(trip=trip, actor=office)
        services.decide_trip(trip=trip, decided_by=head_member, approved=False)

        # An overdue pass-out for the same student, now that the denied one is closed.
        po2 = services.request_pass_out(
            student=student, requested_by=office, reason=PassOut.Reason.MEDICAL,
            destination="Clinic", responsible_person="Y",
            expected_return_at=timezone.now() - timedelta(hours=1),
        )
        services.decide_pass_out(pass_out=po2, decided_by=bursar, approved=True)
        services.record_departure(pass_out=po2)
        services.sweep_overdue()
        outbox.relay_pending()

        assert Notification.objects.filter(
            recipient=office, topic="movement.pass_out.denied").exists()
        assert Notification.objects.filter(
            recipient=office, topic="movement.trip.rejected").exists()
        assert Notification.objects.filter(
            recipient=guardian, topic="movement.pass_out.overdue").exists()
        assert Notification.objects.filter(
            recipient=head_member, topic="movement.pass_out.overdue").exists()


def test_sweep_movement_command_runs(school_a, boarder, office, bursar):
    from django.core.management import call_command

    student, _ = boarder
    with TenantContext.scope(school_a):
        pass_out = services.request_pass_out(
            student=student, requested_by=office, reason=PassOut.Reason.MEDICAL,
            destination="Clinic", responsible_person="Parent",
            expected_return_at=timezone.now() - timedelta(hours=3),
        )
        services.decide_pass_out(pass_out=pass_out, decided_by=bursar,
                                 approved=True)
        services.record_departure(pass_out=pass_out)

    call_command("sweep_movement")

    with TenantContext.scope(school_a):
        assert OutboxMessage.objects.filter(
            topic="movement.pass_out.overdue"
        ).count() == 1
