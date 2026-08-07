"""Tenant context and the ORM enforcement layer (isolation layers 1 and 2).

Layer 3 -- PostgreSQL row-level security -- lives in db.py and is the backstop
that survives mistakes in this file. See ADR-0001.
"""

from __future__ import annotations

import contextlib
import contextvars
import uuid

from django.db import models

from .db import uuid7

_current_school: contextvars.ContextVar[uuid.UUID | None] = contextvars.ContextVar(
    "educore_current_school", default=None
)

# Set by operator tooling only. Never reachable from an HTTP request path.
_unscoped: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "educore_unscoped", default=False
)


class TenantContext:
    """The tenant bound to the current execution context."""

    @staticmethod
    def get() -> uuid.UUID | None:
        return _current_school.get()

    @staticmethod
    def require() -> uuid.UUID:
        school_id = _current_school.get()
        if school_id is None:
            raise TenantScopeError(
                "No tenant bound. Every write must happen inside a tenant "
                "scope -- see TenantContext.scope()."
            )
        return school_id

    @staticmethod
    def set(school_id: uuid.UUID | None) -> contextvars.Token:
        return _current_school.set(school_id)

    @staticmethod
    def reset(token: contextvars.Token) -> None:
        _current_school.reset(token)

    @staticmethod
    @contextlib.contextmanager
    def scope(school_id):
        """Bind a tenant for the duration of the block."""
        value = getattr(school_id, "pk", school_id)
        token = _current_school.set(value)
        try:
            yield value
        finally:
            _current_school.reset(token)

    @staticmethod
    @contextlib.contextmanager
    def unscoped():
        """Escape hatch for platform operations spanning tenants.

        Every use is a deliberate decision that must be justified in review and
        accompanied by an audit event. Under PostgreSQL the RLS policy still
        applies: bypassing it additionally requires connecting as the operator
        role, which the application never does.
        """
        token = _unscoped.set(True)
        try:
            yield
        finally:
            _unscoped.reset(token)

    @staticmethod
    def is_unscoped() -> bool:
        return _unscoped.get()


class TenantScopeError(RuntimeError):
    """Raised when tenant-owned data is written with no tenant in context."""


class TenantQuerySet(models.QuerySet):
    def for_current_tenant(self):
        if TenantContext.is_unscoped():
            return self
        school_id = TenantContext.get()
        if school_id is None:
            # Deliberately empty rather than an error: an unbound read is a
            # bug, and the safe failure of a bug is "sees nothing".
            return self.none()
        return self.filter(school_id=school_id)


class TenantManager(models.Manager.from_queryset(TenantQuerySet)):
    """Default manager. Every queryset it produces is tenant-scoped."""

    def get_queryset(self):
        return super().get_queryset().for_current_tenant()


class AllTenantsManager(models.Manager):
    """Unfiltered manager.

    Also serves as ``base_manager_name`` so Django's own related-object
    traversal is not silently filtered -- a filtered base manager produces
    confusing DoesNotExist errors on legitimate cross-references. Isolation on
    those paths is carried by RLS and by the composite foreign keys.
    """


class UUIDModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class TenantOwnedModel(UUIDModel):
    """Base class for every model holding school data.

    Subclasses get:
      * a mandatory, immutable ``school`` foreign key;
      * a tenant-scoped default manager;
      * a ``(school, id)`` unique constraint, which is what lets *other*
        tables reference them with a composite foreign key so that a
        cross-tenant reference becomes unrepresentable in the database.
    """

    school = models.ForeignKey(
        "core.School",
        on_delete=models.PROTECT,
        related_name="+",
        editable=False,
        db_index=True,
    )

    objects = TenantManager()
    all_tenants = AllTenantsManager()

    class Meta:
        abstract = True
        base_manager_name = "all_tenants"
        # Concrete subclasses MUST declare `class Meta(TenantOwnedModel.Meta)`.
        # Django does not merge a child's fresh Meta with the parent's, so a
        # plain `class Meta:` silently drops base_manager_name and related
        # traversal starts going through the *filtered* manager. The
        # introspective test in tests/test_tenant_isolation.py fails the build
        # if this is forgotten.

    def save(self, *args, **kwargs):
        if self.school_id is None:
            self.school_id = TenantContext.require()
        elif not TenantContext.is_unscoped():
            current = TenantContext.get()
            if current is not None and self.school_id != current:
                raise TenantScopeError(
                    f"Refusing to write {type(self).__name__} owned by "
                    f"{self.school_id} while scoped to {current}."
                )
        return super().save(*args, **kwargs)

    @classmethod
    def tenant_constraints(cls, name_prefix: str) -> list:
        """Constraints every tenant-owned concrete model must declare."""
        return [
            models.UniqueConstraint(
                fields=["school", "id"], name=f"{name_prefix}_school_id_uniq"
            )
        ]
