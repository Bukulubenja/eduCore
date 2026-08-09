"""Invite issuance and redemption (doc 05, "Authentication").

Covers the gap this feature closes: every account created by
`provision_school` or the bulk importers previously started `INVITED` with no
password and no way to ever set one. See `educore/core/tokens.py`
(`issue_invite`/`accept_invite`) and `educore/core/views.py`
(`AcceptInviteView`).
"""

from __future__ import annotations

import threading

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from educore.core import tokens
from educore.core.models import InviteToken, Membership, User
from educore.core.tenancy import TenantContext

pytestmark = pytest.mark.django_db

NEW_PASSWORD = "a-brand-new-strong-password-42"


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def invited(school_a):
    """A freshly-imported staff member: INVITED, unusable password."""
    user = User.objects.create_user(email="new-hire@example.com",
                                    full_name="New Hire", password=None)
    user.set_unusable_password()
    user.save(update_fields=["password"])
    with TenantContext.scope(school_a):
        membership = Membership.objects.create(
            school_id=school_a.id, user=user, status=Membership.Status.INVITED,
        )
    return membership


@pytest.fixture
def invite_raw(invited):
    raw, _token = tokens.issue_invite(invited)
    return raw


def accept(api, raw, password=NEW_PASSWORD):
    return api.post(reverse("v1:auth-accept-invite"),
                    {"token": raw, "password": password}, format="json")


# -- Happy path ----------------------------------------------------------


def test_accepting_a_valid_invite_activates_the_membership(api, invited,
                                                            invite_raw):
    response = accept(api, invite_raw)

    assert response.status_code == 200
    assert response.data["token_type"] == "Bearer"
    assert response.data["access_token"]
    assert response.data["refresh_token"]

    invited.refresh_from_db()
    assert invited.status == Membership.Status.ACTIVE
    invited.user.refresh_from_db()
    assert invited.user.has_usable_password()
    assert invited.user.check_password(NEW_PASSWORD)


def test_the_returned_tokens_actually_authenticate(api, invited, invite_raw):
    pair = accept(api, invite_raw).data

    api.credentials(HTTP_AUTHORIZATION=f"Bearer {pair['access_token']}")
    response = api.get(reverse("v1:me"))

    assert response.status_code == 200
    assert response.data["active_membership_id"] == str(invited.pk)


def test_activation_is_audited(api, invited, invite_raw):
    from educore.core.models import AuditEvent

    accept(api, invite_raw)

    with TenantContext.scope(invited.school_id):
        event = AuditEvent.objects.get(action="core.membership.activated")
    assert event.object_id == invited.pk


def test_a_now_active_member_can_log_in_normally_afterwards(api, invited,
                                                             invite_raw):
    accept(api, invite_raw)

    response = api.post(reverse("v1:auth-token"),
                        {"email": "new-hire@example.com",
                         "password": NEW_PASSWORD}, format="json")

    assert response.status_code == 200


# -- Rejections ------------------------------------------------------------


def test_an_expired_token_is_refused(api, invited):
    raw, token = tokens.issue_invite(invited)
    InviteToken.all_tenants.filter(pk=token.pk).update(
        expires_at=timezone.now() - timezone.timedelta(minutes=1)
    )

    response = accept(api, raw)

    assert response.status_code == 400
    assert response.data["type"].endswith("/invite-token-invalid")
    invited.refresh_from_db()
    assert invited.status == Membership.Status.INVITED


def test_an_already_consumed_token_cannot_be_redeemed_twice(api, invited,
                                                             invite_raw):
    first = accept(api, invite_raw)
    assert first.status_code == 200

    second = accept(api, invite_raw)

    assert second.status_code == 400
    assert second.data["type"].endswith("/invite-token-invalid")


def test_an_unknown_token_gives_the_identical_generic_message(api, invited,
                                                               invite_raw):
    """Distinguishing "never existed" from "expired"/"used" tells an attacker
    which links are live -- the same discipline TokenObtainView applies to
    login."""
    accept(api, invite_raw)   # consume it

    consumed = accept(api, invite_raw)
    bogus = accept(api, "not-a-real-token-at-all")

    assert consumed.status_code == bogus.status_code == 400
    assert consumed.data["detail"] == bogus.data["detail"]
    assert consumed.data["type"] == bogus.data["type"]


def test_a_weak_password_is_rejected_with_a_field_error(api, invited,
                                                         invite_raw):
    response = accept(api, invite_raw, password="short")

    assert response.status_code == 400
    assert any(e["field"] == "password" for e in response.data["errors"])
    invited.refresh_from_db()
    assert invited.status == Membership.Status.INVITED


def test_a_token_for_one_school_cannot_activate_a_membership_elsewhere(
    api, invited, invite_raw, school_b,
):
    """The token is bound to the Membership it was issued for; nothing about
    accepting it lets a caller redirect activation anywhere else."""
    response = accept(api, invite_raw)

    assert response.status_code == 200
    assert response.data["school_id"] == str(invited.school_id)
    assert response.data["school_id"] != str(school_b.id)


# -- Storage discipline ------------------------------------------------------


def test_invite_tokens_are_never_stored_in_the_clear(invited, invite_raw):
    stored = InviteToken.all_tenants.get(token_hash=tokens.hash_refresh(invite_raw))

    assert invite_raw not in stored.token_hash
    assert not InviteToken.all_tenants.filter(token_hash=invite_raw).exists()


# -- Pre-flight GET ----------------------------------------------------------


def test_get_reports_a_live_token_as_valid(api, invite_raw):
    response = api.get(reverse("v1:auth-accept-invite"), {"token": invite_raw})

    assert response.status_code == 200
    assert response.data["valid"] is True


def test_get_reports_an_expired_token_as_invalid(api, invited):
    raw, token = tokens.issue_invite(invited)
    InviteToken.all_tenants.filter(pk=token.pk).update(
        expires_at=timezone.now() - timezone.timedelta(minutes=1)
    )

    response = api.get(reverse("v1:auth-accept-invite"), {"token": raw})

    assert response.data["valid"] is False


def test_get_reports_a_bogus_token_as_invalid(api):
    response = api.get(reverse("v1:auth-accept-invite"), {"token": "nope"})

    assert response.data["valid"] is False


def test_get_with_no_token_is_invalid(api):
    response = api.get(reverse("v1:auth-accept-invite"))

    assert response.data["valid"] is False


# -- Concurrency --------------------------------------------------------


@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
def test_concurrent_redemption_of_the_same_token_lets_exactly_one_through(
    invited, invite_raw,
):
    """Two requests racing to redeem the identical invite link -- opened
    twice, or a leaked link raced against its real recipient -- must not
    both activate the account. Needs real concurrent connections (SQLite
    serialises writes globally), same rationale as the refresh-token
    concurrency test in test_auth_api.py.
    """
    from django.db import connections

    results = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def attempt():
        connections.close_all()
        barrier.wait()
        try:
            tokens.accept_invite(invite_raw, NEW_PASSWORD)
            outcome = "success"
        except tokens.InviteTokenError:
            outcome = "rejected"
        finally:
            connections.close_all()
        with lock:
            results.append(outcome)

    racers = [threading.Thread(target=attempt) for _ in range(2)]
    for racer in racers:
        racer.start()
    for racer in racers:
        racer.join()

    assert sorted(results) == ["rejected", "success"]
