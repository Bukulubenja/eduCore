"""The staff check-in reminder ladder (presence, SSOMS §6)."""

from __future__ import annotations

import uuid
from datetime import date as date_cls
from datetime import datetime, time

import pytest
from django.utils import timezone

from educore.core.models import OutboxMessage
from educore.core.tenancy import TenantContext
from educore.presence import services
from educore.presence.models import (
    AttendanceEvent,
    AttendanceRecord,
    AttendanceStatus,
    Disposition,
)

pytestmark = pytest.mark.django_db

# A Monday, so a weekday-0 DutySchedule matches and nothing is a weekend.
MONDAY = date_cls(2026, 9, 7)


def _at(hour, minute):
    tz = timezone.get_current_timezone()
    return datetime.combine(MONDAY, time(hour, minute), tzinfo=tz)


def _reminders(school):
    with TenantContext.scope(school):
        services.raise_checkin_reminders(date=MONDAY, now=_at(7, 35))
        return list(
            OutboxMessage.objects
            .filter(topic="presence.checkin.reminder_due")
            .values_list("payload", flat=True)
        )


@pytest.mark.parametrize("hour,minute,expected", [
    (7, 0, "opening_soon"),   # 06:45-07:30
    (7, 35, "due"),           # 07:30-07:45
    (8, 30, "late"),          # 07:45-09:45
])
def test_each_stage_fires_in_its_window(school_a, policy, teacher, hour, minute,
                                        expected):
    from educore.comms.models import Notification
    from educore.core import outbox

    with TenantContext.scope(school_a):
        count = services.raise_checkin_reminders(date=MONDAY,
                                                 now=_at(hour, minute))
        assert count == 1
        message = OutboxMessage.objects.get(topic="presence.checkin.reminder_due")
        assert message.payload["stage"] == expected
        assert message.payload["membership_id"] == str(teacher.pk)

        outbox.relay_pending()
        note = Notification.objects.get(recipient=teacher,
                                        topic="presence.checkin.reminder_due")
    assert note.payload["stage"] == expected


def test_no_reminder_outside_every_window(school_a, policy, teacher):
    with TenantContext.scope(school_a):
        assert services.raise_checkin_reminders(date=MONDAY,
                                                now=_at(11, 0)) == 0


def test_no_reminder_once_checked_in(school_a, policy, teacher, campus):
    with TenantContext.scope(school_a):
        AttendanceEvent.objects.create(
            school_id=school_a.id, membership=teacher, campus=campus,
            kind=AttendanceEvent.Kind.CHECK_IN, captured_at=_at(7, 20),
            client_event_id=uuid.uuid4(), payload_digest="x",
            disposition=Disposition.VERIFIED, confidence=90, policy_version=1,
        )
        assert services.raise_checkin_reminders(date=MONDAY,
                                                now=_at(7, 35)) == 0


def test_no_reminder_when_on_approved_leave(school_a, policy, teacher):
    with TenantContext.scope(school_a):
        AttendanceRecord.objects.create(
            school_id=school_a.id, membership=teacher, date=MONDAY,
            status=AttendanceStatus.ON_LEAVE, disposition=Disposition.VERIFIED,
            policy_version=1,
        )
        assert services.raise_checkin_reminders(date=MONDAY,
                                                now=_at(7, 35)) == 0


def test_no_reminder_on_a_non_working_day(school_a, policy, teacher):
    from educore.timetable.models import CalendarException

    with TenantContext.scope(school_a):
        CalendarException.objects.create(
            school_id=school_a.id, date=MONDAY,
            kind=CalendarException.Kind.HOLIDAY, suppresses_lessons=True,
            description="Public holiday",
        )
        assert services.raise_checkin_reminders(date=MONDAY,
                                                now=_at(7, 35)) == 0


def test_a_member_not_on_duty_today_is_not_reminded(school_a, policy, teacher):
    from educore.presence.models import DutySchedule

    with TenantContext.scope(school_a):
        # Tuesday-only duty; Monday is a day off for this teacher.
        DutySchedule.objects.create(
            school_id=school_a.id, membership=teacher, weekday=1,
            starts_at=time(7, 30), ends_at=time(17, 0),
        )
        assert services.raise_checkin_reminders(date=MONDAY,
                                                now=_at(7, 35)) == 0


def test_students_and_parents_are_never_reminded(school_a, policy,
                                                 make_membership, grant_role):
    parent = make_membership(school_a, email="p@example.com", name="Parent")
    grant_role(parent, "parent", "Parent")
    with TenantContext.scope(school_a):
        assert services.raise_checkin_reminders(date=MONDAY,
                                                now=_at(7, 35)) == 0


def test_reminders_do_not_cross_schools(school_a, school_b, policy, teacher):
    _reminders(school_a)
    with TenantContext.scope(school_b):
        assert OutboxMessage.objects.filter(
            topic="presence.checkin.reminder_due"
        ).count() == 0


def test_remind_staff_checkins_command_runs(school_a, policy, teacher):
    from django.core.management import call_command

    # Wall-clock dependent, so this asserts the command executes cleanly, not
    # that a reminder fires -- the stage logic is covered above with an
    # injected `now`.
    call_command("remind_staff_checkins")
