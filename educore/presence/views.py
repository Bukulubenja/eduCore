"""Presence endpoints (doc 05, "Presence" and "Offline sync protocol")."""

from __future__ import annotations

from datetime import timedelta

from django.db import IntegrityError
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from educore.core.models import Campus, Membership
from educore.core.views import ProblemError

from . import qr, services
from .models import AttendanceEvent, AttendanceException, AttendanceRecord, Disposition
from .serializers import (
    AppealSerializer,
    CheckInSerializer,
    DecisionSerializer,
    EventSerializer,
    ExceptionSerializer,
    PolicySerializer,
    RecordSerializer,
    SyncSerializer,
)

REVIEW_ROLES = {"director", "head_teacher", "deputy", "dos", "hod", "ict_admin"}
# Narrower than REVIEW_ROLES: a deputy can decide appeals but should not be
# able to move the thresholds that decide everyone's disposition.
POLICY_ROLES = {"director", "head_teacher", "dos"}


def _membership(request) -> Membership:
    membership = getattr(request, "membership", None)
    if membership is None:
        raise ProblemError("No active school membership for this session.",
                           "no-active-membership", status.HTTP_403_FORBIDDEN)
    return membership


def _roles(membership) -> set[str]:
    today = timezone.localdate()
    return {ra.role.code for ra in membership.role_assignments.select_related("role")
            if ra.is_valid_on(today)}


def _may_review(membership) -> bool:
    return bool(_roles(membership) & REVIEW_ROLES)


class CheckInView(APIView):
    """Record a check-in.

    Note what this endpoint does *not* do: refuse. Weak evidence produces a
    `provisional` disposition and a review task, and the teacher gets on with
    their day (ADR-0002).
    """

    permission_classes = [IsAuthenticated]
    kind = AttendanceEvent.Kind.CHECK_IN

    @extend_schema(request=CheckInSerializer, responses=dict)
    def post(self, request):
        payload = CheckInSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data

        membership = _membership(request)
        campus = Campus.objects.filter(pk=data["campus_id"]).first()
        if campus is None:
            raise ProblemError("Campus not found.", "campus-not-found",
                               status.HTTP_404_NOT_FOUND)

        try:
            outcome = services.submit_event(
                membership=membership,
                campus=campus,
                kind=self.kind,
                captured_at=data["captured_at"],
                client_event_id=data["client_event_id"],
                raw_signals=data["signals"],
            )
        except services.DuplicateEventConflictError as exc:
            raise ProblemError(str(exc), "duplicate-event",
                               status.HTTP_409_CONFLICT) from exc
        except services.PresenceError as exc:
            raise ProblemError(str(exc), "event-not-acceptable",
                               status.HTTP_422_UNPROCESSABLE_ENTITY) from exc

        return Response(
            {
                "event": EventSerializer(outcome.event).data,
                "record": RecordSerializer(outcome.record).data,
                "server_time": timezone.now(),
                "clock_skew_seconds": outcome.event.clock_skew_seconds,
            },
            status=status.HTTP_201_CREATED if outcome.created else status.HTTP_200_OK,
        )


class CheckOutView(CheckInView):
    kind = AttendanceEvent.Kind.CHECK_OUT


class CampusListView(APIView):
    """Every campus the caller's school has, for the check-in client to pick
    from.

    Presence used to resolve a caller's campus once, silently, to the
    school's primary campus (`resolve_campus_id` in `console/src/lib/checkin
    /campus.ts`), and cache that choice in the browser indefinitely. A staff
    member at a second campus had no way to correct that and no indication
    it was even wrong -- they would simply score as absent, every day,
    against a fence they were never inside (doc 01, principle 6: symmetric
    visibility). Letting the client list campuses and show which one is
    selected is the fix; there is deliberately no per-staff "home campus" on
    the data model to get out of sync with reality -- a person chooses where
    they are, same as the system already lets them submit a check-in from
    anywhere within the fence.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=dict)
    def get(self, request):
        membership = _membership(request)
        campuses = Campus.objects.filter(school_id=membership.school_id).order_by(
            "-is_primary", "name"
        )
        return Response({"results": [
            {"id": str(c.id), "name": c.name, "is_primary": c.is_primary}
            for c in campuses
        ]})


class QrTokenView(APIView):
    """Issue a rotating code for a display device in the staff room."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=dict)
    def get(self, request):
        membership = _membership(request)
        campus_id = request.query_params.get("campus_id")
        campus = (Campus.objects.filter(pk=campus_id).first() if campus_id
                  else Campus.objects.filter(is_primary=True).first())
        if campus is None:
            raise ProblemError("Campus not found.", "campus-not-found",
                               status.HTTP_404_NOT_FOUND)

        policy = services.active_policy(membership.school_id)
        issued = qr.issue(school_id=membership.school_id, campus_id=campus.id,
                          ttl_seconds=policy.qr_token_ttl_seconds)
        return Response({
            "token": issued["token"],
            "campus_id": str(campus.id),
            "expires_at": issued["expires_at"],
            "refresh_after_seconds": issued["refresh_after_seconds"],
        })


class MyAttendanceView(ListAPIView):
    """A member of staff's own record, with the evidence behind it.

    Symmetric visibility is a design requirement, not a courtesy: a workforce
    that cannot see what the system says about them will work around it
    (doc 01, principle 6).
    """

    serializer_class = RecordSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        membership = _membership(self.request)
        since = self.request.query_params.get("from")
        until = self.request.query_params.get("to")
        qs = AttendanceRecord.objects.filter(membership=membership)
        if since:
            qs = qs.filter(date__gte=since)
        if until:
            qs = qs.filter(date__lte=until)
        return qs.order_by("-date")


class AttendanceRecordListView(ListAPIView):
    serializer_class = RecordSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        membership = _membership(self.request)
        qs = AttendanceRecord.objects.select_related("membership")
        if not _may_review(membership):
            qs = qs.filter(membership=membership)

        params = self.request.query_params
        if params.get("membership_id"):
            qs = qs.filter(membership_id=params["membership_id"])
        if params.get("from"):
            qs = qs.filter(date__gte=params["from"])
        if params.get("to"):
            qs = qs.filter(date__lte=params["to"])
        if params.get("status"):
            qs = qs.filter(status=params["status"])
        return qs.order_by("-date")


class AppealView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=AppealSerializer, responses=ExceptionSerializer)
    def post(self, request, record_id):
        payload = AppealSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        membership = _membership(request)

        record = AttendanceRecord.objects.filter(pk=record_id).first()
        if record is None:
            raise ProblemError("Record not found.", "record-not-found",
                               status.HTTP_404_NOT_FOUND)
        if record.membership_id != membership.pk and not _may_review(membership):
            raise ProblemError("Record not found.", "record-not-found",
                               status.HTTP_404_NOT_FOUND)

        try:
            exception = services.raise_appeal(
                record=record, raised_by=membership,
                reason_code=payload.validated_data["reason_code"],
                narrative=payload.validated_data.get("narrative", ""),
                requested_status=payload.validated_data.get("requested_status", ""),
            )
        except services.PresenceError as exc:
            raise ProblemError(str(exc), "appeal-already-open",
                               status.HTTP_409_CONFLICT) from exc

        return Response(ExceptionSerializer(exception).data,
                        status=status.HTTP_201_CREATED)


class ReviewQueueView(ListAPIView):
    """Everything awaiting a human: provisional records and open appeals."""

    serializer_class = ExceptionSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        membership = _membership(self.request)
        if not _may_review(membership):
            return AttendanceException.objects.none()
        return AttendanceException.objects.filter(
            decision=AttendanceException.Decision.PENDING
        ).order_by("created_at")

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        membership = _membership(request)
        if _may_review(membership):
            response.data["provisional_records"] = RecordSerializer(
                AttendanceRecord.objects.filter(
                    disposition=Disposition.PROVISIONAL,
                    resolution=AttendanceRecord.Resolution.AUTO,
                ).order_by("-date")[:100],
                many=True,
            ).data
        return response


class DecideAppealView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=DecisionSerializer, responses=ExceptionSerializer)
    def post(self, request, exception_id):
        payload = DecisionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        membership = _membership(request)

        if not _may_review(membership):
            raise ProblemError("You may not decide attendance appeals.",
                               "not-permitted", status.HTTP_403_FORBIDDEN)

        exception = AttendanceException.objects.filter(pk=exception_id).first()
        if exception is None:
            raise ProblemError("Appeal not found.", "appeal-not-found",
                               status.HTTP_404_NOT_FOUND)

        try:
            decided = services.decide_appeal(
                exception=exception, decided_by=membership,
                decision=payload.validated_data["decision"],
                note=payload.validated_data.get("note", ""),
            )
        except services.PresenceError as exc:
            raise ProblemError(str(exc), "appeal-already-decided",
                               status.HTTP_409_CONFLICT) from exc

        return Response(ExceptionSerializer(decided).data)


class HealthMetricsView(APIView):
    """Whether this subsystem can currently be trusted (ADR-0002)."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=dict)
    def get(self, request):
        membership = _membership(request)
        if not _may_review(membership):
            raise ProblemError("Not permitted.", "not-permitted",
                               status.HTTP_403_FORBIDDEN)

        until = timezone.localdate()
        since = until - timedelta(days=int(request.query_params.get("days", 30)))
        return Response(services.health_metrics(since=since, until=until))


class PolicyView(APIView):
    """The school's attendance-confidence thresholds (doc 07 phase 1).

    A policy is never edited in place (see `AttendancePolicy` and
    `services.revise_policy`), so `POST` here always produces a new version
    rather than mutating the one every existing record cites.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=PolicySerializer)
    def get(self, request):
        membership = _membership(request)
        policy = services.active_policy(membership.school_id)
        return Response(PolicySerializer(policy).data)

    @extend_schema(request=PolicySerializer, responses=PolicySerializer)
    def post(self, request):
        membership = _membership(request)
        if not (_roles(membership) & POLICY_ROLES):
            raise ProblemError("You may not change the attendance policy.",
                               "not-permitted", status.HTTP_403_FORBIDDEN)

        payload = PolicySerializer(data=request.data, partial=True)
        payload.is_valid(raise_exception=True)
        changes = payload.validated_data
        if not changes:
            raise ProblemError("No changes were supplied.", "no-changes",
                               status.HTTP_400_BAD_REQUEST)

        try:
            revised = services.revise_policy(**changes)
        except IntegrityError as exc:
            raise ProblemError(
                "accept_threshold must be greater than review_threshold.",
                "invalid-policy-thresholds",
                status.HTTP_422_UNPROCESSABLE_ENTITY,
            ) from exc

        return Response(PolicySerializer(revised).data)

    put = post


class SyncView(APIView):
    """Batched offline upload.

    Results are per operation. One bad record must never fail the batch, or a
    single malformed event blocks a device's entire queue until it is evicted
    (ADR-0003).
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(request=SyncSerializer, responses=dict)
    def post(self, request):
        payload = SyncSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        membership = _membership(request)

        results = []
        for operation in payload.validated_data["operations"]:
            results.append(self._apply(membership, operation))

        server_time = timezone.now()
        device_time = payload.validated_data.get("device_time")
        return Response({
            "server_time": server_time,
            "clock_skew_seconds": (int((server_time - device_time).total_seconds())
                                   if device_time else None),
            "results": results,
        })

    @staticmethod
    def _apply(membership, operation) -> dict:
        body = operation["payload"]
        kind = (AttendanceEvent.Kind.CHECK_IN
                if operation["type"] == "attendance.check_in"
                else AttendanceEvent.Kind.CHECK_OUT)

        campus = Campus.objects.filter(pk=body.get("campus_id")).first()
        if campus is None:
            return {
                "client_event_id": str(operation["client_event_id"]),
                "status": "rejected",
                "error": {"type": "campus-not-found", "code": "campus_not_found"},
            }

        try:
            outcome = services.submit_event(
                membership=membership,
                campus=campus,
                kind=kind,
                captured_at=operation["captured_at"],
                client_event_id=operation["client_event_id"],
                raw_signals=body.get("signals", {}),
            )
        except services.DuplicateEventConflictError as exc:
            return {
                "client_event_id": str(operation["client_event_id"]),
                "status": "rejected",
                "error": {"type": "duplicate-event", "code": "replayed",
                          "detail": str(exc)},
            }
        except services.PresenceError as exc:
            return {
                "client_event_id": str(operation["client_event_id"]),
                "status": "rejected",
                "error": {"type": "event-not-acceptable", "code": "not_acceptable",
                          "detail": str(exc)},
            }

        return {
            "client_event_id": str(operation["client_event_id"]),
            "status": "accepted" if outcome.created else "duplicate",
            "resource": {
                "type": "attendance_record",
                "id": str(outcome.record.id),
                "disposition": outcome.event.disposition,
                "confidence": outcome.event.confidence,
                "status": outcome.record.status,
            },
        }
