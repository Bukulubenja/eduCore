"""Nightly integrity check of every school's audit chain.

A hash chain that is never verified provides no assurance at all -- it only
looks like it does. This command is the thing that turns the chain into a
control. Schedule it daily and alert on any non-zero exit.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from educore.core.audit import verify_chain
from educore.core.models import School


class Command(BaseCommand):
    help = "Verify the audit hash chain for every school; exit non-zero on any break."

    def add_arguments(self, parser):
        parser.add_argument("--school", dest="school_slug", default=None,
                            help="Verify a single school by slug.")

    def handle(self, *args, **options):
        schools = School.objects.all()
        if options["school_slug"]:
            schools = schools.filter(slug=options["school_slug"])

        total_breaks = 0
        for school in schools.iterator():
            breaks = verify_chain(school.id)
            if not breaks:
                self.stdout.write(f"  ok   {school.slug}")
                continue

            total_breaks += len(breaks)
            self.stderr.write(self.style.ERROR(
                f"  BREAK {school.slug}: {len(breaks)} problem(s)"
            ))
            for item in breaks[:20]:
                self.stderr.write(
                    f"        seq {item['sequence']}: {item['reason']} -- {item['detail']}"
                )

        if total_breaks:
            # Treat as a security incident until proven otherwise (doc 06).
            raise SystemExit(1)

        self.stdout.write(self.style.SUCCESS("All audit chains intact."))
