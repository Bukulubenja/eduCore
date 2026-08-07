"""Phase 2 endpoints: the teacher's day, lesson scan, register, coverage."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from educore.core.tenancy import TenantContext
from educore.delivery import services as delivery_services
from educore.students.models import GateEvent, StudentAttendanceStatus
from educore.timetable import services as timetable_services
from educore.timetable.models import LessonInstance

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
def live_lesson(school_a, draft_version, slot, course, class_group, teacher,
                room, make_scheduled_lesson):
    """A published lesson happening right now."""
    make_scheduled_lesson(draft_version, weekday=0, slot=slot, course=course,
                          class_group=class_group, teacher=teacher, room=room)
    with TenantContext.scope(school_a):
        timetable_services.publish(draft_version)
        instance = LessonInstance.objects.first()
        now = timezone.now()
        LessonInstance.objects.filter(pk=instance.pk).update(
            date=timezone.localdate(), scheduled_start_at=now,
            scheduled_end_at=now + timedelta(minutes=40),
        )
        instance.refresh_from_db()
        return instance


# -- The teacher's day -------------------------------------------------------


def test_my_timetable_lists_todays_lessons(teacher_api, live_lesson):
    response = teacher_api.get(reverse("v1:delivery:my-day"))

    assert response.status_code == 200
    assert len(response.data["lessons"]) == 1
    lesson = response.data["lessons"][0]
    assert lesson["subject"] == "Physics"
    assert lesson["class_group"] == "S4 Blue"
    assert lesson["session_id"] is None


# -- Opening and closing -----------------------------------------------------


def test_scan_opens_a_lesson_and_close_records_coverage(teacher_api, live_lesson,
                                                        room, scheme, school_a):
    token = teacher_api.get(
        reverse("v1:delivery:room-token", args=[room.id])
    ).data["token"]

    opened = teacher_api.post(reverse("v1:delivery:open-session"),
                              {"room_id": str(room.id), "token": token},
                              format="json")
    assert opened.status_code == 201

    with TenantContext.scope(school_a):
        unit = scheme.units.get(sequence=1)

    closed = teacher_api.post(
        reverse("v1:delivery:close-session", args=[opened.data["id"]]),
        {"coverage": [{"unit_id": str(unit.id), "completion": "completed",
                       "homework_set": "Exercise 4.1"}]},
        format="json",
    )

    assert closed.status_code == 200
    assert closed.data["closed_at"] is not None
    with TenantContext.scope(school_a):
        live_lesson.refresh_from_db()
    assert live_lesson.status == LessonInstance.Status.DELIVERED


def test_closing_without_coverage_is_refused(teacher_api, live_lesson, room):
    token = teacher_api.get(
        reverse("v1:delivery:room-token", args=[room.id])
    ).data["token"]
    opened = teacher_api.post(reverse("v1:delivery:open-session"),
                              {"room_id": str(room.id), "token": token},
                              format="json")

    closed = teacher_api.post(
        reverse("v1:delivery:close-session", args=[opened.data["id"]]),
        {"coverage": []}, format="json",
    )

    assert closed.status_code == 422
    assert closed["Content-Type"].startswith("application/problem+json")


def test_a_forged_code_is_refused(teacher_api, live_lesson, room):
    response = teacher_api.post(reverse("v1:delivery:open-session"),
                                {"room_id": str(room.id),
                                 "token": "not-a-real.token"},
                                format="json")

    assert response.status_code == 422
    assert response.data["type"].endswith("/invalid-lesson-code")


def test_scanning_with_nothing_timetabled_explains_itself(teacher_api, room,
                                                          school_a):
    token = teacher_api.get(
        reverse("v1:delivery:room-token", args=[room.id])
    ).data["token"]

    response = teacher_api.post(reverse("v1:delivery:open-session"),
                                {"room_id": str(room.id), "token": token},
                                format="json")

    assert response.status_code == 409
    assert "substitution" in response.data["detail"]


def test_another_teachers_session_cannot_be_closed(teacher_api, live_lesson, room,
                                                   school_a, make_membership,
                                                   scheme, policy):
    token = teacher_api.get(
        reverse("v1:delivery:room-token", args=[room.id])
    ).data["token"]
    opened = teacher_api.post(reverse("v1:delivery:open-session"),
                              {"room_id": str(room.id), "token": token},
                              format="json")

    intruder = make_membership(school_a, email="intruder@example.com")
    with TenantContext.scope(school_a):
        unit = scheme.units.first()

    response = client_for(intruder).post(
        reverse("v1:delivery:close-session", args=[opened.data["id"]]),
        {"coverage": [{"unit_id": str(unit.id)}]}, format="json",
    )

    assert response.status_code == 404


# -- Register ----------------------------------------------------------------


def test_register_returns_the_roster_pre_marked_present(teacher_api, live_lesson,
                                                        students):
    response = teacher_api.get(
        reverse("v1:students:register", args=[live_lesson.id])
    )

    assert response.status_code == 200
    assert len(response.data["students"]) == 5
    assert {s["status"] for s in response.data["students"]} == {"present"}


def test_submitting_exceptions_marks_the_rest_present(teacher_api, live_lesson,
                                                      students):
    response = teacher_api.put(
        reverse("v1:students:register", args=[live_lesson.id]),
        {"exceptions": [{"student_id": str(students[0].id), "status": "absent"},
                        {"student_id": str(students[1].id), "status": "sick"}]},
        format="json",
    )

    assert response.status_code == 200
    assert response.data["marked"] == 5
    assert response.data["exceptions"] == 2


def test_marking_a_student_from_another_class_is_refused(teacher_api, live_lesson,
                                                         students, school_a,
                                                         level, term):
    from educore.academics.models import ClassGroup
    from educore.students.models import Student
    from educore.students.services import enrol

    with TenantContext.scope(school_a):
        other_group = ClassGroup.objects.create(school_id=school_a.id,
                                                level=level, name="S4 Slate")
        outsider = Student.objects.create(school_id=school_a.id,
                                          admission_number="ADM777",
                                          full_name="Outsider")
        enrol(student=outsider, class_group=other_group, term=term)

    response = teacher_api.put(
        reverse("v1:students:register", args=[live_lesson.id]),
        {"exceptions": [{"student_id": str(outsider.id), "status": "absent"}]},
        format="json",
    )

    assert response.status_code == 422


# -- Gate --------------------------------------------------------------------


def test_gate_batch_reports_per_event(teacher_api, students, campus, school_a):
    response = teacher_api.post(reverse("v1:students:gate-events"), {
        "events": [
            {"client_event_id": str(uuid.uuid4()), "scan_code": "SCAN001",
             "campus_id": str(campus.id), "direction": GateEvent.Direction.IN,
             "occurred_at": timezone.now().isoformat()},
            {"client_event_id": str(uuid.uuid4()), "scan_code": "NOT-A-CARD",
             "campus_id": str(campus.id), "direction": GateEvent.Direction.IN,
             "occurred_at": timezone.now().isoformat()},
        ],
    }, format="json")

    assert response.status_code == 200
    statuses = [r["status"] for r in response.data["results"]]
    assert statuses == ["accepted", "rejected"]
    assert response.data["results"][1]["error"]["code"] == "unknown_student"


# -- Coverage ----------------------------------------------------------------


def test_coverage_endpoint_reports_pace(teacher_api, live_lesson, scheme,
                                        school_a, term, teacher, room,
                                        draft_version):
    from educore.delivery.models import CoverageEntry, LessonSession

    with TenantContext.scope(school_a):
        draft_version.refresh_from_db()
        timetable_services.materialise(
            draft_version, term.starts_on, timezone.localdate()
        )
        session = LessonSession.objects.create(
            school_id=school_a.id, lesson_instance=live_lesson,
            actual_teacher=teacher, room=room, opened_at=timezone.now(),
            closed_at=timezone.now(),
        )
        CoverageEntry.objects.create(
            school_id=school_a.id, session=session,
            unit=scheme.units.get(sequence=1),
            completion=CoverageEntry.Completion.COMPLETED,
        )

    response = teacher_api.get(reverse("v1:delivery:coverage"),
                               {"term_id": str(term.id)})

    assert response.status_code == 200
    assert len(response.data["results"]) >= 1
    report = response.data["results"][0]
    assert report["is_behind"] is True
    assert report["pace"] < 0


def test_coverage_for_one_scheme_can_be_requested(teacher_api, scheme, term,
                                                  school_a):
    response = teacher_api.get(reverse("v1:delivery:coverage"),
                               {"scheme_id": str(scheme.id),
                                "term_id": str(term.id)})

    assert response.status_code == 200
    assert response.data["results"][0]["scheme_id"] == str(scheme.id)


# -- The nightly job ---------------------------------------------------------


def test_roll_timetable_settles_missed_lessons(school_a, live_lesson):
    from django.core.management import call_command

    with TenantContext.scope(school_a):
        LessonInstance.objects.filter(pk=live_lesson.pk).update(
            scheduled_start_at=timezone.now() - timedelta(hours=3),
            scheduled_end_at=timezone.now() - timedelta(hours=2),
        )

    call_command("roll_timetable")

    with TenantContext.scope(school_a):
        live_lesson.refresh_from_db()
    assert live_lesson.status == LessonInstance.Status.MISSED


def test_the_nightly_job_is_scoped_per_school(school_b, school_a, live_lesson):
    """A job iterating tenants must bind each one, or it sees nothing at all."""
    from django.core.management import call_command

    with TenantContext.scope(school_a):
        LessonInstance.objects.filter(pk=live_lesson.pk).update(
            scheduled_start_at=timezone.now() - timedelta(hours=3),
            scheduled_end_at=timezone.now() - timedelta(hours=2),
        )

    call_command("roll_timetable", school_slug="entebbe-girls")

    with TenantContext.scope(school_a):
        live_lesson.refresh_from_db()
    assert live_lesson.status == LessonInstance.Status.SCHEDULED


def test_delivery_services_reject_an_unscoped_call(school_a, scheme):
    """Outside a tenant scope the ORM sees nothing -- the safe failure."""
    report = delivery_services.coverage_report(scheme=scheme)
    assert report.total_periods == 0
    assert StudentAttendanceStatus.PRESENT == "present"
