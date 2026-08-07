"""Writing and verifying the audit chain."""

from __future__ import annotations

from django.db import transaction

from .models import AuditEvent
from .tenancy import TenantContext


@transaction.atomic
def record(
    *,
    action: str,
    object_type: str,
    object_id=None,
    actor_membership=None,
    before=None,
    after=None,
    request_id: str = "",
    ip_address=None,
    device=None,
) -> AuditEvent:
    """Append one event to the school's chain.

    Serialised per school by locking the previous row: two concurrent writers
    must not both read the same ``prev_hash``, or the chain forks and the
    verifier can no longer tell a fork from tampering.
    """
    school_id = TenantContext.require()

    previous = (
        AuditEvent.all_tenants.select_for_update()
        .filter(school_id=school_id)
        .order_by("-sequence")
        .first()
    )
    sequence = (previous.sequence + 1) if previous else 1
    prev_hash = previous.hash if previous else ""

    event = AuditEvent(
        school_id=school_id,
        actor_membership=actor_membership,
        action=action,
        object_type=object_type,
        object_id=object_id,
        before=before,
        after=after,
        request_id=request_id,
        ip_address=ip_address,
        device=device,
        sequence=sequence,
        prev_hash=prev_hash,
    )
    event.hash = event.compute_hash()
    event.save()
    return event


def verify_chain(school_id, *, since_sequence: int = 0) -> list[dict]:
    """Walk a school's chain and report breaks.

    Run nightly. An empty result is the only acceptable outcome; anything else
    is a security incident until proven otherwise.
    """
    breaks: list[dict] = []
    expected_prev = None
    expected_sequence = None

    events = (
        AuditEvent.all_tenants.filter(school_id=school_id,
                                      sequence__gt=since_sequence)
        .order_by("sequence")
        .iterator(chunk_size=1000)
    )

    for event in events:
        if expected_sequence is not None and event.sequence != expected_sequence:
            breaks.append({
                "sequence": event.sequence,
                "reason": "gap",
                "detail": f"expected sequence {expected_sequence}",
            })
        if expected_prev is not None and event.prev_hash != expected_prev:
            breaks.append({
                "sequence": event.sequence,
                "reason": "broken_link",
                "detail": "prev_hash does not match the preceding event",
            })
        if event.compute_hash() != event.hash:
            breaks.append({
                "sequence": event.sequence,
                "reason": "tampered",
                "detail": "stored hash does not match recomputed contents",
            })

        expected_prev = event.hash
        expected_sequence = event.sequence + 1

    return breaks
