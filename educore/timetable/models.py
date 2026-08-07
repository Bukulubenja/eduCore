"""Timetable: the template/instance split that the rest of the system rests on.

Doc 03, "Timetable". The distinction to hold onto:

    ScheduledLesson   a recurring template. Has a weekday, no date.
    LessonInstance    that template on one date. What everything attaches to.

Attendance, coverage, substitution, cancellation and -- above all -- "lesson
missed" are all statements about an *instance*. Without instances there is no
row to point at when a lesson does not happen, and a lesson that does not
happen is precisely what the product exists to surface.
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _

from educore.core.tenancy import TenantOwnedModel


class Room(TenantOwnedModel):
    campus = models.ForeignKey("core.Campus", on_delete=models.PROTECT,
                               related_name="rooms")
    name = models.CharField(max_length=64)
    code = models.CharField(max_length=16)
    capacity = models.PositiveSmallIntegerField(null=True, blank=True)

    class Meta(TenantOwnedModel.Meta):
        db_table = "timetable_room"
        ordering = ["name"]
        constraints = [
            *TenantOwnedModel.tenant_constraints("timetable_room"),
            models.UniqueConstraint(fields=["school", "code"],
                                    name="timetable_room_school_code_uniq"),
        ]

    def __str__(self) -> str:
        return self.name


class PeriodGrid(TenantOwnedModel):
    """A bell schedule. A school may run different grids on different days."""

    name = models.CharField(max_length=64)
    is_default = models.BooleanField(default=False)

    class Meta(TenantOwnedModel.Meta):
        db_table = "timetable_periodgrid"
        constraints = [
            *TenantOwnedModel.tenant_constraints("timetable_periodgrid"),
            models.UniqueConstraint(fields=["school", "name"],
                                    name="timetable_grid_school_name_uniq"),
        ]

    def __str__(self) -> str:
        return self.name


class PeriodSlot(TenantOwnedModel):
    grid = models.ForeignKey(PeriodGrid, on_delete=models.CASCADE,
                             related_name="slots")
    name = models.CharField(max_length=32)          # "Period 3"
    sequence = models.PositiveSmallIntegerField()
    starts_at = models.TimeField()
    ends_at = models.TimeField()
    is_teaching = models.BooleanField(default=True)  # False for break, assembly

    class Meta(TenantOwnedModel.Meta):
        db_table = "timetable_periodslot"
        ordering = ["sequence"]
        constraints = [
            *TenantOwnedModel.tenant_constraints("timetable_periodslot"),
            models.UniqueConstraint(fields=["grid", "sequence"],
                                    name="timetable_slot_grid_sequence_uniq"),
            models.CheckConstraint(
                condition=models.Q(ends_at__gt=models.F("starts_at")),
                name="timetable_slot_times_ordered",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class TimetableVersion(TenantOwnedModel):
    """Immutable once published.

    Editing a live timetable in place destroys the meaning of every historical
    "lesson missed": you can no longer reconstruct what was expected on a past
    date, so the record becomes an accusation nobody can check. Changes create
    a new version instead.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        PUBLISHED = "published", _("Published")
        ARCHIVED = "archived", _("Archived")

    name = models.CharField(max_length=64)
    academic_year = models.ForeignKey("academics.AcademicYear",
                                      on_delete=models.PROTECT,
                                      related_name="timetable_versions")
    grid = models.ForeignKey(PeriodGrid, on_delete=models.PROTECT,
                             related_name="versions")
    status = models.CharField(max_length=16, choices=Status, default=Status.DRAFT)
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    published_by = models.ForeignKey("core.Membership", on_delete=models.SET_NULL,
                                     null=True, blank=True, related_name="+")

    class Meta(TenantOwnedModel.Meta):
        db_table = "timetable_timetableversion"
        ordering = ["-effective_from"]
        constraints = [
            *TenantOwnedModel.tenant_constraints("timetable_timetableversion"),
            models.UniqueConstraint(fields=["school", "name"],
                                    name="timetable_version_school_name_uniq"),
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def is_editable(self) -> bool:
        return self.status == self.Status.DRAFT


class ScheduledLesson(TenantOwnedModel):
    """A recurring timetable entry. Template: weekday and slot, never a date."""

    version = models.ForeignKey(TimetableVersion, on_delete=models.CASCADE,
                                related_name="scheduled_lessons")
    weekday = models.PositiveSmallIntegerField(
        help_text="0 = Monday, matching datetime.weekday()."
    )
    slot = models.ForeignKey(PeriodSlot, on_delete=models.PROTECT,
                             related_name="scheduled_lessons")
    course = models.ForeignKey("academics.Course", on_delete=models.PROTECT,
                               related_name="scheduled_lessons")
    class_group = models.ForeignKey("academics.ClassGroup",
                                    on_delete=models.PROTECT,
                                    related_name="scheduled_lessons")
    teacher = models.ForeignKey("core.Membership", on_delete=models.PROTECT,
                                related_name="scheduled_lessons")
    room = models.ForeignKey(Room, on_delete=models.PROTECT,
                             related_name="scheduled_lessons")

    class Meta(TenantOwnedModel.Meta):
        db_table = "timetable_scheduledlesson"
        constraints = [
            *TenantOwnedModel.tenant_constraints("timetable_scheduledlesson"),
            # A class group can only be in one place at a time. Teacher and
            # room clashes are checked at publish, because a draft is allowed
            # to be temporarily inconsistent while it is being built.
            models.UniqueConstraint(
                fields=["version", "weekday", "slot", "class_group"],
                name="timetable_scheduled_group_slot_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["version", "weekday", "slot"],
                         name="timetable_scheduled_slot_idx"),
        ]


class CalendarException(TenantOwnedModel):
    """Holidays, exam days, school events.

    Suppresses materialisation with a recorded reason, so a lesson absent on a
    public holiday is explained rather than counted as a failure.
    """

    class Kind(models.TextChoices):
        HOLIDAY = "holiday", _("Holiday")
        EXAM = "exam", _("Examinations")
        EVENT = "event", _("School event")
        CLOSURE = "closure", _("Unplanned closure")

    date = models.DateField(db_index=True)
    kind = models.CharField(max_length=16, choices=Kind)
    description = models.CharField(max_length=200, blank=True)
    suppresses_lessons = models.BooleanField(default=True)

    class Meta(TenantOwnedModel.Meta):
        db_table = "timetable_calendarexception"
        ordering = ["date"]
        constraints = [
            *TenantOwnedModel.tenant_constraints("timetable_calendarexception"),
            models.UniqueConstraint(fields=["school", "date"],
                                    name="timetable_calendar_school_date_uniq"),
        ]


class LessonInstance(TenantOwnedModel):
    """One scheduled lesson on one date. The unit everything attaches to."""

    class Status(models.TextChoices):
        SCHEDULED = "scheduled", _("Scheduled")
        IN_PROGRESS = "in_progress", _("In progress")
        DELIVERED = "delivered", _("Delivered")
        VERIFIED = "verified", _("Verified")
        MISSED = "missed", _("Missed")
        ABANDONED = "abandoned", _("Abandoned")
        EXCUSED = "excused", _("Excused")
        CANCELLED = "cancelled", _("Cancelled")
        DISPUTED = "disputed", _("Disputed")

    # Terminal states a scheduled job must never re-evaluate.
    SETTLED = {Status.VERIFIED, Status.EXCUSED, Status.CANCELLED}

    scheduled_lesson = models.ForeignKey(ScheduledLesson, on_delete=models.PROTECT,
                                         related_name="instances")
    date = models.DateField(db_index=True)
    scheduled_start_at = models.DateTimeField()
    scheduled_end_at = models.DateTimeField()

    # Denormalised from the template so a later timetable revision cannot
    # rewrite what was expected on a date already past.
    expected_teacher = models.ForeignKey("core.Membership",
                                         on_delete=models.PROTECT,
                                         related_name="lesson_instances")
    expected_room = models.ForeignKey(Room, on_delete=models.PROTECT,
                                      related_name="lesson_instances")
    course = models.ForeignKey("academics.Course", on_delete=models.PROTECT,
                               related_name="lesson_instances")
    class_group = models.ForeignKey("academics.ClassGroup",
                                    on_delete=models.PROTECT,
                                    related_name="lesson_instances")

    status = models.CharField(max_length=16, choices=Status,
                              default=Status.SCHEDULED, db_index=True)
    missed_reason = models.CharField(max_length=200, blank=True)

    class Meta(TenantOwnedModel.Meta):
        db_table = "timetable_lessoninstance"
        ordering = ["scheduled_start_at"]
        constraints = [
            *TenantOwnedModel.tenant_constraints("timetable_lessoninstance"),
            models.UniqueConstraint(fields=["scheduled_lesson", "date"],
                                    name="timetable_instance_lesson_date_uniq"),
        ]
        indexes = [
            models.Index(fields=["school", "date", "status"],
                         name="timetable_instance_day_idx"),
            models.Index(fields=["school", "expected_teacher", "date"],
                         name="timetable_instance_teacher_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.course} / {self.class_group} on {self.date}"
