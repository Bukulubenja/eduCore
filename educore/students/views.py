"""Student register and gate endpoints."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from educore.core.models import Campus
from educore.core.views import ProblemError
from educore.timetable.models import LessonInstance

from . import services
from .models import GateEvent, Student, StudentAttendanceStatus


def _membership(request):
    membership = getattr(request, "membership", None)
    if membership is None:
        raise ProblemError("No active school membership for this session.",
                           "no-active-membership", status.HTTP_403_FORBIDDEN)
    return membership


class RegisterExceptionSerializer(serializers.Serializer):
    student_id = serializers.UUIDField()
    status = serializers.ChoiceField(choices=StudentAttendanceStatus.choices,
                                     default=StudentAttendanceStatus.ABSENT)
    note = serializers.CharField(required=False, allow_blank=True, max_length=200)


class SubmitRegisterSerializer(serializers.Serializer):
    exceptions = RegisterExceptionSerializer(many=True)


class GateEventSerializer(serializers.Serializer):
    client_event_id = serializers.UUIDField()
    scan_code = serializers.CharField()
    campus_id = serializers.UUIDField()
    direction = serializers.ChoiceField(choices=GateEvent.Direction.choices)
    occurred_at = serializers.DateTimeField()
    method = serializers.ChoiceField(choices=GateEvent.Method.choices,
                                     default=GateEvent.Method.QR)


class GateBatchSerializer(serializers.Serializer):
    events = GateEventSerializer(many=True)


def _lesson_for(request, lesson_id) -> LessonInstance:
    instance = (LessonInstance.objects
                .filter(pk=lesson_id)
                .select_related("class_group")
                .first())
    if instance is None:
        raise ProblemError("Lesson not found.", "lesson-not-found",
                           status.HTTP_404_NOT_FOUND)
    return instance


class RegisterView(APIView):
    """The roster, pre-marked present; and the exceptions coming back."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=dict)
    def get(self, request, lesson_id):
        _membership(request)
        instance = _lesson_for(request, lesson_id)
        roster = services.build_register(lesson_instance=instance)
        return Response({
            "lesson_instance_id": str(instance.id),
            "class_group": instance.class_group.name,
            "date": str(instance.date),
            "students": [
                {"student_id": str(row.student_id), "full_name": row.full_name,
                 "admission_number": row.admission_number, "status": row.status}
                for row in roster
            ],
        })

    @extend_schema(request=SubmitRegisterSerializer, responses=dict)
    def put(self, request, lesson_id):
        payload = SubmitRegisterSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        membership = _membership(request)
        instance = _lesson_for(request, lesson_id)

        try:
            marked = services.submit_register(
                lesson_instance=instance, marked_by=membership,
                exceptions=payload.validated_data["exceptions"],
            )
        except services.RegisterError as exc:
            raise ProblemError(str(exc), "student-not-in-class",
                               status.HTTP_422_UNPROCESSABLE_ENTITY) from exc

        return Response({"lesson_instance_id": str(instance.id),
                         "marked": marked,
                         "exceptions": len(payload.validated_data["exceptions"])})


class GateBatchView(APIView):
    """Batched scans from a gate device. Per-event results, like /v1/sync."""

    permission_classes = [IsAuthenticated]

    @extend_schema(request=GateBatchSerializer, responses=dict)
    def post(self, request):
        payload = GateBatchSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        _membership(request)

        results = []
        for event in payload.validated_data["events"]:
            student = Student.objects.filter(
                scan_code=event["scan_code"]
            ).first()
            campus = Campus.objects.filter(pk=event["campus_id"]).first()

            if student is None or campus is None:
                results.append({
                    "client_event_id": str(event["client_event_id"]),
                    "status": "rejected",
                    "error": {"code": "unknown_student" if student is None
                              else "unknown_campus"},
                })
                continue

            recorded = services.record_gate_event(
                student=student, campus=campus, direction=event["direction"],
                occurred_at=event["occurred_at"],
                client_event_id=event["client_event_id"],
                method=event["method"],
            )
            results.append({
                "client_event_id": str(event["client_event_id"]),
                "status": "accepted",
                "resource": {"type": "gate_event", "id": str(recorded.id),
                             "student_id": str(student.id)},
            })

        return Response({"results": results})


class StudentAttendanceView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=dict)
    def get(self, request, student_id):
        _membership(request)
        student = Student.objects.filter(pk=student_id).first()
        if student is None:
            raise ProblemError("Student not found.", "student-not-found",
                               status.HTTP_404_NOT_FOUND)

        return Response(services.attendance_summary(
            student=student,
            since=request.query_params.get("from"),
            until=request.query_params.get("to"),
        ))
