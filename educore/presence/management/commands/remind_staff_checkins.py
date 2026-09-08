"""Timed check-in reminders for staff who have not checked in yet.

Doc 04 / SSOMS §6. The teacher-facing half of the check-in system: a nudge
before the window opens, one when it is due, one when it is late. Escalation
to leadership is the separate `alert_staff_absences` job.

Run on the same cadence as `alert_staff_absences`. Firing often is harmless:
`presence.raise_checkin_reminders` only emits for members currently inside a
stage window, and `comms` dedupes on (member, date, stage) so each nudge
lands once.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from educore.core.models import School
from educore.core.tenancy import TenantContext
from educore.presence import services


class Command(BaseCommand):
    help = "Send staged check-in reminders to staff who have not checked in."

    def add_arguments(self, parser):
        parser.add_argument("--school", dest="school_slug", default=None)

    def handle(self, *args, **options):
        schools = School.objects.filter(status=School.Status.ACTIVE)
        if options["school_slug"]:
            schools = schools.filter(slug=options["school_slug"])

        total = 0
        for school in schools.iterator():
            with TenantContext.scope(school.id):
                sent = services.raise_checkin_reminders()
            total += sent
            if sent:
                self.stdout.write(f"  {school.slug}: {sent} reminder(s)")

        self.stdout.write(self.style.SUCCESS(
            f"Done. {total} check-in reminder(s) queued."
        ))
