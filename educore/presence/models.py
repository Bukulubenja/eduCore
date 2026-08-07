"""Staff presence: events, evidence, derived records, exceptions.

Doc 04 and ADR-0002. The shape to keep in mind:

    AttendanceEvent   append-only   what the device reported
    AttendanceSignal  append-only   the evidence supporting it
    AttendanceRecord  derived       what it means, per person per day
    AttendanceException           a human overruling the derivation

Only the first two are facts. The third is recomputable from them, which is
what lets a policy change apply retroactively without losing evidence.
"""

from __future__ import annotations

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from educore.core.tenancy import TenantOwnedModel


class SignalType(models.TextChoices):
    QR = "qr", _("Rotating QR")
    DEVICE = "device", _("Registered device")
    GEOFENCE = "geofence", _("Geofence")
    WIFI = "wifi", _("School Wi-Fi")
    TIME_WINDOW = "time_window", _("Duty time window")
    BEACON = "beacon", _("Bluetooth beacon")
    FACE = "face", _("Face match")


class Verdict(models.TextChoices):
    PASS = "pass", _("Pass")
    FAIL = "fail", _("Fail")
    UNAVAILABLE = "unavailable", _("Unavailable")


class Disposition(models.TextChoices):
    VERIFIED = "verified", _("Verified")
    PROVISIONAL = "provisional", _("Provisional")
    REJECTED = "rejected", _("Rejected")


class AttendanceStatus(models.TextChoices):
    PRESENT = "present", _("Present")
    LATE = "late", _("Late")
    PARTIAL = "partial", _("Partial")          # checked in, never checked out
    ABSENT = "absent", _("Absent")
    ON_LEAVE = "on_leave", _("On leave")
    HOLIDAY = "holiday", _("Holiday")


# -- Policy ------------------------------------------------------------------


def default_weights() -> dict:
    """Weights for a school with no beacons and no biometrics.

    QR carries the most weight because it is the only signal that is both
    cheap and genuinely hard to forge: server-signed and alive for 30 seconds.
    Face is present at weight 0 -- the column exists, the capability does not
    (ADR-0004).
    """
    return {
        SignalType.QR: 35,
        SignalType.DEVICE: 25,
        SignalType.GEOFENCE: 15,
        SignalType.WIFI: 10,
        SignalType.TIME_WINDOW: 10,
        SignalType.BEACON: 15,
        SignalType.FACE: 0,
    }


class AttendancePolicy(TenantOwnedModel):
    """Versioned scoring rules for one school.

    Never edited in place. Changing the rules creates a new version, so an
    AttendanceRecord can always be explained in terms of the policy that was
    in force when it was written.
    """

    version = models.PositiveIntegerField()
    is_active = models.BooleanField(default=True)

    weights = models.JSONField(default=default_weights)
    accept_threshold = models.PositiveSmallIntegerField(default=75)
    review_threshold = models.PositiveSmallIntegerField(default=45)
    min_evidence_weight = models.PositiveSmallIntegerField(default=40)

    # Geofence
    geofence_radius_m = models.PositiveIntegerField(default=150)
    # Below this reported accuracy, a fix outside the fence is evidence of
    # absence. Above it, the fix is too vague to accuse anyone with.
    geofence_trusted_accuracy_m = models.PositiveIntegerField(default=50)

    # Duty window defaults, overridable per member by DutySchedule.
    day_starts_at = models.TimeField(default="07:30")
    day_ends_at = models.TimeField(default="17:00")
    late_grace_minutes = models.PositiveSmallIntegerField(default=15)
    early_checkin_minutes = models.PositiveSmallIntegerField(default=90)

    max_clock_skew_seconds = models.PositiveIntegerField(default=300)
    qr_token_ttl_seconds = models.PositiveIntegerField(default=30)

    class Meta(TenantOwnedModel.Meta):
        db_table = "presence_attendancepolicy"
        ordering = ["-version"]
        constraints = [
            *TenantOwnedModel.tenant_constraints("presence_attendancepolicy"),
            models.UniqueConstraint(fields=["school", "version"],
                                    name="presence_policy_school_version_uniq"),
            models.UniqueConstraint(
                fields=["school"], condition=models.Q(is_active=True),
                name="presence_policy_one_active_per_school",
            ),
            models.CheckConstraint(
                condition=models.Q(accept_threshold__gt=models.F("review_threshold")),
                name="presence_policy_thresholds_ordered",
            ),
        ]

    def weight_for(self, signal_type: str) -> int:
        return int(self.weights.get(signal_type, 0))

    def __str__(self) -> str:
        return f"policy v{self.version}"


class DutySchedule(TenantOwnedModel):
    """Per-member override of the policy's default duty window."""

    membership = models.ForeignKey("core.Membership", on_delete=models.CASCADE,
                                   related_name="duty_schedules")
    weekday = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(0), MaxValueValidator(6)],
        help_text="0 = Monday, matching datetime.weekday().",
    )
    starts_at = models.TimeField()
    ends_at = models.TimeField()

    class Meta(TenantOwnedModel.Meta):
        db_table = "presence_dutyschedule"
        constraints = [
            *TenantOwnedModel.tenant_constraints("presence_dutyschedule"),
            models.UniqueConstraint(fields=["membership", "weekday"],
                                    name="presence_duty_member_weekday_uniq"),
        ]


# -- Facts -------------------------------------------------------------------


class AttendanceEvent(TenantOwnedModel):
    """One check-in or check-out as reported by a device. Append-only."""

    class Kind(models.TextChoices):
        CHECK_IN = "check_in", _("Check in")
        CHECK_OUT = "check_out", _("Check out")

    membership = models.ForeignKey("core.Membership", on_delete=models.PROTECT,
                                   related_name="attendance_events")
    campus = models.ForeignKey("core.Campus", on_delete=models.PROTECT,
                               related_name="+")
    device = models.ForeignKey("core.Device", on_delete=models.SET_NULL,
                               null=True, blank=True, related_name="+")
    kind = models.CharField(max_length=16, choices=Kind)

    # The device's claim. Attacker-controlled: a phone's clock can be set back.
    captured_at = models.DateTimeField()
    # Server time. Authoritative.
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)
    clock_skew_seconds = models.IntegerField(default=0)

    # Generated on the device before any round trip; the idempotency key that
    # makes retries free over a bad network (ADR-0003).
    client_event_id = models.UUIDField()
    payload_digest = models.CharField(max_length=64)

    disposition = models.CharField(max_length=16, choices=Disposition)
    confidence = models.PositiveSmallIntegerField(default=0)
    rejection_reason = models.CharField(max_length=64, blank=True)
    policy_version = models.PositiveIntegerField()

    class Meta(TenantOwnedModel.Meta):
        db_table = "presence_attendanceevent"
        ordering = ["captured_at"]
        constraints = [
            *TenantOwnedModel.tenant_constraints("presence_attendanceevent"),
            models.UniqueConstraint(fields=["school", "client_event_id"],
                                    name="presence_event_school_client_id_uniq"),
        ]
        indexes = [
            models.Index(fields=["school", "membership", "captured_at"],
                         name="presence_event_member_idx"),
        ]

    @property
    def is_accepted(self) -> bool:
        return self.disposition != Disposition.REJECTED


class AttendanceSignal(TenantOwnedModel):
    """One piece of evidence. Append-only, and never discarded.

    Retained so records can be recomputed when the policy changes -- a policy
    change must never silently rewrite history it cannot justify.
    """

    event = models.ForeignKey(AttendanceEvent, on_delete=models.CASCADE,
                              related_name="signals")
    signal_type = models.CharField(max_length=20, choices=SignalType)
    verdict = models.CharField(max_length=16, choices=Verdict)
    weight_applied = models.PositiveSmallIntegerField(default=0)
    hard_fail = models.BooleanField(default=False)
    detail = models.CharField(max_length=200, blank=True)
    payload = models.JSONField(default=dict, blank=True)

    class Meta(TenantOwnedModel.Meta):
        db_table = "presence_attendancesignal"
        constraints = [
            *TenantOwnedModel.tenant_constraints("presence_attendancesignal"),
            models.UniqueConstraint(fields=["event", "signal_type"],
                                    name="presence_signal_event_type_uniq"),
        ]


class QrRedemption(TenantOwnedModel):
    """A nonce consumed by one member.

    Uniqueness is per (nonce, member), not per nonce: a displayed code is
    scanned by everyone arriving in its 30-second life. What must not happen
    is the *same* person redeeming the *same* code twice.
    """

    nonce = models.CharField(max_length=64)
    membership = models.ForeignKey("core.Membership", on_delete=models.CASCADE,
                                   related_name="+")
    redeemed_at = models.DateTimeField(auto_now_add=True)

    class Meta(TenantOwnedModel.Meta):
        db_table = "presence_qrredemption"
        constraints = [
            *TenantOwnedModel.tenant_constraints("presence_qrredemption"),
            models.UniqueConstraint(fields=["school", "nonce", "membership"],
                                    name="presence_qr_nonce_member_uniq"),
        ]


# -- Derived -----------------------------------------------------------------


class AttendanceRecord(TenantOwnedModel):
    """What the evidence means, for one person on one day."""

    class Resolution(models.TextChoices):
        AUTO = "auto", _("Automatic")
        REVIEWED = "reviewed", _("Reviewed")
        OVERRIDDEN = "overridden", _("Overridden")

    membership = models.ForeignKey("core.Membership", on_delete=models.PROTECT,
                                   related_name="attendance_records")
    date = models.DateField(db_index=True)
    status = models.CharField(max_length=16, choices=AttendanceStatus)
    disposition = models.CharField(max_length=16, choices=Disposition)
    confidence = models.PositiveSmallIntegerField(default=0)
    first_in_at = models.DateTimeField(null=True, blank=True)
    last_out_at = models.DateTimeField(null=True, blank=True)
    minutes_on_site = models.PositiveIntegerField(default=0)
    resolution = models.CharField(max_length=16, choices=Resolution,
                                  default=Resolution.AUTO)
    policy_version = models.PositiveIntegerField()

    class Meta(TenantOwnedModel.Meta):
        db_table = "presence_attendancerecord"
        ordering = ["-date"]
        constraints = [
            *TenantOwnedModel.tenant_constraints("presence_attendancerecord"),
            models.UniqueConstraint(fields=["membership", "date"],
                                    name="presence_record_member_date_uniq"),
        ]
        indexes = [
            models.Index(fields=["school", "date", "disposition"],
                         name="presence_record_review_idx"),
        ]

    @property
    def needs_review(self) -> bool:
        return (self.disposition == Disposition.PROVISIONAL
                and self.resolution == self.Resolution.AUTO)


class AttendanceException(TenantOwnedModel):
    """A dispute or a supervised override. Never edits the original record.

    The appeal route is not a courtesy. A workforce that cannot contest what
    the system says about them will work around the system instead, and the
    evidence chain breaks at its first link (doc 01, principle 6).
    """

    class Decision(models.TextChoices):
        PENDING = "pending", _("Pending")
        UPHELD = "upheld", _("Upheld")
        DECLINED = "declined", _("Declined")

    class Reason(models.TextChoices):
        DEVICE_FAULT = "device_fault", _("Device fault")
        NO_CONNECTIVITY = "no_connectivity", _("No connectivity")
        OFF_SITE_DUTY = "off_site_duty", _("Off-site school duty")
        APPROVED_LEAVE = "approved_leave", _("Approved leave")
        SICK = "sick", _("Sick")
        OTHER = "other", _("Other")

    record = models.ForeignKey(AttendanceRecord, on_delete=models.PROTECT,
                               related_name="exceptions")
    raised_by = models.ForeignKey("core.Membership", on_delete=models.PROTECT,
                                  related_name="+")
    reason_code = models.CharField(max_length=32, choices=Reason)
    narrative = models.TextField(blank=True)
    requested_status = models.CharField(max_length=16, choices=AttendanceStatus,
                                        blank=True)

    decision = models.CharField(max_length=16, choices=Decision,
                                default=Decision.PENDING)
    decided_by = models.ForeignKey("core.Membership", on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True)

    class Meta(TenantOwnedModel.Meta):
        db_table = "presence_attendanceexception"
        ordering = ["-created_at"]
        constraints = [
            *TenantOwnedModel.tenant_constraints("presence_attendanceexception"),
            models.UniqueConstraint(
                fields=["record"], condition=models.Q(decision="pending"),
                name="presence_exception_one_open_per_record",
            ),
        ]
