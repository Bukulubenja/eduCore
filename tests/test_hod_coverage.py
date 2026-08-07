"""Department-scoped coverage: a head of department sees only their own
subject's pace, while the DOS/leadership keep the school-wide (or
any-department) view (doc 07 Phase 2, "HOD department view")."""

from __future__ import annotations

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from educore.academics.models import Course, Department, StaffProfile, Subject
from educore.core.tenancy import TenantContext
from educore.delivery import services as delivery_services
from educore.delivery.models import SchemeOfWork, SyllabusUnit
from educore.insights import services as insights_services
from educore.timetable import services as timetable_services

from .conftest_phase2 import TERM_START

pytestmark = pytest.mark.django_db

PASSWORD = "correct-horse-battery"


def client_for(membership) -> APIClient:
    api = APIClient()
    response = api.post(reverse("v1:auth-token"),
                        {"email": membership.user.email, "password": PASSWORD},
                        format="json")
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access_token']}")
    return api


# -- Two departments, each behind on its own scheme ---------------------------


@pytest.fixture
def science(school_a):
    with TenantContext.scope(school_a):
        return Department.objects.create(school_id=school_a.id, name="Science",
                                         code="SCI")


@pytest.fixture
def arts(school_a):
    with TenantContext.scope(school_a):
        return Department.objects.create(school_id=school_a.id, name="Arts",
                                         code="ART")


@pytest.fixture
def science_course(school_a, level, academic_year, science):
    with TenantContext.scope(school_a):
        subject = Subject.objects.create(school_id=school_a.id, name="Physics",
                                         code="PHY-H", department=science)
        return Course.objects.create(school_id=school_a.id, subject=subject,
                                     level=level, academic_year=academic_year,
                                     periods_per_week=4)


@pytest.fixture
def arts_course(school_a, level, academic_year, arts):
    with TenantContext.scope(school_a):
        subject = Subject.objects.create(school_id=school_a.id, name="Literature",
                                         code="LIT-H", department=arts)
        return Course.objects.create(school_id=school_a.id, subject=subject,
                                     level=level, academic_year=academic_year,
                                     periods_per_week=4)


def _make_scheme(school_a, course, term):
    with TenantContext.scope(school_a):
        scheme = SchemeOfWork.objects.create(school_id=school_a.id, course=course,
                                             term=term)
        SyllabusUnit.objects.create(school_id=school_a.id, scheme=scheme,
                                    sequence=1, title="Unit 1", planned_periods=4)
        return scheme


@pytest.fixture
def two_departments_behind(school_a, term, science_course, arts_course, class_group,
                          teacher, room, draft_version, slot, make_scheduled_lesson):
    """One course per department, each with a scheme nobody has covered and a
    term's worth of elapsed lessons -- both fully behind schedule."""
    science_scheme = _make_scheme(school_a, science_course, term)
    arts_scheme = _make_scheme(school_a, arts_course, term)

    make_scheduled_lesson(draft_version, weekday=0, slot=slot, course=science_course,
                          class_group=class_group, teacher=teacher, room=room)
    make_scheduled_lesson(draft_version, weekday=1, slot=slot, course=arts_course,
                          class_group=class_group, teacher=teacher, room=room)

    with TenantContext.scope(school_a):
        timetable_services.publish(draft_version)
        draft_version.refresh_from_db()
        timetable_services.materialise(draft_version, TERM_START,
                                       timezone.localdate())

    return {"science": science_scheme, "arts": arts_scheme}


@pytest.fixture
def science_hod(school_a, make_membership, grant_role, science):
    membership = make_membership(school_a, email="hod-science@example.com",
                                 name="Science HOD")
    grant_role(membership, "hod", "Head of Department")
    with TenantContext.scope(school_a):
        StaffProfile.objects.create(school_id=school_a.id, membership=membership,
                                    department=science)
    return membership


@pytest.fixture
def deptless_hod(school_a, make_membership, grant_role):
    """Holds the 'hod' role but was never assigned a department."""
    membership = make_membership(school_a, email="hod-nobody@example.com",
                                 name="Unassigned HOD")
    grant_role(membership, "hod", "Head of Department")
    return membership


# -- Service layer -------------------------------------------------------------


def test_schemes_behind_filters_by_department(school_a, term, two_departments_behind,
                                              science):
    with TenantContext.scope(school_a):
        behind = delivery_services.schemes_behind(term=term, department_id=science.id)

    assert len(behind) == 1
    assert behind[0].scheme_id == two_departments_behind["science"].id


def test_schemes_behind_with_no_department_is_school_wide(school_a, term,
                                                           two_departments_behind):
    with TenantContext.scope(school_a):
        behind = delivery_services.schemes_behind(term=term)

    scheme_ids = {r.scheme_id for r in behind}
    assert two_departments_behind["science"].id in scheme_ids
    assert two_departments_behind["arts"].id in scheme_ids


def test_coverage_overview_reports_the_requested_department(school_a, term,
                                                             two_departments_behind,
                                                             science):
    with TenantContext.scope(school_a):
        overview = insights_services.coverage_overview(term=term,
                                                        department_id=science.id)

    assert overview["department_id"] == str(science.id)
    assert overview["groups_behind"] == 1
    assert overview["worst"][0]["scheme_id"] == str(
        two_departments_behind["science"].id
    )


# -- insights/coverage endpoint -------------------------------------------------


def test_hod_sees_only_their_own_department(school_a, term, two_departments_behind,
                                            science_hod, science):
    response = client_for(science_hod).get(reverse("v1:insights:coverage-overview"))

    assert response.status_code == 200
    assert response.data["department_id"] == str(science.id)
    assert response.data["groups_behind"] == 1
    assert response.data["worst"][0]["scheme_id"] == str(
        two_departments_behind["science"].id
    )


def test_leadership_sees_the_whole_school_by_default(school_a, term,
                                                      two_departments_behind, deputy):
    response = client_for(deputy).get(reverse("v1:insights:coverage-overview"))

    assert response.status_code == 200
    assert response.data["department_id"] is None
    assert response.data["groups_behind"] == 2


def test_leadership_can_filter_to_any_department(school_a, term,
                                                  two_departments_behind, deputy,
                                                  arts):
    response = client_for(deputy).get(
        reverse("v1:insights:coverage-overview"), {"department_id": str(arts.id)}
    )

    assert response.status_code == 200
    assert response.data["groups_behind"] == 1
    assert response.data["worst"][0]["scheme_id"] == str(
        two_departments_behind["arts"].id
    )


def test_a_hod_may_not_view_another_departments_coverage(school_a, term,
                                                          two_departments_behind,
                                                          science_hod, arts):
    response = client_for(science_hod).get(
        reverse("v1:insights:coverage-overview"), {"department_id": str(arts.id)}
    )

    assert response.status_code == 403
    assert response.data["type"].endswith("/not-permitted")


def test_a_hod_with_no_department_assignment_is_refused(school_a, term,
                                                         two_departments_behind,
                                                         deptless_hod):
    response = client_for(deptless_hod).get(
        reverse("v1:insights:coverage-overview")
    )

    assert response.status_code == 403
    assert response.data["type"].endswith("/no-department")


def test_a_teacher_may_not_see_the_coverage_overview(school_a, term,
                                                      two_departments_behind, teacher):
    response = client_for(teacher).get(reverse("v1:insights:coverage-overview"))

    assert response.status_code == 403


# -- delivery/coverage endpoint -------------------------------------------------


def test_hod_is_auto_scoped_on_the_delivery_endpoint(school_a, term,
                                                      two_departments_behind,
                                                      science_hod, science):
    response = client_for(science_hod).get(reverse("v1:delivery:coverage"),
                                           {"term_id": str(term.id)})

    assert response.status_code == 200
    assert response.data["department_id"] == str(science.id)
    scheme_ids = {r["scheme_id"] for r in response.data["results"]}
    assert scheme_ids == {str(two_departments_behind["science"].id)}


def test_hod_cannot_widen_the_delivery_view_with_department_id(
    school_a, term, two_departments_behind, science_hod, arts
):
    response = client_for(science_hod).get(
        reverse("v1:delivery:coverage"),
        {"term_id": str(term.id), "department_id": str(arts.id)},
    )

    assert response.status_code == 403
    assert response.data["type"].endswith("/not-permitted")


def test_a_teacher_still_sees_the_whole_school_on_the_delivery_endpoint(
    school_a, term, two_departments_behind, teacher
):
    """This endpoint has never been role-gated; department_id only narrows
    what a caller could already see, it does not add a new restriction."""
    response = client_for(teacher).get(reverse("v1:delivery:coverage"),
                                       {"term_id": str(term.id)})

    assert response.status_code == 200
    scheme_ids = {r["scheme_id"] for r in response.data["results"]}
    assert scheme_ids == {
        str(two_departments_behind["science"].id),
        str(two_departments_behind["arts"].id),
    }


def test_dos_can_filter_the_delivery_endpoint_by_department(
    school_a, term, two_departments_behind, deputy, arts
):
    response = client_for(deputy).get(
        reverse("v1:delivery:coverage"),
        {"term_id": str(term.id), "department_id": str(arts.id)},
    )

    assert response.status_code == 200
    scheme_ids = {r["scheme_id"] for r in response.data["results"]}
    assert scheme_ids == {str(two_departments_behind["arts"].id)}
