"""Notifications and their delivery record.

The Delivery table is the point of this module. One row per recipient per
channel is what turns "we notified the parents" from an assumption into a
defensible claim, and it is what drives SMS fallback when a push goes
undelivered (doc 03, "Communication").
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _

from educore.core.tenancy import TenantOwnedModel


class Channel(models.TextChoices):
    IN_APP = "in_app", _("In app")
    PUSH = "push", _("Push notification")
    SMS = "sms", _("SMS")
    EMAIL = "email", _("Email")


class Notification(TenantOwnedModel):
    """One rendered message for one recipient.

    Rendered at relay time rather than at send time so the record shows what
    the recipient was actually told, even if the underlying data changes
    afterwards.
    """

    class Importance(models.TextChoices):
        ROUTINE = "routine", _("Routine")
        IMPORTANT = "important", _("Important")
        URGENT = "urgent", _("Urgent")

    recipient = models.ForeignKey("core.Membership", on_delete=models.CASCADE,
                                  related_name="notifications")
    topic = models.CharField(max_length=100, db_index=True)
    title = models.CharField(max_length=200)
    body = models.TextField()
    importance = models.CharField(max_length=16, choices=Importance,
                                  default=Importance.ROUTINE)
    payload = models.JSONField(default=dict, blank=True)

    # Set when the same event must not notify the same person twice, however
    # many times the relay retries or the event is re-emitted.
    dedupe_key = models.CharField(max_length=200, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta(TenantOwnedModel.Meta):
        db_table = "comms_notification"
        ordering = ["-created_at"]
        constraints = [
            *TenantOwnedModel.tenant_constraints("comms_notification"),
            models.UniqueConstraint(
                fields=["school", "recipient", "dedupe_key"],
                condition=~models.Q(dedupe_key=""),
                name="comms_notification_dedupe_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["school", "recipient", "read_at"],
                         name="comms_notification_inbox_idx"),
        ]

    def __str__(self) -> str:
        return self.title


class Delivery(TenantOwnedModel):
    """One attempt to get a notification to someone down one channel."""

    class Status(models.TextChoices):
        QUEUED = "queued", _("Queued")
        SENT = "sent", _("Sent")
        DELIVERED = "delivered", _("Delivered")
        READ = "read", _("Read")
        FAILED = "failed", _("Failed")
        SKIPPED = "skipped", _("Skipped")

    notification = models.ForeignKey(Notification, on_delete=models.CASCADE,
                                     related_name="deliveries")
    channel = models.CharField(max_length=16, choices=Channel)
    status = models.CharField(max_length=16, choices=Status,
                              default=Status.QUEUED, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    provider_message_id = models.CharField(max_length=200, blank=True)
    failed_reason = models.CharField(max_length=300, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    # Set when this delivery exists because an earlier channel went unanswered.
    escalated_from = models.ForeignKey("self", on_delete=models.SET_NULL,
                                       null=True, blank=True, related_name="+")

    class Meta(TenantOwnedModel.Meta):
        db_table = "comms_delivery"
        constraints = [
            *TenantOwnedModel.tenant_constraints("comms_delivery"),
            models.UniqueConstraint(fields=["notification", "channel"],
                                    name="comms_delivery_notification_channel_uniq"),
        ]
        indexes = [
            models.Index(fields=["school", "status", "sent_at"],
                         name="comms_delivery_escalation_idx"),
        ]

    @property
    def is_settled(self) -> bool:
        return self.status in {self.Status.DELIVERED, self.Status.READ,
                               self.Status.FAILED, self.Status.SKIPPED}


class Announcement(TenantOwnedModel):
    """A message to a selected audience.

    The audience is stored as a *selector*, not as a resolved recipient list.
    Resolving at publish time means a parent who enrols a child on Tuesday
    receives Wednesday's notice, and a member of staff who leaves stops
    receiving them -- without anyone maintaining a distribution list.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        SCHEDULED = "scheduled", _("Scheduled")
        PUBLISHED = "published", _("Published")
        WITHDRAWN = "withdrawn", _("Withdrawn")

    title = models.CharField(max_length=200)
    body = models.TextField()
    author = models.ForeignKey("core.Membership", on_delete=models.PROTECT,
                               related_name="announcements")
    status = models.CharField(max_length=16, choices=Status,
                              default=Status.DRAFT, db_index=True)
    importance = models.CharField(max_length=16,
                                  choices=Notification.Importance,
                                  default=Notification.Importance.ROUTINE)

    # {"roles": [...], "class_groups": [...], "levels": [...],
    #  "memberships": [...], "guardians_of_class_groups": [...]}
    audience = models.JSONField(default=dict)
    channels = models.JSONField(default=list)

    publish_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    requires_acknowledgement = models.BooleanField(default=False)
    recipients_count = models.PositiveIntegerField(default=0)

    class Meta(TenantOwnedModel.Meta):
        db_table = "comms_announcement"
        ordering = ["-created_at"]
        constraints = TenantOwnedModel.tenant_constraints("comms_announcement")

    def __str__(self) -> str:
        return self.title


class ChannelPreference(TenantOwnedModel):
    """What a person has agreed to receive, and how.

    Opt-out is per channel rather than all-or-nothing: a parent who mutes
    routine push notices should still get an urgent one, and muting SMS must
    not silently mute everything.
    """

    membership = models.OneToOneField("core.Membership", on_delete=models.CASCADE,
                                      related_name="channel_preference")
    push_enabled = models.BooleanField(default=True)
    sms_enabled = models.BooleanField(default=True)
    email_enabled = models.BooleanField(default=False)
    quiet_hours_start = models.TimeField(null=True, blank=True)
    quiet_hours_end = models.TimeField(null=True, blank=True)

    class Meta(TenantOwnedModel.Meta):
        db_table = "comms_channelpreference"
        constraints = TenantOwnedModel.tenant_constraints("comms_channelpreference")

    def allows(self, channel: str, *, importance: str) -> bool:
        if importance == Notification.Importance.URGENT:
            return True             # An emergency broadcast ignores preferences.
        return {
            Channel.IN_APP: True,
            Channel.PUSH: self.push_enabled,
            Channel.SMS: self.sms_enabled,
            Channel.EMAIL: self.email_enabled,
        }.get(channel, False)


class Thread(TenantOwnedModel):
    """A bounded conversation between defined participants. Not open chat.

    A thread is opened for a reason -- about one student, within one
    department -- and that reason is what determines who may ever be a
    correspondent, not an invite list a moderator has to police afterwards
    (doc 03, "Communication").
    """

    class Kind(models.TextChoices):
        GUARDIAN = "guardian", _("Teacher and guardians")
        STAFF = "staff", _("Staff")
        DEPARTMENT = "department", _("Department")

    kind = models.CharField(max_length=16, choices=Kind)
    subject = models.CharField(max_length=200)
    about_student = models.ForeignKey("students.Student", on_delete=models.PROTECT,
                                      null=True, blank=True, related_name="threads")
    opened_by = models.ForeignKey("core.Membership", on_delete=models.PROTECT,
                                  related_name="opened_threads")
    is_closed = models.BooleanField(default=False)
    last_message_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta(TenantOwnedModel.Meta):
        db_table = "comms_thread"
        ordering = ["-last_message_at", "-created_at"]
        constraints = TenantOwnedModel.tenant_constraints("comms_thread")

    def __str__(self) -> str:
        return self.subject


class Message(TenantOwnedModel):
    """One post in a thread. Retracted, never deleted -- a correction that
    erased its own trail would be worse than the mistake it corrected."""

    thread = models.ForeignKey(Thread, on_delete=models.CASCADE,
                               related_name="messages")
    author = models.ForeignKey("core.Membership", on_delete=models.PROTECT,
                               related_name="messages")
    body = models.TextField()
    attachment = models.FileField(upload_to="messages/", blank=True)
    retracted_at = models.DateTimeField(null=True, blank=True)
    retracted_by = models.ForeignKey("core.Membership", on_delete=models.SET_NULL,
                                     null=True, blank=True, related_name="+")

    class Meta(TenantOwnedModel.Meta):
        db_table = "comms_message"
        ordering = ["created_at"]
        constraints = [
            *TenantOwnedModel.tenant_constraints("comms_message"),
        ]
        indexes = [
            models.Index(fields=["thread", "created_at"],
                         name="comms_message_thread_idx"),
        ]

    @property
    def is_retracted(self) -> bool:
        return self.retracted_at is not None


class ThreadParticipant(TenantOwnedModel):
    """One person's membership of a thread, and how much of it they've seen."""

    thread = models.ForeignKey(Thread, on_delete=models.CASCADE,
                               related_name="participants")
    membership = models.ForeignKey("core.Membership", on_delete=models.CASCADE,
                                   related_name="thread_participations")
    can_post = models.BooleanField(default=True)
    is_observer = models.BooleanField(default=False)
    last_read_at = models.DateTimeField(null=True, blank=True)
    left_at = models.DateTimeField(null=True, blank=True)

    class Meta(TenantOwnedModel.Meta):
        db_table = "comms_threadparticipant"
        constraints = [
            *TenantOwnedModel.tenant_constraints("comms_threadparticipant"),
            models.UniqueConstraint(fields=["thread", "membership"],
                                    name="comms_participant_unique_per_thread"),
        ]
        indexes = [
            models.Index(fields=["membership", "last_read_at"],
                         name="comms_participant_inbox_idx"),
        ]
