"""Audience selectors and the results-released notification."""

from __future__ import annotations

import pyotp
import pytest

from educore.comms import services as comms
from educore.comms.models import Announcement, Channel, Notification
from educore.core import mfa
from educore.core.outbox import relay_all_tenants
from educore.core.tenancy import TenantContext
from educore.students.models import GuardianLink

pytestmark = pytest.mark.django_db


@pytest.fixture
def parents(school_a, students, make_membership):
    """One verified guardian per student, plus one unverified."""
    created = []
    with TenantContext.scope(school_a):
        for index, student in enumerate(students[:3]):
            membership = make_membership(school_a,
                                         email=f"parent{index}@example.com")
            GuardianLink.objects.create(
                school_id=school_a.id, student=student, membership=membership,
                relationship=GuardianLink.Relationship.MOTHER, verified=True,
            )
            created.append(membership)

        stranger = make_membership(school_a, email="unverified@example.com")
        GuardianLink.objects.create(
            school_id=school_a.id, student=students[0], membership=stranger,
            relationship=GuardianLink.Relationship.OTHER, verified=False,
        )
    return created


# -- Audience resolution -----------------------------------------------------


def test_a_role_selector_resolves_to_its_holders(school_a, deputy, teacher):
    with TenantContext.scope(school_a):
        resolved = comms.resolve_audience({"roles": ["deputy"]})

    assert [m.pk for m in resolved] == [deputy.pk]


def test_a_class_group_selector_reaches_verified_guardians(school_a, parents,
                                                           class_group):
    """An unverified link is not a distribution channel."""
    with TenantContext.scope(school_a):
        resolved = comms.resolve_audience(
            {"class_groups": [str(class_group.id)]}
        )

    assert sorted(m.pk for m in resolved) == sorted(p.pk for p in parents)


def test_an_empty_selector_reaches_nobody(school_a, parents):
    """The default must be silence, not the whole school."""
    with TenantContext.scope(school_a):
        assert comms.resolve_audience({}) == []


def test_selectors_combine_without_duplicating(school_a, parents, class_group,
                                               deputy):
    with TenantContext.scope(school_a):
        resolved = comms.resolve_audience({
            "roles": ["deputy"],
            "class_groups": [str(class_group.id)],
            "memberships": [str(parents[0].pk)],
        })

    ids = [m.pk for m in resolved]
    assert len(ids) == len(set(ids))
    assert deputy.pk in ids
    assert parents[0].pk in ids


def test_the_audience_is_resolved_at_publish_not_at_draft(school_a, parents,
                                                          class_group, students,
                                                          term, make_membership,
                                                          teacher):
    """A guardian added on Tuesday receives Wednesday's notice.

    A distribution list maintained by hand is a list that is wrong within a
    fortnight.
    """
    with TenantContext.scope(school_a):
        announcement = Announcement.objects.create(
            school_id=school_a.id, author=teacher, title="Sports day",
            body="Sports day is on Friday.",
            audience={"class_groups": [str(class_group.id)]},
        )

        latecomer = make_membership(school_a, email="latecomer@example.com")
        GuardianLink.objects.create(
            school_id=school_a.id, student=students[3], membership=latecomer,
            relationship=GuardianLink.Relationship.FATHER, verified=True,
        )

        comms.publish_announcement(announcement, actor=teacher)
        announcement.refresh_from_db()
        reached = set(Notification.objects
                      .filter(topic="comms.announcement")
                      .values_list("recipient_id", flat=True))

    assert announcement.recipients_count == 4
    assert latecomer.pk in reached


def test_publishing_twice_is_refused(school_a, parents, class_group, teacher):
    with TenantContext.scope(school_a):
        announcement = Announcement.objects.create(
            school_id=school_a.id, author=teacher, title="Notice", body="Body",
            audience={"class_groups": [str(class_group.id)]},
        )
        comms.publish_announcement(announcement, actor=teacher)

        with pytest.raises(comms.CommsError, match="already been published"):
            comms.publish_announcement(announcement, actor=teacher)


def test_an_urgent_announcement_ignores_muted_channels(school_a, parents,
                                                       class_group, teacher):
    from educore.comms.models import ChannelPreference, Delivery

    with TenantContext.scope(school_a):
        ChannelPreference.objects.create(school_id=school_a.id,
                                         membership=parents[0],
                                         push_enabled=False)
        announcement = Announcement.objects.create(
            school_id=school_a.id, author=teacher, title="Evacuate",
            body="Leave the building now.",
            importance=Notification.Importance.URGENT,
            audience={"class_groups": [str(class_group.id)]},
            channels=[Channel.IN_APP, Channel.PUSH],
        )
        comms.publish_announcement(announcement, actor=teacher)

        push = Delivery.objects.get(channel=Channel.PUSH,
                                    notification__recipient=parents[0])

    assert push.status == Delivery.Status.QUEUED


# -- Results released --------------------------------------------------------


def test_released_results_notify_guardians_without_disclosing_marks(
    school_a, course, term, class_group, teacher, students, parents,
    make_membership, grant_role
):
    """A push preview on a lock screen is not a private channel."""
    from decimal import Decimal

    from django.core.files.base import ContentFile

    from educore.assessment import services as assessment_services
    from educore.assessment.models import Assessment, GradingScale

    head = make_membership(school_a, email="head@example.com")
    grant_role(head, "head_teacher", "Head Teacher")

    with TenantContext.scope(school_a):
        GradingScale.objects.create(school_id=school_a.id, name="Default",
                                    is_default=True)
        assessment = Assessment.objects.create(
            school_id=school_a.id, course=course, term=term, title="Term Test",
            kind=Assessment.Kind.END_OF_TERM, max_score=Decimal("100"),
            author=teacher,
        )
        assessment.class_groups.add(class_group)
        assessment.paper.save("p.pdf", ContentFile(b"%PDF-1.4"), save=True)

        assessment_services.submit(assessment, actor=teacher)
        assessment_services.approve(assessment, actor=head)
        assessment_services.lock(assessment, actor=head)
        assessment_services.begin_scoring(assessment, actor=head)
        assessment_services.enter_scores(
            assessment, actor=teacher,
            marks=[{"student_id": s.id, "raw_score": 71} for s in students],
        )
        assessment_services.submit_marks(assessment, actor=teacher)
        assessment_services.moderate(assessment, actor=head)

        details = mfa.provision(head.user)
        mfa.confirm(head.user, pyotp.TOTP(details["secret"]).now())
        _grant, token = mfa.grant_step_up(head, pyotp.TOTP(details["secret"]).now())
        assessment_services.release(assessment, actor=head, step_up_token=token)

    relay_all_tenants()

    with TenantContext.scope(school_a):
        notifications = Notification.objects.filter(topic="assessment.released")
        bodies = " ".join(n.body for n in notifications)

    # Three verified guardians; the unverified link gets nothing.
    assert notifications.count() == 3
    assert "71" not in bodies
    assert "Sign in" in bodies
