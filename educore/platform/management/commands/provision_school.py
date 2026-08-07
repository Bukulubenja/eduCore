"""Create a new tenant, ready to use.

The Phase 4 exit criterion is that a non-engineer can onboard a school in
under a day. This command is the first half of that: everything a new school
needs to be usable on day one is created here, not left on a checklist.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from educore.platform import services


class Command(BaseCommand):
    help = "Provision a new school with roles, policy, grading scale and admin."

    def add_arguments(self, parser):
        parser.add_argument("name")
        parser.add_argument("--slug", default="")
        parser.add_argument("--plan", dest="plan_code", default="")
        parser.add_argument("--timezone", dest="timezone_name",
                            default="Africa/Kampala")
        parser.add_argument("--country", default="UG")
        parser.add_argument("--admin-email", dest="admin_email", default="")
        parser.add_argument("--admin-name", dest="admin_name", default="")
        parser.add_argument("--trial-days", dest="trial_days", type=int,
                            default=30)

    def handle(self, *args, **options):
        try:
            result = services.provision_school(
                name=options["name"],
                slug=options["slug"],
                plan_code=options["plan_code"],
                timezone_name=options["timezone_name"],
                country=options["country"],
                admin_email=options["admin_email"],
                admin_name=options["admin_name"],
                trial_days=options["trial_days"],
            )
        except services.ProvisioningError as exc:
            raise SystemExit(f"Could not provision: {exc}") from exc

        school = result["school"]
        self.stdout.write(self.style.SUCCESS(f"Provisioned {school.name}"))
        self.stdout.write(f"  slug:     {school.slug}")
        self.stdout.write(f"  id:       {school.id}")
        self.stdout.write(f"  campus:   {result['campus'].name}")
        if result["subscription"]:
            subscription = result["subscription"]
            self.stdout.write(
                f"  plan:     {subscription.plan.code} "
                f"(trial ends {subscription.trial_ends_on})"
            )
        if result["admin_membership"]:
            self.stdout.write(
                f"  admin:    {result['admin_membership'].user.email} "
                "(invited; must set a password)"
            )
        self.stdout.write(
            "\nNext: import staff, then class groups, then students "
            "(POST /api/v1/imports/...)."
        )
