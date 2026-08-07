"""Provisioning, suspension, metering, health, and bulk import."""

from __future__ import annotations

import pytest
from django.core.management import call_command

from educore.core.models import Membership, Role, School
from educore.core.tenancy import TenantContext
from educore.platform import importers, services
from educore.platform.models import ImportBatch, Plan, Subscription

pytestmark = pytest.mark.django_db


@pytest.fixture
def plan(db):
    return Plan.objects.create(code="standard", name="Standard",
                               price_per_staff_per_term=1500000,
                               modules=["presence", "delivery"])


# -- Provisioning ------------------------------------------------------------


def test_a_provisioned_school_is_usable_immediately(plan):
    """Everything a school would otherwise be talked through on the phone."""
    result = services.provision_school(
        name="Mbarara Secondary", plan_code="standard",
        admin_email="head@mbarara.example", admin_name="A Head",
    )
    school = result["school"]

    with TenantContext.scope(school.id):
        from educore.assessment.models import GradingScale
        from educore.core.models import Campus
        from educore.presence.models import AttendancePolicy

        assert Role.objects.count() == len(services.ROLE_TEMPLATES)
        assert Campus.objects.filter(is_primary=True).exists()
        assert AttendancePolicy.objects.filter(is_active=True).exists()
        assert GradingScale.objects.filter(is_default=True).exists()

    assert school.slug == "mbarara-secondary"
    assert result["subscription"].status == Subscription.Status.TRIAL


def test_the_seeded_administrator_can_be_invited_but_not_yet_log_in(plan):
    result = services.provision_school(name="Jinja College", plan_code="standard",
                                       admin_email="admin@jinja.example")
    membership = result["admin_membership"]

    assert membership.status == Membership.Status.INVITED
    assert not membership.user.has_usable_password()
    with TenantContext.scope(result["school"].id):
        codes = {ra.role.code for ra in membership.role_assignments.all()}
    assert codes == {"director", "ict_admin"}


def test_a_duplicate_slug_is_refused(plan):
    services.provision_school(name="Gulu High", plan_code="standard")
    with pytest.raises(services.ProvisioningError, match="already exists"):
        services.provision_school(name="Gulu High", plan_code="standard")


def test_an_unknown_plan_is_refused():
    with pytest.raises(services.ProvisioningError, match="No active plan"):
        services.provision_school(name="Nowhere Academy", plan_code="ghost")


def test_the_provision_command_works(plan):
    call_command("provision_school", "Lira Girls", "--plan", "standard",
                 "--admin-email", "head@lira.example")
    assert School.objects.filter(slug="lira-girls").exists()


# -- Suspension --------------------------------------------------------------


def test_suspension_preserves_data_and_blocks_access(school_a, plan, teacher):
    """Non-payment is a commercial dispute; a school locked out mid-term still
    owns its records."""
    from django.urls import reverse
    from rest_framework.test import APIClient

    Subscription.objects.create(school=school_a, plan=plan,
                                started_on="2026-01-01")
    services.suspend_school(school_a, reason="Invoice 42 unpaid for 90 days.")

    response = APIClient().post(
        reverse("v1:auth-token"),
        {"email": teacher.user.email, "password": "correct-horse-battery"},
        format="json",
    )

    assert response.status_code == 403
    school_a.refresh_from_db()
    assert school_a.status == School.Status.SUSPENDED
    with TenantContext.scope(school_a.id):
        assert Membership.objects.filter(pk=teacher.pk).exists()


def test_reactivation_restores_access(school_a, plan, teacher):
    from django.urls import reverse
    from rest_framework.test import APIClient

    Subscription.objects.create(school=school_a, plan=plan,
                                started_on="2026-01-01")
    services.suspend_school(school_a, reason="Test")
    services.reactivate_school(school_a)

    response = APIClient().post(
        reverse("v1:auth-token"),
        {"email": teacher.user.email, "password": "correct-horse-battery"},
        format="json",
    )
    assert response.status_code == 200


# -- Metering and health -----------------------------------------------------


def test_metering_snapshots_active_staff(school_a, plan, teacher, deputy,
                                         students):
    Subscription.objects.create(school=school_a, plan=plan,
                                started_on="2026-01-01")
    record = services.meter_school(school_a)

    assert record.active_staff == 2
    assert record.active_students == 5


def test_an_invoice_preview_uses_the_stored_snapshot(school_a, plan, teacher,
                                                     deputy):
    """Billing that recalculates from live data cannot survive a dispute."""
    Subscription.objects.create(school=school_a, plan=plan,
                                started_on="2026-01-01",
                                status=Subscription.Status.ACTIVE)
    services.meter_school(school_a)

    preview = services.invoice_preview(school_a)

    assert preview["active_staff"] == 2
    assert preview["total_minor"] == 2 * 1500000
    assert preview["currency"] == "UGX"


def test_a_silent_school_is_flagged(school_a, teacher):
    health = services.tenant_health(school_a)

    assert health["healthy"] is False
    assert "no attendance has ever been recorded" in health["warnings"][0]


def test_a_school_with_recent_activity_is_healthy(school_a, campus, teacher,
                                                  morning, good_signals, policy):
    import uuid

    from educore.presence import services as presence

    with TenantContext.scope(school_a):
        presence.submit_event(membership=teacher, campus=campus,
                              kind="check_in", captured_at=morning,
                              client_event_id=uuid.uuid4(),
                              raw_signals=good_signals(morning))

    health = services.tenant_health(school_a)
    assert health["silent_days"] == 0


def test_a_high_provisional_rate_is_flagged_as_calibration(school_a, campus,
                                                           teacher, morning,
                                                           policy):
    """ADR-0002: this means the weights are wrong, not that staff are dishonest."""
    import uuid

    from educore.presence import services as presence

    with TenantContext.scope(school_a):
        presence.submit_event(membership=teacher, campus=campus,
                              kind="check_in", captured_at=morning,
                              client_event_id=uuid.uuid4(), raw_signals={})

    health = services.tenant_health(school_a)
    assert any("recalibrating" in w for w in health["warnings"])


# -- Bulk import -------------------------------------------------------------


STAFF_CSV = """full_name,email,role,staff_number
Grace Nakato,grace@example.com,teacher,T001
Peter Okello,peter@example.com,dos,T002
"""


def test_a_dry_run_writes_nothing(school_a, make_role):
    make_role(school_a, code="teacher")
    make_role(school_a, code="dos", name="DOS")

    result = importers.import_staff(STAFF_CSV, school_id=school_a.id,
                                    dry_run=True)

    assert result.ok
    assert result.rows_ok == 2
    assert result.applied is False
    with TenantContext.scope(school_a.id):
        assert not Membership.objects.filter(user__email="grace@example.com").exists()
        assert ImportBatch.objects.get().status == ImportBatch.Status.VALIDATED


def test_applying_a_clean_file_creates_staff(school_a, make_role):
    make_role(school_a, code="teacher")
    make_role(school_a, code="dos", name="DOS")

    result = importers.import_staff(STAFF_CSV, school_id=school_a.id,
                                    dry_run=False)

    assert result.applied
    with TenantContext.scope(school_a.id):
        assert Membership.objects.count() == 2
        membership = Membership.objects.get(user__email="grace@example.com")
        assert membership.status == Membership.Status.INVITED
        assert {ra.role.code for ra in membership.role_assignments.all()} == {"teacher"}


def test_a_file_with_any_bad_row_imports_nothing(school_a, make_role):
    """A half-applied import is the worst outcome available: the operator
    cannot tell what landed, and re-running produces duplicates."""
    make_role(school_a, code="teacher")
    csv_content = STAFF_CSV + "Bad Row,bad@example.com,wizard,T003\n"

    result = importers.import_staff(csv_content, school_id=school_a.id,
                                    dry_run=False)

    assert not result.ok
    assert result.applied is False
    with TenantContext.scope(school_a.id):
        assert Membership.objects.count() == 0
        assert ImportBatch.objects.get().status == ImportBatch.Status.REJECTED


def test_errors_carry_the_spreadsheet_row_number(school_a, make_role):
    make_role(school_a, code="teacher")
    csv_content = "full_name,email,role\n,missing@example.com,teacher\n"

    result = importers.import_staff(csv_content, school_id=school_a.id)

    assert result.errors[0]["row"] == 2          # 1 is the header
    assert result.errors[0]["column"] == "full_name"


def test_duplicates_within_the_file_are_caught(school_a, make_role):
    make_role(school_a, code="teacher")
    csv_content = ("full_name,email,role\n"
                   "A,same@example.com,teacher\n"
                   "B,same@example.com,teacher\n")

    result = importers.import_staff(csv_content, school_id=school_a.id)

    assert any("Duplicated within this file" in e["message"]
               for e in result.errors)


def test_an_unknown_role_names_the_valid_ones(school_a, make_role):
    make_role(school_a, code="teacher")
    csv_content = "full_name,email,role\nA,a@example.com,wizard\n"

    result = importers.import_staff(csv_content, school_id=school_a.id)

    assert "Known roles" in result.errors[0]["message"]


def test_students_import_and_enrol(school_a, class_group, term):
    csv_content = ("full_name,admission_number,class_group,residency\n"
                   "Amina Nabwire,ADM100,S4 Blue,day\n"
                   "Joseph Mugisha,ADM101,S4 Blue,boarding\n")

    result = importers.import_students(csv_content, school_id=school_a.id,
                                       term=term, dry_run=False)

    assert result.applied
    with TenantContext.scope(school_a.id):
        from educore.students.models import Enrolment, Student
        assert Student.objects.count() == 2
        assert Enrolment.objects.filter(is_active=True).count() == 2


def test_an_unknown_class_is_rejected_with_guidance(school_a, class_group, term):
    csv_content = ("full_name,admission_number,class_group\n"
                   "A,ADM200,S9 Purple\n")

    result = importers.import_students(csv_content, school_id=school_a.id,
                                       term=term)

    assert "Create it before importing" in result.errors[0]["message"]


def test_guardian_links_are_created_unverified(school_a, students, class_group):
    """An import file is not evidence that this adult is entitled to a child's
    attendance, location and grades."""
    from educore.students.models import GuardianLink

    csv_content = ("admission_number,guardian_name,email,relationship\n"
                   f"{students[0].admission_number},Mary N,mary@example.com,mother\n")

    result = importers.import_guardians(csv_content, school_id=school_a.id,
                                        dry_run=False)

    assert result.applied
    with TenantContext.scope(school_a.id):
        link = GuardianLink.objects.get()
        assert link.verified is False


def test_a_guardian_with_no_contact_details_is_rejected(school_a, students):
    csv_content = ("admission_number,guardian_name,email,phone,relationship\n"
                   f"{students[0].admission_number},No Contact,,,mother\n")

    result = importers.import_guardians(csv_content, school_id=school_a.id)

    assert any("email or a phone" in e["message"] for e in result.errors)


# -- The onboarding path, end to end -----------------------------------------


def _admin_client(membership):
    from django.urls import reverse
    from rest_framework.test import APIClient

    api = APIClient()
    response = api.post(reverse("v1:auth-token"),
                        {"email": membership.user.email,
                         "password": "correct-horse-battery"},
                        format="json")
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access_token']}")
    return api


@pytest.fixture
def ict_admin(school_a, make_membership, grant_role):
    membership = make_membership(school_a, email="ict@example.com")
    grant_role(membership, "ict_admin", "ICT Administrator")
    return membership


def _upload(api, url_name, content, **extra):
    from io import BytesIO

    from django.urls import reverse

    upload = BytesIO(content.encode("utf-8"))
    upload.name = "import.csv"
    return api.post(reverse(url_name), {"file": upload, **extra},
                    format="multipart")


def test_the_import_endpoint_dry_runs_by_default(school_a, ict_admin, make_role):
    make_role(school_a, code="teacher")
    api = _admin_client(ict_admin)

    response = _upload(api, "v1:platform:import-staff", STAFF_CSV.replace(
        "peter@example.com,dos", "peter@example.com,teacher"
    ))

    assert response.status_code == 200
    assert response.data["dry_run"] is True
    assert response.data["applied"] is False
    assert response.data["rows_ok"] == 2


def test_the_import_endpoint_applies_when_asked(school_a, ict_admin, make_role):
    make_role(school_a, code="teacher")
    api = _admin_client(ict_admin)

    response = _upload(api, "v1:platform:import-staff", STAFF_CSV.replace(
        "peter@example.com,dos", "peter@example.com,teacher"
    ), apply="true")

    assert response.status_code == 200
    assert response.data["applied"] is True
    with TenantContext.scope(school_a.id):
        assert Membership.objects.filter(user__email="grace@example.com").exists()


def test_a_bad_file_returns_422_with_row_numbers(school_a, ict_admin, make_role):
    make_role(school_a, code="teacher")
    api = _admin_client(ict_admin)

    response = _upload(api, "v1:platform:import-staff",
                       "full_name,email,role\nA,a@example.com,wizard\n",
                       apply="true")

    assert response.status_code == 422
    assert response.data["errors"][0]["row"] == 2
    assert "Nothing was imported" in response.data["detail"]


def test_a_teacher_may_not_import(school_a, teacher, make_role):
    make_role(school_a, code="teacher")
    response = _upload(_admin_client(teacher), "v1:platform:import-staff",
                       STAFF_CSV)

    assert response.status_code == 403


def test_import_history_is_visible_to_administrators(school_a, ict_admin,
                                                     make_role):
    make_role(school_a, code="teacher")
    api = _admin_client(ict_admin)
    _upload(api, "v1:platform:import-staff",
            "full_name,email,role\nA,a@example.com,teacher\n")

    from django.urls import reverse
    history = api.get(reverse("v1:platform:import-history"))

    assert history.status_code == 200
    assert history.data["results"][0]["status"] == ImportBatch.Status.VALIDATED


def test_the_estate_report_command_runs(school_a, plan, teacher):
    Subscription.objects.create(school=school_a, plan=plan,
                                started_on="2026-01-01")

    call_command("estate_report")

    from educore.platform.models import UsageRecord
    assert UsageRecord.objects.filter(school=school_a).exists()
