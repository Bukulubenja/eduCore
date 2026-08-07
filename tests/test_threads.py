"""Threaded messaging: who may join a thread, who may post, and what a
retraction actually erases (doc 03, "Communication")."""

from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from educore.comms import services as comms
from educore.comms.models import Message, Notification, Thread
from educore.comms.services import NotMessageAuthorError, NoVerifiedGuardianError
from educore.core.tenancy import TenantContext
from educore.students.models import GuardianLink

pytestmark = pytest.mark.django_db

PASSWORD = "correct-horse-battery"


def client_for(membership) -> APIClient:
    api = APIClient()
    response = api.post(reverse("v1:auth-token"),
                        {"email": membership.user.email, "password": PASSWORD},
                        format="json")
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access_token']}")
    return api


def _guardian_link(school_a, student, membership, *, verified):
    with TenantContext.scope(school_a):
        return GuardianLink.objects.create(
            school_id=school_a.id, student=student, membership=membership,
            relationship=GuardianLink.Relationship.MOTHER, verified=verified,
        )


@pytest.fixture
def parent(school_a, students, make_membership):
    membership = make_membership(school_a, email="parent@example.com",
                                 name="A Parent")
    _guardian_link(school_a, students[0], membership, verified=True)
    return membership


@pytest.fixture
def other_parent(school_a, students, make_membership):
    membership = make_membership(school_a, email="other-parent@example.com",
                                 name="Another Parent")
    _guardian_link(school_a, students[0], membership, verified=True)
    return membership


@pytest.fixture
def unverified_parent(school_a, students, make_membership):
    membership = make_membership(school_a, email="unverified@example.com",
                                 name="Unverified Parent")
    _guardian_link(school_a, students[0], membership, verified=False)
    return membership


@pytest.fixture
def stranger(school_a, make_membership):
    """A staff member with no connection to the thread under test."""
    return make_membership(school_a, email="stranger@example.com", name="A Stranger")


@pytest.fixture
def thread(school_a, teacher, students, parent):
    with TenantContext.scope(school_a):
        return comms.open_thread(opened_by=teacher, student=students[0],
                                 subject="Attendance concern")


# -- Who may be in the thread -------------------------------------------


def test_a_thread_includes_the_teacher_and_verified_guardians(
    school_a, teacher, students, parent, other_parent
):
    with TenantContext.scope(school_a):
        thread = comms.open_thread(opened_by=teacher, student=students[0],
                                   subject="Attendance concern")
        member_ids = set(thread.participants.values_list("membership_id", flat=True))

    assert member_ids == {teacher.pk, parent.pk, other_parent.pk}


def test_an_unverified_guardian_is_not_a_correspondent(
    school_a, teacher, students, parent, unverified_parent
):
    with TenantContext.scope(school_a):
        thread = comms.open_thread(opened_by=teacher, student=students[0],
                                   subject="Attendance concern")
        member_ids = set(thread.participants.values_list("membership_id", flat=True))

    assert parent.pk in member_ids
    assert unverified_parent.pk not in member_ids


def test_a_student_with_no_verified_guardian_cannot_be_discussed(
    school_a, teacher, students, unverified_parent
):
    with TenantContext.scope(school_a), pytest.raises(NoVerifiedGuardianError):
        comms.open_thread(opened_by=teacher, student=students[0],
                          subject="Attendance concern")


# -- Posting --------------------------------------------------------------


def test_a_non_participant_cannot_post(school_a, thread, stranger):
    api = client_for(stranger)
    response = api.post(
        reverse("v1:comms:thread-messages", args=[thread.id]), {"body": "Hello"},
        format="json",
    )

    with TenantContext.scope(school_a):
        assert not Message.objects.filter(thread=thread).exists()
    assert response.status_code == status.HTTP_404_NOT_FOUND


def test_a_non_participant_gets_404_not_403(school_a, thread, stranger):
    api = client_for(stranger)
    response = api.get(reverse("v1:comms:thread-messages", args=[thread.id]))

    assert response.status_code == status.HTTP_404_NOT_FOUND


def test_an_observer_can_read_but_not_post(school_a, thread, teacher, deputy):
    with TenantContext.scope(school_a):
        thread.participants.create(school_id=school_a.id, membership=deputy,
                                   is_observer=True)

    api = client_for(deputy)
    read = api.get(reverse("v1:comms:thread-messages", args=[thread.id]))
    posted = api.post(
        reverse("v1:comms:thread-messages", args=[thread.id]), {"body": "Hello"},
        format="json",
    )

    assert read.status_code == status.HTTP_200_OK
    assert posted.status_code == status.HTTP_403_FORBIDDEN


def test_an_empty_message_is_refused(school_a, thread, teacher):
    api = client_for(teacher)
    response = api.post(
        reverse("v1:comms:thread-messages", args=[thread.id]), {"body": "   "},
        format="json",
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST


def test_a_closed_thread_takes_no_more_messages(school_a, thread, teacher):
    with TenantContext.scope(school_a):
        thread.is_closed = True
        thread.save(update_fields=["is_closed", "updated_at"])

    api = client_for(teacher)
    response = api.post(
        reverse("v1:comms:thread-messages", args=[thread.id]), {"body": "Hello"},
        format="json",
    )

    with TenantContext.scope(school_a):
        assert not Message.objects.filter(thread=thread).exists()
    assert response.status_code == status.HTTP_409_CONFLICT


def test_posting_notifies_the_other_participants_only(school_a, thread, teacher, parent):
    with TenantContext.scope(school_a):
        comms.post_message(thread=thread, author=teacher, body="Please call me.")

        notified = set(Notification.objects.filter(
            topic="comms.thread.message"
        ).values_list("recipient_id", flat=True))

    assert notified == {parent.pk}


def test_the_notification_does_not_leak_the_message(school_a, thread, teacher, parent):
    with TenantContext.scope(school_a):
        comms.post_message(thread=thread, author=teacher,
                           body="This is a private detail about the student.")
        notification = Notification.objects.get(topic="comms.thread.message")

    assert "private detail" not in notification.body


# -- Retracting -------------------------------------------------------------


def test_only_the_author_may_retract(school_a, thread, teacher, parent):
    with TenantContext.scope(school_a):
        message = comms.post_message(thread=thread, author=teacher, body="Oops.")

        with pytest.raises(NotMessageAuthorError):
            comms.retract_message(message=message, actor=parent)

        comms.retract_message(message=message, actor=teacher)
        message.refresh_from_db()

    assert message.retracted_at is not None


def test_a_retracted_message_stays_in_place(school_a, thread, teacher):
    with TenantContext.scope(school_a):
        message = comms.post_message(thread=thread, author=teacher, body="Oops.")
        comms.retract_message(message=message, actor=teacher)

        assert Message.objects.filter(pk=message.pk, thread=thread).exists()


def test_a_retracted_message_is_blanked_on_read_but_still_listed(
    school_a, thread, teacher
):
    with TenantContext.scope(school_a):
        message = comms.post_message(thread=thread, author=teacher, body="Oops.")
        comms.retract_message(message=message, actor=teacher)

    api = client_for(teacher)
    response = api.get(reverse("v1:comms:thread-messages", args=[thread.id]))
    results = response.data["results"]

    assert len(results) == 1
    assert results[0]["is_retracted"] is True
    assert results[0]["body"] == ""


# -- Reading and unread counts ----------------------------------------------


def test_reading_a_thread_clears_its_unread_flag(school_a, thread, teacher, parent):
    with TenantContext.scope(school_a):
        comms.post_message(thread=thread, author=teacher, body="Hello")
        participant = thread.participants.get(membership=parent)
        assert comms.unread_count(thread=thread, participant=participant) == 1

    api = client_for(parent)
    api.get(reverse("v1:comms:thread-messages", args=[thread.id]))

    with TenantContext.scope(school_a):
        participant.refresh_from_db()
        assert comms.unread_count(thread=thread, participant=participant) == 0


def test_unread_counts_track_the_watermark(school_a, thread, teacher, parent):
    with TenantContext.scope(school_a):
        comms.post_message(thread=thread, author=teacher, body="One")
        comms.post_message(thread=thread, author=teacher, body="Two")
        participant = thread.participants.get(membership=parent)
        assert comms.unread_count(thread=thread, participant=participant) == 2

        comms.mark_thread_read(thread=thread, membership=parent)
        participant.refresh_from_db()
        assert comms.unread_count(thread=thread, participant=participant) == 0

        comms.post_message(thread=thread, author=teacher, body="Three")
        assert comms.unread_count(thread=thread, participant=participant) == 1


def test_the_inbox_lists_notifications(school_a, thread, teacher, parent):
    with TenantContext.scope(school_a):
        comms.post_message(thread=thread, author=teacher, body="Hello")
        comms.send_queued()

    api = client_for(parent)
    response = api.get(reverse("v1:comms:inbox"))

    assert response.status_code == status.HTTP_200_OK
    topics = {n["topic"] for n in response.data["results"]}
    assert "comms.thread.message" in topics


# -- End to end via the API --------------------------------------------------


def test_the_api_opens_a_thread_and_posts(school_a, teacher, students, parent):
    api = client_for(teacher)

    opened = api.post(reverse("v1:comms:threads"), {
        "student_id": str(students[0].id), "subject": "Attendance concern",
    }, format="json")
    assert opened.status_code == status.HTTP_201_CREATED
    thread_id = opened.data["id"]

    posted = api.post(reverse("v1:comms:thread-messages", args=[thread_id]),
                      {"body": "Could we speak this week?"}, format="json")

    with TenantContext.scope(school_a):
        assert Thread.objects.filter(pk=thread_id).exists()
        assert Message.objects.filter(thread_id=thread_id).count() == 1
    assert posted.status_code == status.HTTP_201_CREATED
    assert posted.data["body"] == "Could we speak this week?"
