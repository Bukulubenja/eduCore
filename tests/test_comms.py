"""Notifications end to end: event queued, relayed, rendered, delivered."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.core import mail
from django.test import override_settings
from django.utils import timezone

from educore.comms import services as comms
from educore.comms.channels import reset_channel_cache
from educore.comms.models import Channel, ChannelPreference, Delivery, Notification
from educore.core.outbox import relay_all_tenants
from educore.core.tenancy import TenantContext
from educore.students import services as student_services
from educore.students.models import GuardianLink, StudentAttendanceStatus
from educore.timetable import services as timetable_services
from educore.timetable.models import LessonInstance

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _reset_channels():
    reset_channel_cache()
    yield
    reset_channel_cache()


@pytest.fixture
def lesson(school_a, draft_version, slot, course, class_group, teacher, room,
           make_scheduled_lesson):
    make_scheduled_lesson(draft_version, weekday=0, slot=slot, course=course,
                          class_group=class_group, teacher=teacher, room=room)
    with TenantContext.scope(school_a):
        timetable_services.publish(draft_version)
        return LessonInstance.objects.first()


@pytest.fixture
def parent(school_a, students, make_membership):
    membership = make_membership(school_a, email="parent@example.com",
                                 name="A Parent")
    with TenantContext.scope(school_a):
        GuardianLink.objects.create(
            school_id=school_a.id, student=students[0], membership=membership,
            relationship=GuardianLink.Relationship.MOTHER,
            is_primary_contact=True, verified=True,
        )
    return membership


# -- The whole chain ---------------------------------------------------------


def test_an_absence_reaches_the_guardians_inbox(school_a, lesson, students,
                                                teacher, parent):
    """Register submitted -> outbox row -> relay -> notification -> delivery."""
    with TenantContext.scope(school_a):
        student_services.submit_register(
            lesson_instance=lesson, marked_by=teacher,
            exceptions=[{"student_id": students[0].id,
                         "status": StudentAttendanceStatus.ABSENT}],
        )

    relay_all_tenants()

    with TenantContext.scope(school_a):
        notification = Notification.objects.get(recipient=parent)
        channels = set(notification.deliveries.values_list("channel", flat=True))

    assert notification.topic == "students.absence.detected"
    assert notification.importance == Notification.Importance.IMPORTANT
    assert channels == {Channel.IN_APP, Channel.PUSH}


def test_the_same_absence_relayed_twice_notifies_once(school_a, lesson, students,
                                                      teacher, parent):
    """Handlers must be idempotent: the relay retries, and a half-succeeded
    handler is called again with the same payload."""
    with TenantContext.scope(school_a):
        student_services.submit_register(
            lesson_instance=lesson, marked_by=teacher,
            exceptions=[{"student_id": students[0].id,
                         "status": StudentAttendanceStatus.ABSENT}],
        )
        from educore.core.models import OutboxMessage
        event = OutboxMessage.objects.get(topic="students.absence.detected")

    relay_all_tenants()

    with TenantContext.scope(school_a):
        OutboxMessage.objects.filter(pk=event.pk).update(
            status=OutboxMessage.Status.PENDING, published_at=None
        )
    relay_all_tenants()

    with TenantContext.scope(school_a):
        assert Notification.objects.filter(recipient=parent).count() == 1


def test_provisional_checkins_alert_leadership_but_verified_ones_do_not(
    school_a, campus, teacher, deputy, morning, good_signals, policy
):
    """Notifying on every arrival buries the few that need a human."""
    import uuid

    from educore.presence import services as presence

    with TenantContext.scope(school_a):
        presence.submit_event(membership=teacher, campus=campus,
                              kind="check_in", captured_at=morning,
                              client_event_id=uuid.uuid4(),
                              raw_signals=good_signals(morning))
        presence.submit_event(membership=teacher, campus=campus,
                              kind="check_out",
                              captured_at=morning + timedelta(hours=8),
                              client_event_id=uuid.uuid4(), raw_signals={})

    relay_all_tenants()

    with TenantContext.scope(school_a):
        alerts = Notification.objects.filter(recipient=deputy,
                                             topic="presence.event.recorded")

    assert alerts.count() == 1


# -- Channels and preferences ------------------------------------------------


def test_a_muted_channel_is_skipped_not_sent(school_a, parent):
    with TenantContext.scope(school_a):
        ChannelPreference.objects.create(school_id=school_a.id,
                                         membership=parent, push_enabled=False)
        comms.notify(recipient=parent, topic="t", title="Hello", body="Body")

        push = Delivery.objects.get(channel=Channel.PUSH)
        in_app = Delivery.objects.get(channel=Channel.IN_APP)

    assert push.status == Delivery.Status.SKIPPED
    assert in_app.status == Delivery.Status.QUEUED


def test_an_urgent_message_overrides_preferences(school_a, parent):
    """An emergency broadcast is not something a mute setting can silence."""
    with TenantContext.scope(school_a):
        ChannelPreference.objects.create(school_id=school_a.id,
                                         membership=parent, push_enabled=False)
        comms.notify(recipient=parent, topic="t", title="Evacuate",
                     body="Leave the building.",
                     importance=Notification.Importance.URGENT)

        push = Delivery.objects.get(channel=Channel.PUSH)

    assert push.status == Delivery.Status.QUEUED


def test_sending_records_the_outcome_per_delivery(school_a, parent):
    with TenantContext.scope(school_a):
        comms.notify(recipient=parent, topic="t", title="Hello", body="Body")
        stats = comms.send_queued()

        in_app = Delivery.objects.get(channel=Channel.IN_APP)
        push = Delivery.objects.get(channel=Channel.PUSH)

    assert stats["sent"] >= 1
    # In-app needs no transport, so it is delivered outright.
    assert in_app.status == Delivery.Status.DELIVERED
    # Push has no registered device in this test and must say so, not pretend.
    assert push.status == Delivery.Status.FAILED
    assert "push token" in push.failed_reason


def test_push_with_a_registered_device_is_sent_not_delivered(school_a, parent):
    """Sent means handed over, not received -- which is why SMS fallback is
    time-based rather than waiting for a response that may never come."""
    from educore.core.models import Device

    with TenantContext.scope(school_a):
        Device.objects.create(school_id=school_a.id, membership=parent,
                              fingerprint="parent-phone", platform="android",
                              push_token="tok-123", status=Device.Status.ACTIVE)
        comms.notify(recipient=parent, topic="t", title="Hello", body="Body")
        comms.send_queued()

        push = Delivery.objects.get(channel=Channel.PUSH)

    assert push.status == Delivery.Status.SENT
    assert push.delivered_at is None


@override_settings(
    COMMS_CHANNEL_BACKENDS={"email": "educore.comms.channels.EmailChannel"}
)
def test_email_actually_sends(school_a, parent):
    with TenantContext.scope(school_a):
        ChannelPreference.objects.create(school_id=school_a.id,
                                         membership=parent, email_enabled=True)
        comms.notify(recipient=parent, topic="t", title="Report ready",
                     body="Your child's report card is available.",
                     channels=[Channel.EMAIL])
        comms.send_queued()

    assert len(mail.outbox) == 1
    assert mail.outbox[0].subject == "Report ready"
    assert mail.outbox[0].to == ["parent@example.com"]


# -- Escalation --------------------------------------------------------------


def test_an_unconfirmed_push_escalates_to_sms(school_a, parent):
    """A parent whose phone is off is exactly the parent who needs the SMS."""
    from educore.core.models import Device

    with TenantContext.scope(school_a):
        Device.objects.create(school_id=school_a.id, membership=parent,
                              fingerprint="parent-phone", platform="android",
                              push_token="tok-123", status=Device.Status.ACTIVE)
        comms.notify(recipient=parent, topic="t", title="Absent",
                     body="Your child was marked absent.")
        comms.send_queued()

        Delivery.objects.filter(channel=Channel.PUSH).update(
            sent_at=timezone.now() - timedelta(hours=1)
        )
        escalated = comms.escalate_stalled()

        sms = Delivery.objects.get(channel=Channel.SMS)

    assert escalated == 1
    assert sms.status == Delivery.Status.QUEUED
    assert sms.escalated_from is not None


def test_a_confirmed_push_does_not_escalate(school_a, parent):
    with TenantContext.scope(school_a):
        comms.notify(recipient=parent, topic="t", title="Hi", body="Body")
        comms.send_queued()
        Delivery.objects.filter(channel=Channel.PUSH).update(
            status=Delivery.Status.DELIVERED,
            sent_at=timezone.now() - timedelta(hours=1),
        )

        assert comms.escalate_stalled() == 0


def test_escalation_does_not_duplicate_an_existing_sms(school_a, parent):
    from educore.core.models import Device

    with TenantContext.scope(school_a):
        Device.objects.create(school_id=school_a.id, membership=parent,
                              fingerprint="parent-phone", platform="android",
                              push_token="tok-123", status=Device.Status.ACTIVE)
        comms.notify(recipient=parent, topic="t", title="Absent", body="Body")
        comms.send_queued()
        Delivery.objects.filter(channel=Channel.PUSH).update(
            sent_at=timezone.now() - timedelta(hours=1)
        )

        assert comms.escalate_stalled() == 1
        assert comms.escalate_stalled() == 0


def test_reading_a_notification_settles_its_deliveries(school_a, parent):
    with TenantContext.scope(school_a):
        notification = comms.notify(recipient=parent, topic="t", title="Hi",
                                    body="Body")
        comms.send_queued()
        comms.mark_read(notification=notification)

        in_app = Delivery.objects.get(channel=Channel.IN_APP)

    assert notification.read_at is not None
    assert in_app.status == Delivery.Status.READ


def test_delivery_rate_excludes_muted_channels(school_a, parent):
    """The claim is "we reached them", not "we tried everything"."""
    with TenantContext.scope(school_a):
        ChannelPreference.objects.create(school_id=school_a.id,
                                         membership=parent, push_enabled=False)
        comms.notify(recipient=parent, topic="t", title="Hi", body="Body")
        comms.send_queued()

        today = timezone.localdate()
        rate = comms.delivery_rate(since=today - timedelta(days=1), until=today)

    assert rate["deliveries"] == 1          # in-app only; push was skipped
    assert rate["rate"] == 1.0


# -- The command -------------------------------------------------------------


def test_relay_outbox_command_drains_and_sends(school_a, lesson, students,
                                               teacher, parent):
    from django.core.management import call_command

    with TenantContext.scope(school_a):
        student_services.submit_register(
            lesson_instance=lesson, marked_by=teacher,
            exceptions=[{"student_id": students[0].id,
                         "status": StudentAttendanceStatus.ABSENT}],
        )

    call_command("relay_outbox")

    with TenantContext.scope(school_a):
        in_app = Delivery.objects.get(channel=Channel.IN_APP,
                                      notification__recipient=parent)

    assert in_app.status == Delivery.Status.DELIVERED
