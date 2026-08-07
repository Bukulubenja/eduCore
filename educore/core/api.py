"""API conventions: RFC 9457 errors and cursor pagination (ADR-0006)."""

from __future__ import annotations

from django.http import Http404
from rest_framework import exceptions, pagination
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

PROBLEM_BASE = "https://docs.educore.app/errors"

_TYPE_BY_STATUS = {
    400: "invalid-request",
    401: "unauthenticated",
    403: "forbidden",
    404: "not-found",
    405: "method-not-allowed",
    409: "conflict",
    415: "unsupported-media-type",
    429: "rate-limited",
    500: "internal-error",
}


class CursorPagination(pagination.CursorPagination):
    """Cursor paging only.

    Offset paging degrades sharply on high-volume, high-insert-rate tables
    such as attendance -- it produces duplicates and gaps while rows are
    being inserted, which for attendance is continuously. Partitioning those
    tables (docs/07-delivery-plan.md Phase 4, docs/partitioning-plan.md) will
    make deep offset pages even worse, not better, so cursor-only pagination
    is chosen ahead of that rollout, not because of it.
    """

    page_size = 50
    max_page_size = 200
    page_size_query_param = "limit"
    cursor_query_param = "cursor"
    ordering = "-created_at"

    def get_paginated_response(self, data):
        return Response({
            "results": data,
            "next_cursor": self.get_next_link(),
            "has_more": self.has_next,
        })


def exception_handler(exc, context):
    """Render every error as application/problem+json."""
    if isinstance(exc, Http404):
        exc = exceptions.NotFound()

    response = drf_exception_handler(exc, context)
    if response is None:
        return None                      # Unhandled: let it 500 and reach Sentry.

    request = context.get("request")
    status = response.status_code
    slug = getattr(exc, "problem_type", None) or _TYPE_BY_STATUS.get(status, "error")

    detail = response.data
    errors = []
    if isinstance(detail, dict) and "detail" not in detail:
        # Serializer errors: {"field": ["msg", ...]}
        for field, messages in detail.items():
            for message in (messages if isinstance(messages, list) else [messages]):
                errors.append({
                    "field": field,
                    "code": getattr(message, "code", "invalid"),
                    "detail": str(message),
                })
        summary = "One or more fields failed validation."
    else:
        summary = str(detail.get("detail")) if isinstance(detail, dict) else str(detail)

    response.data = {
        "type": f"{PROBLEM_BASE}/{slug}",
        "title": _TITLES.get(status, "Request failed"),
        "status": status,
        "detail": summary,
        "instance": request.path if request is not None else None,
        "request_id": getattr(request, "request_id", "") if request is not None else "",
    }
    if errors:
        response.data["errors"] = errors

    response.content_type = "application/problem+json"
    return response


_TITLES = {
    400: "Invalid request",
    401: "Authentication required",
    403: "Not permitted",
    404: "Not found",
    405: "Method not allowed",
    409: "Conflict",
    415: "Unsupported media type",
    429: "Too many requests",
    500: "Internal error",
}
