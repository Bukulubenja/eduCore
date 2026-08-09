"""Core domain: tenancy, identity, access control, audit, outbox.

Corresponds to doc 03 "Identity and tenancy". Every model except School and
User is tenant-owned.
"""

from __future__ import annotations

import hashlib
import json

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .tenancy import TenantOwnedModel, UUIDModel

# -- The tenant --------------------------------------------------------------


class School(UUIDModel):
    """The tenant. The isolation boundary of the entire system."""

    class Status(models.TextChoices):
        PROVISIONING = "provisioning", _("Provisioning")
        ACTIVE = "active", _("Active")
        SUSPENDED = "suspended", _("Suspended")
        CLOSED = "closed", _("Closed")

    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=64, unique=True)
    status = models.CharField(max_length=20, choices=Status,
                              default=Status.PROVISIONING)
    timezone = models.CharField(max_length=64, default="Africa/Kampala")
    locale = models.CharField(max_length=16, default="en-GB")
    country = models.CharField(max_length=2, default="UG")

    class Meta:
        db_table = "core_school"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Campus(TenantOwnedModel):
    """A physical site. Owns the geofence -- a school may have several."""

    name = models.CharField(max_length=200)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    # Polygon, not radius: school grounds are rarely circular and a radius
    # generous enough to cover the far classroom also covers the road outside.
    boundary = models.JSONField(default=list, blank=True)
    is_primary = models.BooleanField(default=False)

    # Hashes, not SSIDs: an SSID is a string anyone can clone onto a hotspot,
    # whereas a BSSID identifies the physical access point. Hashed so a stolen
    # database does not hand out a map of the school's network.
    wifi_bssid_hashes = models.JSONField(default=list, blank=True)
    beacon_ids = models.JSONField(default=list, blank=True)

    class Meta(TenantOwnedModel.Meta):
        db_table = "core_campus"
        constraints = TenantOwnedModel.tenant_constraints("core_campus")

    def __str__(self) -> str:
        return self.name


# -- Identity ----------------------------------------------------------------


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create(self, email, phone_e164, password, **extra):
        if not email and not phone_e164:
            raise ValueError("A user needs either an email address or a phone number.")
        user = self.model(
            email=self.normalize_email(email) if email else None,
            phone_e164=phone_e164 or None,
            **extra,
        )
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email=None, phone_e164=None, password=None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create(email, phone_e164, password, **extra)

    def create_superuser(self, email=None, phone_e164=None, password=None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        return self._create(email, phone_e164, password, **extra)


class User(AbstractBaseUser, PermissionsMixin, UUIDModel):
    """A person. Global identity, no school data.

    A user is not a role. The same person may teach at one school and parent a
    child at another; both are Memberships, and neither belongs on this model.
    """

    email = models.EmailField(unique=True, null=True, blank=True)
    phone_e164 = models.CharField(max_length=16, unique=True, null=True, blank=True)
    full_name = models.CharField(max_length=200)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)      # Django admin / operator portal
    mfa_enabled = models.BooleanField(default=False)
    # Restricted data (doc 06): never logged, never in a lower environment,
    # and field-level encrypted before this reaches production.
    mfa_secret = models.CharField(max_length=64, blank=True)
    last_login_at = models.DateTimeField(null=True, blank=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["full_name"]

    objects = UserManager()

    class Meta:
        db_table = "core_user"

    def __str__(self) -> str:
        return self.full_name or self.email or self.phone_e164 or str(self.pk)


class Membership(TenantOwnedModel):
    """A person's role-bearing link to one school.

    Authorisation is evaluated against a Membership, never against a User.
    """

    class Status(models.TextChoices):
        INVITED = "invited", _("Invited")
        ACTIVE = "active", _("Active")
        SUSPENDED = "suspended", _("Suspended")
        ENDED = "ended", _("Ended")

    user = models.ForeignKey(User, on_delete=models.PROTECT,
                             related_name="memberships")
    staff_number = models.CharField(max_length=32, blank=True)
    status = models.CharField(max_length=20, choices=Status,
                              default=Status.INVITED)
    started_on = models.DateField(default=timezone.localdate)
    ended_on = models.DateField(null=True, blank=True)

    class Meta(TenantOwnedModel.Meta):
        db_table = "core_membership"
        constraints = [
            *TenantOwnedModel.tenant_constraints("core_membership"),
            models.UniqueConstraint(
                fields=["school", "user"], name="core_membership_school_user_uniq"
            ),
            models.CheckConstraint(
                condition=models.Q(ended_on__isnull=True)
                | models.Q(ended_on__gte=models.F("started_on")),
                name="core_membership_dates_ordered",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user} @ {self.school_id}"

    @property
    def is_active(self) -> bool:
        return self.status == self.Status.ACTIVE


# -- Access control ----------------------------------------------------------


class Role(TenantOwnedModel):
    """A named permission bundle, seeded per school from platform templates."""

    code = models.CharField(max_length=40)
    name = models.CharField(max_length=100)
    permissions = models.JSONField(default=list)
    is_system = models.BooleanField(default=True)

    class Meta(TenantOwnedModel.Meta):
        db_table = "core_role"
        constraints = [
            *TenantOwnedModel.tenant_constraints("core_role"),
            models.UniqueConstraint(fields=["school", "code"],
                                    name="core_role_school_code_uniq"),
        ]

    def __str__(self) -> str:
        return self.name


class RoleAssignment(TenantOwnedModel):
    """Grants a Role to a Membership, optionally scoped to one object.

    Scope is what makes coarse RBAC workable: head of the Science department,
    class teacher of S4 Blue, guardian of one student.
    """

    membership = models.ForeignKey(Membership, on_delete=models.CASCADE,
                                   related_name="role_assignments")
    role = models.ForeignKey(Role, on_delete=models.PROTECT,
                             related_name="assignments")
    scope_type = models.CharField(max_length=40, blank=True)
    scope_id = models.UUIDField(null=True, blank=True)
    valid_from = models.DateField(default=timezone.localdate)
    valid_to = models.DateField(null=True, blank=True)

    class Meta(TenantOwnedModel.Meta):
        db_table = "core_roleassignment"
        constraints = [
            *TenantOwnedModel.tenant_constraints("core_roleassignment"),
            models.UniqueConstraint(
                fields=["membership", "role", "scope_type", "scope_id"],
                name="core_roleassignment_unique_grant",
            ),
        ]

    def is_valid_on(self, day) -> bool:
        return self.valid_from <= day and (self.valid_to is None or day <= self.valid_to)


class Device(TenantOwnedModel):
    """A mobile device bound to a Membership.

    Device binding plus platform attestation is the cheapest control that
    defeats the dominant attendance fraud -- checking in from home (doc 04).
    """

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending approval")
        ACTIVE = "active", _("Active")
        REVOKED = "revoked", _("Revoked")

    membership = models.ForeignKey(Membership, on_delete=models.CASCADE,
                                   related_name="devices")
    fingerprint = models.CharField(max_length=128)
    platform = models.CharField(max_length=16)
    model_name = models.CharField(max_length=100, blank=True)
    push_token = models.CharField(max_length=512, blank=True)
    attested = models.BooleanField(default=False)
    status = models.CharField(max_length=16, choices=Status, default=Status.PENDING)
    approved_by = models.ForeignKey(Membership, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name="+")
    last_seen_at = models.DateTimeField(null=True, blank=True)

    class Meta(TenantOwnedModel.Meta):
        db_table = "core_device"
        constraints = [
            *TenantOwnedModel.tenant_constraints("core_device"),
            models.UniqueConstraint(fields=["school", "fingerprint"],
                                    name="core_device_school_fingerprint_uniq"),
        ]


class RefreshToken(TenantOwnedModel):
    """A single-use refresh token, stored only as a hash.

    `family_id` chains every rotation of one login together. Replaying a
    consumed token revokes the family, which is what makes theft detectable
    rather than silent (see core/tokens.py).
    """

    membership = models.ForeignKey(Membership, on_delete=models.CASCADE,
                                   related_name="refresh_tokens")
    device = models.ForeignKey(Device, on_delete=models.SET_NULL,
                               null=True, blank=True, related_name="+")
    family_id = models.UUIDField(db_index=True)
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta(TenantOwnedModel.Meta):
        db_table = "core_refreshtoken"
        constraints = TenantOwnedModel.tenant_constraints("core_refreshtoken")
        indexes = [
            models.Index(fields=["membership", "expires_at"],
                         name="core_refresh_member_idx"),
        ]

    @property
    def is_live(self) -> bool:
        return (self.consumed_at is None and self.revoked_at is None
                and self.expires_at > timezone.now())


class InviteToken(TenantOwnedModel):
    """A single-use token that lets a newly created Membership set a password.

    Same hashed-at-rest, single-use shape as `RefreshToken` above, but scoped
    to onboarding rather than a session: issued once when a Membership is
    created `INVITED` (by `provision_school`'s first administrator or the
    staff/guardian bulk importers -- see `educore/core/invites.py`) and
    consumed once, by `core.tokens.accept_invite`, to set a password and
    activate the membership.
    """

    membership = models.ForeignKey(Membership, on_delete=models.CASCADE,
                                   related_name="invite_tokens")
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta(TenantOwnedModel.Meta):
        db_table = "core_invitetoken"
        constraints = TenantOwnedModel.tenant_constraints("core_invitetoken")
        indexes = [
            models.Index(fields=["membership", "expires_at"],
                         name="core_invite_member_idx"),
        ]

    @property
    def is_live(self) -> bool:
        return self.consumed_at is None and self.expires_at > timezone.now()


class StepUpGrant(TenantOwnedModel):
    """Proof that someone re-authenticated moments ago.

    Single-use and short-lived. Being logged in is not the same as being
    present: a 15-minute token in a browser left open in a staff room is not
    evidence that the head teacher is at the keyboard, and releasing a term's
    results is precisely where that distinction matters.
    """

    membership = models.ForeignKey(Membership, on_delete=models.CASCADE,
                                   related_name="step_up_grants")
    token = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    used_for = models.CharField(max_length=100, blank=True)

    class Meta(TenantOwnedModel.Meta):
        db_table = "core_stepupgrant"
        constraints = TenantOwnedModel.tenant_constraints("core_stepupgrant")
        indexes = [
            models.Index(fields=["membership", "expires_at"],
                         name="core_stepup_member_idx"),
        ]


# -- Audit -------------------------------------------------------------------


class AuditEvent(TenantOwnedModel):
    """Append-only, hash-chained record of every consequential action.

    The chain makes silent tampering detectable -- including by us. When a
    school disputes a mark change six months later, an unbroken chain is the
    difference between evidence and a claim.
    """

    actor_membership = models.ForeignKey(Membership, on_delete=models.SET_NULL,
                                         null=True, blank=True, related_name="+")
    action = models.CharField(max_length=80, db_index=True)
    object_type = models.CharField(max_length=80, db_index=True)
    object_id = models.UUIDField(null=True, blank=True, db_index=True)
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    request_id = models.CharField(max_length=64, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    device = models.ForeignKey(Device, on_delete=models.SET_NULL,
                               null=True, blank=True, related_name="+")
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)
    sequence = models.BigIntegerField()
    prev_hash = models.CharField(max_length=64, blank=True)
    hash = models.CharField(max_length=64)

    class Meta(TenantOwnedModel.Meta):
        db_table = "core_auditevent"
        ordering = ["sequence"]
        constraints = [
            *TenantOwnedModel.tenant_constraints("core_auditevent"),
            models.UniqueConstraint(fields=["school", "sequence"],
                                    name="core_auditevent_school_sequence_uniq"),
        ]
        indexes = [
            models.Index(fields=["school", "object_type", "object_id"],
                         name="core_audit_object_idx"),
        ]

    # updated_at is meaningless on an append-only table, but the abstract base
    # provides it; it is never written after insert.

    def compute_hash(self) -> str:
        payload = {
            "school": str(self.school_id),
            "sequence": self.sequence,
            "actor": str(self.actor_membership_id) if self.actor_membership_id else None,
            "action": self.action,
            "object_type": self.object_type,
            "object_id": str(self.object_id) if self.object_id else None,
            "before": self.before,
            "after": self.after,
            "occurred_at": self.occurred_at.isoformat(),
            "prev_hash": self.prev_hash,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                               default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def save(self, *args, **kwargs):
        if self._state.adding is False:
            raise ValueError("AuditEvent is append-only and cannot be modified.")
        return super().save(*args, **kwargs)


# -- Transactional outbox ----------------------------------------------------


class OutboxMessage(TenantOwnedModel):
    """Domain events, written in the same transaction as the change they describe.

    A relay publishes them to Celery afterwards. Enqueuing a task directly from
    a request handler publishes work for transactions that may still roll back
    -- the classic dual-write bug, and a very hard one to see in testing.
    """

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        PUBLISHED = "published", _("Published")
        FAILED = "failed", _("Failed")

    topic = models.CharField(max_length=100, db_index=True)
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=16, choices=Status, default=Status.PENDING)
    attempts = models.PositiveIntegerField(default=0)
    available_at = models.DateTimeField(default=timezone.now, db_index=True)
    published_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    class Meta(TenantOwnedModel.Meta):
        db_table = "core_outboxmessage"
        constraints = TenantOwnedModel.tenant_constraints("core_outboxmessage")
        indexes = [
            models.Index(fields=["status", "available_at"],
                         name="core_outbox_dispatch_idx"),
        ]
