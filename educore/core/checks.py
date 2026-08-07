"""Startup checks.

These exist because the controls they guard are invisible when broken: a
missing RLS policy and a present one look identical until the day someone
reads another school's data.
"""

from __future__ import annotations

from django.apps import apps
from django.conf import settings
from django.core.checks import Error, Warning, register
from django.db import connection

from .tenancy import TenantManager, TenantOwnedModel


@register("educore")
def tenant_models_configured(app_configs, **kwargs):
    """Every model with a school FK must inherit TenantOwnedModel."""
    issues = []
    for model in apps.get_models():
        if not model._meta.app_label.startswith(("core", "academics", "timetable",
                                                 "presence", "delivery", "students",
                                                 "assessment", "comms", "insights",
                                                 "platform")):
            continue
        field_names = {f.name for f in model._meta.get_fields()}
        if "school" not in field_names:
            continue
        if model is TenantOwnedModel or model._meta.abstract:
            continue
        if getattr(model, "operator_owned", False):
            # Declared escape hatch for records that reference a school but
            # belong to the operator, not to it -- billing terms and metering
            # above all. Must be set on the model so the exemption is visible
            # in review and greppable, never inferred here.
            continue
        if not issubclass(model, TenantOwnedModel):
            issues.append(Error(
                f"{model._meta.label} has a 'school' field but does not inherit "
                "TenantOwnedModel, so it is not tenant-scoped by default.",
                hint="Inherit educore.core.tenancy.TenantOwnedModel.",
                id="educore.E001",
            ))
            continue
        if not isinstance(model._default_manager, TenantManager):
            issues.append(Error(
                f"{model._meta.label}'s default manager is not a TenantManager.",
                hint="Do not shadow 'objects' on a tenant-owned model.",
                id="educore.E002",
            ))
        constraint_names = {c.name for c in model._meta.constraints}
        expected = f"{model._meta.db_table}_school_id_uniq"
        if expected not in constraint_names:
            issues.append(Warning(
                f"{model._meta.label} lacks the (school, id) unique constraint "
                f"'{expected}', so other tables cannot reference it with a "
                "composite foreign key.",
                hint="Add TenantOwnedModel.tenant_constraints(<db_table>).",
                id="educore.W001",
            ))
    return issues


@register("educore", deploy=True)
def rls_enabled(app_configs, **kwargs):
    """Refuse to deploy with the isolation backstop missing (ADR-0001)."""
    if connection.vendor != "postgresql":
        return [Error(
            "PostgreSQL is required: row-level security has no equivalent on "
            f"'{connection.vendor}'.",
            id="educore.E003",
        )]

    tenant_tables = {
        m._meta.db_table for m in apps.get_models()
        if issubclass(m, TenantOwnedModel) and not m._meta.abstract
    }
    if not tenant_tables:
        return []

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT c.relname
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = current_schema()
              AND c.relname = ANY(%s)
              AND (c.relrowsecurity IS FALSE OR c.relforcerowsecurity IS FALSE)
            """,
            [list(tenant_tables)],
        )
        unprotected = [row[0] for row in cursor.fetchall()]

    if unprotected:
        return [Error(
            "Row-level security is not enabled and forced on: "
            + ", ".join(sorted(unprotected)),
            hint="Run migrations; see educore.core.db.rls().",
            id="educore.E004",
        )]
    return []


@register("educore", deploy=True)
def secure_defaults(app_configs, **kwargs):
    issues = []
    if settings.DEBUG:
        issues.append(Error("DEBUG must be False in production.", id="educore.E005"))
    if not settings.SECRET_KEY or "insecure" in settings.SECRET_KEY:
        issues.append(Error("SECRET_KEY is unset or a development placeholder.",
                            id="educore.E006"))
    if settings.PASSWORD_HASHERS[0] != \
            "django.contrib.auth.hashers.Argon2PasswordHasher":
        issues.append(Error("Argon2 must be the first password hasher.",
                            id="educore.E007"))
    return issues
