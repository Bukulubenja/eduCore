"""Platform-level records: plans, subscriptions, metering, provisioning.

These are the operator's view of the estate, not a school's view of itself.
Plan and Subscription are global rather than tenant-owned: a school must not be
able to read, still less edit, its own billing terms.
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _

from educore.core.tenancy import TenantOwnedModel, UUIDModel


class Plan(UUIDModel):
    """A subscription tier.

    Priced per active staff member per term. Students are deliberately not a
    billable unit: charging on enrolment punishes growth and invites
    under-reporting, which corrupts the very data the product depends on
    (doc 01, "Commercial shape").
    """

    code = models.SlugField(max_length=32, unique=True)
    name = models.CharField(max_length=64)
    price_per_staff_per_term = models.PositiveIntegerField(
        help_text="Minor units. Never a float."
    )
    currency = models.CharField(max_length=3, default="UGX")
    max_staff = models.PositiveIntegerField(null=True, blank=True)
    modules = models.JSONField(default=list)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "platform_plan"
        ordering = ["price_per_staff_per_term"]

    def __str__(self) -> str:
        return self.name

    def includes(self, module: str) -> bool:
        return module in self.modules


class Subscription(UUIDModel):
    class Status(models.TextChoices):
        TRIAL = "trial", _("Trial")
        ACTIVE = "active", _("Active")
        PAST_DUE = "past_due", _("Past due")
        SUSPENDED = "suspended", _("Suspended")
        CANCELLED = "cancelled", _("Cancelled")

    # Operator-owned: references a school but is not readable by it. There is
    # no RLS policy here because there is no tenant path to this table -- the
    # API never exposes it to a school-scoped session.
    operator_owned = True

    school = models.OneToOneField("core.School", on_delete=models.PROTECT,
                                  related_name="subscription")
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT,
                             related_name="subscriptions")
    status = models.CharField(max_length=16, choices=Status,
                              default=Status.TRIAL)
    started_on = models.DateField()
    trial_ends_on = models.DateField(null=True, blank=True)
    current_period_end = models.DateField(null=True, blank=True)
    cancelled_on = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        db_table = "platform_subscription"

    def __str__(self) -> str:
        return f"{self.school} on {self.plan}"

    @property
    def is_live(self) -> bool:
        return self.status in {self.Status.TRIAL, self.Status.ACTIVE,
                               self.Status.PAST_DUE}


class UsageRecord(UUIDModel):
    """A metering snapshot: what a school was using when we counted.

    Stored rather than recomputed. Billing that recalculates historic usage
    from live data cannot survive a dispute -- staff join and leave, and an
    invoice must reflect what was true when it was raised.
    """

    operator_owned = True       # See Subscription.

    school = models.ForeignKey("core.School", on_delete=models.PROTECT,
                               related_name="usage_records")
    measured_on = models.DateField(db_index=True)
    active_staff = models.PositiveIntegerField(default=0)
    active_students = models.PositiveIntegerField(default=0)
    lessons_recorded = models.PositiveIntegerField(default=0)
    notifications_sent = models.PositiveIntegerField(default=0)
    storage_bytes = models.BigIntegerField(default=0)

    class Meta:
        db_table = "platform_usagerecord"
        ordering = ["-measured_on"]
        constraints = [
            models.UniqueConstraint(fields=["school", "measured_on"],
                                    name="platform_usage_school_date_uniq"),
        ]


class ImportBatch(TenantOwnedModel):
    """One bulk upload, with its outcome.

    Kept after the fact so an onboarding that half-succeeded can be explained.
    "The import worked" is not a claim anyone should have to take on trust the
    week a school goes live.
    """

    class Kind(models.TextChoices):
        STAFF = "staff", _("Staff")
        STUDENTS = "students", _("Students")
        GUARDIANS = "guardians", _("Guardians")

    class Status(models.TextChoices):
        VALIDATED = "validated", _("Validated (dry run)")
        APPLIED = "applied", _("Applied")
        REJECTED = "rejected", _("Rejected")

    kind = models.CharField(max_length=16, choices=Kind)
    status = models.CharField(max_length=16, choices=Status)
    filename = models.CharField(max_length=255, blank=True)
    uploaded_by = models.ForeignKey("core.Membership", on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name="+")
    rows_total = models.PositiveIntegerField(default=0)
    rows_ok = models.PositiveIntegerField(default=0)
    rows_failed = models.PositiveIntegerField(default=0)
    errors = models.JSONField(default=list)

    class Meta(TenantOwnedModel.Meta):
        db_table = "platform_importbatch"
        ordering = ["-created_at"]
        constraints = TenantOwnedModel.tenant_constraints("platform_importbatch")
