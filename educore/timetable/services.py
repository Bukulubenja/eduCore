"""Timetable use cases: conflict detection, publishing, materialisation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone

from educore.core import audit
from educore.core.models import OutboxMessage

from .models import (
    CalendarException,
    LessonInstance,
    ScheduledLesson,
    TimetableVersion,
)

# How far ahead instances are materialised. Long enough that a teacher's app
# can cache a fortnight offline (ADR-0003), short enough that a mid-term
# timetable revision does not leave a term of stale rows to rewrite.
HORIZON_DAYS = 14


class TimetableError(Exception):
    pass


@dataclass
class Conflict:
    kind: str
    weekday: int
    slot_id: object
    detail: str


def find_conflicts(version: TimetableVersion) -> list[Conflict]:
    """Double-booked teachers and rooms within one version.

    Class-group clashes are already impossible -- a unique constraint covers
    them -- because a group being in two places is never a legitimate draft
    state. Teacher and room clashes are checked here rather than constrained,
    since a half-built draft is allowed to be temporarily inconsistent.
    """
    lessons = list(
        ScheduledLesson.objects.filter(version=version)
        .select_related("teacher__user", "room", "slot", "class_group")
    )

    conflicts: list[Conflict] = []
    for field, kind in (("teacher_id", "teacher"), ("room_id", "room")):
        seen: dict[tuple, ScheduledLesson] = {}
        for lesson in lessons:
            key = (lesson.weekday, lesson.slot_id, getattr(lesson, field))
            clash = seen.get(key)
            if clash is not None:
                subject = (lesson.teacher.user.full_name if kind == "teacher"
                           else lesson.room.name)
                conflicts.append(Conflict(
                    kind=kind,
                    weekday=lesson.weekday,
                    slot_id=lesson.slot_id,
                    detail=(f"{subject} is double-booked in {lesson.slot.name}: "
                            f"{clash.class_group.name} and "
                            f"{lesson.class_group.name}"),
                ))
            else:
                seen[key] = lesson
    return conflicts


@transaction.atomic
def publish(version: TimetableVersion, *, published_by=None,
            horizon_days: int = HORIZON_DAYS) -> int:
    """Publish a draft and materialise its first horizon of instances.

    Refuses on any conflict. A timetable that double-books a teacher will
    generate "missed lesson" rows for a lesson nobody could ever have taught,
    and those false accusations are what teach a staff room to distrust the
    whole system.
    """
    if version.status != TimetableVersion.Status.DRAFT:
        raise TimetableError("Only a draft timetable can be published.")

    conflicts = find_conflicts(version)
    if conflicts:
        raise TimetableError(
            "Cannot publish with unresolved conflicts:\n"
            + "\n".join(f"  - {c.detail}" for c in conflicts)
        )

    if not ScheduledLesson.objects.filter(version=version).exists():
        raise TimetableError("Cannot publish a timetable with no lessons.")

    # Supersede whatever is live: two published versions overlapping in time
    # would materialise two instances for the same slot.
    TimetableVersion.objects.filter(
        status=TimetableVersion.Status.PUBLISHED,
        academic_year=version.academic_year,
    ).exclude(pk=version.pk).update(
        status=TimetableVersion.Status.ARCHIVED,
        effective_to=version.effective_from - timedelta(days=1),
    )

    version.status = TimetableVersion.Status.PUBLISHED
    version.published_at = timezone.now()
    version.published_by = published_by
    version.save(update_fields=["status", "published_at", "published_by",
                                "updated_at"])

    start = max(version.effective_from, timezone.localdate())
    created = materialise(version, start, start + timedelta(days=horizon_days))

    audit.record(action="timetable.published",
                 object_type="timetable.TimetableVersion",
                 object_id=version.id, actor_membership=published_by,
                 after={"name": version.name, "instances_created": created})
    OutboxMessage.objects.create(
        school_id=version.school_id, topic="timetable.published",
        payload={"version_id": str(version.id), "instances": created},
    )
    return created


def materialise(version: TimetableVersion, start_date, end_date) -> int:
    """Create LessonInstance rows for a date range. Idempotent.

    Safe to re-run: existing instances are left exactly as they are, so a
    nightly job cannot resurrect a cancelled lesson or overwrite one already
    delivered.
    """
    if version.status != TimetableVersion.Status.PUBLISHED:
        return 0

    lessons = list(
        ScheduledLesson.objects.filter(version=version)
        .select_related("slot", "course", "class_group")
    )
    if not lessons:
        return 0

    by_weekday: dict[int, list[ScheduledLesson]] = {}
    for lesson in lessons:
        by_weekday.setdefault(lesson.weekday, []).append(lesson)

    suppressed = set(
        CalendarException.objects
        .filter(date__gte=start_date, date__lte=end_date, suppresses_lessons=True)
        .values_list("date", flat=True)
    )

    existing = set(
        LessonInstance.objects
        .filter(date__gte=start_date, date__lte=end_date,
                scheduled_lesson__version=version)
        .values_list("scheduled_lesson_id", "date")
    )

    tz = timezone.get_current_timezone()
    pending: list[LessonInstance] = []
    day = start_date
    while day <= end_date:
        if day in suppressed or (version.effective_to and day > version.effective_to):
            day += timedelta(days=1)
            continue
        if day < version.effective_from:
            day += timedelta(days=1)
            continue

        for lesson in by_weekday.get(day.weekday(), []):
            if (lesson.pk, day) in existing:
                continue
            pending.append(LessonInstance(
                school_id=version.school_id,
                scheduled_lesson=lesson,
                date=day,
                scheduled_start_at=datetime.combine(
                    day, lesson.slot.starts_at, tzinfo=tz),
                scheduled_end_at=datetime.combine(
                    day, lesson.slot.ends_at, tzinfo=tz),
                expected_teacher_id=lesson.teacher_id,
                expected_room_id=lesson.room_id,
                course_id=lesson.course_id,
                class_group_id=lesson.class_group_id,
            ))
        day += timedelta(days=1)

    if pending:
        LessonInstance.objects.bulk_create(pending, ignore_conflicts=True)
    return len(pending)


def materialise_horizon(*, horizon_days: int = HORIZON_DAYS) -> int:
    """Roll the horizon forward. Run nightly."""
    today = timezone.localdate()
    total = 0
    for version in TimetableVersion.objects.filter(
        status=TimetableVersion.Status.PUBLISHED
    ):
        total += materialise(version, today, today + timedelta(days=horizon_days))
    return total


def close_out_missed(*, grace_minutes: int = 10, now=None) -> dict:
    """Settle instances whose window has passed. Run every few minutes.

    Deliberately a machine judgement, never a user's. That the system draws
    this conclusion consistently and without favour is precisely the
    accountability the product sells; a human "marking" lessons missed would
    reintroduce exactly the discretion schools already have.
    """
    now = now or timezone.now()
    cutoff = now - timedelta(minutes=grace_minutes)

    missed = LessonInstance.objects.filter(
        status=LessonInstance.Status.SCHEDULED,
        scheduled_end_at__lt=cutoff,
    ).update(status=LessonInstance.Status.MISSED,
             missed_reason="No lesson session was ever opened.")

    abandoned = LessonInstance.objects.filter(
        status=LessonInstance.Status.IN_PROGRESS,
        scheduled_end_at__lt=now - timedelta(hours=4),
    ).update(status=LessonInstance.Status.ABANDONED,
             missed_reason="Session opened but never closed.")

    return {"missed": missed, "abandoned": abandoned}


def excuse(instance: LessonInstance, *, reason: str, actor=None) -> LessonInstance:
    """Accept a reason for a missed lesson, without deleting the fact of it."""
    if instance.status not in {LessonInstance.Status.MISSED,
                               LessonInstance.Status.ABANDONED}:
        raise TimetableError("Only a missed or abandoned lesson can be excused.")

    before = {"status": instance.status, "reason": instance.missed_reason}
    instance.status = LessonInstance.Status.EXCUSED
    instance.missed_reason = reason
    instance.save(update_fields=["status", "missed_reason", "updated_at"])

    audit.record(action="timetable.lesson.excused",
                 object_type="timetable.LessonInstance", object_id=instance.id,
                 actor_membership=actor, before=before,
                 after={"status": instance.status, "reason": reason})
    return instance
