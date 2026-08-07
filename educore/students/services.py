"""Student registers, gate events, and the on-site-but-not-in-class check."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from educore.core import audit
from educore.core.models import OutboxMessage
from educore.timetable.models import LessonInstance

from .models import (
    Enrolment,
    GateEvent,
    GuardianLink,
    Student,
    StudentAttendance,
    StudentAttendanceStatus,
)

# Notify guardians once, after this many consecutive missed lessons -- never
# per lesson. A parent who gets six notifications a day stops reading them,
# and then the one that mattered is missed too.
ABSENCE_NOTIFY_AFTER_LESSONS = 1


class RegisterError(Exception):
    pass


@dataclass
class RegisterRow:
    student_id: object
    full_name: str
    admission_number: str
    status: str


def build_register(*, lesson_instance: LessonInstance) -> list[RegisterRow]:
    """The roster for a lesson, pre-marked present.

    Default-present with tap-absent is the whole design. Marking forty
    students individually is the reason paper registers survive; marking three
    absentees takes about eight seconds, which is a ritual a teacher will
    actually perform every lesson.
    """
    enrolled = (
        Student.objects
        .filter(enrolments__class_group_id=lesson_instance.class_group_id,
                enrolments__is_active=True,
                status=Student.Status.ENROLLED)
        .order_by("full_name")
        .distinct()
    )

    existing = {
        mark.student_id: mark.status
        for mark in StudentAttendance.objects.filter(
            lesson_instance=lesson_instance
        )
    }

    return [
        RegisterRow(
            student_id=student.id,
            full_name=student.full_name,
            admission_number=student.admission_number,
            status=existing.get(student.id, StudentAttendanceStatus.PRESENT),
        )
        for student in enrolled
    ]


@transaction.atomic
def submit_register(*, lesson_instance: LessonInstance, marked_by,
                    exceptions: list[dict]) -> int:
    """Save a register. Only the exceptions are sent; everyone else is present.

    `exceptions` is a list of {student_id, status, note}. Anyone not named is
    recorded present, which keeps the payload tiny on a poor connection.
    """
    roster = {row.student_id for row in build_register(lesson_instance=lesson_instance)}

    marks = {}
    for entry in exceptions:
        student_id = entry["student_id"]
        if student_id not in roster:
            raise RegisterError("That student is not enrolled in this class.")
        marks[student_id] = (entry.get("status", StudentAttendanceStatus.ABSENT),
                             entry.get("note", ""))

    StudentAttendance.objects.filter(lesson_instance=lesson_instance).delete()
    StudentAttendance.objects.bulk_create([
        StudentAttendance(
            school_id=lesson_instance.school_id,
            student_id=student_id,
            lesson_instance=lesson_instance,
            date=lesson_instance.date,
            status=marks.get(student_id, (StudentAttendanceStatus.PRESENT, ""))[0],
            note=marks.get(student_id, ("", ""))[1],
            marked_by=marked_by,
        )
        for student_id in roster
    ])

    absent_ids = [sid for sid, (status, _note) in marks.items()
                  if status == StudentAttendanceStatus.ABSENT]
    if absent_ids:
        _queue_absence_notifications(lesson_instance, absent_ids)

    audit.record(action="students.register.submitted",
                 object_type="timetable.LessonInstance",
                 object_id=lesson_instance.id, actor_membership=marked_by,
                 after={"present": len(roster) - len(marks),
                        "exceptions": len(marks)})
    return len(roster)


def _queue_absence_notifications(lesson_instance, absent_ids) -> None:
    """Notify guardians of students absent from their first lesson of the day."""
    earlier_today = (
        LessonInstance.objects
        .filter(date=lesson_instance.date,
                class_group_id=lesson_instance.class_group_id,
                scheduled_start_at__lt=lesson_instance.scheduled_start_at)
        .count()
    )
    if earlier_today >= ABSENCE_NOTIFY_AFTER_LESSONS:
        return          # Already notified on the first lesson of the day.

    recipients = list(
        GuardianLink.objects
        .filter(student_id__in=absent_ids, receives_notifications=True,
                verified=True)
        .values_list("student_id", "membership_id")
    )
    if not recipients:
        return

    OutboxMessage.objects.create(
        school_id=lesson_instance.school_id,
        topic="students.absence.detected",
        payload={
            "date": str(lesson_instance.date),
            "lesson_instance_id": str(lesson_instance.id),
            "recipients": [{"student_id": str(s), "membership_id": str(m)}
                           for s, m in recipients],
        },
    )


# -- Gate --------------------------------------------------------------------


@transaction.atomic
def record_gate_event(*, student: Student, campus, direction: str,
                      occurred_at, client_event_id, method: str = "qr") -> GateEvent:
    """Record an arrival or departure. Idempotent on client_event_id."""
    existing = GateEvent.objects.filter(client_event_id=client_event_id).first()
    if existing is not None:
        return existing

    event = GateEvent.objects.create(
        school_id=student.school_id, student=student, campus=campus,
        direction=direction, method=method, occurred_at=occurred_at,
        client_event_id=client_event_id,
    )
    OutboxMessage.objects.create(
        school_id=student.school_id, topic="students.gate.scanned",
        payload={"student_id": str(student.id), "direction": direction,
                 "occurred_at": occurred_at.isoformat()},
    )
    return event


def reconcile_gate_and_registers(*, date=None) -> list[dict]:
    """Students scanned in at the gate but absent from their lessons.

    A child on campus and not in class matters far more than an attendance
    percentage, and neither record surfaces it alone. This is the reconciliation
    that finds them.
    """
    date = date or timezone.localdate()

    arrived = set(
        GateEvent.objects
        .filter(occurred_at__date=date, direction=GateEvent.Direction.IN)
        .values_list("student_id", flat=True)
    )
    if not arrived:
        return []

    absences: dict[object, int] = {}
    for mark in StudentAttendance.objects.filter(
        date=date, student_id__in=arrived,
        status=StudentAttendanceStatus.ABSENT,
        lesson_instance__isnull=False,
    ).only("student_id"):
        absences[mark.student_id] = absences.get(mark.student_id, 0) + 1

    flagged = [
        {"student_id": student_id, "lessons_missed": count, "date": str(date)}
        for student_id, count in absences.items()
        if count >= 2
    ]
    if flagged:
        OutboxMessage.objects.create(
            school_id=Student.objects.filter(
                id__in=[f["student_id"] for f in flagged]
            ).values_list("school_id", flat=True).first(),
            topic="students.on_site_not_in_class",
            payload={"date": str(date),
                     "students": [{"student_id": str(f["student_id"]),
                                   "lessons_missed": f["lessons_missed"]}
                                  for f in flagged]},
        )
    return flagged


def enrol(*, student: Student, class_group, term) -> Enrolment:
    """Place a student in a class for a term, ending any prior placement."""
    Enrolment.objects.filter(student=student, term=term, is_active=True).update(
        is_active=False
    )
    enrolment = Enrolment.objects.create(
        school_id=student.school_id, student=student,
        class_group=class_group, term=term,
    )
    audit.record(action="students.enrolled", object_type="students.Enrolment",
                 object_id=enrolment.id,
                 after={"student": str(student.id),
                        "class_group": str(class_group.pk),
                        "term": str(term.pk)})
    return enrolment


def attendance_summary(*, student: Student, since=None, until=None) -> dict:
    until = until or timezone.localdate()
    since = since or (until - timedelta(days=30))

    marks = StudentAttendance.objects.filter(student=student, date__gte=since,
                                             date__lte=until)
    total = marks.count()
    present = marks.filter(status__in=[StudentAttendanceStatus.PRESENT,
                                       StudentAttendanceStatus.LATE]).count()
    return {
        "student_id": str(student.id),
        "from": str(since),
        "to": str(until),
        "marks": total,
        "present": present,
        "rate": round(present / total, 4) if total else None,
    }
