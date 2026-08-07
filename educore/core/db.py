"""Database primitives: identifiers and row-level security.

Everything here is engine-aware. PostgreSQL gets the real behaviour; SQLite
gets a documented no-op so the suite runs on a workstation without a server.
Production refuses to start on anything but PostgreSQL (config/settings/production.py).
"""

from __future__ import annotations

import os
import time
import uuid

from django.db import migrations

# -- Identifiers -------------------------------------------------------------

try:                                    # Python 3.14+
    from uuid import uuid7 as _uuid7
except ImportError:                     # pragma: no cover - older interpreters
    _uuid7 = None


def uuid7() -> uuid.UUID:
    """Time-ordered UUID (RFC 9562 v7).

    Time-ordered matters: these are primary keys on tables that grow by
    millions of rows a year, and random v4 keys scatter B-tree inserts across
    the whole index. They are also generated on mobile devices before any
    server round trip (ADR-0003), so they must be collision-safe off-line.
    """
    if _uuid7 is not None:
        return _uuid7()

    unix_ms = int(time.time() * 1000)
    rand = os.urandom(10)
    raw = bytearray(unix_ms.to_bytes(6, "big") + rand)
    raw[6] = (raw[6] & 0x0F) | 0x70          # version 7
    raw[8] = (raw[8] & 0x3F) | 0x80          # RFC 4122 variant
    return uuid.UUID(bytes=bytes(raw))


# -- Row level security ------------------------------------------------------

TENANT_GUC = "educore.school_id"

_ENABLE_RLS = """
ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {table} FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON {table};
CREATE POLICY tenant_isolation ON {table}
    USING      (school_id = NULLIF(current_setting('{guc}', true), '')::uuid)
    WITH CHECK (school_id = NULLIF(current_setting('{guc}', true), '')::uuid);
"""

_DISABLE_RLS = """
DROP POLICY IF EXISTS tenant_isolation ON {table};
ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;
ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;
"""


def _run(sql_template):
    def _apply(apps, schema_editor):
        if schema_editor.connection.vendor != "postgresql":
            return                      # SQLite: layer 3 unavailable, see doc 02.
        for table in _apply.tables:
            schema_editor.execute(
                sql_template.format(table=table, guc=TENANT_GUC)
            )

    return _apply


def rls(*tables: str) -> migrations.RunPython:
    """Migration operation enabling tenant isolation on the given tables.

    FORCE is not optional. Without it the policy is bypassed by the table
    owner -- which is very likely the role the application connects as, making
    the whole control silently inert.
    """
    forward = _run(_ENABLE_RLS)
    reverse = _run(_DISABLE_RLS)
    forward.tables = reverse.tables = tables
    return migrations.RunPython(forward, reverse)


def composite_fks(*specs) -> migrations.RunPython:
    """Add ``(school_id, fk) -> (school_id, id)`` foreign keys.

    Each spec is ``(table, column, referenced_table)``. Django models a
    single-column FK; this composite one sits alongside it and makes a
    cross-tenant *reference* unrepresentable -- a row in School A physically
    cannot point at a row in School B.

    MATCH SIMPLE so nullable FKs are exempt when null; ON DELETE NO ACTION
    because cascading a composite key would try to null a NOT NULL school_id,
    and Django's own single-column FK already governs delete behaviour.
    """
    def _apply(apps, schema_editor):
        if schema_editor.connection.vendor != "postgresql":
            return
        for table, column, ref_table in specs:
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

    def _revert(apps, schema_editor):
        if schema_editor.connection.vendor != "postgresql":
            return
        for table, column, _ref in specs:
            schema_editor.execute(
                f"ALTER TABLE {table} "
                f"DROP CONSTRAINT IF EXISTS {table}_{column}_same_school_fk;"
            )

    return migrations.RunPython(_apply, _revert)


def append_only(*tables: str) -> migrations.RunPython:
    """Reject UPDATE and DELETE on tables that hold immutable facts.

    A Python-level guard protects the ORM path and nothing else. GRANTs do not
    help either, because the application usually connects as the table owner.
    A trigger does.
    """
    def _apply(apps, schema_editor):
        if schema_editor.connection.vendor != "postgresql":
            return
        for table in tables:
            schema_editor.execute(f"""
                DROP TRIGGER IF EXISTS {table}_append_only ON {table};
                CREATE TRIGGER {table}_append_only
                    BEFORE UPDATE OR DELETE ON {table}
                    FOR EACH ROW EXECUTE FUNCTION educore_reject_mutation();
            """)

    def _revert(apps, schema_editor):
        if schema_editor.connection.vendor != "postgresql":
            return
        for table in tables:
            schema_editor.execute(
                f"DROP TRIGGER IF EXISTS {table}_append_only ON {table};"
            )

    return migrations.RunPython(_apply, _revert)


def set_tenant(connection, school_id) -> None:
    """Bind the tenant to the current transaction.

    SET LOCAL, not SET: the value is scoped to the enclosing transaction and
    is discarded on commit or rollback. That is what makes this safe under
    PgBouncer transaction pooling, where the next transaction on this physical
    connection may belong to a different school.
    """
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT set_config('{TENANT_GUC}', %s, true)",
                       [str(school_id) if school_id else ""])


def clear_tenant(connection) -> None:
    set_tenant(connection, None)
