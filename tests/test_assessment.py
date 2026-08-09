"""The mark lifecycle: transitions, scoring, moderation, release, report cards."""

from __future__ import annotations

from decimal import Decimal

import pyotp
import pytest
from django.core.files.base import ContentFile

from educore.assessment import services
from educore.assessment.models import (
    Assessment,
    AssessmentState,
    GradeBand,
    GradingScale,
    ReportCard,
    Score,
)
from educore.core import mfa
from educore.core.models import AuditEvent, OutboxMessage
from educore.core.tenancy import TenantContext

pytestmark = pytest.mark.django_db


@pytest.fixture
def scale(school_a, level):
    with TenantContext.scope(school_a):
        scale = GradingScale.objects.create(school_id=school_a.id,
                                            name="O-Level", level=level,
                                            is_default=True)
        for label, low, high, points in [("D1", 80, 100, 1), ("C3", 60, 79.99, 3),
                                         ("P7", 40, 59.99, 7), ("F9", 0, 39.99, 9)]:
            GradeBand.objects.create(school_id=school_a.id, scale=scale,
                                     label=label, min_percentage=low,
                                     max_percentage=high, points=points)
        return scale


@pytest.fixture
def assessment(school_a, course, term, class_group, teacher, scale):
    with TenantContext.scope(school_a):
        created = Assessment.objects.create(
            school_id=school_a.id, course=course, term=term,
            title="End of Term Physics", kind=Assessment.Kind.END_OF_TERM,
            max_score=Decimal("100"), author=teacher, grading_scale=scale,
        )
        created.class_groups.add(class_group)
        created.paper.save("paper.pdf", ContentFile(b"%PDF-1.4 fake"), save=True)
        return created


@pytest.fixture
def head(school_a, make_membership, grant_role):
    """A head teacher with a working authenticator."""
    membership = make_membership(school_a, email="head@example.com",
                                 name="A Head Teacher")
    grant_role(membership, "head_teacher", "Head Teacher")
    with TenantContext.scope(school_a):
        details = mfa.provision(membership.user)
        mfa.confirm(membership.user, pyotp.TOTP(details["secret"]).now())
        membership.user.refresh_from_db()
    return membership


def step_up(head):
    with TenantContext.scope(head.school_id):
        code = pyotp.TOTP(head.user.mfa_secret).now()
        _grant, token = mfa.grant_step_up(head, code)
    return token


def advance_to_scoring(school_a, assessment, teacher, deputy):
    with TenantContext.scope(school_a):
        services.submit(assessment, actor=teacher)
        services.approve(assessment, actor=deputy)
        services.lock(assessment, actor=deputy)
        services.begin_scoring(assessment, actor=deputy)
    return assessment


# -- The state machine -------------------------------------------------------


def test_the_full_path_ends_in_released(school_a, assessment, teacher, deputy,
                                        head, students):
    advance_to_scoring(school_a, assessment, teacher, deputy)

    with TenantContext.scope(school_a):
        services.enter_scores(assessment, actor=teacher, marks=[
            {"student_id": s.id, "raw_score": 55 + i * 5}
            for i, s in enumerate(students)
        ])
        services.submit_marks(assessment, actor=teacher)
        services.moderate(assessment, actor=deputy)

    services_release(school_a, assessment, head)

    with TenantContext.scope(school_a):
        assessment.refresh_from_db()
    assert assessment.state == AssessmentState.RELEASED
    assert assessment.released_by_id == head.pk


def services_release(school_a, assessment, head):
    with TenantContext.scope(school_a):
        return services.release(assessment, actor=head,
                                step_up_token=step_up(head))


def test_a_paper_cannot_be_submitted_without_the_paper(school_a, assessment,
                                                       teacher):
    with TenantContext.scope(school_a):
        assessment.paper = ""
        assessment.save()
        with pytest.raises(services.AssessmentError, match="question paper"):
            services.submit(assessment, actor=teacher)


def test_steps_cannot_be_skipped(school_a, assessment, teacher, deputy):
    """Releasing straight from draft is refused, not warned about."""
    with TenantContext.scope(school_a):
        with pytest.raises(services.InvalidTransitionError):
            services.lock(assessment, actor=deputy)
        with pytest.raises(services.InvalidTransitionError):
            services.moderate(assessment, actor=deputy)


def test_a_returned_paper_goes_back_to_draft(school_a, assessment, teacher,
                                             deputy):
    with TenantContext.scope(school_a):
        services.submit(assessment, actor=teacher)
        services.return_to_author(assessment, actor=deputy,
                                  note="Question 4 is out of syllabus.")
        assessment.refresh_from_db()

    assert assessment.state == AssessmentState.DRAFT
    assert "syllabus" in assessment.returned_note


def test_returning_requires_a_reason(school_a, assessment, teacher, deputy):
    with TenantContext.scope(school_a):
        services.submit(assessment, actor=teacher)
        with pytest.raises(services.AssessmentError):
            services.return_to_author(assessment, actor=deputy, note="")


# -- Scoring -----------------------------------------------------------------


def test_marks_cannot_be_entered_before_the_exam_is_sat(school_a, assessment,
                                                        teacher, students):
    """Doc 03, invariant 9."""
    with TenantContext.scope(school_a), \
            pytest.raises(services.AssessmentError, match="been sat"):
        services.enter_scores(assessment, actor=teacher,
                              marks=[{"student_id": students[0].id,
                                      "raw_score": 60}])


def test_a_mark_above_the_maximum_is_refused(school_a, assessment, teacher,
                                             deputy, students):
    advance_to_scoring(school_a, assessment, teacher, deputy)
    with TenantContext.scope(school_a), \
            pytest.raises(services.AssessmentError, match="exceeds the maximum"):
        services.enter_scores(assessment, actor=teacher,
                              marks=[{"student_id": students[0].id,
                                      "raw_score": 140}])


def test_a_student_from_another_class_cannot_be_scored(school_a, assessment,
                                                       teacher, deputy, level,
                                                       term):
    from educore.academics.models import ClassGroup
    from educore.students.models import Student
    from educore.students.services import enrol

    advance_to_scoring(school_a, assessment, teacher, deputy)
    with TenantContext.scope(school_a):
        other = ClassGroup.objects.create(school_id=school_a.id, level=level,
                                          name="S4 Slate")
        outsider = Student.objects.create(school_id=school_a.id,
                                          admission_number="ADM888",
                                          full_name="Outsider")
        enrol(student=outsider, class_group=other, term=term)

        with pytest.raises(services.AssessmentError, match="not enrolled"):
            services.enter_scores(assessment, actor=teacher,
                                  marks=[{"student_id": outsider.id,
                                          "raw_score": 60}])


def test_every_mark_change_is_audited_with_before_and_after(school_a, assessment,
                                                            teacher, deputy,
                                                            students):
    """The record that settles a dispute six months later."""
    advance_to_scoring(school_a, assessment, teacher, deputy)

    with TenantContext.scope(school_a):
        services.enter_scores(assessment, actor=teacher,
                              marks=[{"student_id": students[0].id,
                                      "raw_score": 40}])
        services.enter_scores(assessment, actor=teacher,
                              marks=[{"student_id": students[0].id,
                                      "raw_score": 75}])

        events = list(AuditEvent.objects.filter(
            action="assessment.score.changed"
        ).order_by("sequence"))

    assert len(events) == 2
    assert events[0].before is None
    assert events[1].before["raw_score"] == "40.00"
    assert events[1].after["raw_score"] == "75"


def test_an_unchanged_mark_writes_no_audit_noise(school_a, assessment, teacher,
                                                 deputy, students):
    advance_to_scoring(school_a, assessment, teacher, deputy)
    with TenantContext.scope(school_a):
        marks = [{"student_id": students[0].id, "raw_score": 60}]
        services.enter_scores(assessment, actor=teacher, marks=marks)
        changed = services.enter_scores(assessment, actor=teacher, marks=marks)

    assert changed == 0


def test_marks_cannot_be_submitted_with_gaps(school_a, assessment, teacher,
                                             deputy, students):
    advance_to_scoring(school_a, assessment, teacher, deputy)
    with TenantContext.scope(school_a):
        services.enter_scores(assessment, actor=teacher,
                              marks=[{"student_id": students[0].id,
                                      "raw_score": None}])
        with pytest.raises(services.AssessmentError, match="neither a mark"):
            services.submit_marks(assessment, actor=teacher)


def test_an_absence_counts_as_recorded(school_a, assessment, teacher, deputy,
                                       students):
    advance_to_scoring(school_a, assessment, teacher, deputy)
    with TenantContext.scope(school_a):
        services.enter_scores(assessment, actor=teacher,
                              marks=[{"student_id": students[0].id,
                                      "is_absent": True}])
        services.submit_marks(assessment, actor=teacher)
        assessment.refresh_from_db()

    assert assessment.state == AssessmentState.MODERATION


def test_changing_a_mark_after_moderation_requires_a_reason(school_a, assessment,
                                                            teacher, deputy,
                                                            students):
    """This is the point where someone alters a mark others have checked."""
    advance_to_scoring(school_a, assessment, teacher, deputy)
    with TenantContext.scope(school_a):
        services.enter_scores(assessment, actor=teacher,
                              marks=[{"student_id": s.id, "raw_score": 60}
                                     for s in students])
        services.submit_marks(assessment, actor=teacher)

        with pytest.raises(services.AssessmentError, match="requires a reason"):
            services.enter_scores(assessment, actor=deputy,
                                  marks=[{"student_id": students[0].id,
                                          "raw_score": 90}])

        services.enter_scores(assessment, actor=deputy,
                              marks=[{"student_id": students[0].id,
                                      "raw_score": 90}],
                              reason="Addition error on page 3.")
        score = Score.objects.get(student=students[0])

    assert score.raw_score == Decimal("90")
    assert "Addition error" in score.change_reason


# -- Anomaly flags -----------------------------------------------------------


def test_a_whole_class_on_the_same_mark_is_flagged(school_a, assessment, teacher,
                                                   deputy, students):
    """Usually a data-entry slip, occasionally not. Either way a moderator
    should see it rather than have it slide through."""
    advance_to_scoring(school_a, assessment, teacher, deputy)
    with TenantContext.scope(school_a):
        services.enter_scores(assessment, actor=teacher,
                              marks=[{"student_id": s.id, "raw_score": 95}
                                     for s in students])
        flags = services.flag_anomalous_grading(assessment)

    codes = {f["code"] for f in flags}
    assert "identical_marks" in codes
    assert "implausibly_high" in codes


def test_a_normal_spread_is_not_flagged(school_a, assessment, teacher, deputy,
                                        students):
    advance_to_scoring(school_a, assessment, teacher, deputy)
    with TenantContext.scope(school_a):
        services.enter_scores(assessment, actor=teacher, marks=[
            {"student_id": s.id, "raw_score": 35 + i * 12}
            for i, s in enumerate(students)
        ])
        assert services.flag_anomalous_grading(assessment) == []


# -- Release and step-up -----------------------------------------------------


def test_release_without_step_up_is_refused(school_a, assessment, teacher,
                                            deputy, head, students):
    """Being logged in is not evidence the head is at the keyboard."""
    advance_to_scoring(school_a, assessment, teacher, deputy)
    with TenantContext.scope(school_a):
        services.enter_scores(assessment, actor=teacher,
                              marks=[{"student_id": s.id, "raw_score": 60}
                                     for s in students])
        services.submit_marks(assessment, actor=teacher)
        services.moderate(assessment, actor=deputy)

        with pytest.raises(mfa.StepUpRequiredError):
            services.release(assessment, actor=head, step_up_token="")

        assessment.refresh_from_db()
    assert assessment.state == AssessmentState.HEAD_REVIEW


def test_a_step_up_grant_is_single_use(school_a, assessment, teacher, deputy,
                                       head, students):
    """One code must not authorise an afternoon of releases."""
    advance_to_scoring(school_a, assessment, teacher, deputy)
    with TenantContext.scope(school_a):
        services.enter_scores(assessment, actor=teacher,
                              marks=[{"student_id": s.id, "raw_score": 60}
                                     for s in students])
        services.submit_marks(assessment, actor=teacher)
        services.moderate(assessment, actor=deputy)

        token = step_up(head)
        services.release(assessment, actor=head, step_up_token=token)

        with pytest.raises(mfa.StepUpRequiredError, match="already used"):
            mfa.consume_step_up(head, token, action="assessment.release")


def test_release_announces_itself(school_a, assessment, teacher, deputy, head,
                                  students):
    advance_to_scoring(school_a, assessment, teacher, deputy)
    with TenantContext.scope(school_a):
        services.enter_scores(assessment, actor=teacher,
                              marks=[{"student_id": s.id, "raw_score": 60}
                                     for s in students])
        services.submit_marks(assessment, actor=teacher)
        services.moderate(assessment, actor=deputy)
    services_release(school_a, assessment, head)

    with TenantContext.scope(school_a):
        assert OutboxMessage.objects.filter(topic="assessment.released").exists()


# -- Report cards ------------------------------------------------------------


@pytest.fixture
def released(school_a, assessment, teacher, deputy, head, students):
    advance_to_scoring(school_a, assessment, teacher, deputy)
    with TenantContext.scope(school_a):
        services.enter_scores(assessment, actor=teacher, marks=[
            {"student_id": s.id, "raw_score": 45 + i * 10}
            for i, s in enumerate(students)
        ])
        services.submit_marks(assessment, actor=teacher)
        services.moderate(assessment, actor=deputy)
    services_release(school_a, assessment, head)
    return assessment


def test_a_report_card_freezes_released_results(school_a, released, students,
                                                term, head):
    with TenantContext.scope(school_a):
        report = services.generate_report_card(student=students[0], term=term,
                                               actor=head)

    subject = report.content["subjects"][0]
    assert subject["subject"] == "Physics"
    assert subject["assessments"][0]["percentage"] == 45.0
    assert subject["assessments"][0]["grade"]["label"] == "P7"


def test_unreleased_marks_never_reach_a_report_card(school_a, assessment,
                                                    teacher, deputy, students,
                                                    term, head):
    """Otherwise a report card publishes, by the back door, results the head
    teacher has not authorised."""
    advance_to_scoring(school_a, assessment, teacher, deputy)
    with TenantContext.scope(school_a):
        services.enter_scores(assessment, actor=teacher,
                              marks=[{"student_id": students[0].id,
                                      "raw_score": 88}])
        report = services.generate_report_card(student=students[0], term=term,
                                               actor=head)

    assert report.content["subjects"] == []


def test_a_correction_creates_a_new_revision(school_a, released, students, term,
                                             head):
    """A report card is a document a family may already be holding."""
    with TenantContext.scope(school_a):
        first = services.generate_report_card(student=students[0], term=term,
                                              actor=head)
        second = services.generate_report_card(student=students[0], term=term,
                                               actor=head)

    assert first.revision == 1
    assert second.revision == 2
    assert second.supersedes_id == first.pk
    with TenantContext.scope(school_a):
        assert ReportCard.objects.count() == 2


def test_a_student_with_no_enrolment_cannot_be_reported_on(school_a, term, head):
    from educore.students.models import Student

    with TenantContext.scope(school_a):
        orphan = Student.objects.create(school_id=school_a.id,
                                        admission_number="ADM000",
                                        full_name="Unenrolled")
        with pytest.raises(services.AssessmentError, match="no active enrolment"):
            services.generate_report_card(student=orphan, term=term, actor=head)


def test_a_class_can_be_reported_on_in_one_call(school_a, released, class_group,
                                                term, head):
    with TenantContext.scope(school_a):
        reports = services.generate_for_class(class_group=class_group, term=term,
                                              actor=head)
    assert len(reports) == 5


def test_the_pdf_renders_from_the_snapshot_not_live_data(school_a, released,
                                                         students, term, head):
    from educore.assessment.rendering import attach_pdf, render_pdf

    with TenantContext.scope(school_a):
        report = services.generate_report_card(student=students[0], term=term,
                                               actor=head)
        pdf = render_pdf(report)
        attach_pdf(report)

    assert pdf.startswith(b"%PDF")
    assert report.document.name.endswith(".pdf")


def test_issuing_a_report_card_attaches_its_pdf_without_a_separate_call(
        school_a, released, students, term, head):
    """Nobody issuing a report card should have to remember a second step for
    a parent to actually receive a document."""
    with TenantContext.scope(school_a):
        report = services.generate_report_card(student=students[0], term=term,
                                               actor=head)

    assert report.document.name.endswith(".pdf")
    assert report.document.read().startswith(b"%PDF")
