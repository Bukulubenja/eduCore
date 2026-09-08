"""Student movement use cases: the pass-out and trip state machines.

Every transition here does the same three things: move the workflow's
``status``, append an event to the audit chain, and drop an ``OutboxMessage``
for `comms` to turn into notifications. Guardian recipients are resolved *here*
-- deciding who may be told about a child is this module's business, not
`comms`'s (doc 03) -- and travel in the payload already filtered for
verification and opt-out.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.utils import timezone

from educore.core import audit
from educore.core.models import Membership, OutboxMessage
from educore.core.tenancy import TenantContext
from educore.students.models import GuardianLink, Student

from .models import (
    OPEN_PASS_OUT_STATES,
    PassOut,
    Trip,
    TripParticipant,
    TripSupervisor,
)


class MovementError(Exception):
    """A movement request that cannot be processed in the workflow's state."""


# -- Guardian resolution ---------------------------------------------------


def _guardian_recipients(student_ids) -> list[dict]:
    """`{student_id, membership_id}` for every verified, opted-in guardian."""
    return [
        {"student_id": str(student_id), "membership_id": str(membership_id)}
        for student_id, membership_id in (
            GuardianLink.objects
            .filter(student_id__in=list(student_ids), verified=True,
                    receives_notifications=True)
            .values_list("student_id", "membership_id")
        )
    ]


# -- Pass-outs -----------------------------------------------------------------


@transaction.atomic
def request_pass_out(*, student: Student, requested_by: Membership, reason: str,
                     destination: str, responsible_person: str,
                     expected_return_at, narrative: str = "") -> PassOut:
    """Open a pass-out request for a boarder.

    Refused if the student already has one open -- a second live pass-out for
    the same child is always a mistake, and the partial unique constraint
    would reject the write anyway; this turns that into a readable error.
    """
    if PassOut.objects.filter(student=student,
                              status__in=OPEN_PASS_OUT_STATES).exists():
        raise MovementError(
            "This student already has a pass-out in progress."
        )

    try:
        pass_out = PassOut.objects.create(
            school_id=student.school_id,
            student=student,
            reason=reason,
            destination=destination,
            responsible_person=responsible_person,
            narrative=narrative,
            requested_by=requested_by,
            expected_return_at=expected_return_at,
        )
    except IntegrityError as exc:                       # concurrent request
        raise MovementError(
            "This student already has a pass-out in progress."
        ) from exc

    audit.record(action="movement.pass_out.requested",
                 object_type="movement.PassOut", object_id=pass_out.id,
                 actor_membership=requested_by,
                 after={"student": str(student.id), "reason": reason,
                        "expected_return_at": expected_return_at.isoformat()})
    OutboxMessage.objects.create(
        school_id=student.school_id, topic="movement.pass_out.requested",
        payload={"pass_out_id": str(pass_out.id), "student_id": str(student.id),
                 "reason": reason,
                 "expected_return_at": expected_return_at.isoformat()},
    )
    return pass_out


@transaction.atomic
def decide_pass_out(*, pass_out: PassOut, decided_by: Membership, approved: bool,
                    note: str = "", at=None) -> PassOut:
    if pass_out.status != PassOut.Status.REQUESTED:
        raise MovementError("This pass-out has already been decided.")

    at = at or timezone.now()
    pass_out.status = (PassOut.Status.APPROVED if approved
                       else PassOut.Status.DENIED)
    pass_out.approved_by = decided_by
    pass_out.decided_at = at
    pass_out.decision_note = note
    pass_out.save(update_fields=["status", "approved_by", "decided_at",
                                 "decision_note", "updated_at"])

    audit.record(action="movement.pass_out.decided",
                 object_type="movement.PassOut", object_id=pass_out.id,
                 actor_membership=decided_by,
                 after={"status": pass_out.status, "note": note})

    if approved:
        OutboxMessage.objects.create(
            school_id=pass_out.school_id, topic="movement.pass_out.approved",
            payload={"pass_out_id": str(pass_out.id),
                     "student_id": str(pass_out.student_id),
                     "requester_membership_id": str(pass_out.requested_by_id),
                     "expected_return_at":
                         pass_out.expected_return_at.isoformat(),
                     "recipients": _guardian_recipients([pass_out.student_id])},
        )
    else:
        OutboxMessage.objects.create(
            school_id=pass_out.school_id, topic="movement.pass_out.denied",
            payload={"pass_out_id": str(pass_out.id),
                     "student_id": str(pass_out.student_id),
                     "requester_membership_id": str(pass_out.requested_by_id),
                     "note": note},
        )
    return pass_out


@transaction.atomic
def record_departure(*, pass_out: PassOut, at=None, gate_event=None) -> PassOut:
    if pass_out.status != PassOut.Status.APPROVED:
        raise MovementError("Only an approved pass-out can be marked departed.")

    at = at or timezone.now()
    pass_out.status = PassOut.Status.DEPARTED
    pass_out.departed_at = at
    pass_out.departure_gate_event = gate_event
    pass_out.save(update_fields=["status", "departed_at",
                                 "departure_gate_event", "updated_at"])

    audit.record(action="movement.pass_out.departed",
                 object_type="movement.PassOut", object_id=pass_out.id,
                 after={"departed_at": at.isoformat()})
    OutboxMessage.objects.create(
        school_id=pass_out.school_id, topic="movement.pass_out.departed",
        payload={"pass_out_id": str(pass_out.id),
                 "student_id": str(pass_out.student_id),
                 "departed_at": at.isoformat(),
                 "expected_return_at": pass_out.expected_return_at.isoformat(),
                 "recipients": _guardian_recipients([pass_out.student_id])},
    )
    return pass_out


@transaction.atomic
def record_return(*, pass_out: PassOut, at=None, gate_event=None) -> PassOut:
    if pass_out.status != PassOut.Status.DEPARTED:
        raise MovementError("Only a departed pass-out can be marked returned.")

    at = at or timezone.now()
    pass_out.status = PassOut.Status.RETURNED
    pass_out.returned_at = at
    pass_out.return_gate_event = gate_event
    pass_out.save(update_fields=["status", "returned_at", "return_gate_event",
                                 "updated_at"])

    audit.record(action="movement.pass_out.returned",
                 object_type="movement.PassOut", object_id=pass_out.id,
                 after={"returned_at": at.isoformat()})
    OutboxMessage.objects.create(
        school_id=pass_out.school_id, topic="movement.pass_out.returned",
        payload={"pass_out_id": str(pass_out.id),
                 "student_id": str(pass_out.student_id),
                 "returned_at": at.isoformat(),
                 "recipients": _guardian_recipients([pass_out.student_id])},
    )
    return pass_out


@transaction.atomic
def cancel_pass_out(*, pass_out: PassOut, actor: Membership,
                    note: str = "") -> PassOut:
    if pass_out.status not in (PassOut.Status.REQUESTED,
                               PassOut.Status.APPROVED):
        raise MovementError("A pass-out can only be cancelled before departure.")

    pass_out.status = PassOut.Status.CANCELLED
    pass_out.decision_note = note or pass_out.decision_note
    pass_out.save(update_fields=["status", "decision_note", "updated_at"])
    audit.record(action="movement.pass_out.cancelled",
                 object_type="movement.PassOut", object_id=pass_out.id,
                 actor_membership=actor, after={"note": note})
    return pass_out


def overdue_pass_outs(*, now=None):
    now = now or timezone.now()
    return (PassOut.objects
            .filter(status=PassOut.Status.DEPARTED,
                    expected_return_at__lt=now)
            .select_related("student"))


# -- Trips -------------------------------------------------------------------


@transaction.atomic
def create_trip(*, created_by: Membership, title: str, purpose: str,
                destination: str, departs_at, returns_at) -> Trip:
    if returns_at <= departs_at:
        raise MovementError("A trip must return after it departs.")
    trip = Trip.objects.create(
        school_id=created_by.school_id, created_by=created_by, title=title,
        purpose=purpose, destination=destination, departs_at=departs_at,
        returns_at=returns_at,
    )
    audit.record(action="movement.trip.created",
                 object_type="movement.Trip", object_id=trip.id,
                 actor_membership=created_by, after={"title": title})
    return trip


@transaction.atomic
def set_trip_roster(*, trip: Trip, student_ids: list, supervisor_ids: list,
                    lead_id=None) -> Trip:
    """Replace a trip's participants and supervisors from the database.

    Only while the trip is still editable (draft or submitted). Every id is
    checked against this tenant's active records; the tenant-scoped manager
    means an id from another school simply does not resolve.
    """
    if trip.status not in Trip.ROSTER_EDITABLE_STATES:
        raise MovementError("The roster is fixed once the trip is approved.")

    students = list(
        Student.objects.filter(id__in=student_ids,
                               status=Student.Status.ENROLLED)
    )
    if len(students) != len(set(map(str, student_ids))):
        raise MovementError(
            "One or more students are not enrolled at this school."
        )

    supervisors = list(
        Membership.objects.filter(id__in=supervisor_ids,
                                  status=Membership.Status.ACTIVE)
    )
    if len(supervisors) != len(set(map(str, supervisor_ids))):
        raise MovementError("One or more supervisors are not active staff.")
    if lead_id is not None and str(lead_id) not in {str(s.pk) for s in supervisors}:
        raise MovementError("The lead supervisor must be on the supervisor list.")

    TripParticipant.objects.filter(trip=trip).delete()
    TripParticipant.objects.bulk_create([
        TripParticipant(school_id=trip.school_id, trip=trip, student=student)
        for student in students
    ])
    TripSupervisor.objects.filter(trip=trip).delete()
    TripSupervisor.objects.bulk_create([
        TripSupervisor(school_id=trip.school_id, trip=trip, membership=member,
                       is_lead=(lead_id is not None and str(member.pk) == str(lead_id)))
        for member in supervisors
    ])

    audit.record(action="movement.trip.roster_set",
                 object_type="movement.Trip", object_id=trip.id,
                 after={"students": len(students),
                        "supervisors": len(supervisors)})
    return trip


@transaction.atomic
def submit_trip(*, trip: Trip, actor: Membership) -> Trip:
    if trip.status != Trip.Status.DRAFT:
        raise MovementError("Only a draft trip can be submitted.")
    if not trip.participants.exists():
        raise MovementError("Add at least one student before submitting.")
    if not trip.supervisors.exists():
        raise MovementError("Add at least one supervising teacher before submitting.")

    trip.status = Trip.Status.SUBMITTED
    trip.save(update_fields=["status", "updated_at"])
    audit.record(action="movement.trip.submitted",
                 object_type="movement.Trip", object_id=trip.id,
                 actor_membership=actor)
    OutboxMessage.objects.create(
        school_id=trip.school_id, topic="movement.trip.submitted",
        payload={"trip_id": str(trip.id), "title": trip.title,
                 "departs_at": trip.departs_at.isoformat(),
                 "participant_count": trip.participants.count()},
    )
    return trip


@transaction.atomic
def decide_trip(*, trip: Trip, decided_by: Membership, approved: bool,
                note: str = "", at=None) -> Trip:
    if trip.status != Trip.Status.SUBMITTED:
        raise MovementError("This trip is not awaiting a decision.")

    at = at or timezone.now()
    trip.status = Trip.Status.APPROVED if approved else Trip.Status.REJECTED
    trip.approved_by = decided_by
    trip.decided_at = at
    trip.decision_note = note
    trip.save(update_fields=["status", "approved_by", "decided_at",
                             "decision_note", "updated_at"])
    audit.record(action="movement.trip.decided",
                 object_type="movement.Trip", object_id=trip.id,
                 actor_membership=decided_by,
                 after={"status": trip.status, "note": note})

    if approved:
        student_ids = list(trip.participants.values_list("student_id", flat=True))
        OutboxMessage.objects.create(
            school_id=trip.school_id, topic="movement.trip.approved",
            payload={"trip_id": str(trip.id), "title": trip.title,
                     "departs_at": trip.departs_at.isoformat(),
                     "returns_at": trip.returns_at.isoformat(),
                     "recipients": _guardian_recipients(student_ids)},
        )
    else:
        OutboxMessage.objects.create(
            school_id=trip.school_id, topic="movement.trip.rejected",
            payload={"trip_id": str(trip.id),
                     "creator_membership_id": str(trip.created_by_id),
                     "note": note},
        )
    return trip


@transaction.atomic
def record_trip_departure(*, trip: Trip, at=None,
                          present_student_ids=None) -> Trip:
    if trip.status != Trip.Status.APPROVED:
        raise MovementError("Only an approved trip can depart.")

    at = at or timezone.now()
    participants = trip.participants.all()
    if present_student_ids is not None:
        present = {str(s) for s in present_student_ids}
        participants = [p for p in participants if str(p.student_id) in present]
    else:
        participants = list(participants)

    for participant in participants:
        participant.departure_recorded_at = at
    TripParticipant.objects.bulk_update(participants, ["departure_recorded_at"])

    trip.status = Trip.Status.DEPARTED
    trip.actual_departed_at = at
    trip.save(update_fields=["status", "actual_departed_at", "updated_at"])
    audit.record(action="movement.trip.departed",
                 object_type="movement.Trip", object_id=trip.id,
                 after={"departed_at": at.isoformat(),
                        "students": len(participants)})
    OutboxMessage.objects.create(
        school_id=trip.school_id, topic="movement.trip.departed",
        payload={"trip_id": str(trip.id), "title": trip.title,
                 "departed_at": at.isoformat(),
                 "recipients": _guardian_recipients(
                     [p.student_id for p in participants])},
    )
    return trip


@transaction.atomic
def record_trip_return(*, trip: Trip, at=None, returned_student_ids=None) -> Trip:
    if trip.status != Trip.Status.DEPARTED:
        raise MovementError("Only a departed trip can return.")

    at = at or timezone.now()
    participants = list(trip.participants.all())
    if returned_student_ids is not None:
        returned = {str(s) for s in returned_student_ids}
        to_stamp = [p for p in participants if str(p.student_id) in returned]
    else:
        to_stamp = participants

    for participant in to_stamp:
        participant.return_recorded_at = at
    TripParticipant.objects.bulk_update(to_stamp, ["return_recorded_at"])

    trip.status = Trip.Status.RETURNED
    trip.actual_returned_at = at
    trip.save(update_fields=["status", "actual_returned_at", "updated_at"])

    unreturned = [p for p in participants if p.return_recorded_at is None]
    audit.record(action="movement.trip.returned",
                 object_type="movement.Trip", object_id=trip.id,
                 after={"returned_at": at.isoformat(),
                        "unreturned": len(unreturned)})
    OutboxMessage.objects.create(
        school_id=trip.school_id, topic="movement.trip.returned",
        payload={"trip_id": str(trip.id), "title": trip.title,
                 "unreturned": len(unreturned),
                 "recipients": _guardian_recipients(
                     [p.student_id for p in to_stamp])},
    )
    return trip


@transaction.atomic
def cancel_trip(*, trip: Trip, actor: Membership, note: str = "") -> Trip:
    if trip.status not in (Trip.Status.DRAFT, Trip.Status.SUBMITTED,
                           Trip.Status.APPROVED):
        raise MovementError("A trip can only be cancelled before departure.")
    trip.status = Trip.Status.CANCELLED
    trip.decision_note = note or trip.decision_note
    trip.save(update_fields=["status", "decision_note", "updated_at"])
    audit.record(action="movement.trip.cancelled",
                 object_type="movement.Trip", object_id=trip.id,
                 actor_membership=actor, after={"note": note})
    return trip


def overdue_trips(*, now=None):
    now = now or timezone.now()
    return Trip.objects.filter(status=Trip.Status.DEPARTED, returns_at__lt=now)


# -- Read models -------------------------------------------------------------


def students_off_campus(*, now=None) -> list[dict]:
    """Every student currently off campus with authorisation.

    The union of a departed-not-returned pass-out and a participant on a
    departed trip. Feeds the "students currently out" line on the dashboard
    (SSOMS §15) and the overdue-return check.
    """
    now = now or timezone.now()
    out: dict[str, dict] = {}

    for pass_out in (PassOut.objects
                     .filter(status=PassOut.Status.DEPARTED)
                     .select_related("student")):
        out[str(pass_out.student_id)] = {
            "student_id": str(pass_out.student_id),
            "student_name": pass_out.student.full_name,
            "via": "pass_out",
            "reference_id": str(pass_out.id),
            "expected_back": pass_out.expected_return_at.isoformat(),
            "overdue": pass_out.is_overdue(now=now),
        }

    for participant in (TripParticipant.objects
                        .filter(trip__status=Trip.Status.DEPARTED,
                                return_recorded_at__isnull=True)
                        .select_related("student", "trip")):
        out.setdefault(str(participant.student_id), {
            "student_id": str(participant.student_id),
            "student_name": participant.student.full_name,
            "via": "trip",
            "reference_id": str(participant.trip_id),
            "expected_back": participant.trip.returns_at.isoformat(),
            "overdue": participant.trip.is_overdue(now=now),
        })

    return sorted(out.values(), key=lambda row: row["expected_back"])


# -- Scheduled sweep --------------------------------------------------------


def sweep_overdue(*, now=None) -> dict:
    """Emit one overdue event per newly-overdue pass-out and trip.

    `comms` collapses repeat runs onto a single notification per record (the
    dedupe key carries only the record id), so this is safe to run on a tight
    schedule.
    """
    TenantContext.require()
    now = now or timezone.now()
    counts = {"pass_outs": 0, "trips": 0}  # nosec B105 -- "pass" in a dict key

    for pass_out in overdue_pass_outs(now=now):
        OutboxMessage.objects.create(
            school_id=pass_out.school_id, topic="movement.pass_out.overdue",
            payload={"pass_out_id": str(pass_out.id),
                     "student_id": str(pass_out.student_id),
                     "expected_return_at":
                         pass_out.expected_return_at.isoformat(),
                     "recipients": _guardian_recipients([pass_out.student_id])},
        )
        counts["pass_outs"] += 1

    for trip in overdue_trips(now=now):
        OutboxMessage.objects.create(
            school_id=trip.school_id, topic="movement.trip.overdue",
            payload={"trip_id": str(trip.id), "title": trip.title,
                     "returns_at": trip.returns_at.isoformat()},
        )
        counts["trips"] += 1

    return counts
