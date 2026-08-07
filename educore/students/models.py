"""Students, guardians, enrolment, and attendance.

Enrolment lives here rather than in `academics` because it references Student,
and `academics` sits below `students` in the module layering (ADR-0005).
Putting it above would invert the dependency for no gain: `academics` owns the
curriculum shape, `students` owns who is in it.
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _

from educore.core.tenancy import TenantOwnedModel


class Student(TenantOwnedModel):
    """A student belongs to the school, not to a class.

    Class membership is Enrolment, which is term-scoped, so a student who
    repeats a year or moves stream keeps correct history instead of having
    their record mutated out from under the past.
    """

    class Residency(models.TextChoices):
        DAY = "day", _("Day scholar")
        BOARDING = "boarding", _("Boarder")

    class Status(models.TextChoices):
        ENROLLED = "enrolled", _("Enrolled")
        SUSPENDED = "suspended", _("Suspended")
        TRANSFERRED = "transferred", _("Transferred out")
        GRADUATED = "graduated", _("Graduated")
        WITHDRAWN = "withdrawn", _("Withdrawn")

    admission_number = models.CharField(max_length=32)
    full_name = models.CharField(max_length=200)
    date_of_birth = models.DateField(null=True, blank=True)
    residency = models.CharField(max_length=16, choices=Residency,
                                 default=Residency.DAY)
    status = models.CharField(max_length=16, choices=Status,
                              default=Status.ENROLLED)
    admitted_on = models.DateField(null=True, blank=True)
    # Printed on the ID card and scanned at the gate. Not a secret, and never
    # used for authentication.
    scan_code = models.CharField(max_length=64, blank=True)

    class Meta(TenantOwnedModel.Meta):
        db_table = "students_student"
        ordering = ["full_name"]
        constraints = [
            *TenantOwnedModel.tenant_constraints("students_student"),
            models.UniqueConstraint(fields=["school", "admission_number"],
                                    name="students_student_admission_uniq"),
            models.UniqueConstraint(
                fields=["school", "scan_code"],
                condition=~models.Q(scan_code=""),
                name="students_student_scan_code_uniq",
            ),
        ]

    def __str__(self) -> str:
        return self.full_name


class Enrolment(TenantOwnedModel):
    student = models.ForeignKey(Student, on_delete=models.PROTECT,
                                related_name="enrolments")
    class_group = models.ForeignKey("academics.ClassGroup",
                                    on_delete=models.PROTECT,
                                    related_name="enrolments")
    term = models.ForeignKey("academics.Term", on_delete=models.PROTECT,
                             related_name="enrolments")
    is_active = models.BooleanField(default=True)

    class Meta(TenantOwnedModel.Meta):
        db_table = "students_enrolment"
        constraints = [
            *TenantOwnedModel.tenant_constraints("students_enrolment"),
            # Doc 03, invariant 5: at most one active enrolment per term.
            models.UniqueConstraint(
                fields=["student", "term"], condition=models.Q(is_active=True),
                name="students_enrolment_one_active_per_term",
            ),
        ]


class GuardianLink(TenantOwnedModel):
    """Student to parent Membership.

    Contact and access are separate fields on purpose. A guardian may be
    legally entitled to reports while another handles day-to-day messages, and
    conflating the two is how a school sends a child's results to the wrong
    parent. `verified` exists because showing a child's location and grades to
    someone not entitled to them is a safeguarding incident, not a bug -- so
    the link is never inferred from a shared surname or phone number.
    """

    class Relationship(models.TextChoices):
        MOTHER = "mother", _("Mother")
        FATHER = "father", _("Father")
        GUARDIAN = "guardian", _("Guardian")
        OTHER = "other", _("Other")

    student = models.ForeignKey(Student, on_delete=models.CASCADE,
                                related_name="guardians")
    membership = models.ForeignKey("core.Membership", on_delete=models.CASCADE,
                                   related_name="wards")
    relationship = models.CharField(max_length=16, choices=Relationship)
    is_primary_contact = models.BooleanField(default=False)
    receives_notifications = models.BooleanField(default=True)
    has_portal_access = models.BooleanField(default=True)
    verified = models.BooleanField(default=False)
    verified_by = models.ForeignKey("core.Membership", on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name="+")

    class Meta(TenantOwnedModel.Meta):
        db_table = "students_guardianlink"
        constraints = [
            *TenantOwnedModel.tenant_constraints("students_guardianlink"),
            models.UniqueConstraint(fields=["student", "membership"],
                                    name="students_guardian_unique_link"),
            models.UniqueConstraint(
                fields=["student"], condition=models.Q(is_primary_contact=True),
                name="students_guardian_one_primary",
            ),
        ]


class StudentAttendanceStatus(models.TextChoices):
    PRESENT = "present", _("Present")
    ABSENT = "absent", _("Absent")
    LATE = "late", _("Late")
    EXCUSED = "excused", _("Excused")
    SICK = "sick", _("Sick")


class StudentAttendance(TenantOwnedModel):
    """A register mark, per lesson or per day."""

    class Method(models.TextChoices):
        TEACHER_MARKED = "teacher_marked", _("Marked by teacher")
        STUDENT_SCAN = "student_scan", _("Scanned by student")
        GATE = "gate", _("Gate scan")
        INFERRED = "inferred", _("Inferred")

    student = models.ForeignKey(Student, on_delete=models.PROTECT,
                                related_name="attendance")
    lesson_instance = models.ForeignKey("timetable.LessonInstance",
                                        on_delete=models.PROTECT, null=True,
                                        blank=True, related_name="register")
    date = models.DateField(db_index=True)
    status = models.CharField(max_length=16, choices=StudentAttendanceStatus)
    method = models.CharField(max_length=20, choices=Method,
                              default=Method.TEACHER_MARKED)
    marked_by = models.ForeignKey("core.Membership", on_delete=models.SET_NULL,
                                  null=True, blank=True, related_name="+")
    note = models.CharField(max_length=200, blank=True)

    class Meta(TenantOwnedModel.Meta):
        db_table = "students_studentattendance"
        constraints = [
            *TenantOwnedModel.tenant_constraints("students_studentattendance"),
            models.UniqueConstraint(
                fields=["student", "lesson_instance"],
                condition=models.Q(lesson_instance__isnull=False),
                name="students_attendance_lesson_uniq",
            ),
            models.UniqueConstraint(
                fields=["student", "date"],
                condition=models.Q(lesson_instance__isnull=True),
                name="students_attendance_day_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["school", "date", "status"],
                         name="students_attendance_day_idx"),
        ]


class GateEvent(TenantOwnedModel):
    """A day scholar arriving at or leaving a campus entrance."""

    class Direction(models.TextChoices):
        IN = "in", _("Arrival")
        OUT = "out", _("Departure")

    class Method(models.TextChoices):
        QR = "qr", _("QR code")
        NFC = "nfc", _("NFC card")
        BARCODE = "barcode", _("Barcode")
        MANUAL = "manual", _("Recorded manually")

    student = models.ForeignKey(Student, on_delete=models.PROTECT,
                                related_name="gate_events")
    campus = models.ForeignKey("core.Campus", on_delete=models.PROTECT,
                               related_name="+")
    direction = models.CharField(max_length=8, choices=Direction)
    method = models.CharField(max_length=16, choices=Method, default=Method.QR)
    occurred_at = models.DateTimeField(db_index=True)
    client_event_id = models.UUIDField()

    class Meta(TenantOwnedModel.Meta):
        db_table = "students_gateevent"
        ordering = ["occurred_at"]
        constraints = [
            *TenantOwnedModel.tenant_constraints("students_gateevent"),
            models.UniqueConstraint(fields=["school", "client_event_id"],
                                    name="students_gate_client_id_uniq"),
        ]
