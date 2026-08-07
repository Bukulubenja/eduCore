"""Assessment: the most contested data in any school.

Doc 03, "Assessment". The state machine, the audit of every score change, and
the requirement that release is the head teacher's explicit act are the point
of this module -- not overhead to be optimised away. When a parent disputes a
grade six months later, what settles it is being able to show who entered
what, when, who moderated it, and who authorised its release.
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _

from educore.core.tenancy import TenantOwnedModel


class GradingScale(TenantOwnedModel):
    """Per school, per level. Never hard-code A-F.

    Grading conventions differ by country, by level, and by school within a
    country. A scale baked into code is a scale that blocks the next customer.
    """

    name = models.CharField(max_length=64)
    level = models.ForeignKey("academics.Level", on_delete=models.CASCADE,
                              null=True, blank=True, related_name="grading_scales")
    is_default = models.BooleanField(default=False)

    class Meta(TenantOwnedModel.Meta):
        db_table = "assessment_gradingscale"
        constraints = [
            *TenantOwnedModel.tenant_constraints("assessment_gradingscale"),
            models.UniqueConstraint(fields=["school", "name"],
                                    name="assessment_scale_school_name_uniq"),
        ]

    def __str__(self) -> str:
        return self.name

    def band_for(self, percentage):
        return (self.bands
                .filter(min_percentage__lte=percentage,
                        max_percentage__gte=percentage)
                .first())


class GradeBand(TenantOwnedModel):
    scale = models.ForeignKey(GradingScale, on_delete=models.CASCADE,
                              related_name="bands")
    label = models.CharField(max_length=8)              # "A", "D1", "7"
    min_percentage = models.DecimalField(max_digits=5, decimal_places=2)
    max_percentage = models.DecimalField(max_digits=5, decimal_places=2)
    points = models.DecimalField(max_digits=5, decimal_places=2, null=True,
                                 blank=True)
    descriptor = models.CharField(max_length=64, blank=True)

    class Meta(TenantOwnedModel.Meta):
        db_table = "assessment_gradeband"
        ordering = ["-min_percentage"]
        constraints = [
            *TenantOwnedModel.tenant_constraints("assessment_gradeband"),
            models.UniqueConstraint(fields=["scale", "label"],
                                    name="assessment_band_scale_label_uniq"),
            models.CheckConstraint(
                condition=models.Q(max_percentage__gte=models.F("min_percentage")),
                name="assessment_band_range_ordered",
            ),
        ]

    def __str__(self) -> str:
        return self.label


class AssessmentState(models.TextChoices):
    DRAFT = "draft", _("Draft")
    SUBMITTED = "submitted", _("Submitted for approval")
    APPROVED = "approved", _("Approved")
    LOCKED = "locked", _("Locked")
    SCORING = "scoring", _("Scoring")
    MODERATION = "moderation", _("In moderation")
    HEAD_REVIEW = "head_review", _("Awaiting head teacher")
    RELEASED = "released", _("Released")


class Assessment(TenantOwnedModel):
    """One piece of assessed work for one course.

    The state machine is enforced in services, not by convention. Transitions
    that skip a step -- entering scores before the paper was approved, or
    releasing without moderation -- are refused rather than warned about.
    """

    class Kind(models.TextChoices):
        EXERCISE = "exercise", _("Exercise")
        TEST = "test", _("Class test")
        MIDTERM = "midterm", _("Mid-term")
        END_OF_TERM = "end_of_term", _("End of term")
        MOCK = "mock", _("Mock")
        NATIONAL = "national", _("National examination")

    course = models.ForeignKey("academics.Course", on_delete=models.PROTECT,
                               related_name="assessments")
    term = models.ForeignKey("academics.Term", on_delete=models.PROTECT,
                             related_name="assessments")
    class_groups = models.ManyToManyField("academics.ClassGroup",
                                          related_name="assessments")
    title = models.CharField(max_length=200)
    kind = models.CharField(max_length=16, choices=Kind)
    max_score = models.DecimalField(max_digits=6, decimal_places=2)
    weight = models.DecimalField(max_digits=5, decimal_places=2, default=1)
    grading_scale = models.ForeignKey(GradingScale, on_delete=models.PROTECT,
                                      null=True, blank=True, related_name="+")
    scheduled_for = models.DateField(null=True, blank=True)

    state = models.CharField(max_length=16, choices=AssessmentState,
                             default=AssessmentState.DRAFT, db_index=True)
    author = models.ForeignKey("core.Membership", on_delete=models.PROTECT,
                               related_name="authored_assessments")
    paper = models.FileField(upload_to="assessments/papers/", blank=True)

    approved_by = models.ForeignKey("core.Membership", on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)
    moderated_by = models.ForeignKey("core.Membership", on_delete=models.SET_NULL,
                                     null=True, blank=True, related_name="+")
    moderated_at = models.DateTimeField(null=True, blank=True)
    released_by = models.ForeignKey("core.Membership", on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name="+")
    released_at = models.DateTimeField(null=True, blank=True)
    returned_note = models.TextField(blank=True)

    class Meta(TenantOwnedModel.Meta):
        db_table = "assessment_assessment"
        ordering = ["-scheduled_for", "title"]
        constraints = [
            *TenantOwnedModel.tenant_constraints("assessment_assessment"),
            models.CheckConstraint(condition=models.Q(max_score__gt=0),
                                   name="assessment_max_score_positive"),
        ]
        indexes = [
            models.Index(fields=["school", "term", "state"],
                         name="assessment_term_state_idx"),
        ]

    def __str__(self) -> str:
        return self.title

    @property
    def accepts_scores(self) -> bool:
        return self.state in {AssessmentState.SCORING, AssessmentState.MODERATION}

    @property
    def is_released(self) -> bool:
        return self.state == AssessmentState.RELEASED


class Score(TenantOwnedModel):
    """One student's mark.

    `raw_score` is what the teacher entered; the percentage and grade are
    derived on read so that a change to the grading scale does not silently
    rewrite history that was already released.
    """

    assessment = models.ForeignKey(Assessment, on_delete=models.PROTECT,
                                   related_name="scores")
    student = models.ForeignKey("students.Student", on_delete=models.PROTECT,
                                related_name="scores")
    raw_score = models.DecimalField(max_digits=6, decimal_places=2, null=True,
                                    blank=True)
    is_absent = models.BooleanField(default=False)
    comment = models.CharField(max_length=200, blank=True)
    entered_by = models.ForeignKey("core.Membership", on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name="+")
    entered_at = models.DateTimeField(null=True, blank=True)
    # Set when a mark is altered after moderation. Doc 03: a change at that
    # point requires a reason, and the reason is part of the record.
    change_reason = models.CharField(max_length=200, blank=True)

    class Meta(TenantOwnedModel.Meta):
        db_table = "assessment_score"
        constraints = [
            *TenantOwnedModel.tenant_constraints("assessment_score"),
            models.UniqueConstraint(fields=["assessment", "student"],
                                    name="assessment_score_unique_per_student"),
            models.CheckConstraint(
                condition=models.Q(raw_score__gte=0) | models.Q(raw_score__isnull=True),
                name="assessment_score_not_negative",
            ),
        ]

    @property
    def percentage(self):
        if self.raw_score is None or self.is_absent:
            return None
        return round(float(self.raw_score) / float(self.assessment.max_score) * 100, 2)


class ReportCard(TenantOwnedModel):
    """A student's results for a term, frozen at the moment of issue.

    Immutable once issued. A correction produces a new revision rather than an
    edit, because a report card is a document a family may already be holding.
    """

    student = models.ForeignKey("students.Student", on_delete=models.PROTECT,
                                related_name="report_cards")
    term = models.ForeignKey("academics.Term", on_delete=models.PROTECT,
                             related_name="report_cards")
    class_group = models.ForeignKey("academics.ClassGroup",
                                    on_delete=models.PROTECT, related_name="+")
    revision = models.PositiveSmallIntegerField(default=1)
    # The rendered snapshot. Recomputing from live data would let a later
    # correction silently change what a parent was told in week 12.
    content = models.JSONField(default=dict)
    document = models.FileField(upload_to="report-cards/", blank=True)
    issued_by = models.ForeignKey("core.Membership", on_delete=models.SET_NULL,
                                  null=True, blank=True, related_name="+")
    issued_at = models.DateTimeField(null=True, blank=True)
    supersedes = models.ForeignKey("self", on_delete=models.SET_NULL, null=True,
                                   blank=True, related_name="+")

    class Meta(TenantOwnedModel.Meta):
        db_table = "assessment_reportcard"
        ordering = ["-issued_at"]
        constraints = [
            *TenantOwnedModel.tenant_constraints("assessment_reportcard"),
            models.UniqueConstraint(fields=["student", "term", "revision"],
                                    name="assessment_report_student_term_rev_uniq"),
        ]

    def __str__(self) -> str:
        return f"Report {self.student} {self.term} r{self.revision}"
