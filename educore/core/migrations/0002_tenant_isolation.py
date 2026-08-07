"""Isolation layers 2 and 3 at the database level (ADR-0001).

Two distinct controls:

* Composite foreign keys ``(school_id, <fk>)`` -> ``(school_id, id)``. These
  make a cross-tenant *reference* unrepresentable: a Device in School A cannot
  point at a Membership in School B, because the database rejects the row.
  Django models a single-column FK; the composite constraint is added here in
  addition to it.

* Row-level security, the backstop that survives an ORM mistake or raw SQL.

Both are PostgreSQL-only and are skipped on other engines, leaving layer 3
unexercised. That is acceptable on a workstation and refused in production by
config/settings/production.py.
"""

from django.db import migrations

from educore.core.db import rls

TENANT_TABLES = [
    "core_campus",
    "core_membership",
    "core_role",
    "core_roleassignment",
    "core_device",
    "core_auditevent",
    "core_outboxmessage",
]

# (table, column, referenced table, on delete)
COMPOSITE_FKS = [
    ("core_device", "membership_id", "core_membership", "CASCADE"),
    ("core_device", "approved_by_id", "core_membership", "SET NULL"),
    ("core_roleassignment", "membership_id", "core_membership", "CASCADE"),
    ("core_roleassignment", "role_id", "core_role", "RESTRICT"),
    ("core_auditevent", "actor_membership_id", "core_membership", "SET NULL"),
    ("core_auditevent", "device_id", "core_device", "SET NULL"),
]


def _constraint_name(table, column):
    return f"{table}_{column}_same_school_fk"


def add_composite_fks(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    for table, column, ref_table, on_delete in COMPOSITE_FKS:
        name = _constraint_name(table, column)
        # ON DELETE SET NULL on a composite key would try to null school_id
        # too, which is NOT NULL. MATCH SIMPLE means the constraint is not
        # enforced when any referencing column is NULL, so a nullable FK is
        # already handled; deletes are guarded by the single-column FK Django
        # created alongside this one.
        action = "NO ACTION" if on_delete == "SET NULL" else on_delete
        schema_editor.execute(f"""
            ALTER TABLE {table}
            DROP CONSTRAINT IF EXISTS {name};
            ALTER TABLE {table}
            ADD CONSTRAINT {name}
            FOREIGN KEY (school_id, {column})
            REFERENCES {ref_table} (school_id, id)
            MATCH SIMPLE
            ON DELETE {action}
            DEFERRABLE INITIALLY DEFERRED;
        """)


def drop_composite_fks(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    for table, column, _ref, _on_delete in COMPOSITE_FKS:
        schema_editor.execute(
            f"ALTER TABLE {table} "
            f"DROP CONSTRAINT IF EXISTS {_constraint_name(table, column)};"
        )


class Migration(migrations.Migration):

    dependencies = [("core", "0001_initial")]

    operations = [
        migrations.RunPython(add_composite_fks, drop_composite_fks),
        rls(*TENANT_TABLES),
    ]
