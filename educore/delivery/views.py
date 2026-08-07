"""Lesson delivery endpoints (doc 05, "Lesson delivery")."""

from __future__ import annotations

from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from educore.academics.models import Term
from educore.core import signed_tokens
from educore.core.views import ProblemError
from educore.timetable.models import LessonInstance, Room

from . import services
from .models import CoverageEntry, LessonSession, SchemeOfWork


def _membership(request):
    membership = getattr(request, "membership", None)
    if membership is None:
        raise ProblemError("No active school membership for this session.",
                           "no-active-membership", status.HTTP_403_FORBIDDEN)
    return membership


class OpenSessionSerializer(serializers.Serializer):
    room_id = serializers.UUIDField()
    token = serializers.CharField()
    captured_at = serializers.DateTimeField(required=False)


class CoverageItemSerializer(serializers.Serializer):
    unit_id = serializers.UUIDField()
    completion = serializers.ChoiceField(choices=CoverageEntry.Completion.choices,
                                         default=CoverageEntry.Completion.PARTIAL)
    homework_set = serializers.CharField(required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True)


class CloseSessionSerializer(serializers.Serializer):
    coverage = CoverageItemSerializer(many=True)
    notes = serializers.CharField(required=False, allow_blank=True)


class SessionSerializer(serializers.ModelSerializer):
    duration_minutes = serializers.IntegerField(read_only=True)

    class Meta:
        model = LessonSession
        fields = ["id", "lesson_instance", "actual_teacher", "room",
                  "opened_at", "closed_at", "room_changed",
                  "late_start_minutes", "verification_confidence",
                  "duration_minutes", "notes"]


class RoomTokenView(APIView):
    """Rotating code for a classroom display."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=dict)
    def get(self, request, room_id):
        membership = _membership(request)
        room = Room.objects.filter(pk=room_id).first()
        if room is None:
            raise ProblemError("Room not found.", "room-not-found",
                               status.HTTP_404_NOT_FOUND)

        issued = services.issue_room_token(school_id=membership.school_id,
                                           room=room)
        return Response({
            "token": issued["token"],
            "room_id": str(room.id),
            "expires_at": issued["expires_at"],
            "refresh_after_seconds": issued["refresh_after_seconds"],
        })


class OpenSessionView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=OpenSessionSerializer, responses=SessionSerializer)
    def post(self, request):
        payload = OpenSessionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        membership = _membership(request)

        room = Room.objects.filter(pk=data["room_id"]).first()
        if room is None:
            raise ProblemError("Room not found.", "room-not-found",
                               status.HTTP_404_NOT_FOUND)

        try:
            session = services.open_session(
                teacher=membership, room=room, token=data["token"],
                at=data.get("captured_at"),
            )
        except services.NoScheduledLessonError as exc:
            raise ProblemError(str(exc), "no-scheduled-lesson",
                               status.HTTP_409_CONFLICT) from exc
        except signed_tokens.SignedTokenError as exc:
            raise ProblemError(str(exc), "invalid-lesson-code",
                               status.HTTP_422_UNPROCESSABLE_ENTITY) from exc
        except services.DeliveryError as exc:
            raise ProblemError(str(exc), "session-not-openable",
                               status.HTTP_409_CONFLICT) from exc

        return Response(SessionSerializer(session).data,
                        status=status.HTTP_201_CREATED)


class CloseSessionView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=CloseSessionSerializer, responses=SessionSerializer)
    def post(self, request, session_id):
        payload = CloseSessionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        membership = _membership(request)

        session = LessonSession.objects.filter(pk=session_id).first()
        if session is None:
            raise ProblemError("Session not found.", "session-not-found",
                               status.HTTP_404_NOT_FOUND)
        if session.actual_teacher_id != membership.pk:
            raise ProblemError("Session not found.", "session-not-found",
                               status.HTTP_404_NOT_FOUND)

        try:
            session = services.close_session(
                session=session,
                coverage=payload.validated_data["coverage"],
                notes=payload.validated_data.get("notes", ""),
            )
        except services.DeliveryError as exc:
            raise ProblemError(str(exc), "session-not-closable",
                               status.HTTP_422_UNPROCESSABLE_ENTITY) from exc

        return Response(SessionSerializer(session).data)


class MyDayView(APIView):
    """A teacher's lessons for a date, with delivery state. The app's home screen."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=dict)
    def get(self, request):
        membership = _membership(request)
        day = request.query_params.get("date") or timezone.localdate()

        instances = (
            LessonInstance.objects
            .filter(date=day)
            .filter(expected_teacher=membership)
            .select_related("course__subject", "class_group", "expected_room")
            .order_by("scheduled_start_at")
        )
        return Response({
            "date": str(day),
            "lessons": [
                {
                    "id": str(i.id),
                    "subject": i.course.subject.name,
                    "class_group": i.class_group.name,
                    "room": i.expected_room.name,
                    "starts_at": i.scheduled_start_at,
                    "ends_at": i.scheduled_end_at,
                    "status": i.status,
                    "session_id": (str(i.session.id) if hasattr(i, "session")
                                   else None),
                }
                for i in instances
            ],
        })


class CoverageView(APIView):
    """Coverage and pace. The number a DOS acts on in week 9, not week 13."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=dict)
    def get(self, request):
        _membership(request)
        term_id = request.query_params.get("term_id")
        term = (Term.objects.filter(pk=term_id).first() if term_id
                else Term.objects.filter(is_current=True).first())
        if term is None:
            raise ProblemError("No current term.", "term-not-found",
                               status.HTTP_404_NOT_FOUND)

        scheme_id = request.query_params.get("scheme_id")
        group_id = request.query_params.get("class_group_id")

        if scheme_id:
            scheme = SchemeOfWork.objects.filter(pk=scheme_id).first()
            if scheme is None:
                raise ProblemError("Scheme not found.", "scheme-not-found",
                                   status.HTTP_404_NOT_FOUND)
            reports = [services.coverage_report(scheme=scheme,
                                                class_group=group_id)]
        else:
            reports = services.schemes_behind(term=term)

        return Response({
            "term_id": str(term.id),
            "results": [
                {
                    "scheme_id": str(r.scheme_id),
                    "class_group_id": str(r.class_group_id) if r.class_group_id
                    else None,
                    "coverage": r.coverage,
                    "expected": r.expected,
                    "pace": r.pace,
                    "is_behind": r.is_behind,
                    "periods_elapsed": r.periods_elapsed,
                    "total_periods": r.total_periods,
                    "units_completed": r.units_completed,
                    "units_total": r.units_total,
                }
                for r in reports
            ],
        })
