"""The isolation battery (ADR-0001, doc 07 Phase 0 exit criterion).

Layers under test:

  1. request context  -- test_middleware.py
  2. ORM default manager  -- everything here that is not marked `postgres`
  3. PostgreSQL row-level security  -- the `postgres`-marked tests

Layer 3 is the one that matters when layers 1 and 2 have a bug, so its tests
deliberately bypass the ORM's filtering and go at the database directly.
"""

from __future__ import annotations

import uuid

import pytest
from django.db import connection, transaction
from django.db.utils import InternalError, ProgrammingError

from educore.core.db import set_tenant
from educore.core.models import AuditEvent, Campus, Membership, Role
from educore.core.tenancy import TenantContext, TenantScopeError

pytestmark = pytest.mark.django_db


# -- Layer 2: the ORM default manager ---------------------------------------


def test_default_manager_hides_other_tenants(school_a, school_b, make_role):
    make_role(school_a, code="dos", name="Director of Studies")
    make_role(school_b, code="dos", name="Director of Studies")

    with TenantContext.scope(school_a):
        assert Role.objects.count() == 1
        assert Role.objects.get().school_id == school_a.id

    with TenantContext.scope(school_b):
        assert Role.objects.count() == 1
        assert Role.objects.get().school_id == school_b.id


def test_unbound_context_sees_nothing(school_a, make_role):
    """The safe failure of a forgotten scope is emptiness, never leakage."""
    make_role(school_a)
    assert TenantContext.get() is None
    assert Role.objects.count() == 0


def test_get_on_foreign_object_raises_does_not_exist(school_a, school_b, make_role):
    """A cross-tenant fetch must be indistinguishable from a missing object.

    The API surfaces this as 404, never 403: a 403 confirms the object exists,
    which is itself a disclosure.
    """
    foreign = make_role(school_b)
    with TenantContext.scope(school_a), pytest.raises(Role.DoesNotExist):
        Role.objects.get(pk=foreign.pk)


def test_write_scoped_to_wrong_tenant_is_refused(school_a, school_b):
    with TenantContext.scope(school_a), pytest.raises(TenantScopeError):
        Campus.objects.create(school=school_b, name="Smuggled campus")


def test_write_without_tenant_is_refused(school_a):
    with pytest.raises(TenantScopeError):
        Campus.objects.create(name="Unscoped campus")


def test_school_is_inferred_from_context(school_a):
    with TenantContext.scope(school_a):
        campus = Campus.objects.create(name="Main")
    assert campus.school_id == school_a.id


def test_unscoped_manager_is_explicit(school_a, school_b, make_role):
    make_role(school_a)
    make_role(school_b)
    assert Role.all_tenants.count() == 2


def test_related_traversal_is_not_silently_filtered(school_a, make_membership):
    """base_manager_name must be the unfiltered manager.

    A filtered base manager turns legitimate related-object access into
    spurious DoesNotExist errors that surface far from their cause.
    """
    membership = make_membership(school_a, email="teacher@example.com")
    with TenantContext.scope(school_a):
        device = Membership.objects.get(pk=membership.pk)
        assert device.school_id == school_a.id


# -- Every tenant-owned model, not just the ones someone remembered ---------


def _tenant_models():
    from django.apps import apps

    from educore.core.tenancy import TenantOwnedModel

    return [
        m for m in apps.get_models()
        if issubclass(m, TenantOwnedModel) and not m._meta.abstract
    ]


@pytest.mark.parametrize("model", _tenant_models(), ids=lambda m: m._meta.label)
def test_every_tenant_model_is_scoped_by_default(model, school_a):
    """Introspective, so a new model joins the battery without being added.

    A battery you have to remember to extend is a battery that will be
    incomplete by the third sprint.
    """
    from educore.core.tenancy import TenantManager

    assert isinstance(model._default_manager, TenantManager), (
        f"{model._meta.label} has an unscoped default manager"
    )
    assert model._meta.base_manager_name == "all_tenants"

    expected = f"{model._meta.db_table}_school_id_uniq"
    names = {c.name for c in model._meta.constraints}
    assert expected in names, (
        f"{model._meta.label} lacks {expected}; other tables cannot form a "
        "composite foreign key to it"
    )


# -- Layer 3: PostgreSQL row-level security ---------------------------------


@pytest.mark.postgres
def test_rls_blocks_reads_that_escape_the_orm(school_a, school_b, make_role):
    """The scenario layers 1-2 cannot cover: raw SQL with no tenant filter."""
    make_role(school_a, code="dos")
    make_role(school_b, code="dos")

    with transaction.atomic():
        set_tenant(connection, school_a.id)
        with connection.cursor() as cursor:
            cursor.execute("SELECT school_id FROM core_role")
            rows = cursor.fetchall()

    assert len(rows) == 1
    assert rows[0][0] == school_a.id


@pytest.mark.postgres
def test_rls_denies_everything_when_no_tenant_is_set(school_a, make_role):
    make_role(school_a)
    with transaction.atomic():
        set_tenant(connection, None)
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM core_role")
            assert cursor.fetchone()[0] == 0


@pytest.mark.postgres
def test_rls_check_blocks_inserting_into_another_tenant(school_a, school_b):
    with transaction.atomic():
        set_tenant(connection, school_a.id)
        with connection.cursor() as cursor, pytest.raises(ProgrammingError):
            cursor.execute(
                "INSERT INTO core_campus "
                "(id, created_at, updated_at, school_id, name, boundary, is_primary) "
                "VALUES (%s, now(), now(), %s, 'Smuggled', '[]'::jsonb, false)",
                [uuid.uuid4(), school_b.id],
            )


@pytest.mark.postgres
def test_composite_fk_forbids_cross_tenant_reference(
    school_a, school_b, make_membership, make_role
):
    """A RoleAssignment cannot join a Membership in A to a Role in B."""
    membership = make_membership(school_a, email="a@example.com")
    foreign_role = make_role(school_b)

    with transaction.atomic():
        set_tenant(connection, school_a.id)
        with connection.cursor() as cursor, pytest.raises(Exception) as excinfo:
            cursor.execute(
                "INSERT INTO core_roleassignment "
                "(id, created_at, updated_at, school_id, membership_id, role_id, "
                " scope_type, valid_from) "
                "VALUES (%s, now(), now(), %s, %s, %s, '', current_date)",
                [uuid.uuid4(), school_a.id, membership.id, foreign_role.id],
            )
    assert "same_school_fk" in str(excinfo.value) or "foreign key" in str(excinfo.value).lower()


@pytest.mark.postgres
def test_audit_events_cannot_be_updated_or_deleted(school_a):
    from educore.core import audit

    with TenantContext.scope(school_a):
        event = audit.record(action="test.event", object_type="core.School",
                             object_id=school_a.id)

    with transaction.atomic():
        set_tenant(connection, school_a.id)
        with connection.cursor() as cursor, pytest.raises(InternalError):
            cursor.execute("UPDATE core_auditevent SET action = 'tampered' "
                           "WHERE id = %s", [event.id])

    with transaction.atomic():
        set_tenant(connection, school_a.id)
        with connection.cursor() as cursor, pytest.raises(InternalError):
            cursor.execute("DELETE FROM core_auditevent WHERE id = %s", [event.id])


@pytest.mark.postgres
def test_rls_is_enabled_and_forced_on_every_tenant_table():
    tables = {m._meta.db_table for m in _tenant_models()}
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = current_schema() AND relname = ANY(%s) "
            "  AND (relrowsecurity IS FALSE OR relforcerowsecurity IS FALSE)",
            [list(tables)],
        )
        unprotected = [r[0] for r in cursor.fetchall()]
    assert unprotected == [], f"RLS not enforced on: {unprotected}"


# -- The audit chain --------------------------------------------------------


def test_audit_chain_links_and_verifies(school_a):
    from educore.core import audit

    with TenantContext.scope(school_a):
        first = audit.record(action="school.created", object_type="core.School",
                             object_id=school_a.id)
        second = audit.record(action="school.renamed", object_type="core.School",
                              object_id=school_a.id,
                              before={"name": "Old"}, after={"name": "New"})

    assert first.sequence == 1
    assert first.prev_hash == ""
    assert second.sequence == 2
    assert second.prev_hash == first.hash
    assert audit.verify_chain(school_a.id) == []


def test_audit_chain_detects_tampering(school_a):
    """Bypasses the ORM guard on purpose -- that is the threat being modelled."""
    from educore.core import audit

    with TenantContext.scope(school_a):
        audit.record(action="mark.changed", object_type="assessment.Score",
                     after={"score": 40})
        audit.record(action="mark.changed", object_type="assessment.Score",
                     after={"score": 45})

    AuditEvent.all_tenants.filter(school=school_a, sequence=1).update(
        after={"score": 95}
    )

    breaks = audit.verify_chain(school_a.id)
    reasons = {b["reason"] for b in breaks}
    assert "tampered" in reasons


def test_audit_chains_are_independent_per_school(school_a, school_b):
    from educore.core import audit

    with TenantContext.scope(school_a):
        audit.record(action="a.one", object_type="core.School")
    with TenantContext.scope(school_b):
        event = audit.record(action="b.one", object_type="core.School")

    assert event.sequence == 1, "sequences must not be shared across tenants"
    assert audit.verify_chain(school_a.id) == []
    assert audit.verify_chain(school_b.id) == []
