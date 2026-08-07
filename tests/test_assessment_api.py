"""Assessment endpoints, MFA step-up, and guardian-scoped results."""

from __future__ import annotations

from decimal import Decimal

import pyotp
import pytest
from django.core.files.base import ContentFile
from django.urls import reverse
from rest_framework.test import APIClient

from educore.assessment import services
from educore.assessment.models import Assessment, AssessmentState, GradingScale
from educore.core import mfa
from educore.core.tenancy import TenantContext
from educore.students.models import GuardianLink

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
def head(school_a, make_membership, grant_role):
    membership = make_membership(school_a, email="head@example.com",
                                 name="A Head")
    grant_role(membership, "head_teacher", "Head Teacher")
    return membership


@pytest.fixture
def dos(school_a, make_membership, grant_role):
    membership = make_membership(school_a, email="dos@example.com", name="A DOS")
    grant_role(membership, "dos", "Director of Studies")
    return membership


@pytest.fixture
def assessment(school_a, course, term, class_group, teacher):
    with TenantContext.scope(school_a):
        scale = GradingScale.objects.create(school_id=school_a.id, name="Default",
                                            is_default=True)
        created = Assessment.objects.create(
            school_id=school_a.id, course=course, term=term, title="Term Test",
            kind=Assessment.Kind.END_OF_TERM, max_score=Decimal("100"),
            author=teacher, grading_scale=scale,
        )
        created.class_groups.add(class_group)
        created.paper.save("p.pdf", ContentFile(b"%PDF-1.4"), save=True)
        return created


# -- MFA ---------------------------------------------------------------------


def test_enrol_confirm_and_step_up(school_a, head):
    api = client_for(head)

    enrol = api.post(reverse("v1:mfa-enrol"), {}, format="json")
    assert enrol.status_code == 200
    secret = enrol.data["secret"]
    assert enrol.data["otpauth_uri"].startswith("otpauth://totp/")

    confirmed = api.post(reverse("v1:mfa-confirm"),
                         {"code": pyotp.TOTP(secret).now()}, format="json")
    assert confirmed.status_code == 200

    granted = api.post(reverse("v1:step-up"),
                       {"code": pyotp.TOTP(secret).now()}, format="json")
    assert granted.status_code == 200
    assert granted.data["step_up_token"]


def test_a_wrong_code_is_refused(school_a, head):
    api = client_for(head)
    secret = api.post(reverse("v1:mfa-enrol"), {}, format="json").data["secret"]
    api.post(reverse("v1:mfa-confirm"), {"code": pyotp.TOTP(secret).now()},
             format="json")

    response = api.post(reverse("v1:step-up"), {"code": "000000"}, format="json")

    assert response.status_code == 400
    assert response.data["type"].endswith("/mfa-code-invalid")


def test_step_up_without_enrolment_explains_itself(school_a, head):
    response = client_for(head).post(reverse("v1:step-up"), {"code": "123456"},
                                     format="json")

    assert response.status_code == 403
    assert response.data["type"].endswith("/mfa-not-enrolled")


# -- The workflow through the API --------------------------------------------


def _enrol_mfa(api):
    secret = api.post(reverse("v1:mfa-enrol"), {}, format="json").data["secret"]
    api.post(reverse("v1:mfa-confirm"), {"code": pyotp.TOTP(secret).now()},
             format="json")
    return secret


def test_the_workflow_runs_end_to_end(school_a, assessment, teacher, dos, head,
                                      students):
    teacher_api, dos_api, head_api = (client_for(teacher), client_for(dos),
                                      client_for(head))

    assert teacher_api.post(
        reverse("v1:assessment:submit", args=[assessment.id]), {}, format="json"
    ).status_code == 200
    assert dos_api.post(
        reverse("v1:assessment:approve", args=[assessment.id]), {}, format="json"
    ).status_code == 200
    assert dos_api.post(
        reverse("v1:assessment:lock", args=[assessment.id]), {}, format="json"
    ).status_code == 200
    assert dos_api.post(
        reverse("v1:assessment:begin-scoring", args=[assessment.id]), {},
        format="json"
    ).status_code == 200

    marks = teacher_api.put(
        reverse("v1:assessment:scores", args=[assessment.id]),
        {"marks": [{"student_id": str(s.id), "raw_score": "60"}
                   for s in students]},
        format="json",
    )
    assert marks.status_code == 200
    assert marks.data["changed"] == 5

    assert teacher_api.post(
        reverse("v1:assessment:submit-marks", args=[assessment.id]), {},
        format="json"
    ).status_code == 200
    assert dos_api.post(
        reverse("v1:assessment:moderate", args=[assessment.id]), {}, format="json"
    ).status_code == 200

    secret = _enrol_mfa(head_api)
    token = head_api.post(reverse("v1:step-up"),
                          {"code": pyotp.TOTP(secret).now()},
                          format="json").data["step_up_token"]

    released = head_api.post(
        reverse("v1:assessment:release", args=[assessment.id]),
        {"step_up_token": token}, format="json",
    )

    assert released.status_code == 200
    assert released.data["state"] == AssessmentState.RELEASED


def test_release_without_step_up_returns_403(school_a, assessment, teacher, dos,
                                             head, students):
    with TenantContext.scope(school_a):
        services.submit(assessment, actor=teacher)
        services.approve(assessment, actor=dos)
        services.lock(assessment, actor=dos)
        services.begin_scoring(assessment, actor=dos)
        services.enter_scores(assessment, actor=teacher,
                              marks=[{"student_id": s.id, "raw_score": 60}
                                     for s in students])
        services.submit_marks(assessment, actor=teacher)
        services.moderate(assessment, actor=dos)

    response = client_for(head).post(
        reverse("v1:assessment:release", args=[assessment.id]), {}, format="json"
    )

    assert response.status_code == 403
    assert response.data["type"].endswith("/step-up-required")


def test_a_teacher_may_not_release_results(school_a, assessment, teacher):
    response = client_for(teacher).post(
        reverse("v1:assessment:release", args=[assessment.id]), {}, format="json"
    )
    assert response.status_code == 403
    assert response.data["type"].endswith("/not-permitted")


def test_an_out_of_order_transition_returns_409(school_a, assessment, dos):
    response = client_for(dos).post(
        reverse("v1:assessment:moderate", args=[assessment.id]), {}, format="json"
    )
    assert response.status_code == 409
    assert response.data["type"].endswith("/invalid-transition")


def test_returning_a_paper_carries_the_note(school_a, assessment, teacher, dos):
    client_for(teacher).post(reverse("v1:assessment:submit", args=[assessment.id]),
                             {}, format="json")

    response = client_for(dos).post(
        reverse("v1:assessment:return", args=[assessment.id]),
        {"note": "Question 4 is out of syllabus."}, format="json",
    )

    assert response.status_code == 200
    assert response.data["state"] == AssessmentState.DRAFT
    assert "syllabus" in response.data["returned_note"]


def test_a_teacher_only_sees_their_own_assessments(school_a, assessment,
                                                   make_membership):
    other = make_membership(school_a, email="other-teacher@example.com")

    response = client_for(other).get(reverse("v1:assessment:list"))

    assert response.status_code == 200
    assert response.data["results"] == []


def test_moderators_see_anomaly_flags_and_teachers_do_not(school_a, assessment,
                                                          teacher, dos, students):
    with TenantContext.scope(school_a):
        services.submit(assessment, actor=teacher)
        services.approve(assessment, actor=dos)
        services.lock(assessment, actor=dos)
        services.begin_scoring(assessment, actor=dos)
        services.enter_scores(assessment, actor=teacher,
                              marks=[{"student_id": s.id, "raw_score": 95}
                                     for s in students])

    as_dos = client_for(dos).get(
        reverse("v1:assessment:scores", args=[assessment.id])
    )
    as_teacher = client_for(teacher).get(
        reverse("v1:assessment:scores", args=[assessment.id])
    )

    assert {f["code"] for f in as_dos.data["anomalies"]} >= {"identical_marks"}
    assert "anomalies" not in as_teacher.data


# -- Guardian scope ----------------------------------------------------------


@pytest.fixture
def released(school_a, assessment, teacher, dos, head, students):
    with TenantContext.scope(school_a):
        services.submit(assessment, actor=teacher)
        services.approve(assessment, actor=dos)
        services.lock(assessment, actor=dos)
        services.begin_scoring(assessment, actor=dos)
        services.enter_scores(assessment, actor=teacher, marks=[
            {"student_id": s.id, "raw_score": 50 + i * 10}
            for i, s in enumerate(students)
        ])
        services.submit_marks(assessment, actor=teacher)
        services.moderate(assessment, actor=dos)

        details = mfa.provision(head.user)
        mfa.confirm(head.user, pyotp.TOTP(details["secret"]).now())
        _grant, token = mfa.grant_step_up(head, pyotp.TOTP(details["secret"]).now())
        services.release(assessment, actor=head, step_up_token=token)
    return assessment


def test_a_guardian_sees_only_their_own_childs_results(school_a, released,
                                                       students, make_membership):
    parent = make_membership(school_a, email="parent@example.com")
    with TenantContext.scope(school_a):
        GuardianLink.objects.create(
            school_id=school_a.id, student=students[0], membership=parent,
            relationship=GuardianLink.Relationship.MOTHER, verified=True,
        )

    response = client_for(parent).get(reverse("v1:assessment:my-results"))

    assert response.status_code == 200
    assert len(response.data["results"]) == 1
    assert response.data["results"][0]["student_id"] == str(students[0].id)


def test_an_unverified_guardian_sees_nothing(school_a, released, students,
                                             make_membership):
    """Showing a child's results to someone not entitled to them is a
    safeguarding incident, not a bug."""
    stranger = make_membership(school_a, email="stranger@example.com")
    with TenantContext.scope(school_a):
        GuardianLink.objects.create(
            school_id=school_a.id, student=students[0], membership=stranger,
            relationship=GuardianLink.Relationship.OTHER, verified=False,
        )

    response = client_for(stranger).get(reverse("v1:assessment:my-results"))

    assert response.data["results"] == []


def test_report_cards_are_issued_and_scoped_to_guardians(school_a, released,
                                                         students, class_group,
                                                         term, dos,
                                                         make_membership):
    issued = client_for(dos).post(
        reverse("v1:assessment:generate-report-cards"),
        {"class_group_id": str(class_group.id), "term_id": str(term.id)},
        format="json",
    )
    assert issued.status_code == 201
    assert issued.data["issued"] == 5

    parent = make_membership(school_a, email="parent2@example.com")
    with TenantContext.scope(school_a):
        GuardianLink.objects.create(
            school_id=school_a.id, student=students[0], membership=parent,
            relationship=GuardianLink.Relationship.FATHER, verified=True,
        )

    as_parent = client_for(parent).get(reverse("v1:assessment:report-cards"))
    as_dos = client_for(dos).get(reverse("v1:assessment:report-cards"))

    assert len(as_parent.data["results"]) == 1
    assert len(as_dos.data["results"]) == 5


def test_a_teacher_cannot_issue_report_cards(school_a, class_group, term,
                                             teacher):
    response = client_for(teacher).post(
        reverse("v1:assessment:generate-report-cards"),
        {"class_group_id": str(class_group.id), "term_id": str(term.id)},
        format="json",
    )
    assert response.status_code == 403
