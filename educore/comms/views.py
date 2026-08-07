"""Announcements, inbox, and threaded messaging endpoints (doc 03, "Communication")."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from educore.core.views import ProblemError
from educore.students.models import Student

from . import services
from .models import Announcement, Notification, Thread, ThreadParticipant


def _membership(request):
    membership = getattr(request, "membership", None)
    if membership is None:
        raise ProblemError("No active school membership for this session.",
                           "no-active-membership", status.HTTP_403_FORBIDDEN)
    return membership


def _thread_and_participant(thread_id, membership):
    """A thread and the requester's live participation in it.

    Not-found and not-a-participant collapse to the same 404: whether a
    thread exists at all is not something a non-participant is entitled to
    learn.
    """
    thread = Thread.objects.filter(pk=thread_id).first()
    participant = (services.live_participant(thread, membership)
                   if thread is not None else None)
    if thread is None or participant is None:
        raise ProblemError("Thread not found.", "thread-not-found",
                           status.HTTP_404_NOT_FOUND)
    return thread, participant


def _message_dict(message) -> dict:
    retracted = message.is_retracted
    return {
        "id": str(message.id),
        "thread_id": str(message.thread_id),
        "author_id": str(message.author_id),
        "author_name": message.author.user.full_name,
        "body": "" if retracted else message.body,
        "is_retracted": retracted,
        "retracted_at": message.retracted_at,
        "created_at": message.created_at,
    }


def _thread_dict(thread: Thread, *, unread: int) -> dict:
    return {
        "id": str(thread.id),
        "kind": thread.kind,
        "subject": thread.subject,
        "about_student_id": (str(thread.about_student_id)
                             if thread.about_student_id else None),
        "is_closed": thread.is_closed,
        "last_message_at": thread.last_message_at,
        "unread_count": unread,
    }


# -- Announcements ------------------------------------------------------


class AnnouncementSerializer(serializers.ModelSerializer):
    class Meta:
        model = Announcement
        fields = ["id", "title", "body", "importance", "audience", "channels",
                  "requires_acknowledgement", "status", "published_at",
                  "recipients_count"]
        read_only_fields = ["id", "status", "published_at", "recipients_count"]


class AnnouncementListView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=dict)
    def get(self, request):
        _membership(request)
        announcements = Announcement.objects.order_by("-created_at")
        return Response({
            "results": AnnouncementSerializer(announcements, many=True).data,
        })

    @extend_schema(request=AnnouncementSerializer, responses=AnnouncementSerializer)
    def post(self, request):
        membership = _membership(request)
        payload = AnnouncementSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        announcement = payload.save(school_id=membership.school_id,
                                    author=membership)
        return Response(AnnouncementSerializer(announcement).data,
                        status=status.HTTP_201_CREATED)


class PublishAnnouncementView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=None, responses=AnnouncementSerializer)
    def post(self, request, announcement_id):
        _membership(request)
        announcement = Announcement.objects.filter(pk=announcement_id).first()
        if announcement is None:
            raise ProblemError("Announcement not found.", "announcement-not-found",
                               status.HTTP_404_NOT_FOUND)

        try:
            announcement = services.publish_announcement(announcement)
        except services.CommsError as exc:
            raise ProblemError(str(exc), "already-published",
                               status.HTTP_409_CONFLICT) from exc

        return Response(AnnouncementSerializer(announcement).data)


# -- Threads --------------------------------------------------------------


class OpenThreadSerializer(serializers.Serializer):
    student_id = serializers.UUIDField()
    subject = serializers.CharField(max_length=200)
    first_message = serializers.CharField(required=False, allow_blank=True,
                                          default="")


class PostMessageSerializer(serializers.Serializer):
    body = serializers.CharField(max_length=8000)

    def validate_body(self, value):
        if not value.strip():
            raise serializers.ValidationError("A message cannot be empty.")
        return value


class ThreadListView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=dict)
    def get(self, request):
        membership = _membership(request)
        threads = list(services.threads_for(membership))
        participants = {
            p.thread_id: p
            for p in ThreadParticipant.objects.filter(
                thread_id__in=[t.id for t in threads], membership=membership,
            )
        }
        return Response({
            "results": [
                _thread_dict(thread, unread=services.unread_count(
                    thread=thread, participant=participants[thread.id]
                ))
                for thread in threads
            ],
        })

    @extend_schema(request=OpenThreadSerializer, responses=dict)
    def post(self, request):
        payload = OpenThreadSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        membership = _membership(request)

        student = Student.objects.filter(pk=data["student_id"]).first()
        if student is None:
            raise ProblemError("Student not found.", "student-not-found",
                               status.HTTP_404_NOT_FOUND)

        try:
            thread = services.open_thread(
                opened_by=membership, student=student, subject=data["subject"],
                first_message=data.get("first_message", ""),
            )
        except services.NoVerifiedGuardianError as exc:
            raise ProblemError(str(exc), "no-verified-guardian",
                               status.HTTP_422_UNPROCESSABLE_ENTITY) from exc

        participant = ThreadParticipant.objects.get(thread=thread,
                                                     membership=membership)
        return Response(
            _thread_dict(thread, unread=services.unread_count(
                thread=thread, participant=participant
            )),
            status=status.HTTP_201_CREATED,
        )


class ThreadMessagesView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=dict)
    def get(self, request, thread_id):
        membership = _membership(request)
        thread, _participant = _thread_and_participant(thread_id, membership)

        messages = (
            thread.messages.select_related("author__user")
            .order_by("created_at")
        )
        results = [_message_dict(message) for message in messages]
        services.mark_thread_read(thread=thread, membership=membership)

        return Response({"thread_id": str(thread.id), "results": results})

    @extend_schema(request=PostMessageSerializer, responses=dict)
    def post(self, request, thread_id):
        payload = PostMessageSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        membership = _membership(request)
        thread, _participant = _thread_and_participant(thread_id, membership)

        try:
            message = services.post_message(
                thread=thread, author=membership,
                body=payload.validated_data["body"],
            )
        except services.ThreadClosedError as exc:
            raise ProblemError(str(exc), "thread-closed",
                               status.HTTP_409_CONFLICT) from exc
        except services.ObserverCannotPostError as exc:
            raise ProblemError(str(exc), "observer-read-only",
                               status.HTTP_403_FORBIDDEN) from exc

        return Response(_message_dict(message), status=status.HTTP_201_CREATED)


class RetractMessageView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=None, responses=dict)
    def post(self, request, thread_id, message_id):
        membership = _membership(request)
        thread, _participant = _thread_and_participant(thread_id, membership)

        message = thread.messages.filter(pk=message_id).first()
        if message is None:
            raise ProblemError("Message not found.", "message-not-found",
                               status.HTTP_404_NOT_FOUND)

        try:
            message = services.retract_message(message=message, actor=membership)
        except services.NotMessageAuthorError as exc:
            raise ProblemError(str(exc), "not-message-author",
                               status.HTTP_403_FORBIDDEN) from exc

        return Response(_message_dict(message))


# -- Inbox ------------------------------------------------------------------


class InboxView(APIView):
    """In-app notifications for the signed-in person."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=dict)
    def get(self, request):
        membership = _membership(request)
        notifications = (
            Notification.objects.filter(recipient=membership)
            .order_by("-created_at")[:100]
        )
        return Response({
            "results": [
                {
                    "id": str(n.id),
                    "topic": n.topic,
                    "title": n.title,
                    "body": n.body,
                    "importance": n.importance,
                    "read_at": n.read_at,
                    "created_at": n.created_at,
                }
                for n in notifications
            ],
            "unread_count": Notification.objects.filter(
                recipient=membership, read_at__isnull=True
            ).count(),
        })


class MarkNotificationReadView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=None, responses=dict)
    def post(self, request, notification_id):
        membership = _membership(request)
        notification = Notification.objects.filter(
            pk=notification_id, recipient=membership
        ).first()
        if notification is None:
            raise ProblemError("Notification not found.", "notification-not-found",
                               status.HTTP_404_NOT_FOUND)

        notification = services.mark_read(notification=notification)
        return Response({"id": str(notification.id), "read_at": notification.read_at})
