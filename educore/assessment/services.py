"""Assessment use cases: the state machine, scoring, moderation, release."""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from educore.core import audit, mfa
from educore.core.models import OutboxMessage
from educore.students.models import Enrolment, GuardianLink, Student

from . import rendering
from .models import (
    Assessment,
    AssessmentState,
    GradingScale,
    ReportCard,
    Score,
)


class AssessmentError(Exception):
    pass


class InvalidTransitionError(AssessmentError):
    pass


# Which states each transition may be applied from. Written as data so the
# permitted paths can be read at a glance rather than reconstructed from a
# scatter of `if` statements.
TRANSITIONS = {
    "submit": ({AssessmentState.DRAFT}, AssessmentState.SUBMITTED),
    "approve": ({AssessmentState.SUBMITTED}, AssessmentState.APPROVED),
    "return_to_author": ({AssessmentState.SUBMITTED, AssessmentState.MODERATION,
                          AssessmentState.HEAD_REVIEW}, AssessmentState.DRAFT),
    "lock": ({AssessmentState.APPROVED}, AssessmentState.LOCKED),
    "begin_scoring": ({AssessmentState.LOCKED}, AssessmentState.SCORING),
    "submit_marks": ({AssessmentState.SCORING}, AssessmentState.MODERATION),
    "return_for_correction": ({AssessmentState.MODERATION},
                              AssessmentState.SCORING),
    "moderate": ({AssessmentState.MODERATION}, AssessmentState.HEAD_REVIEW),
    "release": ({AssessmentState.HEAD_REVIEW}, AssessmentState.RELEASED),
}


def _transition(assessment: Assessment, action: str, *, actor, note: str = "",
                **updates) -> Assessment:
    allowed_from, target = TRANSITIONS[action]
    if assessment.state not in allowed_from:
        raise InvalidTransitionError(
            f"Cannot {action.replace('_', ' ')} an assessment that is "
            f"'{assessment.get_state_display()}'."
        )

    before = {"state": assessment.state}
    assessment.state = target
    assessment.returned_note = note
    for field, value in updates.items():
        setattr(assessment, field, value)
    assessment.save()

    audit.record(action=f"assessment.{action}",
                 object_type="assessment.Assessment", object_id=assessment.id,
                 actor_membership=actor, before=before,
                 after={"state": assessment.state, "note": note})
    return assessment


# -- Lifecycle ---------------------------------------------------------------


def submit(assessment, *, actor):
    """Teacher sends the paper to the DOS."""
    if not assessment.paper:
        raise AssessmentError("Attach the question paper before submitting.")
    return _transition(assessment, "submit", actor=actor)


def approve(assessment, *, actor):
    return _transition(assessment, "approve", actor=actor,
                       approved_by=actor, approved_at=timezone.now())


def return_to_author(assessment, *, actor, note: str):
    if not note:
        raise AssessmentError("Say why it is being returned.")
    return _transition(assessment, "return_to_author", actor=actor, note=note)


def lock(assessment, *, actor):
    """Freeze the paper for the exam window. No edits from here."""
    return _transition(assessment, "lock", actor=actor)


def begin_scoring(assessment, *, actor):
    """The exam has been sat; marks may now be entered."""
    return _transition(assessment, "begin_scoring", actor=actor)


@transaction.atomic
def enter_scores(assessment, *, actor, marks: list[dict],
                 reason: str = "") -> int:
    """Record marks. Every change is audited with its before and after.

    Scores cannot be entered before the assessment is locked and sat (doc 03,
    invariant 9). After moderation, a change additionally requires a reason:
    that is the point at which someone is altering a mark others have already
    checked.
    """
    if not assessment.accepts_scores:
        raise AssessmentError(
            "Marks can only be entered once the assessment has been sat."
        )
    if assessment.state == AssessmentState.MODERATION and not reason:
        raise AssessmentError(
            "Changing a mark after moderation requires a reason."
        )

    eligible = set(
        Student.objects
        .filter(enrolments__class_group__in=assessment.class_groups.all(),
                enrolments__term=assessment.term, enrolments__is_active=True)
        .values_list("id", flat=True)
    )

    now = timezone.now()
    changed = 0
    for entry in marks:
        student_id = entry["student_id"]
        if student_id not in eligible:
            raise AssessmentError(
                "That student is not enrolled in a class sitting this assessment."
            )

        raw = entry.get("raw_score")
        is_absent = bool(entry.get("is_absent"))
        if raw is not None and Decimal(str(raw)) > assessment.max_score:
            raise AssessmentError(
                f"A mark of {raw} exceeds the maximum of {assessment.max_score}."
            )

        score, created = Score.objects.get_or_create(
            assessment=assessment, student_id=student_id,
            defaults={"school_id": assessment.school_id},
        )

        # Compare as Decimals, not strings. The stored value comes back as
        # Decimal("60.00") and the client sends 60; string equality would call
        # every re-submission a change and fill the audit trail with edits
        # that never happened -- which is exactly the trail a dispute relies on
        # being clean.
        new_raw = None if is_absent else (None if raw is None
                                          else Decimal(str(raw)))
        if (not created and score.raw_score == new_raw
                and score.is_absent == is_absent):
            continue

        before = None if created else {"raw_score": str(score.raw_score),
                                       "is_absent": score.is_absent}
        after = {"raw_score": str(new_raw), "is_absent": is_absent}

        score.raw_score = new_raw
        score.is_absent = is_absent
        score.comment = entry.get("comment", "")
        score.entered_by = actor
        score.entered_at = now
        score.change_reason = reason
        score.save()
        changed += 1

        audit.record(action="assessment.score.changed",
                     object_type="assessment.Score", object_id=score.id,
                     actor_membership=actor, before=before,
                     after={**after, "reason": reason,
                            "student": str(student_id)})

    return changed


def submit_marks(assessment, *, actor):
    """Teacher hands the marks to the DOS for moderation."""
    missing = assessment.scores.filter(raw_score__isnull=True,
                                       is_absent=False).count()
    if missing:
        raise AssessmentError(
            f"{missing} student(s) have neither a mark nor an absence recorded."
        )
    if not assessment.scores.exists():
        raise AssessmentError("No marks have been entered.")
    return _transition(assessment, "submit_marks", actor=actor)


def return_for_correction(assessment, *, actor, note: str):
    if not note:
        raise AssessmentError("Say what needs correcting.")
    return _transition(assessment, "return_for_correction", actor=actor, note=note)


def moderate(assessment, *, actor):
    """DOS accepts the marks and passes them to the head teacher."""
    return _transition(assessment, "moderate", actor=actor,
                       moderated_by=actor, moderated_at=timezone.now())


def release(assessment, *, actor, step_up_token: str = ""):
    """Head teacher authorises publication. Requires step-up MFA.

    The most consequential act in the module: after this, marks are visible to
    students and parents and a correction becomes a public revision. Requiring
    a fresh authenticator code is what makes "the head released these" a claim
    about a person rather than about a browser tab.
    """
    mfa.consume_step_up(actor, step_up_token, action="assessment.release")

    assessment = _transition(assessment, "release", actor=actor,
                             released_by=actor, released_at=timezone.now())

    # The event carries its own recipients. Deciding *who* may be told about a
    # child is this module's business, not the notifier's -- and it keeps
    # `comms` free of any dependency on the domain (doc 02).
    student_ids = list(
        assessment.scores.order_by().values_list("student_id", flat=True).distinct()
    )
    recipients = list(
        GuardianLink.objects
        .filter(student_id__in=student_ids, verified=True,
                receives_notifications=True)
        .order_by()
        .values_list("student_id", "membership_id")
    )

    OutboxMessage.objects.create(
        school_id=assessment.school_id, topic="assessment.released",
        payload={"assessment_id": str(assessment.id),
                 "title": assessment.title,
                 "term_id": str(assessment.term_id),
                 "course_id": str(assessment.course_id),
                 "recipients": [{"student_id": str(s), "membership_id": str(m)}
                                for s, m in recipients]},
    )
    return assessment


def flag_anomalous_grading(assessment) -> list[dict]:
    """Marks that deserve a second look before release.

    Not an accusation. A whole class on an identical mark, or a distribution
    with no spread at all, is usually a data-entry slip and occasionally
    something else -- either way a moderator should see it rather than have it
    slide through unremarked.
    """
    scores = [float(s.raw_score) for s in assessment.scores.all()
              if s.raw_score is not None]
    if len(scores) < 5:
        return []

    flags = []
    distinct = set(scores)
    if len(distinct) == 1:
        flags.append({"code": "identical_marks",
                      "detail": f"Every candidate scored {scores[0]}."})

    mean = sum(scores) / len(scores)
    spread = max(scores) - min(scores)
    if spread <= assessment.max_score * Decimal("0.05"):
        flags.append({"code": "no_spread",
                      "detail": f"All marks fall within {spread} of each other."})
    if mean >= float(assessment.max_score) * 0.95:
        flags.append({"code": "implausibly_high",
                      "detail": f"Mean mark is {mean:.1f} of {assessment.max_score}."})

    return flags


# -- Report cards ------------------------------------------------------------


def _grade_for(assessment, percentage, scale_cache: dict):
    scale = assessment.grading_scale
    if scale is None:
        scale = scale_cache.get("default")
        if scale is None:
            scale = GradingScale.objects.filter(is_default=True).first()
            scale_cache["default"] = scale
    if scale is None or percentage is None:
        return None
    band = scale.band_for(percentage)
    return {"label": band.label, "points": str(band.points) if band.points else None,
            "descriptor": band.descriptor} if band else None


@transaction.atomic
def generate_report_card(*, student: Student, term, actor=None) -> ReportCard:
    """Freeze a student's released results for a term.

    Only released assessments are included. A report card assembled from
    unreleased marks would publish, through the back door, results the head
    teacher has not authorised.
    """
    enrolment = (Enrolment.objects
                 .filter(student=student, term=term, is_active=True)
                 .select_related("class_group")
                 .first())
    if enrolment is None:
        raise AssessmentError("This student has no active enrolment this term.")

    scores = (
        Score.objects
        .filter(student=student, assessment__term=term,
                assessment__state=AssessmentState.RELEASED)
        .select_related("assessment__course__subject", "assessment__grading_scale")
        .order_by("assessment__course__subject__name")
    )

    scale_cache: dict = {}
    subjects: dict = {}
    for score in scores:
        subject = score.assessment.course.subject
        row = subjects.setdefault(str(subject.id), {
            "subject": subject.name, "assessments": [],
            "weighted_total": 0.0, "weight": 0.0,
        })
        percentage = score.percentage
        row["assessments"].append({
            "title": score.assessment.title,
            "kind": score.assessment.kind,
            "raw_score": str(score.raw_score) if score.raw_score is not None else None,
            "max_score": str(score.assessment.max_score),
            "percentage": percentage,
            "is_absent": score.is_absent,
            "grade": _grade_for(score.assessment, percentage, scale_cache),
        })
        if percentage is not None:
            weight = float(score.assessment.weight)
            row["weighted_total"] += percentage * weight
            row["weight"] += weight

    for row in subjects.values():
        row["average"] = (round(row["weighted_total"] / row["weight"], 2)
                          if row["weight"] else None)
        del row["weighted_total"]
        del row["weight"]

    previous = (ReportCard.objects
                .filter(student=student, term=term)
                .order_by("-revision").first())

    report = ReportCard.objects.create(
        school_id=student.school_id,
        student=student,
        term=term,
        class_group=enrolment.class_group,
        revision=(previous.revision + 1) if previous else 1,
        supersedes=previous,
        issued_by=actor,
        issued_at=timezone.now(),
        content={
            "student": {"name": student.full_name,
                        "admission_number": student.admission_number},
            "class_group": enrolment.class_group.name,
            "term": str(term),
            "generated_at": timezone.now().isoformat(),
            "subjects": list(subjects.values()),
        },
    )

    rendering.attach_pdf(report)

    audit.record(action="assessment.report_card.issued",
                 object_type="assessment.ReportCard", object_id=report.id,
                 actor_membership=actor,
                 after={"student": str(student.id), "term": str(term.pk),
                        "revision": report.revision})
    return report


def generate_for_class(*, class_group, term, actor=None) -> list[ReportCard]:
    students = Student.objects.filter(
        enrolments__class_group=class_group, enrolments__term=term,
        enrolments__is_active=True, status=Student.Status.ENROLLED,
    ).distinct()
    return [generate_report_card(student=s, term=term, actor=actor)
            for s in students]
