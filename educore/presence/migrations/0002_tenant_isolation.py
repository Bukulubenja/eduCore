"""Tenant isolation and append-only enforcement for the presence tables.

AttendanceEvent and AttendanceSignal are facts. Everything else in the system
is derived from them, so if they can be edited the whole evidence chain is
decorative. The trigger is the control; the Python guard is a convenience.
"""

from django.db import migrations

from educore.core.db import rls

TENANT_TABLES = [
    "presence_attendancepolicy",
    "presence_dutyschedule",
    "presence_attendanceevent",
    "presence_attendancesignal",
    "presence_qrredemption",
    "presence_attendancerecord",
    "presence_attendanceexception",
]

COMPOSITE_FKS = [
    ("presence_dutyschedule", "membership_id", "core_membership"),
    ("presence_attendanceevent", "membership_id", "core_membership"),
    ("presence_attendanceevent", "campus_id", "core_campus"),
    ("presence_attendanceevent", "device_id", "core_device"),
    ("presence_attendancesignal", "event_id", "presence_attendanceevent"),
    ("presence_qrredemption", "membership_id", "core_membership"),
    ("presence_attendancerecord", "membership_id", "core_membership"),
    ("presence_attendanceexception", "record_id", "presence_attendancerecord"),
    ("presence_attendanceexception", "raised_by_id", "core_membership"),
    ("presence_attendanceexception", "decided_by_id", "core_membership"),
]

APPEND_ONLY = ["presence_attendanceevent", "presence_attendancesignal"]


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


def add_append_only(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    for table in APPEND_ONLY:
        schema_editor.execute(f"""
            DROP TRIGGER IF EXISTS {table}_append_only ON {table};
            CREATE TRIGGER {table}_append_only
                BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION educore_reject_mutation();
        """)


def drop_append_only(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    for table in APPEND_ONLY:
        schema_editor.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table};")


class Migration(migrations.Migration):

    dependencies = [
        ("presence", "0001_initial"),
        ("core", "0005_refreshtoken_isolation"),
    ]

    operations = [
        migrations.RunPython(add_composite_fks, drop_composite_fks),
        rls(*TENANT_TABLES),
        migrations.RunPython(add_append_only, drop_append_only),
    ]
