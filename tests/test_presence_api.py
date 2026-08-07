"""Presence endpoints, including the offline sync contract of ADR-0003."""

from __future__ import annotations

import uuid

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from educore.core.tenancy import TenantContext
from educore.presence.models import AttendanceException, Disposition, SignalType

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
def teacher_api(teacher, policy):
    return client_for(teacher)


@pytest.fixture
def deputy_api(deputy, policy):
    return client_for(deputy)


def check_in_body(campus, at, signals):
    return {
        "client_event_id": str(uuid.uuid4()),
        "campus_id": str(campus.id),
        "captured_at": at.isoformat(),
        "signals": signals,
    }


# -- Check-in ----------------------------------------------------------------


def test_check_in_records_and_returns_the_evidence(teacher_api, campus, morning,
                                                   good_signals):
    response = teacher_api.post(reverse("v1:presence:check-in"),
                                check_in_body(campus, morning,
                                              good_signals(morning)),
                                format="json")

    assert response.status_code == 201
    assert response.data["event"]["disposition"] == Disposition.VERIFIED
    # The teacher can see exactly what was weighed, not just the verdict.
    types = {s["signal_type"] for s in response.data["event"]["signals"]}
    assert SignalType.QR in types
    assert "clock_skew_seconds" in response.data


def test_weak_evidence_still_returns_201(teacher_api, campus, morning):
    """Provisional is a recorded arrival, not a refusal (ADR-0002)."""
    response = teacher_api.post(reverse("v1:presence:check-in"),
                                check_in_body(campus, morning, {}),
                                format="json")

    assert response.status_code == 201
    assert response.data["event"]["disposition"] == Disposition.PROVISIONAL
    assert response.data["record"]["needs_review"] is True


def test_a_retry_returns_200_and_creates_nothing(teacher_api, campus, morning,
                                                 good_signals):
    body = check_in_body(campus, morning, good_signals(morning))

    first = teacher_api.post(reverse("v1:presence:check-in"), body, format="json")
    second = teacher_api.post(reverse("v1:presence:check-in"), body, format="json")

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.data["event"]["id"] == second.data["event"]["id"]


def test_unknown_signal_types_are_rejected(teacher_api, campus, morning):
    body = check_in_body(campus, morning, {"telepathy": {"confidence": "high"}})

    response = teacher_api.post(reverse("v1:presence:check-in"), body,
                                format="json")

    assert response.status_code == 400
    assert response["Content-Type"].startswith("application/problem+json")


def test_a_campus_in_another_school_is_not_found(teacher_api, morning, school_b):
    from educore.core.models import Campus

    with TenantContext.scope(school_b):
        foreign = Campus.objects.create(school=school_b, name="Their campus")

    response = teacher_api.post(reverse("v1:presence:check-in"),
                                check_in_body(foreign, morning, {}),
                                format="json")

    assert response.status_code == 404


# -- QR display --------------------------------------------------------------


def test_qr_token_endpoint_serves_a_rotating_code(teacher_api, campus):
    response = teacher_api.get(reverse("v1:presence:qr-token"),
                               {"campus_id": str(campus.id)})

    assert response.status_code == 200
    assert response.data["refresh_after_seconds"] <= 30
    assert response.data["token"].count(".") == 1


# -- Visibility --------------------------------------------------------------


def test_a_teacher_sees_their_own_history(teacher_api, campus, morning,
                                          good_signals):
    teacher_api.post(reverse("v1:presence:check-in"),
                     check_in_body(campus, morning, good_signals(morning)),
                     format="json")

    response = teacher_api.get(reverse("v1:presence:my-attendance"))

    assert response.status_code == 200
    assert len(response.data["results"]) == 1


def test_a_teacher_cannot_browse_a_colleagues_records(teacher_api, campus, morning,
                                                      make_membership, school_a,
                                                      good_signals):
    colleague = make_membership(school_a, email="colleague2@example.com")
    colleague_api = client_for(colleague)
    colleague_api.post(reverse("v1:presence:check-in"),
                       check_in_body(campus, morning, {}), format="json")

    response = teacher_api.get(reverse("v1:presence:records"),
                               {"membership_id": str(colleague.pk)})

    assert response.status_code == 200
    assert response.data["results"] == []


def test_a_deputy_sees_the_whole_school(deputy_api, teacher_api, campus, morning,
                                        good_signals):
    teacher_api.post(reverse("v1:presence:check-in"),
                     check_in_body(campus, morning, good_signals(morning)),
                     format="json")

    response = deputy_api.get(reverse("v1:presence:records"))

    assert response.status_code == 200
    assert len(response.data["results"]) == 1


# -- Appeals -----------------------------------------------------------------


def test_appeal_and_decision_round_trip(teacher_api, deputy_api, campus, morning):
    check_in = teacher_api.post(reverse("v1:presence:check-in"),
                                check_in_body(campus, morning, {}), format="json")
    record_id = check_in.data["record"]["id"]

    appeal = teacher_api.post(
        reverse("v1:presence:appeal", args=[record_id]),
        {"reason_code": AttendanceException.Reason.DEVICE_FAULT,
         "narrative": "Phone Wi-Fi has been broken since Monday.",
         "requested_status": "present"},
        format="json",
    )
    assert appeal.status_code == 201

    queue = deputy_api.get(reverse("v1:presence:reviews"))
    assert len(queue.data["results"]) == 1

    decided = deputy_api.post(
        reverse("v1:presence:decide", args=[appeal.data["id"]]),
        {"decision": AttendanceException.Decision.UPHELD, "note": "Confirmed."},
        format="json",
    )

    assert decided.status_code == 200
    assert decided.data["decision"] == AttendanceException.Decision.UPHELD


def test_a_teacher_may_not_decide_their_own_appeal(teacher_api, campus, morning):
    check_in = teacher_api.post(reverse("v1:presence:check-in"),
                                check_in_body(campus, morning, {}), format="json")
    appeal = teacher_api.post(
        reverse("v1:presence:appeal", args=[check_in.data["record"]["id"]]),
        {"reason_code": AttendanceException.Reason.OTHER}, format="json",
    )

    response = teacher_api.post(
        reverse("v1:presence:decide", args=[appeal.data["id"]]),
        {"decision": AttendanceException.Decision.UPHELD}, format="json",
    )

    assert response.status_code == 403


def test_health_metrics_are_restricted_to_leadership(teacher_api, deputy_api):
    assert teacher_api.get(reverse("v1:presence:health")).status_code == 403
    assert deputy_api.get(reverse("v1:presence:health")).status_code == 200


# -- Offline sync ------------------------------------------------------------


def test_sync_accepts_a_batch_and_reports_per_operation(teacher_api, campus,
                                                        morning, good_signals):
    body = {
        "device_time": morning.isoformat(),
        "operations": [
            {
                "client_event_id": str(uuid.uuid4()),
                "type": "attendance.check_in",
                "captured_at": morning.isoformat(),
                "payload": {"campus_id": str(campus.id),
                            "signals": good_signals(morning)},
            },
            {
                "client_event_id": str(uuid.uuid4()),
                "type": "attendance.check_out",
                "captured_at": morning.replace(hour=16).isoformat(),
                "payload": {"campus_id": str(campus.id),
                            "signals": good_signals(morning.replace(hour=16))},
            },
        ],
    }

    response = teacher_api.post(reverse("v1:presence:sync"), body, format="json")

    assert response.status_code == 200
    assert [r["status"] for r in response.data["results"]] == ["accepted", "accepted"]
    assert response.data["results"][-1]["resource"]["status"] == "present"
    assert response.data["clock_skew_seconds"] is not None


def test_one_bad_operation_does_not_fail_the_batch(teacher_api, campus, morning,
                                                   good_signals):
    """Otherwise a single malformed record blocks a device's queue forever."""
    body = {
        "operations": [
            {
                "client_event_id": str(uuid.uuid4()),
                "type": "attendance.check_in",
                "captured_at": morning.isoformat(),
                "payload": {"campus_id": str(uuid.uuid4()), "signals": {}},
            },
            {
                "client_event_id": str(uuid.uuid4()),
                "type": "attendance.check_in",
                "captured_at": morning.isoformat(),
                "payload": {"campus_id": str(campus.id),
                            "signals": good_signals(morning)},
            },
        ],
    }

    response = teacher_api.post(reverse("v1:presence:sync"), body, format="json")

    statuses = [r["status"] for r in response.data["results"]]
    assert statuses == ["rejected", "accepted"]
    assert response.data["results"][0]["error"]["code"] == "campus_not_found"


def test_resynced_operations_report_duplicate_not_error(teacher_api, campus,
                                                        morning, good_signals):
    operation = {
        "client_event_id": str(uuid.uuid4()),
        "type": "attendance.check_in",
        "captured_at": morning.isoformat(),
        "payload": {"campus_id": str(campus.id), "signals": good_signals(morning)},
    }

    teacher_api.post(reverse("v1:presence:sync"), {"operations": [operation]},
                     format="json")
    again = teacher_api.post(reverse("v1:presence:sync"),
                             {"operations": [operation]}, format="json")

    assert again.data["results"][0]["status"] == "duplicate"


def test_sync_is_scoped_to_the_calling_membership(teacher_api, campus, morning,
                                                  good_signals, teacher):
    """A device can only ever report attendance for its own account."""
    from educore.presence.models import AttendanceEvent

    teacher_api.post(reverse("v1:presence:sync"), {
        "operations": [{
            "client_event_id": str(uuid.uuid4()),
            "type": "attendance.check_in",
            "captured_at": morning.isoformat(),
            "payload": {"campus_id": str(campus.id),
                        "signals": good_signals(morning)},
        }],
    }, format="json")

    with TenantContext.scope(teacher.school_id):
        assert set(AttendanceEvent.objects.values_list("membership_id", flat=True)) \
            == {teacher.pk}
