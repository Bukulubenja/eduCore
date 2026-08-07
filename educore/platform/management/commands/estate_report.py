"""Daily metering and estate health.

The operator's early-warning view. A school that stopped syncing three days
ago has a problem now, not at renewal -- and nobody finds that out by waiting
for them to complain.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from educore.platform import services


class Command(BaseCommand):
    help = "Meter every school and report estate health."

    def add_arguments(self, parser):
        parser.add_argument("--no-meter", dest="meter", action="store_false",
                            default=True)

    def handle(self, *args, **options):
        if options["meter"]:
            metered = services.meter_all()
            self.stdout.write(f"Metered {metered} school(s).")

        unhealthy = 0
        for health in services.estate_health():
            if health["healthy"]:
                self.stdout.write(f"  ok    {health['school']}")
                continue
            unhealthy += 1
            self.stderr.write(self.style.WARNING(f"  warn  {health['school']}"))
            for warning in health["warnings"]:
                self.stderr.write(f"          {warning}")

        if unhealthy:
            self.stdout.write(self.style.WARNING(
                f"\n{unhealthy} school(s) need attention."
            ))
        else:
            self.stdout.write(self.style.SUCCESS("\nWhole estate healthy."))
