"""Extend tenant isolation to the tables added with Phase 1 auth."""

from django.db import migrations

from educore.core.db import rls

COMPOSITE_FKS = [
    ("core_refreshtoken", "membership_id", "core_membership"),
    ("core_refreshtoken", "device_id", "core_device"),
]


def add_composite_fks(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    for table, column, ref_table in COMPOSITE_FKS:
        name = f"{table}_{column}_same_school_fk"
        schema_editor.execute(f"""
            ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name};
            ALTER TABLE {table}
            ADD CONSTRAINT {name}
            FOREIGN KEY (school_id, {column})
            REFERENCES {ref_table} (school_id, id)
            MATCH SIMPLE ON DELETE NO ACTION
            DEFERRABLE INITIALLY DEFERRED;
        """)


def drop_composite_fks(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    for table, column, _ref in COMPOSITE_FKS:
        schema_editor.execute(
            f"ALTER TABLE {table} "
            f"DROP CONSTRAINT IF EXISTS {table}_{column}_same_school_fk;"
        )


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0004_campus_beacon_ids_campus_wifi_bssid_hashes_and_more"),
    ]

    operations = [
        migrations.RunPython(add_composite_fks, drop_composite_fks),
        rls("core_refreshtoken"),
    ]
