"""Academic structure: the calendar and grouping every other module hangs off.

Doc 03, "Academic structure". Phase 1 needs only the calendar and groupings;
courses, schemes of work and enrolment arrive with Phase 2.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

from educore.core.tenancy import TenantOwnedModel


class AcademicYear(TenantOwnedModel):
    name = models.CharField(max_length=32)              # "2026"
    starts_on = models.DateField()
    ends_on = models.DateField()
    is_current = models.BooleanField(default=False)

    class Meta(TenantOwnedModel.Meta):
        db_table = "academics_academicyear"
        ordering = ["-starts_on"]
        constraints = [
            *TenantOwnedModel.tenant_constraints("academics_academicyear"),
            models.UniqueConstraint(fields=["school", "name"],
                                    name="academics_year_school_name_uniq"),
            models.CheckConstraint(
                condition=models.Q(ends_on__gt=models.F("starts_on")),
                name="academics_year_dates_ordered",
            ),
            # At most one current year per school. A partial unique index says
            # this in the schema rather than in a service everyone must
            # remember to call.
            models.UniqueConstraint(
                fields=["school"], condition=models.Q(is_current=True),
                name="academics_year_one_current_per_school",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class Term(TenantOwnedModel):
    academic_year = models.ForeignKey(AcademicYear, on_delete=models.PROTECT,
                                      related_name="terms")
    name = models.CharField(max_length=32)              # "Term 1"
    sequence = models.PositiveSmallIntegerField()
    starts_on = models.DateField()
    ends_on = models.DateField()
    is_current = models.BooleanField(default=False)

    class Meta(TenantOwnedModel.Meta):
        db_table = "academics_term"
        ordering = ["academic_year", "sequence"]
        constraints = [
            *TenantOwnedModel.tenant_constraints("academics_term"),
            models.UniqueConstraint(fields=["academic_year", "sequence"],
                                    name="academics_term_year_sequence_uniq"),
            models.CheckConstraint(
                condition=models.Q(ends_on__gt=models.F("starts_on")),
                name="academics_term_dates_ordered",
            ),
            models.UniqueConstraint(
                fields=["school"], condition=models.Q(is_current=True),
                name="academics_term_one_current_per_school",
            ),
        ]

    def clean(self):
        """Terms within a year must not overlap (doc 03, invariant 2).

        PostgreSQL could enforce this with an exclusion constraint over a
        daterange; that arrives with the Phase 2 timetable work, where the
        range type is needed anyway. Until then this is a service-layer check
        and is called from the serializer.
        """
        if not self.academic_year_id:
            return
        clash = (
            Term.objects.filter(academic_year_id=self.academic_year_id)
            .exclude(pk=self.pk)
            .filter(starts_on__lte=self.ends_on, ends_on__gte=self.starts_on)
            .first()
        )
        if clash is not None:
            raise ValidationError(
                f"Overlaps {clash.name} ({clash.starts_on} to {clash.ends_on})."
            )

    def __str__(self) -> str:
        return f"{self.academic_year} {self.name}"


class Level(TenantOwnedModel):
    """A year of study: S1, Grade 7."""

    name = models.CharField(max_length=64)
    code = models.CharField(max_length=16)
    sequence = models.PositiveSmallIntegerField()

    class Meta(TenantOwnedModel.Meta):
        db_table = "academics_level"
        ordering = ["sequence"]
        constraints = [
            *TenantOwnedModel.tenant_constraints("academics_level"),
            models.UniqueConstraint(fields=["school", "code"],
                                    name="academics_level_school_code_uniq"),
        ]

    def __str__(self) -> str:
        return self.name


class ClassGroup(TenantOwnedModel):
    """A stream within a level: the persistent group a student belongs to."""

    level = models.ForeignKey(Level, on_delete=models.PROTECT,
                             related_name="class_groups")
    name = models.CharField(max_length=64)              # "S4 Blue"
    class_teacher = models.ForeignKey("core.Membership", on_delete=models.SET_NULL,
                                      null=True, blank=True, related_name="+")

    class Meta(TenantOwnedModel.Meta):
        db_table = "academics_classgroup"
        ordering = ["level__sequence", "name"]
        constraints = [
            *TenantOwnedModel.tenant_constraints("academics_classgroup"),
            models.UniqueConstraint(fields=["school", "level", "name"],
                                    name="academics_classgroup_unique_name"),
        ]

    def __str__(self) -> str:
        return self.name


class Department(TenantOwnedModel):
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=16)
    head = models.ForeignKey("core.Membership", on_delete=models.SET_NULL,
                             null=True, blank=True, related_name="+")

    class Meta(TenantOwnedModel.Meta):
        db_table = "academics_department"
        ordering = ["name"]
        constraints = [
            *TenantOwnedModel.tenant_constraints("academics_department"),
            models.UniqueConstraint(fields=["school", "code"],
                                    name="academics_department_school_code_uniq"),
        ]

    def __str__(self) -> str:
        return self.name


class Subject(TenantOwnedModel):
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=16)
    department = models.ForeignKey(Department, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name="subjects")

    class Meta(TenantOwnedModel.Meta):
        db_table = "academics_subject"
        ordering = ["name"]
        constraints = [
            *TenantOwnedModel.tenant_constraints("academics_subject"),
            models.UniqueConstraint(fields=["school", "code"],
                                    name="academics_subject_school_code_uniq"),
        ]

    def __str__(self) -> str:
        return self.name


class Course(TenantOwnedModel):
    """A subject taught to a level in an academic year.

    This, not Subject, is what teachers are assigned to and what schemes of
    work attach to. Hanging timetable entries off Subject alone loses the
    level distinction, and coverage then cannot be computed at all: "Physics
    is 40% covered" is meaningless across S1 and S4.
    """

    subject = models.ForeignKey(Subject, on_delete=models.PROTECT,
                                related_name="courses")
    level = models.ForeignKey(Level, on_delete=models.PROTECT,
                              related_name="courses")
    academic_year = models.ForeignKey(AcademicYear, on_delete=models.PROTECT,
                                      related_name="courses")
    periods_per_week = models.PositiveSmallIntegerField(default=1)

    class Meta(TenantOwnedModel.Meta):
        db_table = "academics_course"
        ordering = ["level__sequence", "subject__name"]
        constraints = [
            *TenantOwnedModel.tenant_constraints("academics_course"),
            models.UniqueConstraint(
                fields=["subject", "level", "academic_year"],
                name="academics_course_unique_offering",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.subject} ({self.level})"


class StaffProfile(TenantOwnedModel):
    """Employment facts about a staff Membership.

    Separate from Membership because Membership is about *access* and this is
    about *employment*: a bursar holds a membership with no teaching duty, and
    a teacher's department has nothing to do with their permissions.
    """

    class Kind(models.TextChoices):
        TEACHING = "teaching", _("Teaching")
        ADMIN = "admin", _("Administrative")
        SUPPORT = "support", _("Support")

    membership = models.OneToOneField("core.Membership", on_delete=models.CASCADE,
                                      related_name="staff_profile")
    department = models.ForeignKey(Department, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name="staff")
    kind = models.CharField(max_length=16, choices=Kind, default=Kind.TEACHING)
    job_title = models.CharField(max_length=100, blank=True)

    class Meta(TenantOwnedModel.Meta):
        db_table = "academics_staffprofile"
        constraints = TenantOwnedModel.tenant_constraints("academics_staffprofile")
