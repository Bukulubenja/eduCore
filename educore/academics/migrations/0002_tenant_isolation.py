from django.db import migrations

from educore.core.db import rls

TENANT_TABLES = [
    "academics_academicyear",
    "academics_term",
    "academics_level",
    "academics_classgroup",
    "academics_department",
    "academics_staffprofile",
]

COMPOSITE_FKS = [
    ("academics_term", "academic_year_id", "academics_academicyear"),
    ("academics_classgroup", "level_id", "academics_level"),
    ("academics_classgroup", "class_teacher_id", "core_membership"),
    ("academics_department", "head_id", "core_membership"),
    ("academics_staffprofile", "membership_id", "core_membership"),
    ("academics_staffprofile", "department_id", "academics_department"),
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
        ("academics", "0001_initial"),
        ("core", "0005_refreshtoken_isolation"),
    ]

    operations = [
        migrations.RunPython(add_composite_fks, drop_composite_fks),
        rls(*TENANT_TABLES),
    ]
