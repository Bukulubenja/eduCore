"""Dashboards and early warning.

Doc 01 opens with a principal at 09:30 who wants to know whether today is
going well. That is this module's brief, and it is the payoff for every link
in the evidence chain: presence, delivery, coverage and registers, read back
as one picture rather than four reports.

Everything here is a read model computed from stored facts. Nothing in this
module writes anything a decision hangs on.
"""

from __future__ import annotations

from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone

from educore.assessment.models import AssessmentState, Score
from educore.core.models import Membership
from educore.delivery.models import LessonSession
from educore.presence.models import AttendanceRecord, AttendanceStatus, Disposition
from educore.students.models import (
    Student,
    StudentAttendance,
    StudentAttendanceStatus,
)
from educore.timetable.models import LessonInstance

# An attendance rate below this, over a meaningful number of marks, is the
# strongest single predictor available that a student is disengaging.
AT_RISK_ATTENDANCE = 0.80
AT_RISK_MIN_MARKS = 10
AT_RISK_AVERAGE = 40.0


def today(*, on=None) -> dict:
    """The 09:30 view: is the school running?

    Deliberately shaped as answers, not tables. "69 of 72 checked in, 3 lessons
    missed" is a thing a head teacher can act on before break; a grid of rows
    is something they will read after lunch, if at all.
    """
    on = on or timezone.localdate()

    expected_staff = (
        Membership.objects
        .filter(status=Membership.Status.ACTIVE)
        .exclude(role_assignments__role__code__in=["parent", "student"])
        .distinct().count()
    )

    records = AttendanceRecord.objects.filter(date=on)
    by_status = dict(
        records.values_list("status").annotate(n=Count("id")).values_list("status", "n")
    )
    checked_in = sum(by_status.get(s, 0) for s in
                     (AttendanceStatus.PRESENT, AttendanceStatus.LATE,
                      AttendanceStatus.PARTIAL))

    instances = LessonInstance.objects.filter(date=on)
    lesson_counts = dict(
        instances.values_list("status").annotate(n=Count("id"))
        .values_list("status", "n")
    )

    marks = StudentAttendance.objects.filter(date=on, lesson_instance__isnull=False)
    students_marked = marks.values("student_id").distinct().count()
    students_absent = (marks.filter(status=StudentAttendanceStatus.ABSENT)
                       .values("student_id").distinct().count())

    needs_review = records.filter(
        disposition=Disposition.PROVISIONAL,
        resolution=AttendanceRecord.Resolution.AUTO,
    ).count()

    return {
        "date": str(on),
        "staff": {
            "expected": expected_staff,
            "checked_in": checked_in,
            "late": by_status.get(AttendanceStatus.LATE, 0),
            "absent": max(0, expected_staff - checked_in),
            "not_checked_out": by_status.get(AttendanceStatus.PARTIAL, 0),
            "needs_review": needs_review,
        },
        "lessons": {
            "scheduled": instances.count(),
            "delivered": (lesson_counts.get(LessonInstance.Status.DELIVERED, 0)
                          + lesson_counts.get(LessonInstance.Status.VERIFIED, 0)),
            "in_progress": lesson_counts.get(LessonInstance.Status.IN_PROGRESS, 0),
            "missed": lesson_counts.get(LessonInstance.Status.MISSED, 0),
            "excused": lesson_counts.get(LessonInstance.Status.EXCUSED, 0),
        },
        "students": {
            "marked": students_marked,
            "absent": students_absent,
        },
    }


def punctuality(*, since=None, until=None, limit: int = 50) -> list[dict]:
    """Per-teacher punctuality over a window.

    Reported alongside the number of records it rests on. A "50% late" built
    from two days is noise, and presenting it without its denominator invites
    someone to act on it.
    """
    until = until or timezone.localdate()
    since = since or (until - timedelta(days=30))

    rows = (
        AttendanceRecord.objects
        .filter(date__gte=since, date__lte=until)
        .values("membership_id", "membership__user__full_name")
        .annotate(
            days=Count("id"),
            late=Count("id", filter=Q(status=AttendanceStatus.LATE)),
            partial=Count("id", filter=Q(status=AttendanceStatus.PARTIAL)),
            absent=Count("id", filter=Q(status=AttendanceStatus.ABSENT)),
        )
        .order_by("-late")[:limit]
    )

    return [
        {
            "membership_id": str(row["membership_id"]),
            "name": row["membership__user__full_name"],
            "days_recorded": row["days"],
            "late": row["late"],
            "never_checked_out": row["partial"],
            "absent": row["absent"],
            "late_rate": round(row["late"] / row["days"], 4) if row["days"] else None,
            # Below this, the sample is too small to mean anything.
            "reliable": row["days"] >= 10,
        }
        for row in rows
    ]


def workload(*, since=None, until=None) -> list[dict]:
    """Lessons actually delivered per teacher, and how many were cover.

    A department where one person takes every substitution is a rota problem,
    and it is invisible in a timetable because the timetable says otherwise.
    """
    until = until or timezone.localdate()
    since = since or (until - timedelta(days=30))

    rows = (
        LessonSession.objects
        .filter(opened_at__date__gte=since, opened_at__date__lte=until)
        .values("actual_teacher_id", "actual_teacher__user__full_name")
        .annotate(
            delivered=Count("id"),
            covering=Count("id", filter=~Q(
                actual_teacher_id=models_expected_teacher()
            )),
        )
        .order_by("-delivered")
    )

    return [
        {
            "membership_id": str(row["actual_teacher_id"]),
            "name": row["actual_teacher__user__full_name"],
            "lessons_delivered": row["delivered"],
            "lessons_covering": row["covering"],
        }
        for row in rows
    ]


def models_expected_teacher():
    """F-expression for the timetabled teacher of a session's instance."""
    from django.db.models import F

    return F("lesson_instance__expected_teacher_id")


def at_risk_students(*, term, since=None, until=None, limit: int = 100) -> list[dict]:
    """Students whose attendance or results suggest they are disengaging.

    Two independent signals, reported separately rather than fused into a
    single score. A composite number nobody can decompose is a number nobody
    can act on -- and a pastoral conversation needs to know *which* thing is
    wrong.
    """
    until = until or timezone.localdate()
    since = since or (until - timedelta(days=60))

    # Both lesson registers and day records count. A boarder absent from
    # morning roll for a fortnight is exactly as disengaged as one absent from
    # lessons, and excluding day records would make this blind to boarders.
    attendance = (
        StudentAttendance.objects
        .filter(date__gte=since, date__lte=until)
        .values("student_id")
        .annotate(
            marks=Count("id"),
            present=Count("id", filter=Q(status__in=[
                StudentAttendanceStatus.PRESENT, StudentAttendanceStatus.LATE,
            ])),
        )
    )
    attendance_by_student = {
        row["student_id"]: (row["marks"], row["present"]) for row in attendance
    }

    averages: dict = {}
    scores = (
        Score.objects
        .filter(assessment__term=term,
                assessment__state=AssessmentState.RELEASED,
                raw_score__isnull=False)
        .select_related("assessment")
    )
    for score in scores:
        percentage = score.percentage
        if percentage is None:
            continue
        total, count = averages.get(score.student_id, (0.0, 0))
        averages[score.student_id] = (total + percentage, count + 1)

    flagged = []
    student_ids = set(attendance_by_student) | set(averages)
    names = dict(
        Student.objects.filter(id__in=student_ids)
        .values_list("id", "full_name")
    )

    for student_id in student_ids:
        marks, present = attendance_by_student.get(student_id, (0, 0))
        rate = (present / marks) if marks else None
        total, count = averages.get(student_id, (0.0, 0))
        average = round(total / count, 2) if count else None

        reasons = []
        if rate is not None and marks >= AT_RISK_MIN_MARKS and rate < AT_RISK_ATTENDANCE:
            reasons.append({"code": "attendance",
                            "detail": f"Present for {rate:.0%} of {marks} marks."})
        if average is not None and average < AT_RISK_AVERAGE:
            reasons.append({"code": "attainment",
                            "detail": f"Average {average:.1f}% across "
                                      f"{count} released assessment(s)."})

        if reasons:
            flagged.append({
                "student_id": str(student_id),
                "name": names.get(student_id, ""),
                "attendance_rate": round(rate, 4) if rate is not None else None,
                "marks_counted": marks,
                "average_percentage": average,
                "assessments_counted": count,
                "reasons": reasons,
            })

    flagged.sort(key=lambda row: (row["attendance_rate"] is None,
                                  row["attendance_rate"] or 1.0))
    return flagged[:limit]


def coverage_overview(*, term, department_id=None) -> dict:
    """How the school -- or one department -- is doing against its schemes.

    `department_id` narrows this to one department's courses: a head of
    department's dashboard, not the DOS's school-wide one.
    """
    from educore.delivery.services import schemes_behind

    behind = schemes_behind(term=term, department_id=department_id)
    return {
        "term_id": str(term.pk),
        "department_id": str(department_id) if department_id else None,
        "groups_behind": len(behind),
        "worst": [
            {"scheme_id": str(r.scheme_id),
             "class_group_id": str(r.class_group_id) if r.class_group_id else None,
             "coverage": r.coverage, "expected": r.expected, "pace": r.pace}
            for r in behind[:10]
        ],
    }
