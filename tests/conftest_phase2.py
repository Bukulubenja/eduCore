"""Fixtures for the timetable, delivery and students modules.

Imported by tests/conftest.py so they are available suite-wide without making
that file unreadable.
"""

from __future__ import annotations

from datetime import time, timedelta

import pytest
from django.utils import timezone

from educore.academics.models import (
    AcademicYear,
    ClassGroup,
    Course,
    Level,
    Subject,
    Term,
)
from educore.core.tenancy import TenantContext
from educore.delivery.models import SchemeOfWork, SyllabusUnit
from educore.students.models import Student
from educore.timetable.models import (
    PeriodGrid,
    PeriodSlot,
    Room,
    ScheduledLesson,
    TimetableVersion,
)

# The term brackets today, so materialised instances land inside it and pace
# arithmetic has real elapsed periods to count rather than calendar guesswork.
_TODAY = timezone.localdate()
TERM_START = _TODAY - timedelta(days=28)
TERM_END = _TODAY + timedelta(days=56)


@pytest.fixture
def academic_year(school_a):
    with TenantContext.scope(school_a):
        return AcademicYear.objects.create(
            school_id=school_a.id, name="2026",
            starts_on=TERM_START - timedelta(days=120),
            ends_on=TERM_END + timedelta(days=120),
            is_current=True,
        )


@pytest.fixture
def term(school_a, academic_year):
    with TenantContext.scope(school_a):
        return Term.objects.create(
            school_id=school_a.id, academic_year=academic_year, name="Term 2",
            sequence=2, starts_on=TERM_START, ends_on=TERM_END, is_current=True,
        )


@pytest.fixture
def level(school_a):
    with TenantContext.scope(school_a):
        return Level.objects.create(school_id=school_a.id, name="Senior 4",
                                    code="S4", sequence=4)


@pytest.fixture
def class_group(school_a, level):
    with TenantContext.scope(school_a):
        return ClassGroup.objects.create(school_id=school_a.id, level=level,
                                         name="S4 Blue")


@pytest.fixture
def course(school_a, level, academic_year):
    with TenantContext.scope(school_a):
        subject = Subject.objects.create(school_id=school_a.id, name="Physics",
                                         code="PHY")
        return Course.objects.create(school_id=school_a.id, subject=subject,
                                     level=level, academic_year=academic_year,
                                     periods_per_week=4)


@pytest.fixture
def room(school_a, campus):
    with TenantContext.scope(school_a):
        return Room.objects.create(school_id=school_a.id, campus=campus,
                                   name="Lab 1", code="L1")


@pytest.fixture
def other_room(school_a, campus):
    with TenantContext.scope(school_a):
        return Room.objects.create(school_id=school_a.id, campus=campus,
                                   name="Room 12", code="R12")


@pytest.fixture
def grid(school_a):
    with TenantContext.scope(school_a):
        grid = PeriodGrid.objects.create(school_id=school_a.id, name="Standard",
                                         is_default=True)
        PeriodSlot.objects.create(school_id=school_a.id, grid=grid,
                                  name="Period 1", sequence=1,
                                  starts_at=time(8, 0), ends_at=time(8, 40))
        PeriodSlot.objects.create(school_id=school_a.id, grid=grid,
                                  name="Period 2", sequence=2,
                                  starts_at=time(8, 45), ends_at=time(9, 25))
        return grid


@pytest.fixture
def slot(grid):
    with TenantContext.scope(grid.school_id):
        return grid.slots.get(sequence=1)


@pytest.fixture
def second_slot(grid):
    with TenantContext.scope(grid.school_id):
        return grid.slots.get(sequence=2)


@pytest.fixture
def draft_version(school_a, academic_year, grid):
    with TenantContext.scope(school_a):
        return TimetableVersion.objects.create(
            school_id=school_a.id, name="2026 Term 2 v1",
            academic_year=academic_year, grid=grid,
            effective_from=TERM_START,
        )


@pytest.fixture
def make_scheduled_lesson(school_a):
    def _make(version, *, weekday, slot, course, class_group, teacher, room):
        with TenantContext.scope(school_a):
            return ScheduledLesson.objects.create(
                school_id=school_a.id, version=version, weekday=weekday,
                slot=slot, course=course, class_group=class_group,
                teacher=teacher, room=room,
            )

    return _make


@pytest.fixture
def scheme(school_a, course, term):
    """Four units, 10 planned periods, deliberately uneven.

    Uneven weights are the point: a scheme of equal-sized units cannot tell a
    period-weighted coverage calculation from a unit-counting one, so a test
    built on it would pass either way.
    """
    with TenantContext.scope(school_a):
        scheme = SchemeOfWork.objects.create(school_id=school_a.id,
                                             course=course, term=term)
        for sequence, (title, periods) in enumerate([
            ("Measurement", 1),
            ("Mechanics", 4),
            ("Waves", 3),
            ("Electricity", 2),
        ], start=1):
            SyllabusUnit.objects.create(school_id=school_a.id, scheme=scheme,
                                        sequence=sequence, title=title,
                                        planned_periods=periods)
        return scheme


@pytest.fixture
def students(school_a, class_group, term):
    from educore.students.services import enrol

    created = []
    with TenantContext.scope(school_a):
        for index in range(1, 6):
            student = Student.objects.create(
                school_id=school_a.id, admission_number=f"ADM{index:03d}",
                full_name=f"Student {index}",
                scan_code=f"SCAN{index:03d}",
            )
            enrol(student=student, class_group=class_group, term=term)
            created.append(student)
    return created


@pytest.fixture
def next_monday():
    """A future Monday, so instances can be materialised ahead of today."""
    today = timezone.localdate()
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)
