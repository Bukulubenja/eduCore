"""Assessment endpoints (doc 05, "Assessment")."""

from __future__ import annotations

from django.http import FileResponse
from django.utils import timezone
from drf_spectacular.utils import OpenApiTypes, extend_schema
from rest_framework import serializers, status
from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from educore.core import mfa
from educore.core.views import ProblemError
from educore.students.models import GuardianLink, Student

from . import services
from .models import Assessment, AssessmentState, ReportCard, Score

MODERATOR_ROLES = {"dos", "head_teacher", "director", "deputy"}
RELEASE_ROLES = {"head_teacher", "director"}


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


def _require_role(membership, allowed: set[str], what: str):
    if not (_roles(membership) & allowed):
        raise ProblemError(f"You may not {what}.", "not-permitted",
                           status.HTTP_403_FORBIDDEN)


class AssessmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Assessment
        fields = ["id", "course", "term", "title", "kind", "max_score", "weight",
                  "scheduled_for", "state", "author", "approved_by",
                  "moderated_by", "released_by", "released_at", "returned_note"]
        read_only_fields = ["state", "author", "approved_by", "moderated_by",
                            "released_by", "released_at", "returned_note"]


class ScoreItemSerializer(serializers.Serializer):
    student_id = serializers.UUIDField()
    raw_score = serializers.DecimalField(max_digits=6, decimal_places=2,
                                         required=False, allow_null=True)
    is_absent = serializers.BooleanField(default=False)
    comment = serializers.CharField(required=False, allow_blank=True,
                                    max_length=200)


class ScoresSerializer(serializers.Serializer):
    marks = ScoreItemSerializer(many=True)
    reason = serializers.CharField(required=False, allow_blank=True,
                                   max_length=200)


class TransitionSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    step_up_token = serializers.CharField(required=False, allow_blank=True)


class AssessmentListView(ListAPIView):
    serializer_class = AssessmentSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        membership = _membership(self.request)
        qs = Assessment.objects.select_related("course__subject", "term")
        if not (_roles(membership) & MODERATOR_ROLES):
            qs = qs.filter(author=membership)

        params = self.request.query_params
        for field in ("course_id", "term_id", "state"):
            if params.get(field):
                qs = qs.filter(**{field.replace("_id", ""): params[field]}
                               if field != "state" else {"state": params[field]})
        return qs


class AssessmentTransitionView(APIView):
    """One endpoint per transition, as sub-resources rather than a state field.

    Exposing `state` as a writable field would let a client jump the machine;
    naming each act keeps the permitted paths in one place and makes the audit
    trail read like the process it describes.
    """

    permission_classes = [IsAuthenticated]
    action: str = ""

    @extend_schema(request=TransitionSerializer, responses=AssessmentSerializer)
    def post(self, request, assessment_id):
        payload = TransitionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        membership = _membership(request)

        assessment = Assessment.objects.filter(pk=assessment_id).first()
        if assessment is None:
            raise ProblemError("Assessment not found.", "assessment-not-found",
                               status.HTTP_404_NOT_FOUND)

        note = payload.validated_data.get("note", "")
        try:
            assessment = self.perform(assessment, membership, note,
                                      payload.validated_data)
        except mfa.StepUpRequiredError as exc:
            raise ProblemError(str(exc), "step-up-required",
                               status.HTTP_403_FORBIDDEN) from exc
        except services.InvalidTransitionError as exc:
            raise ProblemError(str(exc), "invalid-transition",
                               status.HTTP_409_CONFLICT) from exc
        except services.AssessmentError as exc:
            raise ProblemError(str(exc), "transition-refused",
                               status.HTTP_422_UNPROCESSABLE_ENTITY) from exc

        return Response(AssessmentSerializer(assessment).data)

    def perform(self, assessment, membership, note, data):
        raise NotImplementedError


class SubmitView(AssessmentTransitionView):
    def perform(self, assessment, membership, note, data):
        if assessment.author_id != membership.pk:
            raise ProblemError("Assessment not found.", "assessment-not-found",
                               status.HTTP_404_NOT_FOUND)
        return services.submit(assessment, actor=membership)


class ApproveView(AssessmentTransitionView):
    def perform(self, assessment, membership, note, data):
        _require_role(membership, MODERATOR_ROLES, "approve assessments")
        return services.approve(assessment, actor=membership)


class ReturnView(AssessmentTransitionView):
    def perform(self, assessment, membership, note, data):
        _require_role(membership, MODERATOR_ROLES, "return assessments")
        return services.return_to_author(assessment, actor=membership, note=note)


class LockView(AssessmentTransitionView):
    def perform(self, assessment, membership, note, data):
        _require_role(membership, MODERATOR_ROLES, "lock assessments")
        return services.lock(assessment, actor=membership)


class BeginScoringView(AssessmentTransitionView):
    def perform(self, assessment, membership, note, data):
        _require_role(membership, MODERATOR_ROLES, "open scoring")
        return services.begin_scoring(assessment, actor=membership)


class SubmitMarksView(AssessmentTransitionView):
    def perform(self, assessment, membership, note, data):
        return services.submit_marks(assessment, actor=membership)


class ModerateView(AssessmentTransitionView):
    def perform(self, assessment, membership, note, data):
        _require_role(membership, MODERATOR_ROLES, "moderate assessments")
        return services.moderate(assessment, actor=membership)


class ReleaseView(AssessmentTransitionView):
    def perform(self, assessment, membership, note, data):
        _require_role(membership, RELEASE_ROLES, "release results")
        return services.release(assessment, actor=membership,
                                step_up_token=data.get("step_up_token", ""))


class ScoresView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=dict)
    def get(self, request, assessment_id):
        membership = _membership(request)
        assessment = Assessment.objects.filter(pk=assessment_id).first()
        if assessment is None:
            raise ProblemError("Assessment not found.", "assessment-not-found",
                               status.HTTP_404_NOT_FOUND)

        scores = (Score.objects.filter(assessment=assessment)
                  .select_related("student").order_by("student__full_name"))
        body = {
            "assessment_id": str(assessment.id),
            "state": assessment.state,
            "max_score": str(assessment.max_score),
            "scores": [
                {"student_id": str(s.student_id),
                 "full_name": s.student.full_name,
                 "raw_score": str(s.raw_score) if s.raw_score is not None else None,
                 "is_absent": s.is_absent, "percentage": s.percentage}
                for s in scores
            ],
        }
        if _roles(membership) & MODERATOR_ROLES:
            body["anomalies"] = services.flag_anomalous_grading(assessment)
        return Response(body)

    @extend_schema(request=ScoresSerializer, responses=dict)
    def put(self, request, assessment_id):
        payload = ScoresSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        membership = _membership(request)

        assessment = Assessment.objects.filter(pk=assessment_id).first()
        if assessment is None:
            raise ProblemError("Assessment not found.", "assessment-not-found",
                               status.HTTP_404_NOT_FOUND)

        try:
            changed = services.enter_scores(
                assessment, actor=membership,
                marks=payload.validated_data["marks"],
                reason=payload.validated_data.get("reason", ""),
            )
        except services.AssessmentError as exc:
            raise ProblemError(str(exc), "scores-refused",
                               status.HTTP_422_UNPROCESSABLE_ENTITY) from exc

        return Response({"assessment_id": str(assessment.id), "changed": changed})


class ReportCardListView(APIView):
    """Report cards a caller is entitled to see.

    A guardian's scope is the set of students they hold a *verified* link to.
    Showing a child's results to someone not entitled to them is a
    safeguarding incident, not a bug (doc 06, threat 5).
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=dict)
    def get(self, request):
        membership = _membership(request)
        roles = _roles(membership)

        qs = ReportCard.objects.select_related("student", "term", "class_group")
        if not (roles & MODERATOR_ROLES):
            ward_ids = GuardianLink.objects.filter(
                membership=membership, verified=True, has_portal_access=True
            ).values_list("student_id", flat=True)
            qs = qs.filter(student_id__in=list(ward_ids))

        if request.query_params.get("student_id"):
            qs = qs.filter(student_id=request.query_params["student_id"])
        if request.query_params.get("term_id"):
            qs = qs.filter(term_id=request.query_params["term_id"])

        return Response({"results": [
            {"id": str(r.id), "student_id": str(r.student_id),
             "student": r.student.full_name, "term": str(r.term),
             "class_group": r.class_group.name, "revision": r.revision,
             "issued_at": r.issued_at, "content": r.content,
             "supersedes": str(r.supersedes_id) if r.supersedes_id else None}
            for r in qs.order_by("-issued_at")[:100]
        ]})


class ReportCardDocumentView(APIView):
    """Streams the rendered PDF for one report card.

    Same eligibility rule as `ReportCardListView`: a guardian may only reach a
    document for a *verified* ward with portal access. A 404 rather than 403
    for an ineligible caller, so the response does not confirm the report
    card exists at all (doc 06, threat 5).
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(responses={(200, "application/pdf"): OpenApiTypes.BINARY})
    def get(self, request, report_card_id):
        membership = _membership(request)
        roles = _roles(membership)

        report = (ReportCard.objects.select_related("student")
                  .filter(pk=report_card_id).first())
        if report is None:
            raise ProblemError("Report card not found.", "report-card-not-found",
                               status.HTTP_404_NOT_FOUND)

        if not (roles & MODERATOR_ROLES):
            is_ward = GuardianLink.objects.filter(
                membership=membership, student_id=report.student_id,
                verified=True, has_portal_access=True,
            ).exists()
            if not is_ward:
                raise ProblemError("Report card not found.",
                                   "report-card-not-found",
                                   status.HTTP_404_NOT_FOUND)

        if not report.document:
            raise ProblemError("This report card has no rendered document.",
                               "document-not-available",
                               status.HTTP_404_NOT_FOUND)

        filename = (f"report-card-{report.student.admission_number}"
                   f"-r{report.revision}.pdf")
        return FileResponse(report.document.open("rb"),
                            content_type="application/pdf", filename=filename)


class GenerateReportCardsSerializer(serializers.Serializer):
    class_group_id = serializers.UUIDField()
    term_id = serializers.UUIDField()


class GenerateReportCardsView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=GenerateReportCardsSerializer, responses=dict)
    def post(self, request):
        payload = GenerateReportCardsSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        membership = _membership(request)
        _require_role(membership, MODERATOR_ROLES, "issue report cards")

        from educore.academics.models import ClassGroup, Term

        class_group = ClassGroup.objects.filter(
            pk=payload.validated_data["class_group_id"]
        ).first()
        term = Term.objects.filter(pk=payload.validated_data["term_id"]).first()
        if class_group is None or term is None:
            raise ProblemError("Class group or term not found.", "not-found",
                               status.HTTP_404_NOT_FOUND)

        reports = services.generate_for_class(class_group=class_group, term=term,
                                              actor=membership)
        return Response({"issued": len(reports),
                         "report_card_ids": [str(r.id) for r in reports]},
                        status=status.HTTP_201_CREATED)


class MyResultsView(APIView):
    """Released results for a student, or for a guardian's wards."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=dict)
    def get(self, request):
        membership = _membership(request)
        ward_ids = list(
            GuardianLink.objects
            .filter(membership=membership, verified=True, has_portal_access=True)
            .values_list("student_id", flat=True)
        )
        if not ward_ids:
            return Response({"results": []})

        scores = (
            Score.objects
            .filter(student_id__in=ward_ids,
                    assessment__state=AssessmentState.RELEASED)
            .select_related("assessment__course__subject", "student")
            .order_by("-assessment__released_at")
        )
        return Response({"results": [
            {"student_id": str(s.student_id), "student": s.student.full_name,
             "subject": s.assessment.course.subject.name,
             "assessment": s.assessment.title,
             "raw_score": str(s.raw_score) if s.raw_score is not None else None,
             "max_score": str(s.assessment.max_score),
             "percentage": s.percentage, "is_absent": s.is_absent}
            for s in scores[:200]
        ]})


class StudentLookupView(APIView):
    """Minimal student search for teachers building a register or mark sheet."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=dict)
    def get(self, request):
        _membership(request)
        qs = Student.objects.filter(status=Student.Status.ENROLLED)
        if request.query_params.get("class_group_id"):
            qs = qs.filter(
                enrolments__class_group_id=request.query_params["class_group_id"],
                enrolments__is_active=True,
            )
        if request.query_params.get("q"):
            qs = qs.filter(full_name__icontains=request.query_params["q"])

        return Response({"results": [
            {"id": str(s.id), "full_name": s.full_name,
             "admission_number": s.admission_number}
            for s in qs.distinct().order_by("full_name")[:100]
        ]})
