"""Make the audit trail append-only in the database, not merely by convention.

A Python-level guard (AuditEvent.save) protects the ORM path. It does nothing
about ``UPDATE core_auditevent SET ...`` from a psql session, which is exactly
the path an insider with database access would use. GRANT-based restrictions
do not help either, because the application usually connects as the table
owner. A trigger does.
"""

from django.db import migrations

APPEND_ONLY_FN = """
CREATE OR REPLACE FUNCTION educore_reject_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'relation % is append-only; % is not permitted',
        TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;
"""

ATTACH = """
DROP TRIGGER IF EXISTS core_auditevent_append_only ON core_auditevent;
CREATE TRIGGER core_auditevent_append_only
    BEFORE UPDATE OR DELETE ON core_auditevent
    FOR EACH ROW EXECUTE FUNCTION educore_reject_mutation();
"""

DETACH = """
DROP TRIGGER IF EXISTS core_auditevent_append_only ON core_auditevent;
DROP FUNCTION IF EXISTS educore_reject_mutation();
"""


def apply(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(APPEND_ONLY_FN)
    schema_editor.execute(ATTACH)


def revert(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(DETACH)


class Migration(migrations.Migration):

    dependencies = [("core", "0002_tenant_isolation")]

    operations = [migrations.RunPython(apply, revert)]
