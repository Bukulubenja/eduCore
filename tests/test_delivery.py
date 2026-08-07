"""Lesson sessions, substitutions, and the coverage/pace arithmetic."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from educore.core import signed_tokens
from educore.core.tenancy import TenantContext
from educore.delivery import services
from educore.delivery.models import CoverageEntry, LessonSession, Substitution
from educore.timetable import services as timetable_services
from educore.timetable.models import LessonInstance

from .conftest_phase2 import TERM_START

pytestmark = pytest.mark.django_db


@pytest.fixture
def instance(school_a, draft_version, slot, course, class_group, teacher, room,
             make_scheduled_lesson):
    """A published lesson happening right now."""
    make_scheduled_lesson(draft_version, weekday=0, slot=slot, course=course,
                          class_group=class_group, teacher=teacher, room=room)
    with TenantContext.scope(school_a):
        timetable_services.publish(draft_version)
        instance = LessonInstance.objects.first()
        now = timezone.now()
        LessonInstance.objects.filter(pk=instance.pk).update(
            scheduled_start_at=now, scheduled_end_at=now + timedelta(minutes=40)
        )
        instance.refresh_from_db()
        return instance


def scan(room, at=None):
    at = at or timezone.now()
    return services.issue_room_token(school_id=room.school_id, room=room,
                                     at=at)["token"]


def open_session(teacher, room, at=None, **kwargs):
    at = at or timezone.now()
    with TenantContext.scope(teacher.school_id):
        return services.open_session(teacher=teacher, room=room,
                                     token=scan(room, at), at=at, **kwargs)


# -- Opening -----------------------------------------------------------------


def test_scanning_the_classroom_code_opens_the_lesson(school_a, instance,
                                                      teacher, room):
    session = open_session(teacher, room)

    assert session.lesson_instance_id == instance.pk
    assert session.room_changed is False
    with TenantContext.scope(school_a):
        instance.refresh_from_db()
    assert instance.status == LessonInstance.Status.IN_PROGRESS


def test_a_room_change_is_flagged_not_rejected(school_a, instance, teacher,
                                               other_room):
    """Rooms change for ordinary reasons -- a projector, an exam, a flooded lab.

    Rejecting the scan does not stop the room change; it only means the lesson
    goes unrecorded, which is strictly worse than recording it with a flag.
    """
    session = open_session(teacher, other_room)

    assert session.room_changed is True
    assert session.verification_confidence < 100
    with TenantContext.scope(school_a):
        instance.refresh_from_db()
    assert instance.status == LessonInstance.Status.IN_PROGRESS


def test_a_late_start_is_measured(school_a, instance, teacher, room):
    session = open_session(teacher, room,
                          at=instance.scheduled_start_at + timedelta(minutes=7))
    assert session.late_start_minutes == 7


def test_scanning_with_no_timetabled_lesson_is_refused(school_a, teacher, room):
    with pytest.raises(services.NoScheduledLessonError):
        open_session(teacher, room)


def test_an_expired_code_cannot_open_a_lesson(school_a, instance, teacher, room):
    stale = scan(room, at=timezone.now() - timedelta(minutes=5))
    with TenantContext.scope(school_a), \
            pytest.raises(signed_tokens.TokenExpiredError):
        services.open_session(teacher=teacher, room=room, token=stale)


def test_a_staff_checkin_code_cannot_open_a_lesson(school_a, instance, teacher,
                                                   room, campus):
    """Keys are derived per purpose, so codes are not interchangeable."""
    from educore.presence import qr

    staff_code = qr.issue(school_id=school_a.id, campus_id=campus.id)["token"]

    with TenantContext.scope(school_a), \
            pytest.raises(signed_tokens.TokenBadSignatureError):
        services.open_session(teacher=teacher, room=room, token=staff_code)


def test_a_lesson_cannot_be_opened_twice(school_a, instance, teacher, room):
    open_session(teacher, room)
    with pytest.raises(services.DeliveryError, match="already been opened"):
        open_session(teacher, room)


def test_a_code_cannot_be_reused_by_the_same_teacher(school_a, instance,
                                                     teacher, room):
    at = timezone.now()
    token = scan(room, at)
    with TenantContext.scope(school_a):
        services.open_session(teacher=teacher, room=room, token=token, at=at)
        with pytest.raises(signed_tokens.TokenAlreadyRedeemedError):
            services.open_session(teacher=teacher, room=room, token=token, at=at)


# -- Substitution ------------------------------------------------------------


def test_a_colleague_opening_the_lesson_creates_a_substitution(school_a, instance,
                                                               room, teacher,
                                                               make_membership):
    """The lesson must not be silently credited to the absent teacher."""
    cover = make_membership(school_a, email="cover@example.com", name="Cover")

    session = open_session(cover, room)

    with TenantContext.scope(school_a):
        substitution = Substitution.objects.get(lesson_instance=instance)
    assert session.actual_teacher_id == cover.pk
    assert substitution.original_teacher_id == teacher.pk
    assert substitution.origin == Substitution.Origin.INFERRED


def test_a_colleague_in_the_wrong_room_cannot_attach_to_the_lesson(
    school_a, instance, other_room, make_membership
):
    """Otherwise a stray scan silently hijacks someone else's lesson."""
    cover = make_membership(school_a, email="stray@example.com")

    with pytest.raises(services.NoScheduledLessonError):
        open_session(cover, other_room)


# -- Closing -----------------------------------------------------------------


def test_closing_requires_coverage(school_a, instance, teacher, room, scheme):
    session = open_session(teacher, room)

    with TenantContext.scope(school_a), \
            pytest.raises(services.DeliveryError, match="what was covered"):
        services.close_session(session=session, coverage=[])


def test_closing_records_coverage_and_marks_the_lesson_delivered(
    school_a, instance, teacher, room, scheme
):
    session = open_session(teacher, room)
    with TenantContext.scope(school_a):
        unit = scheme.units.get(sequence=1)
        services.close_session(
            session=session,
            coverage=[{"unit_id": unit.id,
                       "completion": CoverageEntry.Completion.COMPLETED,
                       "homework_set": "Exercise 4.1"}],
        )
        session.refresh_from_db()
        instance.refresh_from_db()

    assert session.closed_at is not None
    assert instance.status == LessonInstance.Status.DELIVERED
    with TenantContext.scope(school_a):
        assert CoverageEntry.objects.count() == 1


def test_coverage_must_reference_this_courses_scheme(school_a, instance, teacher,
                                                     room, scheme, term, level,
                                                     academic_year):
    """Guards against an app sending a unit from the wrong subject."""
    from educore.academics.models import Course, Subject
    from educore.delivery.models import SchemeOfWork, SyllabusUnit

    session = open_session(teacher, room)
    with TenantContext.scope(school_a):
        other_subject = Subject.objects.create(school_id=school_a.id,
                                               name="Biology", code="BIO")
        other_course = Course.objects.create(school_id=school_a.id,
                                             subject=other_subject, level=level,
                                             academic_year=academic_year)
        other_scheme = SchemeOfWork.objects.create(school_id=school_a.id,
                                                   course=other_course, term=term)
        stray = SyllabusUnit.objects.create(school_id=school_a.id,
                                            scheme=other_scheme, sequence=1,
                                            title="Cells", planned_periods=2)

        with pytest.raises(services.DeliveryError, match="not in this course"):
            services.close_session(session=session,
                                   coverage=[{"unit_id": stray.id}])


def test_a_lesson_cannot_be_closed_twice(school_a, instance, teacher, room,
                                         scheme):
    session = open_session(teacher, room)
    with TenantContext.scope(school_a):
        unit = scheme.units.first()
        services.close_session(session=session, coverage=[{"unit_id": unit.id}])
        with pytest.raises(services.DeliveryError, match="already been closed"):
            services.close_session(session=session,
                                   coverage=[{"unit_id": unit.id}])


# -- Coverage and pace -------------------------------------------------------


def _cover(school_a, scheme, teacher, room, sequences, completion):
    """Mark the given units covered by one session.

    One session, several coverage entries -- a lesson routinely touches more
    than one topic, and LessonSession is one-to-one with LessonInstance so a
    session per unit is not a shape the domain permits.
    """
    with TenantContext.scope(school_a):
        session = LessonSession.objects.create(
            school_id=school_a.id,
            lesson_instance=LessonInstance.objects.first(),
            actual_teacher=teacher, room=room, opened_at=timezone.now(),
            closed_at=timezone.now(),
        )
        CoverageEntry.objects.bulk_create([
            CoverageEntry(school_id=school_a.id, session=session,
                          unit=scheme.units.get(sequence=sequence),
                          completion=completion)
            for sequence in sequences
        ])


@pytest.fixture
def history(school_a, instance, draft_version):
    """Backfill the term so far, giving pace real elapsed periods to count."""
    with TenantContext.scope(school_a):
        draft_version.refresh_from_db()
        timetable_services.materialise(draft_version, TERM_START,
                                       timezone.localdate())
    return draft_version


def test_coverage_is_weighted_by_planned_periods_not_unit_count(
    school_a, scheme, history, teacher, room
):
    """Units 1 and 4 are 1 + 2 = 3 of 10 periods: 30%, not 2-of-4 = 50%.

    A three-period topic is not equal to a one-period topic, and a percentage
    that pretends otherwise tells a DOS nothing they can act on.
    """
    _cover(school_a, scheme, teacher, room, [1, 4],
           CoverageEntry.Completion.COMPLETED)

    with TenantContext.scope(school_a):
        report = services.coverage_report(scheme=scheme)

    assert report.total_periods == 10
    assert report.covered_periods == 3
    assert report.coverage == 0.3
    assert report.units_completed == 2


def test_partial_coverage_does_not_count(school_a, scheme, history, teacher,
                                         room):
    """Otherwise a term of half-taught topics reports as finished."""
    _cover(school_a, scheme, teacher, room, [1, 2],
           CoverageEntry.Completion.PARTIAL)

    with TenantContext.scope(school_a):
        report = services.coverage_report(scheme=scheme)

    assert report.covered_periods == 0
    assert report.coverage == 0.0


def test_pace_measures_coverage_against_periods_actually_taught(
    school_a, scheme, history, teacher, room
):
    """Four weeks of lessons have happened; three periods' worth is covered."""
    _cover(school_a, scheme, teacher, room, [1, 4],
           CoverageEntry.Completion.COMPLETED)

    with TenantContext.scope(school_a):
        report = services.coverage_report(scheme=scheme)

    # One lesson a week across a four-week backfill.
    assert report.periods_elapsed >= 4
    assert report.expected > report.coverage
    assert report.pace < 0
    assert report.is_behind is True


def test_a_class_that_has_finished_the_plan_is_never_behind(school_a, scheme,
                                                            history, teacher,
                                                            room):
    _cover(school_a, scheme, teacher, room, [1, 2, 3, 4],
           CoverageEntry.Completion.COMPLETED)

    with TenantContext.scope(school_a):
        report = services.coverage_report(scheme=scheme)

    assert report.coverage == 1.0
    assert report.is_behind is False


def test_cancelled_lessons_do_not_raise_expectations(school_a, scheme, history,
                                                     teacher, room):
    """A fortnight of examinations must not push every class into the red."""
    with TenantContext.scope(school_a):
        before = services.coverage_report(scheme=scheme).periods_elapsed
        LessonInstance.objects.filter(
            date__lt=timezone.localdate()
        ).update(status=LessonInstance.Status.CANCELLED)
        after = services.coverage_report(scheme=scheme).periods_elapsed

    assert after < before
    assert after == 0


def test_coverage_at_the_start_of_term_expects_nothing(school_a, scheme, history):
    with TenantContext.scope(school_a):
        report = services.coverage_report(scheme=scheme, as_of=TERM_START)

    assert report.periods_elapsed == 0
    assert report.expected == 0.0
    assert report.is_behind is False


def test_an_empty_scheme_does_not_divide_by_zero(school_a, course, term):
    from educore.delivery.models import SchemeOfWork

    with TenantContext.scope(school_a):
        empty = SchemeOfWork.objects.create(school_id=school_a.id, course=course,
                                            term=term)
        report = services.coverage_report(scheme=empty)

    assert report.coverage == 0.0
    assert report.total_periods == 0


def test_coverage_is_reported_per_class_group(school_a, scheme, history, teacher,
                                              room, class_group, level,
                                              draft_version, slot, course,
                                              make_scheduled_lesson):
    """One stream ahead of another on the same plan must not be averaged away.

    Aggregating them hides exactly the problem a DOS dashboard exists to find.
    """
    from educore.academics.models import ClassGroup

    _cover(school_a, scheme, teacher, room, [1, 2],
           CoverageEntry.Completion.COMPLETED)

    with TenantContext.scope(school_a):
        other = ClassGroup.objects.create(school_id=school_a.id, level=level,
                                          name="S4 Green")
        mine = services.coverage_report(scheme=scheme, class_group=class_group)
        theirs = services.coverage_report(scheme=scheme, class_group=other)

    assert mine.coverage == 0.5
    assert theirs.coverage == 0.0


def test_schemes_behind_leads_with_the_worst(school_a, scheme, history, teacher,
                                             room, term, class_group):
    _cover(school_a, scheme, teacher, room, [1], CoverageEntry.Completion.COMPLETED)

    with TenantContext.scope(school_a):
        behind = services.schemes_behind(term=term)

    assert len(behind) == 1
    assert behind[0].scheme_id == scheme.id
    assert behind[0].class_group_id == class_group.pk
    assert behind[0].pace < 0
