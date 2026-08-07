"""Short-lived signed codes for rotating QR displays.

Lives in core because two sibling modules need it -- `presence` for staff
check-in and `delivery` for classroom lesson scans -- and siblings may not
import each other (ADR-0005). The crypto is generic; what a code *means*, and
the record of it being spent, belong to whichever module issued it.

A code is an HMAC-signed payload with a short life, shown on a screen and
refreshed by a polling display. Codes are not stored when minted -- only when
redeemed -- so a school generating one every 30 seconds all year writes rows
only for real scans.

Offline redemption is the subtle part. A teacher may scan at 07:02 with no
network and sync at 16:00, so expiry is checked against the device's
`captured_at`, which is attacker-controlled. That is safe enough: forging a
redemption still requires a genuine signed nonce, which requires having stood
in front of the display inside its life. Both ends of the window are checked,
because expiry alone catches only codes that are too old -- a backdated clock
presents a perfectly fresh code against an old capture time.
"""

from __future__ import annotations

import base64
import hmac
import json
import secrets
from datetime import timedelta
from hashlib import sha256

from django.conf import settings
from django.utils import timezone

TOKEN_VERSION = 1

# Slack for ordinary device clock drift when checking a code was not scanned
# before it was minted.
CLOCK_TOLERANCE_SECONDS = 5


class SignedTokenError(Exception):
    """Base for every reason a presented code is not acceptable."""


class TokenMalformedError(SignedTokenError):
    pass


class TokenBadSignatureError(SignedTokenError):
    pass


class TokenExpiredError(SignedTokenError):
    pass


class TokenNotYetValidError(SignedTokenError):
    pass


class TokenWrongScopeError(SignedTokenError):
    pass


class TokenAlreadyRedeemedError(SignedTokenError):
    pass


def _key(school_id, purpose: str) -> bytes:
    """Signing key derived per school *and* per purpose.

    Derived rather than shared so a staff check-in code can never be replayed
    as a lesson code, and a code minted for one school cannot verify against
    another even if its payload were tampered to claim otherwise.
    """
    return hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        f"educore.signed_tokens:{purpose}:{school_id}".encode(),
        sha256,
    ).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue(*, school_id, scope_id, purpose: str, ttl_seconds: int = 30,
          at=None) -> dict:
    """Mint a code. `scope_id` is the campus, room, or gate it is bound to."""
    now = at or timezone.now()
    payload = {
        "v": TOKEN_VERSION,
        "sch": str(school_id),
        "scp": str(scope_id),
        "pur": purpose,
        "non": secrets.token_urlsafe(16),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl_seconds)).timestamp()),
    }
    body = _b64(json.dumps(payload, sort_keys=True, separators=(",", ":"))
                .encode("utf-8"))
    signature = _b64(hmac.new(_key(school_id, purpose), body.encode("ascii"),
                              sha256).digest())
    return {
        "token": f"{body}.{signature}",
        "nonce": payload["non"],
        "issued_at": now,
        "expires_at": now + timedelta(seconds=ttl_seconds),
        "refresh_after_seconds": max(1, ttl_seconds // 2),
    }


def verify(token: str, *, school_id, scope_id, purpose: str, at) -> dict:
    """Validate a presented code. `at` is the moment of the scan, not of sync."""
    if not token or token.count(".") != 1:
        raise TokenMalformedError("token is malformed")

    body, signature = token.split(".")
    expected = _b64(hmac.new(_key(school_id, purpose), body.encode("ascii"),
                             sha256).digest())
    if not hmac.compare_digest(signature, expected):
        raise TokenBadSignatureError("token signature does not verify")

    try:
        payload = json.loads(_unb64(body))
    except (ValueError, TypeError) as exc:
        raise TokenMalformedError("token payload is not readable") from exc

    if payload.get("v") != TOKEN_VERSION:
        raise TokenMalformedError("unsupported token version")
    if payload.get("scp") != str(scope_id):
        raise TokenWrongScopeError("token was issued for somewhere else")

    scanned_at = int(at.timestamp())
    if int(payload.get("exp", 0)) < scanned_at:
        raise TokenExpiredError("token had expired when it was scanned")
    if scanned_at < int(payload.get("iat", 0)) - CLOCK_TOLERANCE_SECONDS:
        raise TokenNotYetValidError("token had not been issued when it was scanned")

    return payload
