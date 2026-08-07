"""Dashboard endpoints (doc 05, "Insights")."""

from __future__ import annotations

from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from educore.academics.models import StaffProfile, Term
from educore.core.views import ProblemError

from . import services

LEADERSHIP_ROLES = {"director", "head_teacher", "deputy", "dos"}
PASTORAL_ROLES = LEADERSHIP_ROLES | {"hod", "class_teacher"}
# A head of department may also see coverage -- scoped to their own
# department, unlike the leadership roles' school-wide access.
COVERAGE_ROLES = LEADERSHIP_ROLES | {"hod"}


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


def _require(request, allowed: set[str]):
    membership = _membership(request)
    if not (_roles(membership) & allowed):
        raise ProblemError("You may not view this.", "not-permitted",
                           status.HTTP_403_FORBIDDEN)
    return membership


def _resolve_department_id(request, membership):
    """Optional `department_id` scope for the coverage dashboard.

    Leadership roles may filter by any department or leave it unset for a
    school-wide view. A head of department is pinned to their own -- they may
    omit `department_id` (it is inferred from `StaffProfile`) but may not
    widen the view by passing a different one.
    """
    requested = request.query_params.get("department_id")
    roles = _roles(membership)
    if roles & LEADERSHIP_ROLES:
        return requested or None

    own_department_id = (
        StaffProfile.objects.filter(membership=membership)
        .values_list("department_id", flat=True).first()
    )
    if own_department_id is None:
        raise ProblemError("You are not assigned to a department.",
                           "no-department", status.HTTP_403_FORBIDDEN)
    if requested and str(requested) != str(own_department_id):
        raise ProblemError("You may not view another department's coverage.",
                           "not-permitted", status.HTTP_403_FORBIDDEN)
    return str(own_department_id)


def _current_term(request) -> Term:
    term_id = request.query_params.get("term_id")
    term = (Term.objects.filter(pk=term_id).first() if term_id
            else Term.objects.filter(is_current=True).first())
    if term is None:
        raise ProblemError("No current term.", "term-not-found",
                           status.HTTP_404_NOT_FOUND)
    return term


class TodayView(APIView):
    """Is the school running? The one screen a director opens at 09:30."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=dict)
    def get(self, request):
        _require(request, LEADERSHIP_ROLES)
        return Response(services.today(on=request.query_params.get("date")))


class PunctualityView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=dict)
    def get(self, request):
        _require(request, LEADERSHIP_ROLES)
        return Response({"results": services.punctuality(
            since=request.query_params.get("from"),
            until=request.query_params.get("to"),
        )})


class WorkloadView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=dict)
    def get(self, request):
        _require(request, LEADERSHIP_ROLES)
        return Response({"results": services.workload(
            since=request.query_params.get("from"),
            until=request.query_params.get("to"),
        )})


class AtRiskStudentsView(APIView):
    """Attendance and attainment signals, kept separate rather than fused.

    A composite risk score nobody can decompose is a score nobody can act on,
    and a pastoral conversation needs to know which thing is wrong.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=dict)
    def get(self, request):
        _require(request, PASTORAL_ROLES)
        return Response({"results": services.at_risk_students(
            term=_current_term(request),
        )})


class CoverageOverviewView(APIView):
    """Leadership's school-wide lead, or one HOD's department view."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=dict)
    def get(self, request):
        membership = _require(request, COVERAGE_ROLES)
        department_id = _resolve_department_id(request, membership)
        return Response(services.coverage_overview(
            term=_current_term(request), department_id=department_id
        ))
