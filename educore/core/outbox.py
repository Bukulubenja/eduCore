"""The transactional outbox relay.

Domain events are written in the same transaction as the change they describe
(doc 02). This module is the other half: a poller that picks those rows up and
hands them to whoever subscribed, with retries and a dead-letter state.

Subscription is inverted on purpose. `core` may not import any domain module
(ADR-0005), so handlers register themselves from their own app's `ready()`.
The relay knows only topics and callables.
"""

from __future__ import annotations

import logging
import traceback
from collections.abc import Callable
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import OutboxMessage, School
from .tenancy import TenantContext

logger = logging.getLogger("educore.outbox")

MAX_ATTEMPTS = 6
BATCH_SIZE = 100

_handlers: dict[str, list[Callable]] = {}


def subscribe(topic: str):
    """Register a handler for a topic. Several may subscribe to one topic."""
    def decorator(func):
        _handlers.setdefault(topic, []).append(func)
        return func

    return decorator


def handlers_for(topic: str) -> list[Callable]:
    return _handlers.get(topic, [])


def clear_handlers() -> None:
    """Test seam. Never call this from application code."""
    _handlers.clear()


def _backoff(attempts: int) -> timedelta:
    """Exponential, capped. 1, 2, 4, 8, 16, 30 minutes."""
    return timedelta(minutes=min(2 ** max(0, attempts - 1), 30))


def relay_pending(*, limit: int = BATCH_SIZE, now=None) -> dict:
    """Dispatch one batch of pending messages for the bound tenant.

    Each message is claimed with SELECT ... FOR UPDATE SKIP LOCKED so several
    relay workers can run concurrently without handing the same event to two
    of them. On SQLite the lock is a no-op and concurrency is not available --
    which is fine for a workstation and not fine for production, where the
    relay must run against PostgreSQL.
    """
    now = now or timezone.now()
    stats = {"published": 0, "retried": 0, "failed": 0, "skipped": 0}

    pending = list(
        OutboxMessage.objects
        .filter(status=OutboxMessage.Status.PENDING, available_at__lte=now)
        .order_by("available_at")[:limit]
        .values_list("pk", flat=True)
    )

    for message_id in pending:
        stats_key = _relay_one(message_id, now=now)
        stats[stats_key] += 1

    return stats


def _relay_one(message_id, *, now) -> str:
    with transaction.atomic():
        message = (
            OutboxMessage.objects
            .select_for_update(skip_locked=True)
            .filter(pk=message_id, status=OutboxMessage.Status.PENDING)
            .first()
        )
        if message is None:
            return "skipped"        # Claimed by another worker.

        subscribers = handlers_for(message.topic)
        if not subscribers:
            # Nobody is listening. Publish rather than retry forever: an
            # unconsumed topic is a design gap to notice in metrics, not a
            # row to hammer the database with every minute.
            logger.warning("outbox: no handler for topic %s", message.topic)
            message.status = OutboxMessage.Status.PUBLISHED
            message.published_at = now
            message.last_error = "no subscriber"
            message.save(update_fields=["status", "published_at", "last_error",
                                        "updated_at"])
            return "published"

        message.attempts += 1
        try:
            for handler in subscribers:
                handler(message.payload, message=message)
        except Exception:
            error = traceback.format_exc(limit=6)
            logger.exception("outbox: handler failed for %s", message.topic)

            if message.attempts >= MAX_ATTEMPTS:
                # Dead-letter rather than retry forever. A poison message that
                # never stops retrying starves every well-behaved one behind it.
                message.status = OutboxMessage.Status.FAILED
                message.last_error = error
                message.save(update_fields=["status", "attempts", "last_error",
                                            "updated_at"])
                return "failed"

            message.available_at = now + _backoff(message.attempts)
            message.last_error = error
            message.save(update_fields=["attempts", "available_at", "last_error",
                                        "updated_at"])
            return "retried"

        message.status = OutboxMessage.Status.PUBLISHED
        message.published_at = now
        message.last_error = ""
        message.save(update_fields=["status", "attempts", "published_at",
                                    "last_error", "updated_at"])
        return "published"


def relay_all_tenants(*, limit: int = BATCH_SIZE, school_slug: str | None = None) -> dict:
    """Relay for every active school, each inside its own tenant scope.

    The scope is not optional. Outbox rows are tenant-owned, so a relay that
    forgets to bind a tenant sees an empty table and reports a cheerful zero
    while the queue grows behind it.
    """
    totals = {"published": 0, "retried": 0, "failed": 0, "skipped": 0}

    schools = School.objects.filter(status=School.Status.ACTIVE)
    if school_slug:
        schools = schools.filter(slug=school_slug)

    for school in schools.iterator():
        with TenantContext.scope(school.id):
            for key, value in relay_pending(limit=limit).items():
                totals[key] += value

    return totals
