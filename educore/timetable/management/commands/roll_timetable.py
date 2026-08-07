"""Nightly timetable upkeep.

Two jobs that must run for the accountability chain to mean anything:

  * materialise the next horizon of lesson instances, so a teacher's app can
    cache a fortnight offline;
  * settle instances whose window has passed into `missed` or `abandoned`.

The second is the one that matters. A missed lesson is only visible because a
scheduled job says so, consistently and without favour -- if a human had to
mark it, the discretion schools already have would come straight back.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from educore.core.models import School
from educore.core.tenancy import TenantContext
from educore.timetable import services


class Command(BaseCommand):
    help = "Materialise upcoming lesson instances and settle missed lessons."

    def add_arguments(self, parser):
        parser.add_argument("--school", dest="school_slug", default=None)
        parser.add_argument("--horizon", dest="horizon", type=int,
                            default=services.HORIZON_DAYS)
        parser.add_argument("--grace", dest="grace", type=int, default=10,
                            help="Minutes after a lesson ends before it counts "
                                 "as missed.")

    def handle(self, *args, **options):
        schools = School.objects.filter(status=School.Status.ACTIVE)
        if options["school_slug"]:
            schools = schools.filter(slug=options["school_slug"])

        totals = {"created": 0, "missed": 0, "abandoned": 0}
        for school in schools.iterator():
            with TenantContext.scope(school.id):
                created = services.materialise_horizon(
                    horizon_days=options["horizon"]
                )
                settled = services.close_out_missed(grace_minutes=options["grace"])

            totals["created"] += created
            totals["missed"] += settled["missed"]
            totals["abandoned"] += settled["abandoned"]
            self.stdout.write(
                f"  {school.slug}: +{created} instances, "
                f"{settled['missed']} missed, {settled['abandoned']} abandoned"
            )

        self.stdout.write(self.style.SUCCESS(
            f"Done. {totals['created']} instances created, "
            f"{totals['missed']} missed, {totals['abandoned']} abandoned."
        ))
