"""Subscribers for the domain events other modules emit.

Registered from this app's ready(), so `core` never imports `comms` and the
leaf-module contract of ADR-0005 holds. Every handler here reads an event
payload and produces notifications; none of them reaches back into the module
that raised the event.

Handlers must be idempotent. The relay retries on failure, so a handler that
half-succeeded will be called again with the same payload -- `dedupe_key` is
what stops that becoming a second message to the same person.
"""

from __future__ import annotations

import logging

from educore.core.models import Membership
from educore.core.outbox import subscribe

from .models import Channel, Notification
from .services import notify

logger = logging.getLogger("educore.comms")


def _membership(membership_id) -> Membership | None:
    return Membership.objects.filter(pk=membership_id,
                                     status=Membership.Status.ACTIVE).first()


@subscribe("students.absence.detected")
def notify_guardians_of_absence(payload, *, message=None):
    """Tell verified guardians their child is not in class.

    The event carries the recipient list already filtered for verification and
    opt-out (see students.services): deciding *who* may be told about a child
    is the students module's business, not this one's.
    """
    date = payload["date"]
    for entry in payload.get("recipients", []):
        recipient = _membership(entry["membership_id"])
        if recipient is None:
            continue
        notify(
            recipient=recipient,
            topic="students.absence.detected",
            title="Your child was marked absent",
            body=(f"Our register for {date} shows your child was not in class. "
                  "If you believe this is wrong, please contact the school."),
            payload={"student_id": entry["student_id"], "date": date},
            importance=Notification.Importance.IMPORTANT,
            channels=[Channel.IN_APP, Channel.PUSH],
            dedupe_key=f"absence:{entry['student_id']}:{date}",
        )


@subscribe("students.on_site_not_in_class")
def alert_leadership_to_students_on_site(payload, *, message=None):
    """A child scanned in at the gate but missing from lessons.

    Goes to leadership rather than parents: this is a safeguarding question
    for the school to answer before anyone is telephoned.
    """
    date = payload["date"]
    students = payload.get("students", [])
    if not students:
        return

    for recipient in _leadership(message):
        notify(
            recipient=recipient,
            topic="students.on_site_not_in_class",
            title=f"{len(students)} student(s) on site but not in class",
            body=(f"On {date}, {len(students)} student(s) were scanned in at the "
                  "gate but marked absent from two or more lessons."),
            payload=payload,
            importance=Notification.Importance.URGENT,
            channels=[Channel.IN_APP, Channel.PUSH],
            dedupe_key=f"onsite:{date}",
        )


@subscribe("presence.event.recorded")
def alert_deputy_to_provisional_checkin(payload, *, message=None):
    """Only provisional check-ins are worth anyone's attention.

    Notifying on every arrival would bury the handful that need a human, which
    is how a review queue becomes something nobody opens.
    """
    if payload.get("disposition") != "provisional":
        return

    for recipient in _leadership(message):
        notify(
            recipient=recipient,
            topic="presence.event.recorded",
            title="A staff check-in needs review",
            body=("A check-in was recorded with weak evidence and is waiting in "
                  "the review queue."),
            payload=payload,
            channels=[Channel.IN_APP],
            dedupe_key=f"provisional:{payload['event_id']}",
        )


@subscribe("delivery.substitution.inferred")
def confirm_inferred_substitution(payload, *, message=None):
    for recipient in _leadership(message):
        notify(
            recipient=recipient,
            topic="delivery.substitution.inferred",
            title="A substitution needs confirming",
            body=("A lesson was opened by someone other than the timetabled "
                  "teacher. Please confirm the cover arrangement."),
            payload=payload,
            channels=[Channel.IN_APP],
            dedupe_key=f"substitution:{payload['lesson_instance_id']}",
        )


@subscribe("delivery.room.changed")
def note_room_change(payload, *, message=None):
    for recipient in _leadership(message):
        notify(
            recipient=recipient,
            topic="delivery.room.changed",
            title="A lesson was taught in a different room",
            body="A lesson was opened in a room other than the timetabled one.",
            payload=payload,
            channels=[Channel.IN_APP],
            dedupe_key=f"roomchange:{payload['lesson_instance_id']}",
        )


@subscribe("assessment.released")
def notify_guardians_of_results(payload, *, message=None):
    """Tell guardians their child's results are available.

    The notification carries no marks. A push preview on a lock screen is not
    a private channel, and a grade is not something to disclose to whoever is
    looking at the phone.

    Recipients arrive in the payload, already filtered for verification and
    opt-out by the module that raised the event.
    """
    for entry in payload.get("recipients", []):
        recipient = _membership(entry["membership_id"])
        if recipient is None:
            continue
        notify(
            recipient=recipient,
            topic="assessment.released",
            title="New results are available",
            body=(f"Results for {payload['title']} have been published. "
                  "Sign in to view them."),
            payload={"assessment_id": payload["assessment_id"],
                     "student_id": entry["student_id"]},
            importance=Notification.Importance.IMPORTANT,
            channels=[Channel.IN_APP, Channel.PUSH],
            dedupe_key=f"results:{payload['assessment_id']}:{entry['student_id']}",
        )


LEADERSHIP_ROLES = {"director", "head_teacher", "deputy", "dos"}


def _leadership(message) -> list[Membership]:
    """Active leadership memberships for the event's school.

    Scoped by the tenant the relay has already bound, so this cannot reach
    another school's staff even if a payload were tampered with.
    """
    from educore.core.models import RoleAssignment

    membership_ids = (
        RoleAssignment.objects
        .filter(role__code__in=LEADERSHIP_ROLES)
        .order_by()
        .values_list("membership_id", flat=True)
        .distinct()
    )
    return list(
        Membership.objects.filter(pk__in=list(membership_ids),
                                  status=Membership.Status.ACTIVE)
    )
