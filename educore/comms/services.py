"""Creating notifications, sending them, and escalating the ones that stall."""

from __future__ import annotations

from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from educore.core.models import Membership

from .channels import get_channel
from .models import (
    Channel,
    ChannelPreference,
    Delivery,
    Message,
    Notification,
    Thread,
    ThreadParticipant,
)

# How long a push may sit unconfirmed before SMS is tried instead. Long enough
# that a phone coming out of a lift is not double-messaged, short enough that
# an absent child's parent hears today.
ESCALATE_AFTER = timedelta(minutes=20)

DEFAULT_CHANNELS = [Channel.IN_APP, Channel.PUSH]


class CommsError(Exception):
    pass


class NoVerifiedGuardianError(CommsError):
    pass


class NotAParticipantError(CommsError):
    pass


class ThreadClosedError(CommsError):
    pass


class ObserverCannotPostError(CommsError):
    pass


class NotMessageAuthorError(CommsError):
    pass


def _preference(membership) -> ChannelPreference:
    preference, _created = ChannelPreference.objects.get_or_create(
        membership=membership,
        defaults={"school_id": membership.school_id},
    )
    return preference


@transaction.atomic
def notify(*, recipient: Membership, topic: str, title: str, body: str,
           payload: dict | None = None, importance: str = Notification.Importance.ROUTINE,
           channels: list[str] | None = None, dedupe_key: str = "") -> Notification | None:
    """Create a notification and queue its deliveries.

    Returns None when `dedupe_key` has already been used for this recipient --
    the relay retries, and an event re-emitted after a partial failure must not
    message someone twice.
    """
    try:
        with transaction.atomic():
            notification = Notification.objects.create(
                school_id=recipient.school_id,
                recipient=recipient,
                topic=topic,
                title=title,
                body=body,
                importance=importance,
                payload=payload or {},
                dedupe_key=dedupe_key,
            )
    except IntegrityError:
        return None

    preference = _preference(recipient)
    for channel in (channels or DEFAULT_CHANNELS):
        allowed = preference.allows(channel, importance=importance)
        Delivery.objects.create(
            school_id=recipient.school_id,
            notification=notification,
            channel=channel,
            status=(Delivery.Status.QUEUED if allowed
                    else Delivery.Status.SKIPPED),
            failed_reason="" if allowed else "recipient has muted this channel",
        )

    return notification


def send_queued(*, limit: int = 200, now=None) -> dict:
    """Hand queued deliveries to their channel adapters."""
    now = now or timezone.now()
    stats = {"sent": 0, "failed": 0}

    queued = (
        Delivery.objects
        .filter(status=Delivery.Status.QUEUED)
        .select_related("notification__recipient__user")
        .order_by("created_at")[:limit]
    )

    for delivery in queued:
        adapter = get_channel(delivery.channel)
        membership = delivery.notification.recipient
        delivery.attempts += 1

        result = adapter.send(membership=membership,
                              notification=delivery.notification)

        if result.ok:
            delivery.status = (Delivery.Status.DELIVERED if result.confirmed
                               else Delivery.Status.SENT)
            delivery.provider_message_id = result.provider_message_id
            delivery.sent_at = now
            if result.confirmed:
                delivery.delivered_at = now
            stats["sent"] += 1
        else:
            delivery.status = Delivery.Status.FAILED
            delivery.failed_reason = result.error[:300]
            stats["failed"] += 1

        delivery.save(update_fields=["status", "attempts", "provider_message_id",
                                     "failed_reason", "sent_at", "delivered_at",
                                     "updated_at"])

    return stats


def escalate_stalled(*, after: timedelta = ESCALATE_AFTER, now=None) -> int:
    """Fall back to SMS where push was sent but never confirmed.

    Time-based rather than response-based, because push providers accept a
    message immediately and deliver it whenever the handset next appears. A
    parent whose phone is off is exactly the parent who needs the SMS.
    """
    now = now or timezone.now()
    cutoff = now - after

    stalled = (
        Delivery.objects
        .filter(channel=Channel.PUSH, status=Delivery.Status.SENT,
                sent_at__lt=cutoff)
        .select_related("notification__recipient")
    )

    escalated = 0
    for delivery in stalled:
        notification = delivery.notification
        preference = _preference(notification.recipient)
        if not preference.allows(Channel.SMS, importance=notification.importance):
            continue
        if Delivery.objects.filter(notification=notification,
                                   channel=Channel.SMS).exists():
            continue

        Delivery.objects.create(
            school_id=notification.school_id,
            notification=notification,
            channel=Channel.SMS,
            status=Delivery.Status.QUEUED,
            escalated_from=delivery,
        )
        escalated += 1

    return escalated


def mark_read(*, notification: Notification, at=None) -> Notification:
    at = at or timezone.now()
    if notification.read_at is None:
        notification.read_at = at
        notification.save(update_fields=["read_at", "updated_at"])
        Delivery.objects.filter(
            notification=notification,
            status__in=[Delivery.Status.SENT, Delivery.Status.DELIVERED],
        ).update(status=Delivery.Status.READ, delivered_at=at)
    return notification


def resolve_audience(selector: dict) -> list[Membership]:
    """Turn an audience selector into memberships, at publish time.

    Resolved late on purpose: a distribution list maintained by hand is a list
    that is wrong within a fortnight. A guardian who enrols a child on Tuesday
    should receive Wednesday's notice without anyone remembering to add them.
    """
    from django.db.models import Q

    from educore.core.models import RoleAssignment
    from educore.students.models import Enrolment, GuardianLink

    query = Q(pk__in=[])          # Matches nothing until a clause is added.

    if selector.get("memberships"):
        query |= Q(pk__in=selector["memberships"])

    if selector.get("roles"):
        member_ids = (RoleAssignment.objects
                      .filter(role__code__in=selector["roles"])
                      .order_by().values_list("membership_id", flat=True)
                      .distinct())
        query |= Q(pk__in=list(member_ids))

    group_ids = list(selector.get("class_groups") or [])
    if selector.get("levels"):
        from educore.academics.models import ClassGroup
        group_ids += list(
            ClassGroup.objects.filter(level_id__in=selector["levels"])
            .order_by().values_list("id", flat=True)
        )

    if group_ids:
        # Guardians of students currently enrolled in those groups.
        student_ids = (Enrolment.objects
                       .filter(class_group_id__in=group_ids, is_active=True)
                       .order_by().values_list("student_id", flat=True)
                       .distinct())
        guardian_ids = (GuardianLink.objects
                        .filter(student_id__in=list(student_ids), verified=True,
                                receives_notifications=True)
                        .order_by().values_list("membership_id", flat=True)
                        .distinct())
        query |= Q(pk__in=list(guardian_ids))

    return list(
        Membership.objects
        .filter(query, status=Membership.Status.ACTIVE)
        .select_related("user")
        .distinct()
    )


@transaction.atomic
def publish_announcement(announcement, *, actor=None, now=None):
    """Resolve the audience and queue a notification for each recipient."""
    from .models import Announcement

    if announcement.status == Announcement.Status.PUBLISHED:
        raise CommsError("This announcement has already been published.")

    now = now or timezone.now()
    recipients = resolve_audience(announcement.audience)

    for recipient in recipients:
        notify(
            recipient=recipient,
            topic="comms.announcement",
            title=announcement.title,
            body=announcement.body,
            payload={"announcement_id": str(announcement.id),
                     "requires_acknowledgement":
                         announcement.requires_acknowledgement},
            importance=announcement.importance,
            channels=announcement.channels or DEFAULT_CHANNELS,
            dedupe_key=f"announcement:{announcement.id}",
        )

    announcement.status = Announcement.Status.PUBLISHED
    announcement.published_at = now
    announcement.recipients_count = len(recipients)
    announcement.save(update_fields=["status", "published_at",
                                     "recipients_count", "updated_at"])
    return announcement


def delivery_rate(*, since, until) -> dict:
    """The number doc 01 promises: parent notifications reaching their target."""
    deliveries = Delivery.objects.filter(created_at__date__gte=since,
                                         created_at__date__lte=until)
    total = deliveries.exclude(status=Delivery.Status.SKIPPED).count()
    reached = deliveries.filter(
        status__in=[Delivery.Status.SENT, Delivery.Status.DELIVERED,
                    Delivery.Status.READ]
    ).count()

    return {
        "deliveries": total,
        "reached": reached,
        "rate": round(reached / total, 4) if total else None,
        "failed": deliveries.filter(status=Delivery.Status.FAILED).count(),
    }


# -- Threads ------------------------------------------------------------


@transaction.atomic
def open_thread(*, opened_by: Membership, student, subject: str,
                first_message: str = "") -> Thread:
    """Open a guardian thread about one student.

    Only *verified* guardians become correspondents. An unverified
    GuardianLink exists while identity is still being confirmed; showing a
    child's affairs to someone not yet entitled to them is a safeguarding
    incident, not a permissions bug (doc 03, `GuardianLink.verified`).
    """
    guardians = list(
        student.guardians.filter(verified=True, receives_notifications=True)
        .select_related("membership")
    )
    if not guardians:
        raise NoVerifiedGuardianError(
            "This student has no verified guardian to discuss with."
        )

    thread = Thread.objects.create(
        school_id=opened_by.school_id, kind=Thread.Kind.GUARDIAN,
        subject=subject, about_student=student, opened_by=opened_by,
    )
    ThreadParticipant.objects.create(school_id=opened_by.school_id,
                                     thread=thread, membership=opened_by)
    for link in guardians:
        ThreadParticipant.objects.create(school_id=opened_by.school_id,
                                         thread=thread,
                                         membership=link.membership)

    if first_message:
        post_message(thread=thread, author=opened_by, body=first_message)

    return thread


def live_participant(thread: Thread, membership: Membership) -> ThreadParticipant | None:
    return thread.participants.filter(membership=membership,
                                      left_at__isnull=True).first()


@transaction.atomic
def post_message(*, thread: Thread, author: Membership, body: str) -> Message:
    if thread.is_closed:
        raise ThreadClosedError("This thread is closed.")

    participant = live_participant(thread, author)
    if participant is None:
        raise NotAParticipantError("Not a participant of this thread.")
    if participant.is_observer or not participant.can_post:
        raise ObserverCannotPostError(
            "Observers may read this thread but not post in it."
        )

    message = Message.objects.create(school_id=thread.school_id, thread=thread,
                                     author=author, body=body)

    now = timezone.now()
    Thread.objects.filter(pk=thread.pk).update(last_message_at=now, updated_at=now)
    thread.last_message_at = now
    participant.last_read_at = now
    participant.save(update_fields=["last_read_at", "updated_at"])

    others = (
        thread.participants.filter(left_at__isnull=True)
        .exclude(membership=author)
        .select_related("membership")
    )
    for other in others:
        notify(
            recipient=other.membership,
            topic="comms.thread.message",
            title=f"New message: {thread.subject}",
            # Never the message body -- a push preview on a lock screen is
            # not a private channel (doc 03, "Communication").
            body="You have a new message.",
            payload={"thread_id": str(thread.id)},
            channels=[Channel.IN_APP, Channel.PUSH],
        )

    return message


@transaction.atomic
def retract_message(*, message: Message, actor: Membership) -> Message:
    if message.author_id != actor.pk:
        raise NotMessageAuthorError("Only the author may retract a message.")

    if message.retracted_at is None:
        message.retracted_at = timezone.now()
        message.retracted_by = actor
        message.save(update_fields=["retracted_at", "retracted_by", "updated_at"])
    return message


def mark_thread_read(*, thread: Thread, membership: Membership, at=None) -> None:
    at = at or timezone.now()
    thread.participants.filter(membership=membership).update(last_read_at=at)


def unread_count(*, thread: Thread, participant: ThreadParticipant) -> int:
    messages = thread.messages
    if participant.last_read_at is not None:
        messages = messages.filter(created_at__gt=participant.last_read_at)
    return messages.count()


def threads_for(membership: Membership):
    """Every thread `membership` is a live participant of, newest first."""
    return (
        Thread.objects
        .filter(participants__membership=membership,
               participants__left_at__isnull=True)
        .select_related("about_student", "opened_by__user")
        .distinct()
    )
