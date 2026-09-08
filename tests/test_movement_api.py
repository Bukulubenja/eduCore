"""Movement endpoints: role gates, workflow transitions, cross-tenant 404s."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from educore.core.tenancy import TenantContext
from educore.movement.models import PassOut

pytestmark = pytest.mark.django_db

PASSWORD = "correct-horse-battery"


def client_for(membership) -> APIClient:
    api = APIClient()
    response = api.post(reverse("v1:auth-token"),
                        {"email": membership.user.email, "password": PASSWORD},
                        format="json")
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access_token']}")
    return api


@pytest.fixture
def office(school_a, make_membership, grant_role):
    member = make_membership(school_a, email="office@example.com", name="Office")
    grant_role(member, "class_teacher", "Class Teacher")
    return member


@pytest.fixture
def bursar(school_a, make_membership, grant_role):
    member = make_membership(school_a, email="bursar@example.com", name="Bursar")
    grant_role(member, "bursar", "Bursar")
    return member


@pytest.fixture
def head(school_a, make_membership, grant_role):
    member = make_membership(school_a, email="head@example.com", name="Head")
    grant_role(member, "head_teacher", "Head Teacher")
    return member


def _return_at():
    return (timezone.now() + timedelta(hours=6)).isoformat()


def test_request_a_pass_out_over_the_api(school_a, boarder, office):
    student, _ = boarder
    response = client_for(office).post(reverse("v1:movement:pass-outs"), {
        "student_id": str(student.id), "reason": "medical",
        "destination": "Clinic", "responsible_person": "Parent",
        "expected_return_at": _return_at(),
    }, format="json")

    assert response.status_code == 201
    assert response.data["status"] == "requested"


def test_a_plain_teacher_cannot_approve_a_pass_out(school_a, boarder, office,
                                                   teacher):
    student, _ = boarder
    with TenantContext.scope(school_a):
        pass_out = PassOut.objects.create(
            school_id=school_a.id, student=student, reason="other",
            destination="X", responsible_person="Y", requested_by=office,
            expected_return_at=timezone.now() + timedelta(hours=3),
        )

    response = client_for(teacher).post(
        reverse("v1:movement:pass-out-decision", args=[pass_out.id]),
        {"approved": True}, format="json",
    )
    assert response.status_code == 403


def test_the_bursar_can_approve_a_pass_out(school_a, boarder, office, bursar):
    student, _ = boarder
    with TenantContext.scope(school_a):
        pass_out = PassOut.objects.create(
            school_id=school_a.id, student=student, reason="other",
            destination="X", responsible_person="Y", requested_by=office,
            expected_return_at=timezone.now() + timedelta(hours=3),
        )

    response = client_for(bursar).post(
        reverse("v1:movement:pass-out-decision", args=[pass_out.id]),
        {"approved": True}, format="json",
    )
    assert response.status_code == 200
    assert response.data["status"] == "approved"


def test_unknown_pass_out_is_a_404(school_a, office):
    import uuid
    response = client_for(office).get(
        reverse("v1:movement:pass-out-detail", args=[uuid.uuid4()])
    )
    assert response.status_code == 404


def test_trip_lifecycle_over_the_api(school_a, students, teacher, head):
    api = client_for(teacher)
    created = api.post(reverse("v1:movement:trips"), {
        "title": "Museum", "destination": "Museum",
        "departs_at": (timezone.now() + timedelta(days=1)).isoformat(),
        "returns_at": (timezone.now() + timedelta(days=1, hours=5)).isoformat(),
    }, format="json")
    assert created.status_code == 201
    trip_id = created.data["id"]

    roster = api.put(reverse("v1:movement:trip-roster", args=[trip_id]), {
        "student_ids": [str(students[0].id)],
        "supervisor_ids": [str(teacher.pk)],
    }, format="json")
    assert roster.status_code == 200

    submitted = api.post(reverse("v1:movement:trip-submit", args=[trip_id]))
    assert submitted.status_code == 200
    assert submitted.data["status"] == "submitted"

    # A teacher may not approve.
    denied = api.post(reverse("v1:movement:trip-decision", args=[trip_id]),
                      {"approved": True}, format="json")
    assert denied.status_code == 403

    approved = client_for(head).post(
        reverse("v1:movement:trip-decision", args=[trip_id]),
        {"approved": True}, format="json",
    )
    assert approved.status_code == 200
    assert approved.data["status"] == "approved"


def test_eligible_students_lists_the_enrolled_roster(school_a, students, teacher,
                                                     class_group):
    response = client_for(teacher).get(
        reverse("v1:movement:eligible-students"),
        {"class_group": str(class_group.pk)},
    )
    assert response.status_code == 200
    assert len(response.data["results"]) == len(students)


def test_pass_out_departure_return_and_cancel_over_the_api(school_a, boarder,
                                                          office, bursar):
    student, _ = boarder
    api = client_for(bursar)
    with TenantContext.scope(school_a):
        approved = PassOut.objects.create(
            school_id=school_a.id, student=student, reason="family",
            destination="Home", responsible_person="Parent", requested_by=office,
            status=PassOut.Status.APPROVED, approved_by=bursar,
            expected_return_at=timezone.now() + timedelta(hours=5),
        )

    departed = api.post(
        reverse("v1:movement:pass-out-departure", args=[approved.id]), {},
        format="json")
    assert departed.status_code == 200 and departed.data["status"] == "departed"

    returned = api.post(
        reverse("v1:movement:pass-out-return", args=[approved.id]), {},
        format="json")
    assert returned.status_code == 200 and returned.data["status"] == "returned"

    # A returned pass-out can no longer be cancelled.
    cancelled = api.post(
        reverse("v1:movement:pass-out-cancel", args=[approved.id]), {},
        format="json")
    assert cancelled.status_code == 409


def test_trip_detail_and_roster_validation_over_the_api(school_a, students,
                                                        teacher):
    import uuid
    api = client_for(teacher)
    created = api.post(reverse("v1:movement:trips"), {
        "title": "Zoo", "destination": "Zoo",
        "departs_at": (timezone.now() + timedelta(days=1)).isoformat(),
        "returns_at": (timezone.now() + timedelta(days=1, hours=5)).isoformat(),
    }, format="json")
    trip_id = created.data["id"]

    bad = api.put(reverse("v1:movement:trip-roster", args=[trip_id]), {
        "student_ids": [str(uuid.uuid4())], "supervisor_ids": [str(teacher.pk)],
    }, format="json")
    assert bad.status_code == 409

    detail = api.get(reverse("v1:movement:trip-detail", args=[trip_id]))
    assert detail.status_code == 200
    assert detail.data["participants"] == []


def test_eligible_students_without_a_filter_lists_everyone(school_a, students,
                                                          teacher):
    response = client_for(teacher).get(reverse("v1:movement:eligible-students"))
    assert response.status_code == 200
    assert len(response.data["results"]) >= len(students)


def test_trip_departure_return_and_cancel_over_the_api(school_a, students,
                                                       teacher, head):
    api = client_for(teacher)
    created = api.post(reverse("v1:movement:trips"), {
        "title": "Farm", "destination": "Farm",
        "departs_at": (timezone.now() + timedelta(days=1)).isoformat(),
        "returns_at": (timezone.now() + timedelta(days=1, hours=5)).isoformat(),
    }, format="json")
    trip_id = created.data["id"]
    api.put(reverse("v1:movement:trip-roster", args=[trip_id]), {
        "student_ids": [str(students[0].id)], "supervisor_ids": [str(teacher.pk)],
    }, format="json")

    # Submitting before a roster is impossible here; departure before approval.
    early = api.post(reverse("v1:movement:trip-departure", args=[trip_id]), {},
                     format="json")
    assert early.status_code == 409

    api.post(reverse("v1:movement:trip-submit", args=[trip_id]))
    client_for(head).post(reverse("v1:movement:trip-decision", args=[trip_id]),
                          {"approved": True}, format="json")

    departed = api.post(reverse("v1:movement:trip-departure", args=[trip_id]), {},
                        format="json")
    assert departed.status_code == 200 and departed.data["status"] == "departed"

    returned = api.post(reverse("v1:movement:trip-return", args=[trip_id]), {},
                        format="json")
    assert returned.status_code == 200 and returned.data["status"] == "returned"

    cancelled = api.post(reverse("v1:movement:trip-cancel", args=[trip_id]), {},
                         format="json")
    assert cancelled.status_code == 409


def test_off_campus_endpoint_reports_departed_students(school_a, boarder, office,
                                                       bursar):
    student, _ = boarder
    with TenantContext.scope(school_a):
        pass_out = PassOut.objects.create(
            school_id=school_a.id, student=student, reason="medical",
            destination="Clinic", responsible_person="Parent",
            requested_by=office, status=PassOut.Status.DEPARTED,
            departed_at=timezone.now(),
            expected_return_at=timezone.now() + timedelta(hours=2),
        )

    response = client_for(bursar).get(reverse("v1:movement:off-campus"))
    assert response.status_code == 200
    assert response.data["results"][0]["student_id"] == str(student.id)
    assert pass_out.status == "departed"
