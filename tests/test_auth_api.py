"""Token issue, rotation, reuse detection, and school switching (doc 05)."""

from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from educore.core import tokens
from educore.core.models import Membership
from educore.core.tenancy import TenantContext

pytestmark = pytest.mark.django_db

PASSWORD = "correct-horse-battery"


@pytest.fixture
def api():
    return APIClient()


def obtain(api, email, password=PASSWORD, **extra):
    return api.post(reverse("v1:auth-token"),
                    {"email": email, "password": password, **extra},
                    format="json")


def test_login_returns_a_token_pair(api, teacher):
    response = obtain(api, "teacher@example.com")

    assert response.status_code == 200
    assert response.data["token_type"] == "Bearer"
    assert response.data["school_id"] == str(teacher.school_id)
    assert response.data["expires_in"] == 900


def test_wrong_password_is_indistinguishable_from_unknown_account(api, teacher):
    """Both answers must be identical, or the endpoint enumerates accounts."""
    wrong = obtain(api, "teacher@example.com", password="not-the-password")
    unknown = obtain(api, "nobody@example.com", password="anything-at-all")

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.data["detail"] == unknown.data["detail"]
    assert wrong.data["type"].endswith("/invalid-credentials")


def test_errors_use_problem_json(api, teacher):
    response = obtain(api, "teacher@example.com", password="wrong-password")

    assert response["Content-Type"].startswith("application/problem+json")
    assert set(response.data) >= {"type", "title", "status", "detail"}


def test_a_person_at_two_schools_must_choose(api, teacher, school_b):
    with TenantContext.scope(school_b):
        Membership.objects.create(school=school_b, user=teacher.user,
                                  status=Membership.Status.ACTIVE)

    response = obtain(api, "teacher@example.com")

    assert response.status_code == 300
    assert response.data["choose_membership"] is True
    assert len(response.data["memberships"]) == 2


def test_choosing_a_membership_scopes_the_token(api, teacher, school_b):
    with TenantContext.scope(school_b):
        other = Membership.objects.create(school=school_b, user=teacher.user,
                                          status=Membership.Status.ACTIVE)

    response = obtain(api, "teacher@example.com", membership_id=str(other.pk))

    assert response.status_code == 200
    assert response.data["school_id"] == str(school_b.id)


def test_a_membership_you_do_not_hold_is_not_found(api, teacher, school_b,
                                                   make_membership):
    theirs = make_membership(school_b, email="someone-else@example.com")

    response = obtain(api, "teacher@example.com", membership_id=str(theirs.pk))

    assert response.status_code == 404


def test_bearer_token_authenticates_and_binds_the_tenant(api, teacher):
    access = obtain(api, "teacher@example.com").data["access_token"]
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    response = api.get(reverse("v1:me"))

    assert response.status_code == 200
    assert response.data["active_school_id"] == str(teacher.school_id)
    assert response.data["active_membership_id"] == str(teacher.pk)


def test_no_token_is_unauthenticated(api):
    assert api.get(reverse("v1:me")).status_code == 401


def test_refresh_rotates_and_consumes_the_old_token(api, teacher):
    pair = obtain(api, "teacher@example.com").data

    refreshed = api.post(reverse("v1:auth-refresh"),
                         {"refresh_token": pair["refresh_token"]}, format="json")

    assert refreshed.status_code == 200
    assert refreshed.data["refresh_token"] != pair["refresh_token"]


def test_replaying_a_refresh_token_revokes_the_whole_family(api, teacher):
    """Theft becomes noisy rather than silent and indefinite.

    Both the attacker and the victim are locked out, which is the point: it
    gets reported, whereas a quietly cloned session does not.
    """
    pair = obtain(api, "teacher@example.com").data
    first = api.post(reverse("v1:auth-refresh"),
                     {"refresh_token": pair["refresh_token"]}, format="json")

    replayed = api.post(reverse("v1:auth-refresh"),
                        {"refresh_token": pair["refresh_token"]}, format="json")

    assert replayed.status_code == 401
    assert replayed.data["type"].endswith("/refresh-token-replayed")

    # The token minted by the legitimate rotation is dead too.
    followup = api.post(reverse("v1:auth-refresh"),
                        {"refresh_token": first.data["refresh_token"]},
                        format="json")
    assert followup.status_code == 401


def test_refresh_tokens_are_never_stored_in_the_clear(api, teacher):
    from educore.core.models import RefreshToken

    pair = obtain(api, "teacher@example.com").data
    raw = pair["refresh_token"]

    stored = RefreshToken.all_tenants.get(token_hash=tokens.hash_refresh(raw))

    assert raw not in stored.token_hash
    assert not RefreshToken.all_tenants.filter(token_hash=raw).exists()


def test_switching_school_issues_a_token_for_the_other_membership(api, teacher,
                                                                  school_b):
    with TenantContext.scope(school_b):
        other = Membership.objects.create(school=school_b, user=teacher.user,
                                          status=Membership.Status.ACTIVE)
    access = obtain(api, "teacher@example.com",
                    membership_id=str(teacher.pk)).data["access_token"]
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    response = api.post(reverse("v1:auth-switch-school"),
                        {"membership_id": str(other.pk)}, format="json")

    assert response.status_code == 200
    assert response.data["school_id"] == str(school_b.id)


def test_a_suspended_membership_cannot_use_its_access_token(api, teacher):
    """Revocation is immediate: authorisation reads the database, not the claim."""
    access = obtain(api, "teacher@example.com").data["access_token"]
    Membership.all_tenants.filter(pk=teacher.pk).update(
        status=Membership.Status.SUSPENDED
    )
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    assert api.get(reverse("v1:me")).status_code == 401


def test_a_tampered_token_is_rejected(api, teacher):
    access = obtain(api, "teacher@example.com").data["access_token"]
    header, payload, signature = access.split(".")
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {header}.{payload}.{signature[:-2]}xx")

    assert api.get(reverse("v1:me")).status_code == 401
