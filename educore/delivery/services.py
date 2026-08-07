"""Lesson delivery use cases and the coverage/pace formulas."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from educore.core import audit, signed_tokens
from educore.core.models import OutboxMessage
from educore.timetable.models import LessonInstance, Room

from .models import (
    CoverageEntry,
    LessonQrRedemption,
    LessonSession,
    SchemeOfWork,
    Substitution,
    SyllabusUnit,
)

PURPOSE = "lesson_scan"
DEFAULT_GRACE_MINUTES = 10


class DeliveryError(Exception):
    pass


class NoScheduledLessonError(DeliveryError):
    """No timetabled lesson matches this teacher, room and time."""


# -- Classroom codes ---------------------------------------------------------


def issue_room_token(*, school_id, room: Room, ttl_seconds: int = 30, at=None) -> dict:
    return signed_tokens.issue(school_id=school_id, scope_id=room.id,
                               purpose=PURPOSE, ttl_seconds=ttl_seconds, at=at)


def _verify_room_token(token: str, *, school_id, room: Room, at, membership):
    payload = signed_tokens.verify(token, school_id=school_id, scope_id=room.id,
                                   purpose=PURPOSE, at=at)
    if LessonQrRedemption.objects.filter(nonce=payload["non"],
                                         membership=membership).exists():
        raise signed_tokens.TokenAlreadyRedeemedError(
            "this code has already been used by you"
        )
    return payload


# -- Opening and closing a session -------------------------------------------


def _match_instance(*, teacher, room, at, grace_minutes):
    """Find the lesson this scan belongs to.

    Matched on time and teacher, *not* on room. Rooms change constantly for
    ordinary reasons -- a projector, an exam, a flooded lab -- and rejecting a
    scan because the room differs means the lesson simply goes unrecorded,
    which is a worse outcome than recording it with a flag.
    """
    window_start = at - timedelta(minutes=grace_minutes)
    window_end = at + timedelta(minutes=grace_minutes)

    candidates = list(
        LessonInstance.objects
        .filter(scheduled_start_at__lte=window_end,
                scheduled_end_at__gte=window_start)
        .exclude(status__in=[LessonInstance.Status.CANCELLED,
                             LessonInstance.Status.VERIFIED])
        .select_related("course", "class_group", "expected_room")
        .order_by("scheduled_start_at")
    )
    if not candidates:
        return None, None

    own = [i for i in candidates if i.expected_teacher_id == teacher.pk]
    if own:
        return own[0], None

    # Someone else's lesson: a substitution, if the room agrees. Requiring the
    # room here is deliberate -- without it, a teacher scanning in the wrong
    # room could silently attach themselves to a colleague's lesson.
    in_room = [i for i in candidates if i.expected_room_id == room.pk]
    if in_room:
        return in_room[0], in_room[0].expected_teacher_id
    return None, None


@transaction.atomic
def open_session(*, teacher, room: Room, token: str, at=None,
                 grace_minutes: int = DEFAULT_GRACE_MINUTES) -> LessonSession:
    """Start a lesson by scanning the classroom's rotating code."""
    at = at or timezone.now()

    payload = _verify_room_token(token, school_id=teacher.school_id, room=room,
                                 at=at, membership=teacher)

    instance, substituting_for = _match_instance(
        teacher=teacher, room=room, at=at, grace_minutes=grace_minutes
    )
    if instance is None:
        raise NoScheduledLessonError(
            "No timetabled lesson for you at this time. If you are covering "
            "for a colleague, ask the DOS to record the substitution."
        )
    if hasattr(instance, "session"):
        raise DeliveryError("This lesson has already been opened.")

    late_minutes = int((at - instance.scheduled_start_at).total_seconds() // 60)
    room_changed = room.pk != instance.expected_room_id

    session = LessonSession.objects.create(
        school_id=teacher.school_id,
        lesson_instance=instance,
        actual_teacher=teacher,
        room=room,
        opened_at=at,
        room_changed=room_changed,
        late_start_minutes=max(0, late_minutes),
        verification_confidence=100 if not room_changed else 80,
    )

    LessonQrRedemption.objects.create(nonce=payload["non"], membership=teacher)

    if substituting_for is not None:
        Substitution.objects.create(
            school_id=teacher.school_id,
            lesson_instance=instance,
            original_teacher_id=substituting_for,
            substitute_teacher=teacher,
            origin=Substitution.Origin.INFERRED,
        )
        OutboxMessage.objects.create(
            school_id=teacher.school_id, topic="delivery.substitution.inferred",
            payload={"lesson_instance_id": str(instance.id),
                     "substitute_id": str(teacher.pk)},
        )

    instance.status = LessonInstance.Status.IN_PROGRESS
    instance.save(update_fields=["status", "updated_at"])

    if room_changed:
        OutboxMessage.objects.create(
            school_id=teacher.school_id, topic="delivery.room.changed",
            payload={"lesson_instance_id": str(instance.id),
                     "expected_room_id": str(instance.expected_room_id),
                     "actual_room_id": str(room.id)},
        )

    audit.record(action="delivery.session.opened",
                 object_type="delivery.LessonSession", object_id=session.id,
                 actor_membership=teacher,
                 after={"lesson_instance": str(instance.id),
                        "room_changed": room_changed,
                        "late_start_minutes": session.late_start_minutes})
    return session


@transaction.atomic
def close_session(*, session: LessonSession, coverage: list[dict],
                  notes: str = "", at=None) -> LessonSession:
    """End a lesson. Coverage is required, and that is the whole point.

    This is the only mandatory data entry in a teacher's day, and it is what
    turns "the lesson happened" into "the syllabus is 47% covered and this
    class is two weeks behind".
    """
    if session.closed_at is not None:
        raise DeliveryError("This lesson has already been closed.")
    if not coverage:
        raise DeliveryError(
            "Record what was covered before closing the lesson."
        )

    at = at or timezone.now()
    instance = session.lesson_instance

    unit_ids = [entry["unit_id"] for entry in coverage]
    scheme_units = set(
        SyllabusUnit.objects.filter(
            id__in=unit_ids, scheme__course_id=instance.course_id
        ).values_list("id", flat=True)
    )
    stray = [u for u in unit_ids if u not in scheme_units]
    if stray:
        raise DeliveryError(
            "Those topics are not in this course's scheme of work."
        )

    session.closed_at = at
    session.notes = notes
    session.save(update_fields=["closed_at", "notes", "updated_at"])

    CoverageEntry.objects.bulk_create([
        CoverageEntry(
            school_id=session.school_id,
            session=session,
            unit_id=entry["unit_id"],
            completion=entry.get("completion", CoverageEntry.Completion.PARTIAL),
            homework_set=entry.get("homework_set", ""),
            notes=entry.get("notes", ""),
        )
        for entry in coverage
    ], ignore_conflicts=True)

    instance.status = LessonInstance.Status.DELIVERED
    instance.save(update_fields=["status", "updated_at"])

    OutboxMessage.objects.create(
        school_id=session.school_id, topic="delivery.session.closed",
        payload={"session_id": str(session.id),
                 "lesson_instance_id": str(instance.id),
                 "duration_minutes": session.duration_minutes,
                 "units_covered": len(coverage)},
    )
    audit.record(action="delivery.session.closed",
                 object_type="delivery.LessonSession", object_id=session.id,
                 actor_membership=session.actual_teacher,
                 after={"duration_minutes": session.duration_minutes,
                        "units": len(coverage)})
    return session


def record_substitution(*, instance: LessonInstance, substitute, reason: str,
                        approved_by=None) -> Substitution:
    substitution, _created = Substitution.objects.update_or_create(
        lesson_instance=instance,
        defaults={
            "school_id": instance.school_id,
            "original_teacher_id": instance.expected_teacher_id,
            "substitute_teacher": substitute,
            "origin": Substitution.Origin.PLANNED,
            "reason": reason,
            "approved_by": approved_by,
        },
    )
    audit.record(action="delivery.substitution.recorded",
                 object_type="delivery.Substitution", object_id=substitution.id,
                 actor_membership=approved_by,
                 after={"lesson_instance": str(instance.id),
                        "substitute": str(substitute.pk), "reason": reason})
    return substitution


# -- Coverage and pace -------------------------------------------------------


@dataclass
class CoverageReport:
    scheme_id: object
    class_group_id: object
    total_periods: int
    covered_periods: int
    periods_elapsed: int
    coverage: float          # proportion of the term's plan completed
    expected: float          # proportion that should be done by now
    pace: float              # coverage - expected; negative means behind
    units_total: int
    units_completed: int

    @property
    def is_behind(self) -> bool:
        return self.pace < -0.05


def coverage_report(*, scheme: SchemeOfWork, class_group=None,
                    as_of=None) -> CoverageReport:
    """Coverage and pace for a scheme, optionally for one class group.

        coverage = planned_periods(units completed) / planned_periods(all)
        expected = periods actually elapsed          / planned_periods(all)
        pace     = coverage - expected

    Two distinct numbers, routinely conflated. Coverage of 35% means nothing
    on its own; 35% in week 9 of 13 means the class will not finish, and that
    is the number a DOS needs in week 9 rather than at exams.

    Only `completed` counts toward coverage. Counting `partial` lets a term of
    half-taught topics report as finished.

    Expectation is counted in *lesson periods that have actually occurred*,
    not calendar days. A term with a fortnight of examinations in the middle
    materialises no instances for that fortnight, so it does not silently
    push every class into the red. Calendar-day arithmetic would.

    `class_group` matters more than it looks. A scheme is shared by every
    group taking the course, but coverage is not: S4 Blue can be a fortnight
    ahead of S4 Green on the same plan. Aggregating them hides exactly the
    problem the dashboard exists to surface, so per-group is the useful unit
    and the whole-course figure is the rollup.
    """
    as_of = as_of or timezone.localdate()

    units = list(SyllabusUnit.objects.filter(scheme=scheme).order_by("sequence"))
    total_periods = sum(u.planned_periods for u in units)
    group_id = getattr(class_group, "pk", class_group)
    if total_periods == 0:
        return CoverageReport(scheme.id, group_id, 0, 0, 0, 0.0, 0.0, 0.0, 0, 0)

    entries = CoverageEntry.objects.filter(
        unit__scheme=scheme, completion=CoverageEntry.Completion.COMPLETED
    )
    if group_id is not None:
        entries = entries.filter(
            session__lesson_instance__class_group_id=group_id
        )
    completed_ids = set(entries.values_list("unit_id", flat=True))
    covered_periods = sum(u.planned_periods for u in units if u.id in completed_ids)

    term = scheme.term
    instances = LessonInstance.objects.filter(
        course_id=scheme.course_id,
        date__gte=term.starts_on,
        date__lte=min(as_of, term.ends_on),
    ).exclude(status__in=[LessonInstance.Status.CANCELLED,
                          LessonInstance.Status.EXCUSED])
    if group_id is not None:
        instances = instances.filter(class_group_id=group_id)
    periods_elapsed = instances.count()

    coverage = covered_periods / total_periods
    expected = min(1.0, periods_elapsed / total_periods)

    return CoverageReport(
        scheme_id=scheme.id,
        class_group_id=group_id,
        total_periods=total_periods,
        covered_periods=covered_periods,
        periods_elapsed=periods_elapsed,
        coverage=round(coverage, 4),
        expected=round(expected, 4),
        pace=round(coverage - expected, 4),
        units_total=len(units),
        units_completed=len(completed_ids),
    )


def schemes_behind(*, term, as_of=None) -> list[CoverageReport]:
    """Every class group falling behind, worst first. The DOS dashboard's lead.

    Reported per group rather than per scheme, because "Physics S4 is behind"
    is not actionable when only one of its three streams actually is.
    """
    reports: list[CoverageReport] = []
    for scheme in (SchemeOfWork.objects.filter(term=term)
                   .select_related("course", "term")):
        group_ids = (
            LessonInstance.objects
            .filter(course_id=scheme.course_id, date__gte=term.starts_on,
                    date__lte=term.ends_on)
            # order_by() clears Meta.ordering. Without it Django adds the
            # ordering column to the SELECT, and DISTINCT then dedupes on
            # (class_group_id, scheduled_start_at) -- returning one row per
            # lesson rather than one per group.
            .order_by()
            .values_list("class_group_id", flat=True).distinct()
        )
        for group_id in group_ids:
            reports.append(coverage_report(scheme=scheme, class_group=group_id,
                                           as_of=as_of))

    return sorted([r for r in reports if r.is_behind], key=lambda r: r.pace)
