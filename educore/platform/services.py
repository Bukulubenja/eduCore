"""Provisioning, suspension, metering, tenant health.

Doc 07's Phase 4 exit criterion is that a non-engineer can onboard a school in
under a day. That is a statement about this module: everything a new tenant
needs to be usable on day one has to be created by `provision_school`, not by
someone remembering a checklist.
"""

from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from educore.core import audit
from educore.core.models import Membership, Role, School, User
from educore.core.tenancy import TenantContext

from .models import Plan, Subscription, UsageRecord


class ProvisioningError(Exception):
    pass


# The roles every school starts with. Seeded per tenant rather than shared, so
# a school can rename "DOS" to "Deputy Head (Academic)" without affecting
# anyone else (doc 03).
ROLE_TEMPLATES = [
    ("director", "Director", ["school.*"]),
    ("head_teacher", "Head Teacher", ["academics.*", "assessment.release"]),
    ("deputy", "Deputy Head", ["academics.*", "presence.review"]),
    ("dos", "Director of Studies", ["academics.*", "assessment.moderate"]),
    ("hod", "Head of Department", ["department.*"]),
    ("bursar", "Bursar", ["finance.*"]),
    ("teacher", "Teacher", ["lesson.deliver", "assessment.author"]),
    ("class_teacher", "Class Teacher", ["register.mark"]),
    ("ict_admin", "ICT Administrator", ["users.manage", "devices.approve"]),
    ("parent", "Parent", ["ward.read"]),
    ("student", "Student", ["self.read"]),
]


@transaction.atomic
def provision_school(*, name: str, slug: str = "", plan_code: str = "",
                     timezone_name: str = "Africa/Kampala", country: str = "UG",
                     admin_email: str = "", admin_name: str = "",
                     trial_days: int = 30) -> dict:
    """Create a tenant that is usable the moment it exists.

    Everything seeded here is something a school would otherwise have to be
    talked through on the phone: roles, an attendance policy, a grading scale,
    a campus, and a first administrator who can log in.
    """
    slug = slug or slugify(name)
    if School.objects.filter(slug=slug).exists():
        raise ProvisioningError(f"A school with the slug '{slug}' already exists.")

    school = School.objects.create(
        name=name, slug=slug, timezone=timezone_name, country=country,
        status=School.Status.ACTIVE,
    )

    with TenantContext.scope(school.id):
        for code, label, permissions in ROLE_TEMPLATES:
            Role.objects.create(school_id=school.id, code=code, name=label,
                                permissions=permissions, is_system=True)

        campus = _seed_campus(school)
        _seed_attendance_policy(school)
        _seed_grading_scale(school)

        admin_membership = None
        if admin_email:
            admin_membership = _seed_administrator(school, admin_email, admin_name)

        audit.record(action="platform.school.provisioned",
                     object_type="core.School", object_id=school.id,
                     actor_membership=admin_membership,
                     after={"name": name, "slug": slug, "plan": plan_code})

    subscription = None
    if plan_code:
        plan = Plan.objects.filter(code=plan_code, is_active=True).first()
        if plan is None:
            raise ProvisioningError(f"No active plan with code '{plan_code}'.")
        today = timezone.localdate()
        subscription = Subscription.objects.create(
            school=school, plan=plan, status=Subscription.Status.TRIAL,
            started_on=today, trial_ends_on=today + timedelta(days=trial_days),
        )

    return {
        "school": school,
        "campus": campus,
        "subscription": subscription,
        "admin_membership": admin_membership,
    }


def _seed_campus(school):
    from educore.core.models import Campus

    return Campus.objects.create(school_id=school.id, name="Main Campus",
                                 is_primary=True)


def _seed_attendance_policy(school):
    from educore.presence.services import active_policy

    # Creating it now rather than lazily means the school's first check-in is
    # scored against a policy someone can inspect beforehand.
    return active_policy(school.id)


def _seed_grading_scale(school):
    from educore.assessment.models import GradeBand, GradingScale

    scale = GradingScale.objects.create(school_id=school.id, name="Default",
                                        is_default=True)
    for label, low, high, points in [
        ("A", 80, 100, 1), ("B", 70, 79.99, 2), ("C", 60, 69.99, 3),
        ("D", 50, 59.99, 4), ("E", 40, 49.99, 5), ("F", 0, 39.99, 9),
    ]:
        GradeBand.objects.create(school_id=school.id, scale=scale, label=label,
                                 min_percentage=low, max_percentage=high,
                                 points=points)
    return scale


def _seed_administrator(school, email: str, full_name: str):
    from educore.core.models import RoleAssignment

    user = User.objects.filter(email=email).first()
    if user is None:
        user = User.objects.create_user(email=email,
                                        full_name=full_name or email,
                                        password=None)
        user.set_unusable_password()        # They set one via invitation.
        user.save(update_fields=["password"])

    membership = Membership.objects.create(
        school_id=school.id, user=user, status=Membership.Status.INVITED,
    )
    for code in ("director", "ict_admin"):
        RoleAssignment.objects.create(
            school_id=school.id, membership=membership,
            role=Role.objects.get(code=code),
        )
    return membership


# -- Lifecycle ---------------------------------------------------------------


@transaction.atomic
def suspend_school(school: School, *, reason: str) -> School:
    """Stop a school using the platform without destroying anything.

    Suspension is reversible and data-preserving on purpose: non-payment is a
    commercial dispute, and a school locked out mid-term still owns its
    records.
    """
    school.status = School.Status.SUSPENDED
    school.save(update_fields=["status", "updated_at"])

    Subscription.objects.filter(school=school).update(
        status=Subscription.Status.SUSPENDED
    )

    with TenantContext.scope(school.id):
        audit.record(action="platform.school.suspended",
                     object_type="core.School", object_id=school.id,
                     after={"reason": reason})
    return school


@transaction.atomic
def reactivate_school(school: School) -> School:
    school.status = School.Status.ACTIVE
    school.save(update_fields=["status", "updated_at"])
    Subscription.objects.filter(school=school).update(
        status=Subscription.Status.ACTIVE
    )
    with TenantContext.scope(school.id):
        audit.record(action="platform.school.reactivated",
                     object_type="core.School", object_id=school.id)
    return school


# -- Metering ----------------------------------------------------------------


def meter_school(school: School, *, on=None) -> UsageRecord:
    """Snapshot what a school is using. Run daily."""
    from educore.comms.models import Delivery
    from educore.delivery.models import LessonSession
    from educore.students.models import Student

    on = on or timezone.localdate()

    with TenantContext.scope(school.id):
        active_staff = Membership.objects.filter(
            status=Membership.Status.ACTIVE
        ).exclude(role_assignments__role__code__in=["parent", "student"]).distinct().count()

        active_students = Student.objects.filter(
            status=Student.Status.ENROLLED
        ).count()

        lessons = LessonSession.objects.filter(
            opened_at__date__gte=on - timedelta(days=30)
        ).count()

        notifications = Delivery.objects.filter(
            created_at__date__gte=on - timedelta(days=30)
        ).exclude(status=Delivery.Status.SKIPPED).count()

    record, _created = UsageRecord.objects.update_or_create(
        school=school, measured_on=on,
        defaults={
            "active_staff": active_staff,
            "active_students": active_students,
            "lessons_recorded": lessons,
            "notifications_sent": notifications,
        },
    )
    return record


def meter_all(*, on=None) -> int:
    return sum(1 for school in School.objects.filter(
        status__in=[School.Status.ACTIVE, School.Status.SUSPENDED]
    ).iterator() if meter_school(school, on=on))


def invoice_preview(school: School, *, on=None) -> dict:
    """What this school would be billed for the current term."""
    subscription = Subscription.objects.filter(school=school).first()
    if subscription is None:
        return {"school_id": str(school.id), "billable": False,
                "reason": "no subscription"}

    usage = (UsageRecord.objects.filter(school=school)
             .order_by("-measured_on").first())
    staff = usage.active_staff if usage else 0
    unit = subscription.plan.price_per_staff_per_term

    return {
        "school_id": str(school.id),
        "billable": subscription.is_live,
        "plan": subscription.plan.code,
        "status": subscription.status,
        "active_staff": staff,
        "unit_price_minor": unit,
        "total_minor": staff * unit,
        "currency": subscription.plan.currency,
        "measured_on": str(usage.measured_on) if usage else None,
    }


# -- Health ------------------------------------------------------------------


def tenant_health(school: School, *, on=None) -> dict:
    """Is this school actually using the product, and is it behaving?

    The operator's early-warning view. A school that stopped syncing three
    days ago has a problem now, not at renewal.
    """
    from educore.core.models import OutboxMessage
    from educore.presence.models import AttendanceEvent, AttendanceRecord, Disposition

    on = on or timezone.localdate()
    week_ago = on - timedelta(days=7)

    with TenantContext.scope(school.id):
        last_event = (AttendanceEvent.objects.order_by("-received_at")
                      .values_list("received_at", flat=True).first())
        records = AttendanceRecord.objects.filter(date__gte=week_ago)
        total = records.count()
        provisional = records.filter(disposition=Disposition.PROVISIONAL).count()
        stuck_outbox = OutboxMessage.objects.filter(
            status=OutboxMessage.Status.FAILED
        ).count()

    silent_days = ((timezone.now() - last_event).days
                   if last_event else None)

    warnings = []
    if silent_days is None:
        warnings.append("no attendance has ever been recorded")
    elif silent_days >= 3:
        warnings.append(f"no attendance received for {silent_days} days")
    if total and provisional / total > 0.10:
        # ADR-0002: this means the weights are miscalibrated for this school,
        # not that the staff are dishonest.
        warnings.append("provisional rate above 10% -- policy needs recalibrating")
    if stuck_outbox:
        warnings.append(f"{stuck_outbox} dead-lettered event(s)")

    return {
        "school_id": str(school.id),
        "school": school.name,
        "status": school.status,
        "last_attendance_at": last_event,
        "silent_days": silent_days,
        "records_last_week": total,
        "provisional_rate": round(provisional / total, 4) if total else None,
        "dead_lettered_events": stuck_outbox,
        "warnings": warnings,
        "healthy": not warnings,
    }


def estate_health(*, on=None) -> list[dict]:
    return [tenant_health(school, on=on)
            for school in School.objects.exclude(status=School.Status.CLOSED)]
