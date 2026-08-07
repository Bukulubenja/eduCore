"""Lesson delivery: what actually happened, and how much of the plan it covered.

Presence at school is the weak claim. Presence *in the lesson*, with a record
of what was taught, is the valuable one -- and it is the link that turns a
staff attendance system into an accountability platform (doc 04).
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _

from educore.core.tenancy import TenantOwnedModel


class SchemeOfWork(TenantOwnedModel):
    """The plan for one course in one term."""

    course = models.ForeignKey("academics.Course", on_delete=models.PROTECT,
                               related_name="schemes")
    term = models.ForeignKey("academics.Term", on_delete=models.PROTECT,
                             related_name="schemes")
    author = models.ForeignKey("core.Membership", on_delete=models.SET_NULL,
                               null=True, blank=True, related_name="+")
    approved_by = models.ForeignKey("core.Membership", on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta(TenantOwnedModel.Meta):
        db_table = "delivery_schemeofwork"
        constraints = [
            *TenantOwnedModel.tenant_constraints("delivery_schemeofwork"),
            models.UniqueConstraint(fields=["course", "term"],
                                    name="delivery_scheme_course_term_uniq"),
        ]

    def __str__(self) -> str:
        return f"Scheme: {self.course} / {self.term}"


class SyllabusUnit(TenantOwnedModel):
    """One topic in a scheme, weighted by the periods it is planned to take.

    `planned_periods` is what makes coverage meaningful. Counting units treats
    a three-period topic as equal to a one-period topic, and the resulting
    percentage tells a director of studies nothing they can act on.
    """

    scheme = models.ForeignKey(SchemeOfWork, on_delete=models.CASCADE,
                               related_name="units")
    parent = models.ForeignKey("self", on_delete=models.CASCADE, null=True,
                               blank=True, related_name="children")
    sequence = models.PositiveSmallIntegerField()
    title = models.CharField(max_length=200)
    planned_periods = models.PositiveSmallIntegerField(default=1)

    class Meta(TenantOwnedModel.Meta):
        db_table = "delivery_syllabusunit"
        ordering = ["sequence"]
        constraints = [
            *TenantOwnedModel.tenant_constraints("delivery_syllabusunit"),
            models.UniqueConstraint(fields=["scheme", "sequence"],
                                    name="delivery_unit_scheme_sequence_uniq"),
        ]

    def __str__(self) -> str:
        return self.title


class LessonSession(TenantOwnedModel):
    """The actual delivery of a LessonInstance."""

    lesson_instance = models.OneToOneField("timetable.LessonInstance",
                                           on_delete=models.PROTECT,
                                           related_name="session")
    actual_teacher = models.ForeignKey("core.Membership",
                                       on_delete=models.PROTECT,
                                       related_name="lesson_sessions")
    room = models.ForeignKey("timetable.Room", on_delete=models.PROTECT,
                             related_name="+")
    opened_at = models.DateTimeField()
    closed_at = models.DateTimeField(null=True, blank=True)
    verification_confidence = models.PositiveSmallIntegerField(default=0)
    room_changed = models.BooleanField(default=False)
    late_start_minutes = models.IntegerField(default=0)
    notes = models.TextField(blank=True)

    class Meta(TenantOwnedModel.Meta):
        db_table = "delivery_lessonsession"
        ordering = ["-opened_at"]
        constraints = TenantOwnedModel.tenant_constraints("delivery_lessonsession")

    @property
    def duration_minutes(self) -> int:
        if not self.closed_at:
            return 0
        return int((self.closed_at - self.opened_at).total_seconds() // 60)


class LessonQrRedemption(TenantOwnedModel):
    """A classroom code consumed by one teacher.

    Separate from the staff check-in redemptions in `presence`: sibling
    modules do not share tables, and the two codes have different lifetimes
    and different meanings.
    """

    nonce = models.CharField(max_length=64)
    membership = models.ForeignKey("core.Membership", on_delete=models.CASCADE,
                                   related_name="+")
    redeemed_at = models.DateTimeField(auto_now_add=True)

    class Meta(TenantOwnedModel.Meta):
        db_table = "delivery_lessonqrredemption"
        constraints = [
            *TenantOwnedModel.tenant_constraints("delivery_lessonqrredemption"),
            models.UniqueConstraint(fields=["school", "nonce", "membership"],
                                    name="delivery_qr_nonce_member_uniq"),
        ]


class Substitution(TenantOwnedModel):
    """Someone other than the timetabled teacher took the lesson.

    Recorded rather than prevented. A substitution is normal school life; what
    matters is that the lesson is not silently attributed to the absent
    teacher, and that the DOS can see how often it happens.
    """

    class Origin(models.TextChoices):
        PLANNED = "planned", _("Arranged in advance")
        INFERRED = "inferred", _("Inferred when the session was opened")

    lesson_instance = models.OneToOneField("timetable.LessonInstance",
                                           on_delete=models.PROTECT,
                                           related_name="substitution")
    original_teacher = models.ForeignKey("core.Membership",
                                         on_delete=models.PROTECT,
                                         related_name="+")
    substitute_teacher = models.ForeignKey("core.Membership",
                                           on_delete=models.PROTECT,
                                           related_name="substitutions_taken")
    origin = models.CharField(max_length=16, choices=Origin,
                              default=Origin.INFERRED)
    reason = models.CharField(max_length=200, blank=True)
    approved_by = models.ForeignKey("core.Membership", on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name="+")

    class Meta(TenantOwnedModel.Meta):
        db_table = "delivery_substitution"
        constraints = TenantOwnedModel.tenant_constraints("delivery_substitution")


class CoverageEntry(TenantOwnedModel):
    """What a session actually covered."""

    class Completion(models.TextChoices):
        INTRODUCED = "introduced", _("Introduced")
        PARTIAL = "partial", _("Partly covered")
        COMPLETED = "completed", _("Completed")

    session = models.ForeignKey(LessonSession, on_delete=models.CASCADE,
                                related_name="coverage")
    unit = models.ForeignKey(SyllabusUnit, on_delete=models.PROTECT,
                             related_name="coverage_entries")
    completion = models.CharField(max_length=16, choices=Completion)
    homework_set = models.TextField(blank=True)
    notes = models.TextField(blank=True)

    class Meta(TenantOwnedModel.Meta):
        db_table = "delivery_coverageentry"
        constraints = [
            *TenantOwnedModel.tenant_constraints("delivery_coverageentry"),
            models.UniqueConstraint(fields=["session", "unit"],
                                    name="delivery_coverage_session_unit_uniq"),
        ]
