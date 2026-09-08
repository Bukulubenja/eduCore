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


@subscribe("presence.staff_absence.detected")
def alert_leadership_to_staff_absences(payload, *, message=None):
    """Staff expected on duty today who never checked in at all.

    A stronger claim than the provisional-check-in alert above: not weak
    evidence, but no evidence whatsoever. `presence` only decides *that* this
    happened and *who* -- deciding leadership should hear about it, and
    de-duplicating repeat beat runs onto one notification per day, is this
    module's job (dedupe_key carries only the date: the uniqueness constraint
    is per-recipient already, so nothing here can double-notify).
    """
    date = payload["date"]
    members = payload.get("members", [])
    if not members:
        return

    for recipient in _leadership(message):
        notify(
            recipient=recipient,
            topic="presence.staff_absence.detected",
            title=f"{len(members)} staff member(s) did not check in today",
            body=(f"On {date}, {len(members)} staff member(s) expected on duty "
                  "never checked in."),
            payload=payload,
            importance=Notification.Importance.URGENT,
            channels=[Channel.IN_APP, Channel.PUSH],
            dedupe_key=f"staffabsence:{date}",
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


# -- Student movement (doc 08) ---------------------------------------------
#
# Guardian recipients arrive in the payload already filtered for verification
# and opt-out by `movement.services` -- deciding who may be told about a child
# is that module's business, not this one's. Bodies never carry a destination
# or a reason: a lock-screen preview is not a private channel.


def _notify_guardians(payload, *, topic, title, body, importance, dedupe_prefix):
    for entry in payload.get("recipients", []):
        recipient = _membership(entry["membership_id"])
        if recipient is None:
            continue
        notify(
            recipient=recipient, topic=topic, title=title, body=body,
            payload={"student_id": entry["student_id"], **_ref_ids(payload)},
            importance=importance,
            channels=[Channel.IN_APP, Channel.PUSH],
            dedupe_key=f"{dedupe_prefix}:{entry['student_id']}",
        )


def _ref_ids(payload) -> dict:
    return {k: payload[k] for k in ("pass_out_id", "trip_id") if k in payload}


@subscribe("movement.pass_out.requested")
def alert_leadership_to_pass_out_request(payload, *, message=None):
    for recipient in _leadership(message):
        notify(
            recipient=recipient, topic="movement.pass_out.requested",
            title="A pass-out is waiting for approval",
            body="A boarder's pass-out request needs a decision.",
            payload={"pass_out_id": payload["pass_out_id"]},
            importance=Notification.Importance.IMPORTANT,
            channels=[Channel.IN_APP, Channel.PUSH],
            dedupe_key=f"passout-requested:{payload['pass_out_id']}",
        )


@subscribe("movement.pass_out.approved")
def notify_pass_out_approved(payload, *, message=None):
    requester = _membership(payload.get("requester_membership_id"))
    if requester is not None:
        notify(
            recipient=requester, topic="movement.pass_out.approved",
            title="Pass-out approved",
            body="The pass-out you requested has been approved.",
            payload={"pass_out_id": payload["pass_out_id"]},
            channels=[Channel.IN_APP, Channel.PUSH],
            dedupe_key=f"passout-approved-requester:{payload['pass_out_id']}",
        )
    _notify_guardians(
        payload, topic="movement.pass_out.approved",
        title="Your child has an approved pass-out",
        body="The school has approved a pass-out for your child. Sign in for details.",
        importance=Notification.Importance.IMPORTANT,
        dedupe_prefix=f"passout-approved:{payload['pass_out_id']}",
    )


@subscribe("movement.pass_out.denied")
def notify_pass_out_denied(payload, *, message=None):
    requester = _membership(payload.get("requester_membership_id"))
    if requester is None:
        return
    notify(
        recipient=requester, topic="movement.pass_out.denied",
        title="Pass-out not approved",
        body="The pass-out you requested was not approved.",
        payload={"pass_out_id": payload["pass_out_id"]},
        channels=[Channel.IN_APP, Channel.PUSH],
        dedupe_key=f"passout-denied:{payload['pass_out_id']}",
    )


@subscribe("movement.pass_out.departed")
def notify_pass_out_departed(payload, *, message=None):
    _notify_guardians(
        payload, topic="movement.pass_out.departed",
        title="Your child has left campus",
        body="Your child has signed out on an approved pass-out.",
        importance=Notification.Importance.IMPORTANT,
        dedupe_prefix=f"passout-departed:{payload['pass_out_id']}",
    )


@subscribe("movement.pass_out.returned")
def notify_pass_out_returned(payload, *, message=None):
    _notify_guardians(
        payload, topic="movement.pass_out.returned",
        title="Your child is back on campus",
        body="Your child has signed back in from their pass-out.",
        importance=Notification.Importance.ROUTINE,
        dedupe_prefix=f"passout-returned:{payload['pass_out_id']}",
    )


@subscribe("movement.pass_out.overdue")
def alert_pass_out_overdue(payload, *, message=None):
    for recipient in _leadership(message):
        notify(
            recipient=recipient, topic="movement.pass_out.overdue",
            title="A student is overdue back on campus",
            body="A boarder on a pass-out has not returned by the expected time.",
            payload={"pass_out_id": payload["pass_out_id"]},
            importance=Notification.Importance.URGENT,
            channels=[Channel.IN_APP, Channel.PUSH],
            dedupe_key=f"passout-overdue:{payload['pass_out_id']}",
        )
    _notify_guardians(
        payload, topic="movement.pass_out.overdue",
        title="Your child is overdue back at school",
        body="Your child was expected back from a pass-out and has not returned. "
             "Please contact the school.",
        importance=Notification.Importance.URGENT,
        dedupe_prefix=f"passout-overdue:{payload['pass_out_id']}",
    )


@subscribe("movement.trip.submitted")
def alert_leadership_to_trip_submission(payload, *, message=None):
    for recipient in _leadership(message):
        notify(
            recipient=recipient, topic="movement.trip.submitted",
            title="A trip is waiting for approval",
            body=(f"\"{payload['title']}\" has been submitted with "
                  f"{payload['participant_count']} student(s)."),
            payload={"trip_id": payload["trip_id"]},
            importance=Notification.Importance.IMPORTANT,
            channels=[Channel.IN_APP, Channel.PUSH],
            dedupe_key=f"trip-submitted:{payload['trip_id']}",
        )


@subscribe("movement.trip.approved")
def notify_trip_approved(payload, *, message=None):
    _notify_guardians(
        payload, topic="movement.trip.approved",
        title="Your child is on an approved trip",
        body=(f"Your child is included in \"{payload['title']}\". "
              "Sign in for the details."),
        importance=Notification.Importance.IMPORTANT,
        dedupe_prefix=f"trip-approved:{payload['trip_id']}",
    )


@subscribe("movement.trip.rejected")
def notify_trip_rejected(payload, *, message=None):
    creator = _membership(payload.get("creator_membership_id"))
    if creator is None:
        return
    notify(
        recipient=creator, topic="movement.trip.rejected",
        title="Trip not approved",
        body="The trip you submitted was not approved.",
        payload={"trip_id": payload["trip_id"]},
        channels=[Channel.IN_APP, Channel.PUSH],
        dedupe_key=f"trip-rejected:{payload['trip_id']}",
    )


@subscribe("movement.trip.departed")
def notify_trip_departed(payload, *, message=None):
    _notify_guardians(
        payload, topic="movement.trip.departed",
        title="Your child has left on a trip",
        body=f"\"{payload['title']}\" has departed with your child.",
        importance=Notification.Importance.IMPORTANT,
        dedupe_prefix=f"trip-departed:{payload['trip_id']}",
    )


@subscribe("movement.trip.returned")
def notify_trip_returned(payload, *, message=None):
    _notify_guardians(
        payload, topic="movement.trip.returned",
        title="Your child is back from the trip",
        body=f"\"{payload['title']}\" has returned to school.",
        importance=Notification.Importance.ROUTINE,
        dedupe_prefix=f"trip-returned:{payload['trip_id']}",
    )


@subscribe("movement.trip.overdue")
def alert_trip_overdue(payload, *, message=None):
    for recipient in _leadership(message):
        notify(
            recipient=recipient, topic="movement.trip.overdue",
            title="A trip is overdue back at school",
            body=(f"\"{payload['title']}\" was expected back and has not "
                  "been marked returned."),
            payload={"trip_id": payload["trip_id"]},
            importance=Notification.Importance.URGENT,
            channels=[Channel.IN_APP, Channel.PUSH],
            dedupe_key=f"trip-overdue:{payload['trip_id']}",
        )


# -- Staff check-in reminders (doc 04, SSOMS §6) --------------------------


@subscribe("presence.checkin.reminder_due")
def remind_teacher_to_check_in(payload, *, message=None):
    """A timed nudge to the member of staff themselves -- never leadership.

    Escalation to leadership is a separate event
    (`presence.staff_absence.detected`); this ladder is only about helping the
    teacher not forget. One notification per stage per person per day.
    """
    stage = payload["stage"]
    recipient = _membership(payload["membership_id"])
    if recipient is None:
        return

    bodies = {
        "opening_soon": "Your check-in window opens soon.",
        "due": "Please check in to confirm your presence at school.",
        "late": "You have not checked in yet and are now marked late.",
    }
    notify(
        recipient=recipient,
        topic="presence.checkin.reminder_due",
        title="Check-in reminder",
        body=bodies.get(stage, "Please check in."),
        payload={"date": payload["date"], "stage": stage},
        importance=(Notification.Importance.IMPORTANT if stage == "late"
                    else Notification.Importance.ROUTINE),
        channels=[Channel.IN_APP, Channel.PUSH],
        dedupe_key=f"checkin-reminder:{payload['membership_id']}:{payload['date']}:{stage}",
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
