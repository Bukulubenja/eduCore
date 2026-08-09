from django.db import migrations

from educore.core.db import composite_fks, rls


class Migration(migrations.Migration):

    dependencies = [("core", "0008_invitetoken")]

    operations = [
        composite_fks(
            ("core_invitetoken", "membership_id", "core_membership"),
        ),
        rls("core_invitetoken"),
    ]
