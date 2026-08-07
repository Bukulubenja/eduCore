"""The dashboards, and the endpoints that expose them."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from educore.core.tenancy import TenantContext
from educore.insights import services
from educore.presence import services as presence
from educore.students import services as student_services
from educore.students.models import StudentAttendanceStatus
from educore.timetable import services as timetable_services
from educore.timetable.models import LessonInstance

pytestmark = pytest.mark.django_db

PASSWORD = "correct-horse-battery"


def client_for(membership) -> APIClient:
    api = APIClient()
    response = api.post(reverse("v1:auth-token"),
                        {"email": membership.user.email, "password": PASSWORD},
                        format="json")
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access_token']}")
    return api


@pytest.fixture
def running_day(school_a, draft_version, slot, second_slot, course, class_group,
                teacher, room, make_scheduled_lesson, campus, policy, students,
                good_signals, morning):
    """A school mid-morning: one teacher in, two lessons, one delivered."""
    make_scheduled_lesson(draft_version, weekday=0, slot=slot, course=course,
                          class_group=class_group, teacher=teacher, room=room)
    make_scheduled_lesson(draft_version, weekday=0, slot=second_slot,
                          course=course, class_group=class_group,
                          teacher=teacher, room=room)

    today = timezone.localdate()
    with TenantContext.scope(school_a):
        timetable_services.publish(draft_version)
        instances = list(LessonInstance.objects.order_by("date",
                                                         "scheduled_start_at")[:2])
        LessonInstance.objects.filter(
            pk__in=[i.pk for i in instances]
        ).update(date=today)

        presence.submit_event(
            membership=teacher, campus=campus, kind="check_in",
            captured_at=timezone.now() - timedelta(hours=2),
            client_event_id=uuid.uuid4(),
            raw_signals=good_signals(timezone.now() - timedelta(hours=2)),
        )

        first, second = LessonInstance.objects.filter(date=today).order_by(
            "scheduled_start_at"
        )
        LessonInstance.objects.filter(pk=first.pk).update(
            status=LessonInstance.Status.DELIVERED
        )
        LessonInstance.objects.filter(pk=second.pk).update(
            status=LessonInstance.Status.MISSED
        )

        student_services.submit_register(
            lesson_instance=first, marked_by=teacher,
            exceptions=[{"student_id": students[0].id,
                         "status": StudentAttendanceStatus.ABSENT}],
        )
    return today


# -- The 09:30 view ----------------------------------------------------------


def test_today_answers_the_question_a_head_actually_asks(school_a, running_day,
                                                         teacher, deputy):
    with TenantContext.scope(school_a):
        snapshot = services.today(on=running_day)

    assert snapshot["staff"]["expected"] == 2       # teacher + deputy
    assert snapshot["staff"]["checked_in"] == 1
    assert snapshot["staff"]["absent"] == 1
    assert snapshot["lessons"]["scheduled"] == 2
    assert snapshot["lessons"]["delivered"] == 1
    assert snapshot["lessons"]["missed"] == 1
    assert snapshot["students"]["marked"] == 5
    assert snapshot["students"]["absent"] == 1


def test_a_quiet_day_reports_zeroes_not_errors(school_a, teacher):
    with TenantContext.scope(school_a):
        snapshot = services.today()

    assert snapshot["lessons"]["scheduled"] == 0
    assert snapshot["staff"]["checked_in"] == 0


def test_a_teacher_who_never_checked_out_is_counted_separately(school_a,
                                                               running_day,
                                                               teacher):
    """Signed in and gone is not the same as present, and the dashboard must
    not let the two blur."""
    with TenantContext.scope(school_a):
        snapshot = services.today(on=running_day)

    assert snapshot["staff"]["not_checked_out"] == 1


# -- Punctuality and workload ------------------------------------------------


def test_punctuality_reports_its_own_denominator(school_a, running_day, teacher):
    """A "50% late" built from two days is noise; presenting it without the
    denominator invites someone to act on it."""
    with TenantContext.scope(school_a):
        rows = services.punctuality()

    assert len(rows) == 1
    assert rows[0]["days_recorded"] == 1
    assert rows[0]["reliable"] is False


def test_workload_counts_delivered_lessons(school_a, running_day, teacher, room,
                                           campus):
    from educore.delivery.models import LessonSession

    with TenantContext.scope(school_a):
        instance = LessonInstance.objects.filter(date=running_day).first()
        LessonSession.objects.create(
            school_id=school_a.id, lesson_instance=instance,
            actual_teacher=teacher, room=room, opened_at=timezone.now(),
        )
        rows = services.workload()

    assert rows[0]["lessons_delivered"] == 1


# -- At risk -----------------------------------------------------------------


def test_attendance_and_attainment_are_reported_separately(school_a, students,
                                                           term, lesson_history):
    """A composite score nobody can decompose is a score nobody can act on."""
    with TenantContext.scope(school_a):
        flagged = services.at_risk_students(term=term)

    assert flagged
    reasons = {r["code"] for row in flagged for r in row["reasons"]}
    assert "attendance" in reasons
    assert flagged[0]["marks_counted"] == 12
    assert flagged[0]["attendance_rate"] == pytest.approx(4 / 12, abs=0.001)


@pytest.fixture
def lesson_history(school_a, students, term):
    """Twelve daily marks, with one student absent from two thirds of them."""
    from educore.students.models import StudentAttendance

    with TenantContext.scope(school_a):
        base = timezone.localdate() - timedelta(days=20)
        StudentAttendance.objects.bulk_create([
            StudentAttendance(
                school_id=school_a.id, student=students[0],
                date=base + timedelta(days=day),
                status=(StudentAttendanceStatus.ABSENT if day < 8
                        else StudentAttendanceStatus.PRESENT),
            )
            for day in range(12)
        ])
    return students[0]


def test_a_student_with_too_few_marks_is_not_flagged(school_a, students, term):
    """Below the minimum sample, an attendance rate means nothing."""
    from educore.students.models import StudentAttendance

    with TenantContext.scope(school_a):
        StudentAttendance.objects.create(
            school_id=school_a.id, student=students[1],
            date=timezone.localdate(), status=StudentAttendanceStatus.ABSENT,
        )
        flagged = services.at_risk_students(term=term)

    assert all(row["student_id"] != str(students[1].id) for row in flagged)


# -- Endpoints ---------------------------------------------------------------


def test_leadership_sees_the_dashboard(school_a, running_day, deputy):
    response = client_for(deputy).get(reverse("v1:insights:today"))

    assert response.status_code == 200
    assert response.data["lessons"]["missed"] == 1


def test_a_teacher_may_not_see_the_dashboard(school_a, running_day, teacher):
    response = client_for(teacher).get(reverse("v1:insights:today"))

    assert response.status_code == 403
    assert response.data["type"].endswith("/not-permitted")


def test_at_risk_is_open_to_pastoral_roles(school_a, term, deputy, teacher,
                                           grant_role):
    grant_role(teacher, "class_teacher", "Class Teacher")

    assert client_for(deputy).get(
        reverse("v1:insights:at-risk")).status_code == 200
    assert client_for(teacher).get(
        reverse("v1:insights:at-risk")).status_code == 200


def test_punctuality_and_workload_are_leadership_only(school_a, teacher, deputy):
    assert client_for(teacher).get(
        reverse("v1:insights:punctuality")).status_code == 403
    assert client_for(deputy).get(
        reverse("v1:insights:workload")).status_code == 200
