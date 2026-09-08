"""Student movement endpoints (doc 08).

Approval actions are role-gated the same way `delivery` gates its coverage
view: the nav shows every link to everyone, and the endpoint decides what a
role may actually *do*. A pass-out is approved by leadership or the bursar
(SSOMS §8); a trip, by leadership only.
"""

from __future__ import annotations

from django.db.models import Count
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from educore.core.views import ProblemError
from educore.students.models import Student

from . import services
from .models import PassOut, Trip, TripParticipant

LEADERSHIP_ROLES = {"director", "head_teacher", "deputy", "dos"}
PASS_OUT_APPROVER_ROLES = LEADERSHIP_ROLES | {"bursar"}


def _membership(request):
    membership = getattr(request, "membership", None)
    if membership is None:
        raise ProblemError("No active school membership for this session.",
                           "no-active-membership", status.HTTP_403_FORBIDDEN)
    return membership


def _roles(membership) -> set[str]:
    today = timezone.localdate()
    return {ra.role.code for ra in membership.role_assignments.select_related("role")
            if ra.is_valid_on(today)}


def _require(membership, allowed: set[str]) -> None:
    if not (_roles(membership) & allowed):
        raise ProblemError("You may not approve movement requests.",
                           "not-permitted", status.HTTP_403_FORBIDDEN)


def _get_pass_out(pass_out_id) -> PassOut:
    pass_out = (PassOut.objects.select_related("student")
                .filter(pk=pass_out_id).first())
    if pass_out is None:
        raise ProblemError("Pass-out not found.", "pass-out-not-found",
                           status.HTTP_404_NOT_FOUND)
    return pass_out


def _get_trip(trip_id) -> Trip:
    trip = Trip.objects.filter(pk=trip_id).first()
    if trip is None:
        raise ProblemError("Trip not found.", "trip-not-found",
                           status.HTTP_404_NOT_FOUND)
    return trip


def _domain(exc: services.MovementError):
    return ProblemError(str(exc), "movement-not-permitted",
                        status.HTTP_409_CONFLICT)


# -- Serializers -----------------------------------------------------------


class PassOutRequestSerializer(serializers.Serializer):
    student_id = serializers.UUIDField()
    reason = serializers.ChoiceField(choices=PassOut.Reason.choices)
    destination = serializers.CharField(max_length=200)
    responsible_person = serializers.CharField(max_length=200)
    expected_return_at = serializers.DateTimeField()
    narrative = serializers.CharField(required=False, allow_blank=True)


class MovementDecisionSerializer(serializers.Serializer):
    approved = serializers.BooleanField()
    note = serializers.CharField(required=False, allow_blank=True)


class MovementTimestampSerializer(serializers.Serializer):
    at = serializers.DateTimeField(required=False)


class MovementNoteSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True)


class PassOutSerializer(serializers.ModelSerializer):
    is_overdue = serializers.SerializerMethodField()

    class Meta:
        model = PassOut
        fields = ["id", "student", "reason", "destination", "responsible_person",
                  "narrative", "requested_by", "expected_return_at", "status",
                  "approved_by", "decided_at", "decision_note", "departed_at",
                  "returned_at", "is_overdue", "created_at"]

    def get_is_overdue(self, obj) -> bool:
        return obj.is_overdue()


class TripCreateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=200)
    purpose = serializers.CharField(required=False, allow_blank=True)
    destination = serializers.CharField(max_length=200)
    departs_at = serializers.DateTimeField()
    returns_at = serializers.DateTimeField()


class TripRosterSerializer(serializers.Serializer):
    student_ids = serializers.ListField(child=serializers.UUIDField())
    supervisor_ids = serializers.ListField(child=serializers.UUIDField())
    lead_id = serializers.UUIDField(required=False, allow_null=True)


class TripDepartureSerializer(serializers.Serializer):
    at = serializers.DateTimeField(required=False)
    present_student_ids = serializers.ListField(
        child=serializers.UUIDField(), required=False)


class TripReturnSerializer(serializers.Serializer):
    at = serializers.DateTimeField(required=False)
    returned_student_ids = serializers.ListField(
        child=serializers.UUIDField(), required=False)


class TripSerializer(serializers.ModelSerializer):
    participant_count = serializers.SerializerMethodField()
    supervisor_count = serializers.SerializerMethodField()

    class Meta:
        model = Trip
        fields = ["id", "title", "purpose", "destination", "departs_at",
                  "returns_at", "created_by", "status", "approved_by",
                  "decided_at", "decision_note", "actual_departed_at",
                  "actual_returned_at", "participant_count", "supervisor_count",
                  "created_at"]

    def get_participant_count(self, obj) -> int:
        # Annotated by the list view; falls back to a query for a lone object.
        if (value := getattr(obj, "roster_size", None)) is not None:
            return value
        return obj.participants.count()

    def get_supervisor_count(self, obj) -> int:
        if (value := getattr(obj, "supervisor_size", None)) is not None:
            return value
        return obj.supervisors.count()


# -- Pass-out views ------------------------------------------------------------


class PassOutListView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=PassOutRequestSerializer, responses=PassOutSerializer)
    def post(self, request):
        payload = PassOutRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        membership = _membership(request)

        student = Student.objects.filter(pk=data["student_id"]).first()
        if student is None:
            raise ProblemError("Student not found.", "student-not-found",
                               status.HTTP_404_NOT_FOUND)

        try:
            pass_out = services.request_pass_out(
                student=student, requested_by=membership, reason=data["reason"],
                destination=data["destination"],
                responsible_person=data["responsible_person"],
                expected_return_at=data["expected_return_at"],
                narrative=data.get("narrative", ""),
            )
        except services.MovementError as exc:
            raise _domain(exc) from exc

        return Response(PassOutSerializer(pass_out).data,
                        status=status.HTTP_201_CREATED)

    @extend_schema(responses=PassOutSerializer(many=True))
    def get(self, request):
        _membership(request)
        queryset = PassOut.objects.select_related("student").all()

        status_filter = request.query_params.get("status")
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        if request.query_params.get("open") == "true":
            queryset = queryset.filter(status__in=services.OPEN_PASS_OUT_STATES)
        if request.query_params.get("overdue") == "true":
            queryset = queryset.filter(status=PassOut.Status.DEPARTED,
                                       expected_return_at__lt=timezone.now())

        return Response(PassOutSerializer(queryset, many=True).data)


class PassOutDetailView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=PassOutSerializer)
    def get(self, request, pass_out_id):
        _membership(request)
        return Response(PassOutSerializer(_get_pass_out(pass_out_id)).data)


class PassOutDecisionView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=MovementDecisionSerializer, responses=PassOutSerializer)
    def post(self, request, pass_out_id):
        payload = MovementDecisionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        membership = _membership(request)
        _require(membership, PASS_OUT_APPROVER_ROLES)

        try:
            pass_out = services.decide_pass_out(
                pass_out=_get_pass_out(pass_out_id), decided_by=membership,
                approved=payload.validated_data["approved"],
                note=payload.validated_data.get("note", ""),
            )
        except services.MovementError as exc:
            raise _domain(exc) from exc
        return Response(PassOutSerializer(pass_out).data)


class PassOutDepartureView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=MovementTimestampSerializer, responses=PassOutSerializer)
    def post(self, request, pass_out_id):
        payload = MovementTimestampSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        _membership(request)
        try:
            pass_out = services.record_departure(
                pass_out=_get_pass_out(pass_out_id),
                at=payload.validated_data.get("at"),
            )
        except services.MovementError as exc:
            raise _domain(exc) from exc
        return Response(PassOutSerializer(pass_out).data)


class PassOutReturnView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=MovementTimestampSerializer, responses=PassOutSerializer)
    def post(self, request, pass_out_id):
        payload = MovementTimestampSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        _membership(request)
        try:
            pass_out = services.record_return(
                pass_out=_get_pass_out(pass_out_id),
                at=payload.validated_data.get("at"),
            )
        except services.MovementError as exc:
            raise _domain(exc) from exc
        return Response(PassOutSerializer(pass_out).data)


class PassOutCancelView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=MovementNoteSerializer, responses=PassOutSerializer)
    def post(self, request, pass_out_id):
        payload = MovementNoteSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        membership = _membership(request)
        try:
            pass_out = services.cancel_pass_out(
                pass_out=_get_pass_out(pass_out_id), actor=membership,
                note=payload.validated_data.get("note", ""),
            )
        except services.MovementError as exc:
            raise _domain(exc) from exc
        return Response(PassOutSerializer(pass_out).data)


# -- Trip views -------------------------------------------------------------


class TripListView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=TripCreateSerializer, responses=TripSerializer)
    def post(self, request):
        payload = TripCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        membership = _membership(request)
        data = payload.validated_data
        try:
            trip = services.create_trip(
                created_by=membership, title=data["title"],
                purpose=data.get("purpose", ""), destination=data["destination"],
                departs_at=data["departs_at"], returns_at=data["returns_at"],
            )
        except services.MovementError as exc:
            raise _domain(exc) from exc
        return Response(TripSerializer(trip).data, status=status.HTTP_201_CREATED)

    @extend_schema(responses=TripSerializer(many=True))
    def get(self, request):
        _membership(request)
        queryset = Trip.objects.annotate(
            roster_size=Count("participants", distinct=True),
            supervisor_size=Count("supervisors", distinct=True),
        )
        status_filter = request.query_params.get("status")
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        return Response(TripSerializer(queryset, many=True).data)


class TripDetailView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=dict)
    def get(self, request, trip_id):
        _membership(request)
        trip = _get_trip(trip_id)
        participants = (TripParticipant.objects
                        .filter(trip=trip).select_related("student"))
        return Response({
            **TripSerializer(trip).data,
            "participants": [
                {"student_id": str(p.student_id),
                 "student_name": p.student.full_name,
                 "departure_recorded_at": p.departure_recorded_at,
                 "return_recorded_at": p.return_recorded_at}
                for p in participants
            ],
            "supervisors": [
                {"membership_id": str(s.membership_id), "is_lead": s.is_lead}
                for s in trip.supervisors.all()
            ],
        })


class TripRosterView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=TripRosterSerializer, responses=TripSerializer)
    def put(self, request, trip_id):
        payload = TripRosterSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        _membership(request)
        data = payload.validated_data
        try:
            trip = services.set_trip_roster(
                trip=_get_trip(trip_id), student_ids=data["student_ids"],
                supervisor_ids=data["supervisor_ids"],
                lead_id=data.get("lead_id"),
            )
        except services.MovementError as exc:
            raise _domain(exc) from exc
        return Response(TripSerializer(trip).data)


class TripSubmitView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=None, responses=TripSerializer)
    def post(self, request, trip_id):
        membership = _membership(request)
        try:
            trip = services.submit_trip(trip=_get_trip(trip_id), actor=membership)
        except services.MovementError as exc:
            raise _domain(exc) from exc
        return Response(TripSerializer(trip).data)


class TripDecisionView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=MovementDecisionSerializer, responses=TripSerializer)
    def post(self, request, trip_id):
        payload = MovementDecisionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        membership = _membership(request)
        _require(membership, LEADERSHIP_ROLES)
        try:
            trip = services.decide_trip(
                trip=_get_trip(trip_id), decided_by=membership,
                approved=payload.validated_data["approved"],
                note=payload.validated_data.get("note", ""),
            )
        except services.MovementError as exc:
            raise _domain(exc) from exc
        return Response(TripSerializer(trip).data)


class TripDepartureView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=TripDepartureSerializer, responses=TripSerializer)
    def post(self, request, trip_id):
        payload = TripDepartureSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        _membership(request)
        try:
            trip = services.record_trip_departure(
                trip=_get_trip(trip_id), at=payload.validated_data.get("at"),
                present_student_ids=payload.validated_data.get("present_student_ids"),
            )
        except services.MovementError as exc:
            raise _domain(exc) from exc
        return Response(TripSerializer(trip).data)


class TripReturnView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=TripReturnSerializer, responses=TripSerializer)
    def post(self, request, trip_id):
        payload = TripReturnSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        _membership(request)
        try:
            trip = services.record_trip_return(
                trip=_get_trip(trip_id), at=payload.validated_data.get("at"),
                returned_student_ids=payload.validated_data.get("returned_student_ids"),
            )
        except services.MovementError as exc:
            raise _domain(exc) from exc
        return Response(TripSerializer(trip).data)


class TripCancelView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=MovementNoteSerializer, responses=TripSerializer)
    def post(self, request, trip_id):
        payload = MovementNoteSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        membership = _membership(request)
        try:
            trip = services.cancel_trip(
                trip=_get_trip(trip_id), actor=membership,
                note=payload.validated_data.get("note", ""),
            )
        except services.MovementError as exc:
            raise _domain(exc) from exc
        return Response(TripSerializer(trip).data)


class EligibleStudentsView(APIView):
    """Roster picker: enrolled students, optionally filtered by class group."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=dict)
    def get(self, request):
        _membership(request)
        queryset = (Student.objects
                    .filter(status=Student.Status.ENROLLED)
                    .order_by("full_name"))

        class_group = request.query_params.get("class_group")
        if class_group:
            queryset = queryset.filter(
                enrolments__class_group_id=class_group,
                enrolments__is_active=True,
            ).distinct()

        query = request.query_params.get("q")
        if query:
            queryset = queryset.filter(full_name__icontains=query)

        return Response({
            "results": [
                {"id": str(s.id), "full_name": s.full_name,
                 "admission_number": s.admission_number}
                for s in queryset[:200]
            ],
        })


class StudentsOffCampusView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=dict)
    def get(self, request):
        _membership(request)
        return Response({"results": services.students_off_campus()})
