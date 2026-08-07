"""The relay: claiming, retrying, dead-lettering, and tenant scoping."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from educore.core import outbox
from educore.core.models import OutboxMessage
from educore.core.tenancy import TenantContext

pytestmark = pytest.mark.django_db


@pytest.fixture
def isolated_handlers():
    """Swap the real subscriber registry out for the duration of a test."""
    saved = dict(outbox._handlers)
    outbox.clear_handlers()
    yield
    outbox._handlers.clear()
    outbox._handlers.update(saved)


def queue(school, topic="test.topic", payload=None):
    with TenantContext.scope(school):
        return OutboxMessage.objects.create(
            school_id=school.id, topic=topic, payload=payload or {"n": 1}
        )


def test_a_pending_message_is_delivered_and_marked_published(school_a,
                                                             isolated_handlers):
    seen = []
    outbox.subscribe("test.topic")(lambda payload, **kw: seen.append(payload))
    message = queue(school_a)

    with TenantContext.scope(school_a):
        stats = outbox.relay_pending()
        message.refresh_from_db()

    assert stats["published"] == 1
    assert seen == [{"n": 1}]
    assert message.status == OutboxMessage.Status.PUBLISHED
    assert message.published_at is not None


def test_several_subscribers_all_receive_the_event(school_a, isolated_handlers):
    calls = []
    outbox.subscribe("test.topic")(lambda p, **kw: calls.append("first"))
    outbox.subscribe("test.topic")(lambda p, **kw: calls.append("second"))
    queue(school_a)

    with TenantContext.scope(school_a):
        outbox.relay_pending()

    assert calls == ["first", "second"]


def test_a_published_message_is_not_delivered_twice(school_a, isolated_handlers):
    calls = []
    outbox.subscribe("test.topic")(lambda p, **kw: calls.append(p))
    queue(school_a)

    with TenantContext.scope(school_a):
        outbox.relay_pending()
        outbox.relay_pending()

    assert len(calls) == 1


def test_a_failing_handler_retries_with_backoff(school_a, isolated_handlers):
    def explode(payload, **kwargs):
        raise RuntimeError("provider unreachable")

    outbox.subscribe("test.topic")(explode)
    message = queue(school_a)
    now = timezone.now()

    with TenantContext.scope(school_a):
        stats = outbox.relay_pending(now=now)
        message.refresh_from_db()

    assert stats["retried"] == 1
    assert message.status == OutboxMessage.Status.PENDING
    assert message.attempts == 1
    assert message.available_at > now
    assert "provider unreachable" in message.last_error


def test_a_retry_is_not_attempted_before_its_backoff_expires(school_a,
                                                             isolated_handlers):
    attempts = []

    def explode(payload, **kwargs):
        attempts.append(1)
        raise RuntimeError("still down")

    outbox.subscribe("test.topic")(explode)
    queue(school_a)
    now = timezone.now()

    with TenantContext.scope(school_a):
        outbox.relay_pending(now=now)
        outbox.relay_pending(now=now)          # same instant: still backing off

    assert len(attempts) == 1


def test_a_poison_message_is_dead_lettered(school_a, isolated_handlers):
    """A message that never stops retrying starves every good one behind it."""
    def explode(payload, **kwargs):
        raise RuntimeError("permanently broken")

    outbox.subscribe("test.topic")(explode)
    message = queue(school_a)

    with TenantContext.scope(school_a):
        now = timezone.now()
        for _ in range(outbox.MAX_ATTEMPTS):
            outbox.relay_pending(now=now)
            now += timedelta(hours=1)
        message.refresh_from_db()

    assert message.status == OutboxMessage.Status.FAILED
    assert message.attempts == outbox.MAX_ATTEMPTS


def test_one_bad_handler_does_not_block_the_next_message(school_a,
                                                         isolated_handlers):
    delivered = []
    outbox.subscribe("bad.topic")(
        lambda p, **kw: (_ for _ in ()).throw(RuntimeError("nope"))
    )
    outbox.subscribe("good.topic")(lambda p, **kw: delivered.append(p))

    queue(school_a, topic="bad.topic")
    queue(school_a, topic="good.topic", payload={"n": 2})

    with TenantContext.scope(school_a):
        stats = outbox.relay_pending()

    assert stats == {"published": 1, "retried": 1, "failed": 0, "skipped": 0}
    assert delivered == [{"n": 2}]


def test_an_unsubscribed_topic_is_published_not_retried_forever(school_a,
                                                                isolated_handlers):
    message = queue(school_a, topic="nobody.listening")

    with TenantContext.scope(school_a):
        outbox.relay_pending()
        message.refresh_from_db()

    assert message.status == OutboxMessage.Status.PUBLISHED
    assert message.last_error == "no subscriber"


def test_the_relay_binds_each_tenant(school_a, school_b, isolated_handlers):
    """A relay that forgets to scope sees an empty table and reports zero."""
    seen = []
    outbox.subscribe("test.topic")(lambda p, **kw: seen.append(p["school"]))

    queue(school_a, payload={"school": "a"})
    queue(school_b, payload={"school": "b"})

    stats = outbox.relay_all_tenants()

    assert stats["published"] == 2
    assert sorted(seen) == ["a", "b"]


def test_relaying_one_school_leaves_the_others_queued(school_a, school_b,
                                                      isolated_handlers):
    outbox.subscribe("test.topic")(lambda p, **kw: None)
    queue(school_a, payload={"school": "a"})
    other = queue(school_b, payload={"school": "b"})

    outbox.relay_all_tenants(school_slug="kampala-high")

    with TenantContext.scope(school_b):
        other.refresh_from_db()
    assert other.status == OutboxMessage.Status.PENDING


def test_an_unscoped_relay_finds_nothing(school_a, isolated_handlers):
    """The safe failure of a forgotten scope is emptiness, not leakage."""
    outbox.subscribe("test.topic")(lambda p, **kw: None)
    queue(school_a)

    assert outbox.relay_pending() == {"published": 0, "retried": 0,
                                      "failed": 0, "skipped": 0}
