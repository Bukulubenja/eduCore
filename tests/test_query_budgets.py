"""Query-count budgets for read paths whose cost must not scale with data
volume. A view that queries once per row works in the demo and falls over at
the school it was actually built for."""

from __future__ import annotations

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from educore.academics.models import ClassGroup
from educore.comms import services as comms
from educore.core.tenancy import TenantContext
from educore.insights import services as insights
from educore.presence.models import AttendanceRecord, AttendanceStatus, Disposition
from educore.students import services as student_services
from educore.students.models import (
    GuardianLink,
    Student,
    StudentAttendance,
    StudentAttendanceStatus,
)
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


def _query_count(fn) -> int:
    with CaptureQueriesContext(connection) as ctx:
        fn()
    return len(ctx.captured_queries)


# -- Registers ----------------------------------------------------------


@pytest.fixture
def two_registers(school_a, draft_version, slot, course, teacher, room,
                  make_scheduled_lesson, level, term):
    """Two lesson instances: one class of 2 students, one of 15."""
    with TenantContext.scope(school_a):
        small_group = ClassGroup.objects.create(school_id=school_a.id, level=level,
                                                name="Small Group")
        large_group = ClassGroup.objects.create(school_id=school_a.id, level=level,
                                                name="Large Group")

    make_scheduled_lesson(draft_version, weekday=0, slot=slot, course=course,
                         class_group=small_group, teacher=teacher, room=room)
    make_scheduled_lesson(draft_version, weekday=1, slot=slot, course=course,
                         class_group=large_group, teacher=teacher, room=room)

    with TenantContext.scope(school_a):
        timetable_services.publish(draft_version)

        for i in range(2):
            student = Student.objects.create(
                school_id=school_a.id, admission_number=f"SM{i:03d}",
                full_name=f"Small Student {i}", scan_code=f"SMSC{i:03d}",
            )
            student_services.enrol(student=student, class_group=small_group,
                                   term=term)
        for i in range(15):
            student = Student.objects.create(
                school_id=school_a.id, admission_number=f"LG{i:03d}",
                full_name=f"Large Student {i}", scan_code=f"LGSC{i:03d}",
            )
            student_services.enrol(student=student, class_group=large_group,
                                   term=term)

        small = LessonInstance.objects.filter(class_group=small_group).first()
        large = LessonInstance.objects.filter(class_group=large_group).first()
        return small, large


def test_the_register_does_not_query_per_student(school_a, two_registers):
    small_instance, large_instance = two_registers

    with TenantContext.scope(school_a):
        small_cost = _query_count(
            lambda: student_services.build_register(lesson_instance=small_instance)
        )
        large_cost = _query_count(
            lambda: student_services.build_register(lesson_instance=large_instance)
        )

    assert small_cost == large_cost


def test_submitting_a_register_is_bounded(school_a, two_registers, teacher):
    small_instance, large_instance = two_registers

    def _submit(instance, n_absent):
        with TenantContext.scope(school_a):
            roster = student_services.build_register(lesson_instance=instance)
            exceptions = [
                {"student_id": row.student_id, "status": StudentAttendanceStatus.ABSENT}
                for row in roster[:n_absent]
            ]
            student_services.submit_register(
                lesson_instance=instance, marked_by=teacher, exceptions=exceptions,
            )

    small_cost = _query_count(lambda: _submit(small_instance, n_absent=1))
    large_cost = _query_count(lambda: _submit(large_instance, n_absent=3))

    assert small_cost == large_cost


# -- Dashboards -----------------------------------------------------------


def test_punctuality_does_not_query_per_teacher(school_a, make_membership):
    on = timezone.localdate()

    def _seed(n):
        with TenantContext.scope(school_a):
            for i in range(n):
                member = make_membership(school_a, email=f"pt{n}-{i}@example.com",
                                         name=f"Teacher {n}-{i}")
                AttendanceRecord.objects.create(
                    school_id=school_a.id, membership=member, date=on,
                    status=AttendanceStatus.LATE, disposition=Disposition.VERIFIED,
                    policy_version=1,
                )

    with TenantContext.scope(school_a):
        _seed(2)
        small_cost = _query_count(
            lambda: insights.punctuality(since=on, until=on)
        )
        _seed(15)
        large_cost = _query_count(
            lambda: insights.punctuality(since=on, until=on)
        )

    assert small_cost == large_cost


@pytest.fixture
def one_lesson(school_a, draft_version, slot, course, class_group, teacher, room,
               make_scheduled_lesson):
    make_scheduled_lesson(draft_version, weekday=0, slot=slot, course=course,
                         class_group=class_group, teacher=teacher, room=room)
    with TenantContext.scope(school_a):
        timetable_services.publish(draft_version)
        instance = LessonInstance.objects.first()
        instance.date = timezone.localdate()
        instance.save(update_fields=["date", "updated_at"])
        return instance


def test_the_today_dashboard_is_a_fixed_cost(school_a, one_lesson, teacher,
                                             make_membership):
    on = timezone.localdate()

    def _seed(n):
        with TenantContext.scope(school_a):
            for i in range(n):
                member = make_membership(school_a, email=f"td{n}-{i}@example.com",
                                         name=f"Staff {n}-{i}")
                AttendanceRecord.objects.create(
                    school_id=school_a.id, membership=member, date=on,
                    status=AttendanceStatus.PRESENT, disposition=Disposition.VERIFIED,
                    policy_version=1,
                )
                student = Student.objects.create(
                    school_id=school_a.id, admission_number=f"TD{n}{i:03d}",
                    full_name=f"Today Student {n}-{i}", scan_code=f"TDSC{n}{i:03d}",
                )
                StudentAttendance.objects.create(
                    school_id=school_a.id, student=student,
                    lesson_instance=one_lesson, date=on,
                    status=StudentAttendanceStatus.PRESENT, marked_by=teacher,
                )

    with TenantContext.scope(school_a):
        _seed(2)
        small_cost = _query_count(lambda: insights.today(on=on))
        _seed(12)
        large_cost = _query_count(lambda: insights.today(on=on))

    assert small_cost == large_cost


def test_own_attendance_history_is_bounded(school_a, teacher):
    today = timezone.localdate()
    api = client_for(teacher)

    def _seed(n, offset):
        with TenantContext.scope(school_a):
            for i in range(n):
                AttendanceRecord.objects.create(
                    school_id=school_a.id, membership=teacher,
                    date=today - timezone.timedelta(days=offset + i),
                    status=AttendanceStatus.PRESENT, disposition=Disposition.VERIFIED,
                    policy_version=1,
                )

    _seed(2, offset=0)
    small_cost = _query_count(
        lambda: api.get(reverse("v1:presence:my-attendance"))
    )
    _seed(20, offset=100)
    large_cost = _query_count(
        lambda: api.get(reverse("v1:presence:my-attendance"))
    )

    assert small_cost == large_cost


# -- Comms ------------------------------------------------------------------


def test_the_inbox_is_bounded(school_a, teacher):
    api = client_for(teacher)

    def _seed(n):
        with TenantContext.scope(school_a):
            for i in range(n):
                comms.notify(recipient=teacher, topic="t", title=f"Update {i}",
                            body="Body")

    _seed(2)
    small_cost = _query_count(lambda: api.get(reverse("v1:comms:inbox")))
    _seed(20)
    large_cost = _query_count(lambda: api.get(reverse("v1:comms:inbox")))

    assert small_cost == large_cost


def test_reading_a_thread_does_not_query_per_message(school_a, teacher, students,
                                                      make_membership):
    parent = make_membership(school_a, email="thread-parent@example.com",
                             name="A Parent")
    with TenantContext.scope(school_a):
        GuardianLink.objects.create(school_id=school_a.id, student=students[0],
                                    membership=parent,
                                    relationship=GuardianLink.Relationship.GUARDIAN,
                                    verified=True)
        thread = comms.open_thread(opened_by=teacher, student=students[0],
                                   subject="Progress")

    api = client_for(teacher)

    def _seed(n):
        with TenantContext.scope(school_a):
            for i in range(n):
                comms.post_message(thread=thread, author=teacher, body=f"Message {i}")

    _seed(2)
    small_cost = _query_count(
        lambda: api.get(reverse("v1:comms:thread-messages", args=[thread.id]))
    )
    _seed(20)
    large_cost = _query_count(
        lambda: api.get(reverse("v1:comms:thread-messages", args=[thread.id]))
    )

    assert small_cost == large_cost
