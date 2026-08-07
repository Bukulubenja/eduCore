"""Proactive staff-absence alert to leadership.

Doc 07, Phase 1: "Notifications: staff absence alert to the deputy." Every
other signal in this module is reactive -- it explains an event that already
happened. This is the one job that notices the *absence* of one: staff who
were expected on duty today and have no check-in event at all.

Run every few minutes, same cadence as `roll_timetable`. Firing often is
harmless: `presence.raise_absence_alerts` only acts once the day's grace
window has passed, and `comms` collapses repeat runs onto one notification
per recipient per day (see `comms.handlers.alert_leadership_to_staff_absences`).
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from educore.core.models import School
from educore.core.tenancy import TenantContext
from educore.presence import services


class Command(BaseCommand):
    help = "Alert leadership to staff expected on duty today who never checked in."

    def add_arguments(self, parser):
        parser.add_argument("--school", dest="school_slug", default=None)

    def handle(self, *args, **options):
        schools = School.objects.filter(status=School.Status.ACTIVE)
        if options["school_slug"]:
            schools = schools.filter(slug=options["school_slug"])

        total = 0
        for school in schools.iterator():
            with TenantContext.scope(school.id):
                absent = services.raise_absence_alerts()
            total += absent
            if absent:
                self.stdout.write(f"  {school.slug}: {absent} staff not checked in")

        self.stdout.write(self.style.SUCCESS(
            f"Done. {total} staff absence(s) flagged for leadership."
        ))
