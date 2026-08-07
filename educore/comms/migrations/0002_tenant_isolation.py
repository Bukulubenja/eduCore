from django.db import migrations

from educore.core.db import composite_fks, rls

TENANT_TABLES = [
    "comms_notification",
    "comms_delivery",
    "comms_channelpreference",
]


class Migration(migrations.Migration):

    dependencies = [
        ("comms", "0001_initial"),
        ("core", "0005_refreshtoken_isolation"),
    ]

    operations = [
        composite_fks(
            ("comms_notification", "recipient_id", "core_membership"),
            ("comms_delivery", "notification_id", "comms_notification"),
            ("comms_delivery", "escalated_from_id", "comms_delivery"),
            ("comms_channelpreference", "membership_id", "core_membership"),
        ),
        rls(*TENANT_TABLES),
    ]
