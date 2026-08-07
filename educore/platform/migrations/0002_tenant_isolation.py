"""Isolation for the one tenant-owned table in this module.

Plan, Subscription and UsageRecord are operator-owned: they reference a school
but belong to us, and no school-scoped session has a path to them. ImportBatch
is different -- it is a school's own record of its onboarding -- so it gets the
usual treatment.
"""

from django.db import migrations

from educore.core.db import composite_fks, rls


class Migration(migrations.Migration):

    dependencies = [
        ("platform", "0001_initial"),
        ("core", "0007_stepup_isolation"),
    ]

    operations = [
        composite_fks(
            ("platform_importbatch", "uploaded_by_id", "core_membership"),
        ),
        rls("platform_importbatch"),
    ]
