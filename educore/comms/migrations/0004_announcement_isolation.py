from django.db import migrations

from educore.core.db import composite_fks, rls


class Migration(migrations.Migration):

    dependencies = [("comms", "0003_announcement")]

    operations = [
        composite_fks(
            ("comms_announcement", "author_id", "core_membership"),
        ),
        rls("comms_announcement"),
    ]
