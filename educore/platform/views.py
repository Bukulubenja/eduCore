"""Onboarding endpoints.

The operator-facing half of this module (provisioning, suspension, billing)
deliberately has no school-scoped API. It is reachable only from the operator
portal, which doc 06 puts on a separate hostname behind an IP allowlist and
hardware MFA. What lives here is what a *school's* own administrator needs to
get started: bulk import, with validation.
"""

from __future__ import annotations

from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from educore.academics.models import Term
from educore.core.views import ProblemError

from . import importers
from .models import ImportBatch

ADMIN_ROLES = {"director", "head_teacher", "ict_admin"}
MAX_UPLOAD_BYTES = 5 * 1024 * 1024


def _require_admin(request):
    membership = getattr(request, "membership", None)
    if membership is None:
        raise ProblemError("No active school membership for this session.",
                           "no-active-membership", status.HTTP_403_FORBIDDEN)
    today = timezone.localdate()
    roles = {ra.role.code for ra in membership.role_assignments.select_related("role")
             if ra.is_valid_on(today)}
    if not (roles & ADMIN_ROLES):
        raise ProblemError("You may not import data.", "not-permitted",
                           status.HTTP_403_FORBIDDEN)
    return membership


class ImportSerializer(serializers.Serializer):
    file = serializers.FileField()
    # Defaults to a dry run. Applying is the deliberate second step, so an
    # operator always sees the errors before anything is written.
    apply = serializers.BooleanField(default=False)
    term_id = serializers.UUIDField(required=False)


class BulkImportView(APIView):
    """Validate a CSV, and apply it only when asked and only if it is clean."""

    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]
    kind: str = ""

    @extend_schema(request=ImportSerializer, responses=dict)
    def post(self, request):
        payload = ImportSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        membership = _require_admin(request)

        upload = payload.validated_data["file"]
        if upload.size > MAX_UPLOAD_BYTES:
            raise ProblemError("That file is too large.", "file-too-large",
                               status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)

        content = upload.read()
        dry_run = not payload.validated_data["apply"]

        result = self.run(content, membership, dry_run,
                          payload.validated_data.get("term_id"))

        body = {
            "kind": result.kind,
            "dry_run": dry_run,
            "applied": result.applied,
            "rows_total": result.rows_total,
            "rows_ok": result.rows_ok,
            "rows_failed": len(result.errors),
            "errors": result.errors[:200],
        }
        if result.errors and not dry_run:
            body["detail"] = ("Nothing was imported. Fix the rows listed and "
                              "upload again.")
        return Response(body, status=status.HTTP_200_OK if result.ok
                        else status.HTTP_422_UNPROCESSABLE_ENTITY)

    def run(self, content, membership, dry_run, term_id):
        raise NotImplementedError


class StaffImportView(BulkImportView):
    kind = ImportBatch.Kind.STAFF

    def run(self, content, membership, dry_run, term_id):
        return importers.import_staff(content, school_id=membership.school_id,
                                      actor=membership, dry_run=dry_run)


class StudentImportView(BulkImportView):
    kind = ImportBatch.Kind.STUDENTS

    def run(self, content, membership, dry_run, term_id):
        term = (Term.objects.filter(pk=term_id).first() if term_id
                else Term.objects.filter(is_current=True).first())
        if term is None:
            raise ProblemError("No current term to enrol students into.",
                               "term-not-found", status.HTTP_404_NOT_FOUND)
        return importers.import_students(content, school_id=membership.school_id,
                                         term=term, actor=membership,
                                         dry_run=dry_run)


class GuardianImportView(BulkImportView):
    kind = ImportBatch.Kind.GUARDIANS

    def run(self, content, membership, dry_run, term_id):
        return importers.import_guardians(content,
                                          school_id=membership.school_id,
                                          actor=membership, dry_run=dry_run)


class ImportHistoryView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=dict)
    def get(self, request):
        _require_admin(request)
        batches = ImportBatch.objects.all()[:50]
        return Response({"results": [
            {"id": str(b.id), "kind": b.kind, "status": b.status,
             "rows_total": b.rows_total, "rows_ok": b.rows_ok,
             "rows_failed": b.rows_failed, "created_at": b.created_at,
             "errors": b.errors[:20]}
            for b in batches
        ]})
