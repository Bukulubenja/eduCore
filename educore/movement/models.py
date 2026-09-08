"""Student Movement Management: authorised movement of students off campus.

Doc 08 and ADR-0007. Two workflows share this module because they answer the
same question -- "who is off campus, why, who approved it, and when are they
due back?" -- and differ only in shape:

    PassOut          one boarder leaving for a named reason, then returning
    Trip             a roster of students taken out together and brought back

Both are approval *workflows*, not signal tables: the mutable ``status`` field
follows the ``presence.AttendanceException`` / ``delivery.Substitution``
precedent. The append-only rule (doc 01, principle 5) governs event tables
like ``students.GateEvent`` and ``core.AuditEvent`` -- every state change here
is written to the audit chain and emitted on the outbox, and "overdue" is
computed from ``expected_return_at``, never stored.
"""

from __future__ import annotations

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from educore.core.tenancy import TenantOwnedModel

# Pass-out / trip states in which a student is considered still "out" or the
# request still live. Used for the one-open-per-student guard and for the
# off-campus roll-up in `insights`.
OPEN_PASS_OUT_STATES = ("requested", "approved", "departed")


class PassOut(TenantOwnedModel):
    """One boarder authorised to leave campus and expected back.

    The record stays open from the moment it is requested until a return is
    recorded. An open pass-out past its ``expected_return_at`` is the
    exception a boarding school most needs surfaced, so `is_overdue` is a
    computed read, refreshed every time anyone looks.
    """

    class Reason(models.TextChoices):
        MEDICAL = "medical", _("Medical")
        FAMILY = "family", _("Family")
        WEEKEND_HOME = "weekend_home", _("Weekend at home")
        OFFICIAL = "official", _("Official school business")
        OTHER = "other", _("Other")

    class Status(models.TextChoices):
        REQUESTED = "requested", _("Requested")
        APPROVED = "approved", _("Approved")
        DENIED = "denied", _("Denied")
        DEPARTED = "departed", _("Departed")
        RETURNED = "returned", _("Returned")
        CANCELLED = "cancelled", _("Cancelled")

    student = models.ForeignKey("students.Student", on_delete=models.PROTECT,
                                related_name="pass_outs")
    reason = models.CharField(max_length=16, choices=Reason)
    destination = models.CharField(max_length=200)
    # Who collects or accompanies the student -- a parent, a guardian, a
    # member of staff. Free text on purpose: this is written at the office
    # counter, and forcing it to reference a record it may not have is how
    # the field ends up blank.
    responsible_person = models.CharField(max_length=200)
    narrative = models.TextField(blank=True)

    requested_by = models.ForeignKey("core.Membership", on_delete=models.PROTECT,
                                     related_name="+")
    expected_return_at = models.DateTimeField()

    status = models.CharField(max_length=16, choices=Status,
                              default=Status.REQUESTED)
    approved_by = models.ForeignKey("core.Membership", on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True)

    departed_at = models.DateTimeField(null=True, blank=True)
    returned_at = models.DateTimeField(null=True, blank=True)
    # Set when a gate scan, rather than a manual entry, drove the transition.
    departure_gate_event = models.ForeignKey(
        "students.GateEvent", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    return_gate_event = models.ForeignKey(
        "students.GateEvent", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )

    class Meta(TenantOwnedModel.Meta):
        db_table = "movement_passout"
        ordering = ["-created_at"]
        constraints = [
            *TenantOwnedModel.tenant_constraints("movement_passout"),
            models.UniqueConstraint(
                fields=["student"],
                condition=models.Q(status__in=OPEN_PASS_OUT_STATES),
                name="movement_passout_one_open_per_student",
            ),
        ]
        indexes = [
            models.Index(fields=["school", "status", "expected_return_at"],
                         name="movement_passout_overdue_idx"),
        ]

    def __str__(self) -> str:
        return f"Pass-out: {self.student_id} ({self.status})"

    def is_overdue(self, *, now=None) -> bool:
        if self.status != self.Status.DEPARTED:
            return False
        now = now or timezone.now()
        return now > self.expected_return_at


class Trip(TenantOwnedModel):
    """A supervised outing: a roster of students taken out and brought back.

    Digitises the paper list-and-approval process (SSOMS §9). The roster is
    built from the student database rather than typed, so the trip connects
    directly to attendance, guardian notification and reporting; it is frozen
    once the trip is approved.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        SUBMITTED = "submitted", _("Submitted for approval")
        APPROVED = "approved", _("Approved")
        REJECTED = "rejected", _("Rejected")
        DEPARTED = "departed", _("Departed")
        RETURNED = "returned", _("Returned")
        CANCELLED = "cancelled", _("Cancelled")

    OPEN_STATES = ("draft", "submitted", "approved", "departed")
    ROSTER_EDITABLE_STATES = ("draft", "submitted")

    title = models.CharField(max_length=200)
    purpose = models.TextField(blank=True)
    destination = models.CharField(max_length=200)
    departs_at = models.DateTimeField()
    returns_at = models.DateTimeField()

    created_by = models.ForeignKey("core.Membership", on_delete=models.PROTECT,
                                   related_name="+")

    status = models.CharField(max_length=16, choices=Status,
                              default=Status.DRAFT)
    approved_by = models.ForeignKey("core.Membership", on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True)

    actual_departed_at = models.DateTimeField(null=True, blank=True)
    actual_returned_at = models.DateTimeField(null=True, blank=True)

    class Meta(TenantOwnedModel.Meta):
        db_table = "movement_trip"
        ordering = ["-departs_at"]
        constraints = [
            *TenantOwnedModel.tenant_constraints("movement_trip"),
        ]
        indexes = [
            models.Index(fields=["school", "status", "returns_at"],
                         name="movement_trip_overdue_idx"),
        ]

    def __str__(self) -> str:
        return self.title

    def is_overdue(self, *, now=None) -> bool:
        if self.status != self.Status.DEPARTED:
            return False
        now = now or timezone.now()
        return now > self.returns_at


class TripParticipant(TenantOwnedModel):
    """One student on a trip roster."""

    trip = models.ForeignKey(Trip, on_delete=models.CASCADE,
                             related_name="participants")
    student = models.ForeignKey("students.Student", on_delete=models.PROTECT,
                                related_name="trip_participations")
    departure_recorded_at = models.DateTimeField(null=True, blank=True)
    return_recorded_at = models.DateTimeField(null=True, blank=True)

    class Meta(TenantOwnedModel.Meta):
        db_table = "movement_tripparticipant"
        constraints = [
            *TenantOwnedModel.tenant_constraints("movement_tripparticipant"),
            models.UniqueConstraint(fields=["trip", "student"],
                                    name="movement_trip_participant_uniq"),
        ]


class TripSupervisor(TenantOwnedModel):
    """A member of staff responsible for a trip."""

    trip = models.ForeignKey(Trip, on_delete=models.CASCADE,
                             related_name="supervisors")
    membership = models.ForeignKey("core.Membership", on_delete=models.PROTECT,
                                   related_name="+")
    is_lead = models.BooleanField(default=False)

    class Meta(TenantOwnedModel.Meta):
        db_table = "movement_tripsupervisor"
        constraints = [
            *TenantOwnedModel.tenant_constraints("movement_tripsupervisor"),
            models.UniqueConstraint(fields=["trip", "membership"],
                                    name="movement_trip_supervisor_uniq"),
            models.UniqueConstraint(
                fields=["trip"], condition=models.Q(is_lead=True),
                name="movement_trip_one_lead",
            ),
        ]
