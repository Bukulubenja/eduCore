"""Tenant resolution (isolation layer 1)."""

from __future__ import annotations

import contextlib
import uuid

from django.db import connection, transaction

from .db import set_tenant
from .tenancy import TenantContext

ACTIVE_MEMBERSHIP_SESSION_KEY = "active_membership_id"


def membership_from_session(request, user):
    """Pick the Membership a session-authenticated request acts under."""
    from .models import Membership

    memberships = Membership.all_tenants.filter(
        user=user, status=Membership.Status.ACTIVE
    )

    session = getattr(request, "session", None)
    if session is not None:
        selected = session.get(ACTIVE_MEMBERSHIP_SESSION_KEY)
        if selected:
            with contextlib.suppress(Membership.DoesNotExist, ValueError):
                return memberships.get(pk=selected)

    # Exactly one active membership is the common case and needs no choice.
    # With several, the client must switch explicitly rather than have one
    # picked arbitrarily: a teacher silently bound to the wrong school marks
    # the wrong register, and the mistake is invisible to them.
    found = list(memberships[:2])
    return found[0] if len(found) == 1 else None


class TenantMiddleware:
    """Binds the request's tenant to the execution context and the connection.

    The tenant is derived from the authenticated Membership. It is never read
    from a header, a query parameter or a path segment: a client that cannot
    express another school's identity cannot ask for its data.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        school_id, membership_id = self._resolve(request)
        request.school_id = school_id
        request.membership_id = membership_id

        if school_id is None:
            return self.get_response(request)

        with TenantContext.scope(school_id):
            # SET LOCAL requires a transaction; middleware runs outside the
            # one ATOMIC_REQUESTS opens around the view, so we open our own.
            # It also gives the GUC the same lifetime as the request's work,
            # which is what keeps connection pooling safe (see db.set_tenant).
            if connection.vendor == "postgresql":
                with transaction.atomic():
                    set_tenant(connection, school_id)
                    return self.get_response(request)
            return self.get_response(request)

    @staticmethod
    def _resolve(request) -> tuple[uuid.UUID | None, uuid.UUID | None]:
        from .authentication import resolve_membership

        membership, _claims = resolve_membership(request)
        if membership is None:
            return None, None
        return membership.school_id, membership.pk
