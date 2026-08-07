from __future__ import annotations

from datetime import time, timedelta

import pytest
from django.db import connection
from django.utils import timezone

from educore.core.models import Campus, Membership, Role, RoleAssignment, School, User
from educore.core.tenancy import TenantContext

from .conftest_phase2 import *  # noqa: F403  timetable/delivery/students fixtures


def pytest_collection_modifyitems(config, items):
    """Skip PostgreSQL-only tests on other engines, loudly."""
    if connection.vendor == "postgresql":
        return
    skip = pytest.mark.skip(
        reason=f"needs PostgreSQL; running on '{connection.vendor}'. "
               "Isolation layer 3 (RLS) is NOT verified in this run."
    )
    for item in items:
        if "postgres" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def school_a(db):
    return School.objects.create(name="Kampala High", slug="kampala-high",
                                 status=School.Status.ACTIVE)


@pytest.fixture
def school_b(db):
    return School.objects.create(name="Entebbe Girls", slug="entebbe-girls",
                                 status=School.Status.ACTIVE)


@pytest.fixture
def make_membership(db):
    def _make(school, *, email, name="Test Person"):
        user = User.objects.create_user(email=email, full_name=name,
                                        password="correct-horse-battery")
        with TenantContext.scope(school):
            return Membership.objects.create(
                school=school, user=user, status=Membership.Status.ACTIVE
            )

    return _make


@pytest.fixture
def make_role(db):
    def _make(school, code="teacher", name="Teacher", permissions=None):
        with TenantContext.scope(school):
            return Role.objects.create(
                school=school, code=code, name=name,
                permissions=permissions or [],
            )

    return _make


@pytest.fixture
def grant_role(db, make_role):
    def _grant(membership, code, name=None):
        with TenantContext.scope(membership.school_id):
            role = Role.objects.filter(code=code).first()
            if role is None:
                role = Role.objects.create(school_id=membership.school_id,
                                           code=code, name=name or code.title())
            return RoleAssignment.objects.create(
                school_id=membership.school_id, membership=membership, role=role
            )

    return _grant


@pytest.fixture
def tenant_a(school_a):
    with TenantContext.scope(school_a) as scope:
        yield scope


@pytest.fixture
def tenant_b(school_b):
    with TenantContext.scope(school_b) as scope:
        yield scope


# -- Presence fixtures -------------------------------------------------------


@pytest.fixture
def campus(db, school_a):
    with TenantContext.scope(school_a):
        return Campus.objects.create(
            school=school_a, name="Main Campus", is_primary=True,
            latitude="0.313600", longitude="32.581100",
            wifi_bssid_hashes=["b1946ac92492d2347c6235b4d2611184"],
            beacon_ids=["staffroom-01"],
        )


@pytest.fixture
def teacher(school_a, make_membership):
    return make_membership(school_a, email="teacher@example.com", name="A Teacher")


@pytest.fixture
def deputy(school_a, make_membership, grant_role):
    membership = make_membership(school_a, email="deputy@example.com",
                                 name="A Deputy")
    grant_role(membership, "deputy", "Deputy Head")
    return membership


@pytest.fixture
def policy(db, school_a):
    from educore.presence.services import active_policy

    with TenantContext.scope(school_a):
        created = active_policy(school_a.id)
        created.day_starts_at = time(7, 30)
        created.day_ends_at = time(17, 0)
        created.save()
        return created


@pytest.fixture
def morning(policy):
    """A plausible arrival: 07:20 local, inside the early-check-in window.

    Anchored to *yesterday* so that every time-of-day in a test -- an evening
    check-out included -- is unambiguously in the past whatever hour the suite
    happens to run at. Tests that depend on the wall clock fail at 6am and
    pass at noon, which is the worst kind of flake.
    """
    yesterday = timezone.localtime(timezone.now()) - timedelta(days=1)
    return yesterday.replace(hour=7, minute=20, second=0, microsecond=0)


@pytest.fixture
def good_signals(campus, teacher, db):
    """Evidence for a normal, honest arrival."""
    from educore.core.models import Device
    from educore.presence import qr
    from educore.presence.models import SignalType

    with TenantContext.scope(campus.school_id):
        Device.objects.create(
            school_id=campus.school_id, membership=teacher,
            fingerprint="device-fingerprint-teacher", platform="android",
            attested=True, status=Device.Status.ACTIVE,
        )

    def _build(at):
        # Minted at the moment of the scan, as a real display would.
        issued = qr.issue(school_id=campus.school_id, campus_id=campus.id,
                          ttl_seconds=30, at=at)
        return {
            SignalType.QR: {"token": issued["token"]},
            SignalType.DEVICE: {"fingerprint": "device-fingerprint-teacher",
                                "attestation": "ok"},
            SignalType.GEOFENCE: {"lat": 0.3136, "lon": 32.5811,
                                  "accuracy_m": 12},
            SignalType.WIFI: {"bssid_hash": "b1946ac92492d2347c6235b4d2611184"},
        }

    return _build
