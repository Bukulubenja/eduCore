"""Isolation layer 1: tenant resolution from the authenticated Membership."""

from __future__ import annotations

import pytest
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory

from educore.core.middleware import (
    ACTIVE_MEMBERSHIP_SESSION_KEY,
    TenantMiddleware,
)
from educore.core.models import Membership
from educore.core.tenancy import TenantContext

pytestmark = pytest.mark.django_db


def _request(user, session=None):
    request = RequestFactory().get("/api/v1/me")
    request.user = user
    request.session = session if session is not None else {}
    return request


def _run(request):
    """Run the middleware, capturing the tenant bound during the view call."""
    seen = {}

    def view(req):
        seen["school_id"] = TenantContext.get()
        seen["request_school_id"] = req.school_id
        seen["membership_id"] = req.membership_id
        return "ok"

    TenantMiddleware(view)(request)
    return seen


def test_anonymous_request_binds_no_tenant():
    assert _run(_request(AnonymousUser()))["school_id"] is None


def test_single_membership_is_bound_automatically(school_a, make_membership):
    membership = make_membership(school_a, email="teacher@example.com")
    seen = _run(_request(membership.user))

    assert seen["school_id"] == school_a.id
    assert seen["membership_id"] == membership.pk


def test_tenant_is_released_after_the_request(school_a, make_membership):
    membership = make_membership(school_a, email="teacher@example.com")
    _run(_request(membership.user))
    assert TenantContext.get() is None, "tenant leaked past the request"


def test_multiple_memberships_require_an_explicit_choice(
    school_a, school_b, make_membership
):
    """Never pick a school arbitrarily.

    A teacher at two schools who is silently bound to the wrong one will mark
    the wrong register, and the mistake is invisible to them.
    """
    membership = make_membership(school_a, email="dual@example.com")
    with TenantContext.scope(school_b):
        Membership.objects.create(school=school_b, user=membership.user,
                                  status=Membership.Status.ACTIVE)

    assert _run(_request(membership.user))["school_id"] is None


def test_explicit_selection_binds_the_chosen_membership(
    school_a, school_b, make_membership
):
    membership = make_membership(school_a, email="dual@example.com")
    with TenantContext.scope(school_b):
        other = Membership.objects.create(school=school_b, user=membership.user,
                                          status=Membership.Status.ACTIVE)

    session = {ACTIVE_MEMBERSHIP_SESSION_KEY: str(other.pk)}
    assert _run(_request(membership.user, session))["school_id"] == school_b.id


def test_selection_of_a_membership_you_do_not_hold_is_ignored(
    school_a, school_b, make_membership
):
    """The session is client-influenced; it may propose, never decide."""
    mine = make_membership(school_a, email="mine@example.com")
    theirs = make_membership(school_b, email="theirs@example.com")

    session = {ACTIVE_MEMBERSHIP_SESSION_KEY: str(theirs.pk)}
    seen = _run(_request(mine.user, session))

    assert seen["school_id"] == school_a.id
    assert seen["membership_id"] == mine.pk


def test_suspended_membership_binds_no_tenant(school_a, make_membership):
    membership = make_membership(school_a, email="gone@example.com")
    Membership.all_tenants.filter(pk=membership.pk).update(
        status=Membership.Status.SUSPENDED
    )
    assert _run(_request(membership.user))["school_id"] is None
