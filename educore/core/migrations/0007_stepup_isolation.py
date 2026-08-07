from django.db import migrations

from educore.core.db import composite_fks, rls


class Migration(migrations.Migration):

    dependencies = [("core", "0006_user_mfa_secret_stepupgrant")]

    operations = [
        composite_fks(
            ("core_stepupgrant", "membership_id", "core_membership"),
        ),
        rls("core_stepupgrant"),
    ]
