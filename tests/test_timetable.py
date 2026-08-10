"""Publishing, conflict detection, materialisation, and settling missed lessons."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from educore.core.tenancy import TenantContext
from educore.timetable import services
from educore.timetable.models import LessonInstance, TimetableVersion

from .conftest_phase2 import TERM_START

pytestmark = pytest.mark.django_db


@pytest.fixture
def populated_version(school_a, draft_version, slot, second_slot, course,
                      class_group, teacher, room, make_scheduled_lesson):
    make_scheduled_lesson(draft_version, weekday=0, slot=slot, course=course,
                          class_group=class_group, teacher=teacher, room=room)
    make_scheduled_lesson(draft_version, weekday=2, slot=second_slot,
                          course=course, class_group=class_group,
                          teacher=teacher, room=room)
    return draft_version


# -- Conflict detection ------------------------------------------------------


def test_a_clean_timetable_has_no_conflicts(school_a, populated_version):
    with TenantContext.scope(school_a):
        assert services.find_conflicts(populated_version) == []


def test_a_double_booked_teacher_is_detected(school_a, populated_version, slot,
                                             course, teacher, other_room, level,
                                             make_scheduled_lesson):
    from educore.academics.models import ClassGroup

    with TenantContext.scope(school_a):
        other_group = ClassGroup.objects.create(school_id=school_a.id,
                                                level=level, name="S4 Green")
        make_scheduled_lesson(populated_version, weekday=0, slot=slot,
                              course=course, class_group=other_group,
                              teacher=teacher, room=other_room)

        conflicts = services.find_conflicts(populated_version)

    assert len(conflicts) == 1
    assert conflicts[0].kind == "teacher"
    assert "double-booked" in conflicts[0].detail


def test_a_double_booked_room_is_detected(school_a, populated_version, slot,
                                          course, room, level, make_membership,
                                          make_scheduled_lesson):
    from educore.academics.models import ClassGroup

    with TenantContext.scope(school_a):
        other_group = ClassGroup.objects.create(school_id=school_a.id,
                                                level=level, name="S4 Gold")
    other_teacher = make_membership(school_a, email="other-teacher@example.com")
    with TenantContext.scope(school_a):
        make_scheduled_lesson(populated_version, weekday=0, slot=slot,
                              course=course, class_group=other_group,
                              teacher=other_teacher, room=room)

        conflicts = services.find_conflicts(populated_version)

    assert [c.kind for c in conflicts] == ["room"]


def test_publishing_a_conflicted_timetable_is_refused(school_a, populated_version,
                                                      slot, course, teacher,
                                                      other_room, level,
                                                      make_scheduled_lesson):
    """A double-booked teacher generates missed-lesson rows for a lesson nobody
    could ever have taught -- false accusations, straight out of the gate."""
    from educore.academics.models import ClassGroup

    with TenantContext.scope(school_a):
        other_group = ClassGroup.objects.create(school_id=school_a.id,
                                                level=level, name="S4 Red")
        make_scheduled_lesson(populated_version, weekday=0, slot=slot,
                              course=course, class_group=other_group,
                              teacher=teacher, room=other_room)

        with pytest.raises(services.TimetableError, match="conflicts"):
            services.publish(populated_version)

        populated_version.refresh_from_db()
    assert populated_version.status == TimetableVersion.Status.DRAFT


def test_an_empty_timetable_cannot_be_published(school_a, draft_version):
    with TenantContext.scope(school_a), pytest.raises(services.TimetableError):
        services.publish(draft_version)


# -- Publishing and materialisation ------------------------------------------


def test_publishing_materialises_the_horizon(school_a, populated_version, deputy):
    with TenantContext.scope(school_a):
        created = services.publish(populated_version, published_by=deputy)
        populated_version.refresh_from_db()
        instances = LessonInstance.objects.all()

    assert populated_version.status == TimetableVersion.Status.PUBLISHED
    assert created > 0
    # Two lessons a week over a fortnight.
    assert instances.count() == created
    assert all(i.status == LessonInstance.Status.SCHEDULED for i in instances)


def test_instances_denormalise_the_template(school_a, populated_version, teacher,
                                            room, course):
    """So a later revision cannot rewrite what was expected on a past date."""
    with TenantContext.scope(school_a):
        services.publish(populated_version)
        instance = LessonInstance.objects.first()

    assert instance.expected_teacher_id == teacher.pk
    assert instance.expected_room_id == room.pk
    assert instance.course_id == course.pk


def test_materialisation_is_idempotent(school_a, populated_version):
    with TenantContext.scope(school_a):
        services.publish(populated_version)
        first = LessonInstance.objects.count()

        today = timezone.localdate()
        again = services.materialise(populated_version, today,
                                     today + timedelta(days=14))

    assert again == 0
    with TenantContext.scope(school_a):
        assert LessonInstance.objects.count() == first


def test_rerunning_materialisation_cannot_resurrect_a_cancelled_lesson(
    school_a, populated_version
):
    with TenantContext.scope(school_a):
        services.publish(populated_version)
        instance = LessonInstance.objects.first()
        instance.status = LessonInstance.Status.CANCELLED
        instance.save()

        today = timezone.localdate()
        services.materialise(populated_version, today, today + timedelta(days=14))
        instance.refresh_from_db()

    assert instance.status == LessonInstance.Status.CANCELLED


def test_calendar_exceptions_suppress_lessons(school_a, populated_version,
                                              next_monday):
    from educore.timetable.models import CalendarException

    with TenantContext.scope(school_a):
        CalendarException.objects.create(
            school_id=school_a.id, date=next_monday,
            kind=CalendarException.Kind.HOLIDAY, description="Martyrs' Day",
        )
        services.publish(populated_version)
        on_holiday = LessonInstance.objects.filter(date=next_monday).count()

    assert on_holiday == 0


def test_publishing_supersedes_the_previous_version(school_a, populated_version,
                                                    academic_year, grid,
                                                    make_scheduled_lesson, slot,
                                                    course, class_group, teacher,
                                                    room):
    """Two live versions would materialise two instances for the same slot."""
    with TenantContext.scope(school_a):
        services.publish(populated_version)

        successor = TimetableVersion.objects.create(
            school_id=school_a.id, name="2026 Term 2 v2",
            academic_year=academic_year, grid=grid,
            effective_from=TERM_START + timedelta(days=30),
        )
        make_scheduled_lesson(successor, weekday=1, slot=slot, course=course,
                              class_group=class_group, teacher=teacher, room=room)
        services.publish(successor)

        populated_version.refresh_from_db()

    assert populated_version.status == TimetableVersion.Status.ARCHIVED
    assert populated_version.effective_to is not None


# -- Settling the day --------------------------------------------------------


def test_lessons_never_opened_are_marked_missed(school_a, populated_version):
    """Assigned by the system, never by a user.

    That the machine draws this conclusion consistently and without favour is
    the accountability being sold; a human "marking" lessons missed would
    reintroduce exactly the discretion schools already have.
    """
    with TenantContext.scope(school_a):
        services.publish(populated_version)
        instance = LessonInstance.objects.first()
        # Pull it into the past without touching its status.
        LessonInstance.objects.filter(pk=instance.pk).update(
            scheduled_start_at=timezone.now() - timedelta(hours=2),
            scheduled_end_at=timezone.now() - timedelta(hours=1),
        )

        result = services.close_out_missed()
        instance.refresh_from_db()

    assert result["missed"] == 1
    assert instance.status == LessonInstance.Status.MISSED
    assert instance.missed_reason


def test_a_future_lesson_is_left_alone(school_a, populated_version):
    """close_out_missed must not depend on what wall-clock time the suite
    happens to run at: publish() materialises today's occurrence of any
    lesson whose weekday matches today, and that occurrence's slot can
    already have ended by the time this test executes. Pin `now` to the
    start of today so "missed" reflects the scenario, not the clock."""
    with TenantContext.scope(school_a):
        services.publish(populated_version)
        start_of_today = timezone.now().replace(hour=0, minute=0, second=0,
                                                 microsecond=0)
        result = services.close_out_missed(now=start_of_today)

    assert result["missed"] == 0


def test_a_session_opened_but_never_closed_is_abandoned(school_a,
                                                        populated_version):
    with TenantContext.scope(school_a):
        services.publish(populated_version)
        instance = LessonInstance.objects.first()
        LessonInstance.objects.filter(pk=instance.pk).update(
            status=LessonInstance.Status.IN_PROGRESS,
            scheduled_start_at=timezone.now() - timedelta(hours=9),
            scheduled_end_at=timezone.now() - timedelta(hours=8),
        )

        result = services.close_out_missed()
        instance.refresh_from_db()

    assert result["abandoned"] == 1
    assert instance.status == LessonInstance.Status.ABANDONED


def test_excusing_records_a_reason_without_deleting_the_fact(school_a,
                                                             populated_version,
                                                             deputy):
    with TenantContext.scope(school_a):
        services.publish(populated_version)
        instance = LessonInstance.objects.first()
        LessonInstance.objects.filter(pk=instance.pk).update(
            status=LessonInstance.Status.MISSED,
            scheduled_end_at=timezone.now() - timedelta(hours=1),
        )
        instance.refresh_from_db()

        services.excuse(instance, reason="Teacher at a funeral; DOS approved.",
                        actor=deputy)
        instance.refresh_from_db()

    assert instance.status == LessonInstance.Status.EXCUSED
    assert "funeral" in instance.missed_reason


def test_a_verified_lesson_is_never_re_evaluated(school_a, populated_version):
    with TenantContext.scope(school_a):
        services.publish(populated_version)
        instance = LessonInstance.objects.first()
        LessonInstance.objects.filter(pk=instance.pk).update(
            status=LessonInstance.Status.VERIFIED,
            scheduled_end_at=timezone.now() - timedelta(days=1),
        )

        services.close_out_missed()
        instance.refresh_from_db()

    assert instance.status == LessonInstance.Status.VERIFIED
