"""Flag student-movement records that are past their expected return.

Doc 08. Every other movement event explains something that happened -- a
request, an approval, a departure. This is the one job that notices the
*absence* of one: a boarder or a trip that should be back and is not.

Safe to run on a tight schedule: `movement.services.sweep_overdue` emits one
outbox message per overdue record, and `comms` collapses repeat runs onto a
single notification per record.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from educore.core.models import School
from educore.core.tenancy import TenantContext
from educore.movement import services


class Command(BaseCommand):
    help = "Emit overdue-return alerts for pass-outs and trips past their time."

    def add_arguments(self, parser):
        parser.add_argument("--school", dest="school_slug", default=None)

    def handle(self, *args, **options):
        schools = School.objects.filter(status=School.Status.ACTIVE)
        if options["school_slug"]:
            schools = schools.filter(slug=options["school_slug"])

        totals = {"pass_outs": 0, "trips": 0}  # nosec B105 -- "pass" in a dict key
        for school in schools.iterator():
            with TenantContext.scope(school.id):
                counts = services.sweep_overdue()
            totals["pass_outs"] += counts["pass_outs"]
            totals["trips"] += counts["trips"]
            if counts["pass_outs"] or counts["trips"]:
                self.stdout.write(
                    f"  {school.slug}: {counts['pass_outs']} pass-out(s), "
                    f"{counts['trips']} trip(s) overdue"
                )

        self.stdout.write(self.style.SUCCESS(
            f"Done. {totals['pass_outs']} pass-out(s) and {totals['trips']} "
            "trip(s) flagged overdue."
        ))
