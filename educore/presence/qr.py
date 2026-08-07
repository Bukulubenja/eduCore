"""Staff check-in codes.

The signing and verification live in `core.signed_tokens`, shared with the
classroom codes in `delivery`. What belongs here is the staff-specific part:
the purpose string and the record of a code being spent.
"""

from __future__ import annotations

__all__ = [
    "PURPOSE",
    "QrAlreadyRedeemedError",
    "QrBadSignatureError",
    "QrError",
    "QrExpiredError",
    "QrMalformedError",
    "QrNotYetValidError",
    "QrWrongCampusError",
    "issue",
    "redeem",
    "verify",
]

from educore.core.signed_tokens import (
    SignedTokenError as QrError,
)
from educore.core.signed_tokens import (
    TokenAlreadyRedeemedError as QrAlreadyRedeemedError,
)
from educore.core.signed_tokens import (
    TokenBadSignatureError as QrBadSignatureError,
)
from educore.core.signed_tokens import (
    TokenExpiredError as QrExpiredError,
)
from educore.core.signed_tokens import (
    TokenMalformedError as QrMalformedError,
)
from educore.core.signed_tokens import (
    TokenNotYetValidError as QrNotYetValidError,
)
from educore.core.signed_tokens import (
    TokenWrongScopeError as QrWrongCampusError,
)
from educore.core.signed_tokens import issue as _issue
from educore.core.signed_tokens import verify as _verify

PURPOSE = "staff_checkin"


def issue(*, school_id, campus_id, ttl_seconds: int = 30, at=None) -> dict:
    return _issue(school_id=school_id, scope_id=campus_id, purpose=PURPOSE,
                  ttl_seconds=ttl_seconds, at=at)


def verify(token: str, *, school_id, campus_id, at) -> dict:
    return _verify(token, school_id=school_id, scope_id=campus_id,
                   purpose=PURPOSE, at=at)


def redeem(payload: dict, *, membership) -> None:
    """Record consumption. Raises on a repeat by the same person.

    Uniqueness is per (nonce, member), not per nonce: a displayed code is
    scanned by everyone arriving in its window. What must not happen is the
    same person redeeming the same code twice.
    """
    from django.db import IntegrityError, transaction

    from .models import QrRedemption

    try:
        with transaction.atomic():
            QrRedemption.objects.create(nonce=payload["non"], membership=membership)
    except IntegrityError as exc:
        raise QrAlreadyRedeemedError(
            "this code has already been used by you"
        ) from exc
