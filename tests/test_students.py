"""Registers, guardian notification, gate reconciliation."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

from educore.core.models import OutboxMessage
from educore.core.tenancy import TenantContext
from educore.students import services
from educore.students.models import (
    GateEvent,
    GuardianLink,
    StudentAttendance,
    StudentAttendanceStatus,
)
from educore.timetable import services as timetable_services
from educore.timetable.models import LessonInstance

pytestmark = pytest.mark.django_db


@pytest.fixture
def lesson(school_a, draft_version, slot, course, class_group, teacher, room,
           make_scheduled_lesson):
    make_scheduled_lesson(draft_version, weekday=0, slot=slot, course=course,
                          class_group=class_group, teacher=teacher, room=room)
    with TenantContext.scope(school_a):
        timetable_services.publish(draft_version)
        return LessonInstance.objects.first()


@pytest.fixture
def guardian(school_a, students, make_membership):
    parent = make_membership(school_a, email="parent@example.com", name="A Parent")
    with TenantContext.scope(school_a):
        GuardianLink.objects.create(
            school_id=school_a.id, student=students[0], membership=parent,
            relationship=GuardianLink.Relationship.MOTHER,
            is_primary_contact=True, verified=True,
        )
    return parent


# -- The register ------------------------------------------------------------


def test_the_register_arrives_pre_marked_present(school_a, lesson, students):
    """Default-present, tap-absent.

    Marking forty students individually is why paper registers survive;
    marking three absentees takes about eight seconds, and a teacher will
    actually do that every lesson.
    """
    with TenantContext.scope(school_a):
        roster = services.build_register(lesson_instance=lesson)

    assert len(roster) == 5
    assert {row.status for row in roster} == {StudentAttendanceStatus.PRESENT}


def test_submitting_only_exceptions_marks_everyone_else_present(school_a, lesson,
                                                                students, teacher):
    with TenantContext.scope(school_a):
        services.submit_register(
            lesson_instance=lesson, marked_by=teacher,
            exceptions=[{"student_id": students[1].id,
                         "status": StudentAttendanceStatus.ABSENT}],
        )
        marks = StudentAttendance.objects.filter(lesson_instance=lesson)

    assert marks.count() == 5
    assert marks.filter(status=StudentAttendanceStatus.ABSENT).count() == 1
    assert marks.filter(status=StudentAttendanceStatus.PRESENT).count() == 4


def test_resubmitting_a_register_replaces_it(school_a, lesson, students, teacher):
    with TenantContext.scope(school_a):
        services.submit_register(
            lesson_instance=lesson, marked_by=teacher,
            exceptions=[{"student_id": students[0].id,
                         "status": StudentAttendanceStatus.ABSENT}],
        )
        services.submit_register(
            lesson_instance=lesson, marked_by=teacher,
            exceptions=[{"student_id": students[2].id,
                         "status": StudentAttendanceStatus.SICK}],
        )
        marks = StudentAttendance.objects.filter(lesson_instance=lesson)

    assert marks.count() == 5
    assert marks.get(student=students[0]).status == StudentAttendanceStatus.PRESENT
    assert marks.get(student=students[2]).status == StudentAttendanceStatus.SICK


def test_a_student_outside_the_class_cannot_be_marked(school_a, lesson, students,
                                                      teacher, level, term):
    from educore.academics.models import ClassGroup
    from educore.students.models import Student

    with TenantContext.scope(school_a):
        other_group = ClassGroup.objects.create(school_id=school_a.id,
                                                level=level, name="S4 Grey")
        outsider = Student.objects.create(school_id=school_a.id,
                                          admission_number="ADM999",
                                          full_name="Not In This Class")
        services.enrol(student=outsider, class_group=other_group, term=term)

        with pytest.raises(services.RegisterError):
            services.submit_register(
                lesson_instance=lesson, marked_by=teacher,
                exceptions=[{"student_id": outsider.id,
                             "status": StudentAttendanceStatus.ABSENT}],
            )


# -- Guardian notification ---------------------------------------------------


def test_an_absence_queues_one_notification_for_verified_guardians(
    school_a, lesson, students, teacher, guardian
):
    with TenantContext.scope(school_a):
        services.submit_register(
            lesson_instance=lesson, marked_by=teacher,
            exceptions=[{"student_id": students[0].id,
                         "status": StudentAttendanceStatus.ABSENT}],
        )
        messages = OutboxMessage.objects.filter(
            topic="students.absence.detected"
        )

    assert messages.count() == 1
    assert messages.first().payload["recipients"][0]["membership_id"] == \
        str(guardian.pk)


def test_an_unverified_guardian_is_not_notified(school_a, lesson, students,
                                                teacher, make_membership):
    """Sending a child's whereabouts to an unverified contact is a
    safeguarding incident, not a bug -- so the link is never assumed."""
    stranger = make_membership(school_a, email="unverified@example.com")
    with TenantContext.scope(school_a):
        GuardianLink.objects.create(
            school_id=school_a.id, student=students[0], membership=stranger,
            relationship=GuardianLink.Relationship.OTHER, verified=False,
        )
        services.submit_register(
            lesson_instance=lesson, marked_by=teacher,
            exceptions=[{"student_id": students[0].id,
                         "status": StudentAttendanceStatus.ABSENT}],
        )

        assert not OutboxMessage.objects.filter(
            topic="students.absence.detected"
        ).exists()


def test_a_guardian_who_opted_out_is_not_notified(school_a, lesson, students,
                                                  teacher, guardian):
    with TenantContext.scope(school_a):
        GuardianLink.objects.filter(student=students[0]).update(
            receives_notifications=False
        )
        services.submit_register(
            lesson_instance=lesson, marked_by=teacher,
            exceptions=[{"student_id": students[0].id,
                         "status": StudentAttendanceStatus.ABSENT}],
        )

        assert not OutboxMessage.objects.filter(
            topic="students.absence.detected"
        ).exists()


def test_only_the_first_lesson_of_the_day_notifies(school_a, students, teacher,
                                                   guardian, draft_version, slot,
                                                   second_slot, course,
                                                   class_group, room,
                                                   make_scheduled_lesson):
    """A parent who gets six notifications a day stops reading them, and then
    the one that mattered is missed too."""
    make_scheduled_lesson(draft_version, weekday=0, slot=slot, course=course,
                          class_group=class_group, teacher=teacher, room=room)
    make_scheduled_lesson(draft_version, weekday=0, slot=second_slot,
                          course=course, class_group=class_group,
                          teacher=teacher, room=room)

    with TenantContext.scope(school_a):
        timetable_services.publish(draft_version)
        first_day = LessonInstance.objects.order_by("date", "scheduled_start_at")
        lessons = list(first_day.filter(date=first_day.first().date))

        for instance in lessons:
            services.submit_register(
                lesson_instance=instance, marked_by=teacher,
                exceptions=[{"student_id": students[0].id,
                             "status": StudentAttendanceStatus.ABSENT}],
            )

        messages = OutboxMessage.objects.filter(topic="students.absence.detected")

    assert len(lessons) == 2
    assert messages.count() == 1


# -- Gate --------------------------------------------------------------------


def test_gate_events_are_idempotent(school_a, students, campus):
    event_id = uuid.uuid4()
    with TenantContext.scope(school_a):
        first = services.record_gate_event(
            student=students[0], campus=campus, direction=GateEvent.Direction.IN,
            occurred_at=timezone.now(), client_event_id=event_id,
        )
        second = services.record_gate_event(
            student=students[0], campus=campus, direction=GateEvent.Direction.IN,
            occurred_at=timezone.now(), client_event_id=event_id,
        )

        assert first.pk == second.pk
        assert GateEvent.objects.count() == 1


def test_on_site_but_not_in_class_is_surfaced(school_a, students, campus, teacher,
                                              draft_version, slot, second_slot,
                                              course, class_group, room,
                                              make_scheduled_lesson):
    """A child on campus and not in class matters more than a percentage, and
    neither the gate log nor the register reveals it alone."""
    from datetime import datetime, time

    make_scheduled_lesson(draft_version, weekday=0, slot=slot, course=course,
                          class_group=class_group, teacher=teacher, room=room)
    make_scheduled_lesson(draft_version, weekday=0, slot=second_slot,
                          course=course, class_group=class_group,
                          teacher=teacher, room=room)

    with TenantContext.scope(school_a):
        timetable_services.publish(draft_version)
        day = LessonInstance.objects.order_by("date").first().date
        lessons = list(LessonInstance.objects.filter(date=day))

        services.record_gate_event(
            student=students[0], campus=campus, direction=GateEvent.Direction.IN,
            occurred_at=datetime.combine(day, time(7, 40),
                                         tzinfo=timezone.get_current_timezone()),
            client_event_id=uuid.uuid4(),
        )
        for instance in lessons:
            services.submit_register(
                lesson_instance=instance, marked_by=teacher,
                exceptions=[{"student_id": students[0].id,
                             "status": StudentAttendanceStatus.ABSENT}],
            )

        flagged = services.reconcile_gate_and_registers(date=day)

    assert len(lessons) == 2
    assert len(flagged) == 1
    assert flagged[0]["student_id"] == students[0].id
    assert flagged[0]["lessons_missed"] == 2


def test_a_student_who_never_arrived_is_not_flagged(school_a, lesson, students,
                                                    teacher):
    with TenantContext.scope(school_a):
        services.submit_register(
            lesson_instance=lesson, marked_by=teacher,
            exceptions=[{"student_id": students[0].id,
                         "status": StudentAttendanceStatus.ABSENT}],
        )
        flagged = services.reconcile_gate_and_registers(date=lesson.date)

    assert flagged == []


# -- Enrolment ---------------------------------------------------------------


def test_enrolling_again_supersedes_the_previous_placement(school_a, students,
                                                           term, level):
    """Moving stream must not mutate history out from under the past."""
    from educore.academics.models import ClassGroup
    from educore.students.models import Enrolment

    with TenantContext.scope(school_a):
        destination = ClassGroup.objects.create(school_id=school_a.id,
                                                level=level, name="S4 Gold")
        services.enrol(student=students[0], class_group=destination, term=term)

        enrolments = Enrolment.objects.filter(student=students[0], term=term)

    assert enrolments.count() == 2
    assert enrolments.filter(is_active=True).count() == 1


def test_attendance_summary_reports_a_rate(school_a, lesson, students, teacher):
    with TenantContext.scope(school_a):
        services.submit_register(
            lesson_instance=lesson, marked_by=teacher,
            exceptions=[{"student_id": students[0].id,
                         "status": StudentAttendanceStatus.ABSENT}],
        )
        summary = services.attendance_summary(
            student=students[1],
            since=lesson.date - timedelta(days=1),
            until=lesson.date + timedelta(days=1),
        )

    assert summary["marks"] == 1
    assert summary["rate"] == 1.0
