"""Bearer-token authentication, shared with the tenant middleware.

Ordering problem worth naming: middleware runs before DRF authenticates, but
the tenant must be bound before the view executes. Both paths therefore call
`resolve_membership()`, so there is exactly one place that decides who the
caller is. The middleware uses it to bind the tenant; DRF uses it to populate
request.user.
"""

from __future__ import annotations

from rest_framework import authentication, exceptions

from .tokens import TokenError, decode_access

# A suspended or closed school's members cannot authenticate. Suspension is
# reversible and destroys nothing (see platform.services.suspend_school); this
# is the gate that makes it mean something.
_BLOCKED_SCHOOL_STATUSES = ("suspended", "closed")


def _bearer(request) -> str:
    header = request.META.get("HTTP_AUTHORIZATION", "")
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def resolve_membership(request):
    """Return (membership, claims) for the caller, or (None, None).

    Never raises: an unauthenticated request is a normal condition, and the
    middleware must not turn a bad token into a 500 before DRF has had the
    chance to return a proper 401.
    """
    from .models import Membership

    token = _bearer(request)
    if token:
        try:
            claims = decode_access(token)
        except TokenError:
            return None, None
        membership = (
            Membership.all_tenants
            .select_related("user", "school")
            .filter(pk=claims.get("mbr"), status=Membership.Status.ACTIVE)
            .exclude(school__status__in=_BLOCKED_SCHOOL_STATUSES)
            .first()
        )
        if membership is None or not membership.user.is_active:
            return None, None
        return membership, claims

    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        from .middleware import membership_from_session
        return membership_from_session(request, user), None

    return None, None


class BearerTokenAuthentication(authentication.BaseAuthentication):
    """DRF authentication for access tokens."""

    keyword = "Bearer"

    def authenticate(self, request):
        token = _bearer(request)
        if not token:
            return None

        try:
            claims = decode_access(token)
        except TokenError as exc:
            raise exceptions.AuthenticationFailed(str(exc)) from exc

        from .models import Membership

        membership = (
            Membership.all_tenants
            .select_related("user")
            .filter(pk=claims.get("mbr"), status=Membership.Status.ACTIVE)
            .first()
        )
        if membership is None:
            raise exceptions.AuthenticationFailed("membership is not active")
        if not membership.user.is_active:
            raise exceptions.AuthenticationFailed("user account is disabled")

        request.membership = membership
        return membership.user, claims

    def authenticate_header(self, request):
        return self.keyword
