"""Drain the transactional outbox and push queued notifications out.

Three steps, deliberately in one command so a single cron entry cannot leave
the middle one unscheduled:

  1. relay  -- hand domain events to their subscribers, creating notifications
  2. send   -- give queued deliveries to their channel adapters
  3. escalate -- fall back to SMS where a push was never confirmed

Run every minute. Without it the outbox fills silently: rows accumulate, no
error is raised anywhere, and the first sign of trouble is a parent asking why
they were never told.

The command lives in `comms` rather than `core` because it composes the two:
`core` owns the relay mechanism and may not know that notifications exist
(ADR-0005), so the composition belongs to the outer of the two modules.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from educore.comms import services as comms
from educore.core.models import School
from educore.core.outbox import relay_all_tenants
from educore.core.tenancy import TenantContext


class Command(BaseCommand):
    help = "Relay outbox events, send queued notifications, escalate stalled ones."

    def add_arguments(self, parser):
        parser.add_argument("--school", dest="school_slug", default=None)
        parser.add_argument("--limit", dest="limit", type=int, default=100)
        parser.add_argument("--no-send", dest="send", action="store_false",
                            default=True,
                            help="Relay events only; do not contact providers.")

    def handle(self, *args, **options):
        relayed = relay_all_tenants(limit=options["limit"],
                                    school_slug=options["school_slug"])
        self.stdout.write(
            f"  relayed: {relayed['published']} published, "
            f"{relayed['retried']} retried, {relayed['failed']} dead-lettered"
        )

        if not options["send"]:
            return

        schools = School.objects.filter(status=School.Status.ACTIVE)
        if options["school_slug"]:
            schools = schools.filter(slug=options["school_slug"])

        totals = {"sent": 0, "failed": 0, "escalated": 0}
        for school in schools.iterator():
            with TenantContext.scope(school.id):
                sent = comms.send_queued(limit=options["limit"])
                totals["sent"] += sent["sent"]
                totals["failed"] += sent["failed"]
                totals["escalated"] += comms.escalate_stalled()

        self.stdout.write(self.style.SUCCESS(
            f"  delivered: {totals['sent']} sent, {totals['failed']} failed, "
            f"{totals['escalated']} escalated to SMS"
        ))

        if relayed["failed"]:
            # Dead-lettered events are lost work, not noise. Exit non-zero so
            # a scheduler surfaces them rather than a log nobody reads.
            raise SystemExit(1)
