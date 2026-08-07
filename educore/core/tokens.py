"""Access and refresh tokens (doc 05, "Authentication").

Access tokens are short-lived JWTs carrying the Membership claim. Refresh
tokens are opaque, single-use, and stored hashed, with reuse detection: a
token presented twice revokes its whole family. That is the control that
turns a stolen refresh token from a silent, indefinite compromise into a
noisy one that locks the attacker and the victim out together, which is the
outcome you want because it gets noticed.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import timedelta

import jwt
from django.conf import settings
from django.db import transaction
from django.utils import timezone

ALGORITHM = "HS256"
ACCESS_TTL = timedelta(minutes=15)
REFRESH_TTL = timedelta(days=30)


class TokenError(Exception):
    pass


class TokenExpiredError(TokenError):
    pass


class TokenInvalidError(TokenError):
    pass


class RefreshReuseDetectedError(TokenError):
    """A consumed refresh token was presented again. Treated as compromise."""


def _signing_key() -> str:
    return settings.SECRET_KEY


def issue_access(membership, *, device_id=None, role_codes=None) -> tuple[str, int]:
    now = timezone.now()
    expires_at = now + ACCESS_TTL
    claims = {
        "sub": str(membership.user_id),
        "mbr": str(membership.pk),
        "sch": str(membership.school_id),
        # Roles are a hint for routing and UI only. Every authorisation
        # decision is re-evaluated server-side against the database, so a
        # stale claim can widen nothing.
        "rls": role_codes or [],
        "dev": str(device_id) if device_id else None,
        "jti": uuid.uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(claims, _signing_key(), algorithm=ALGORITHM)
    return token, int(ACCESS_TTL.total_seconds())


def decode_access(token: str) -> dict:
    try:
        return jwt.decode(token, _signing_key(), algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpiredError("access token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenInvalidError("access token is not valid") from exc


def hash_refresh(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def issue_refresh(membership, *, family_id=None, device=None):
    """Mint a refresh token. Returns (raw_token, model instance).

    The raw value is returned once and never stored; only its hash is kept, so
    a database leak does not hand over usable sessions.
    """
    from .models import RefreshToken
    from .tenancy import TenantContext

    raw = secrets.token_urlsafe(48)
    # Scoped to the membership's own school rather than the request's. Issuing
    # a token is inherently an operation *about* a school, and switch-school
    # legitimately mints one for a tenant other than the caller's current
    # scope -- the only such crossing in the auth flow.
    with TenantContext.scope(membership.school_id):
        token = RefreshToken.objects.create(
            school_id=membership.school_id,
            membership=membership,
            device=device,
            family_id=family_id or uuid.uuid4(),
            token_hash=hash_refresh(raw),
            expires_at=timezone.now() + REFRESH_TTL,
        )
    return raw, token


def rotate_refresh(raw: str):
    """Exchange a refresh token for a new pair, or detect reuse.

    Returns (membership, new_raw_refresh). Raises RefreshReuseDetectedError if the
    presented token was already consumed -- in which case the entire family is
    revoked before the exception leaves this function.

    The read-check-write here is guarded with ``select_for_update`` inside an
    atomic block: without it, two requests racing to rotate the *same* token
    (an attacker replaying a stolen token at roughly the moment the victim's
    client refreshes) can both pass the "not yet consumed" check before
    either write lands, and reuse detection never fires for either caller --
    the exact scenario this function exists to catch. The row lock forces the
    second caller to wait for the first to finish and then see its outcome,
    so exactly one of the two ever succeeds.
    """
    from .models import RefreshToken

    reused = False
    with transaction.atomic():
        token = (
            RefreshToken.all_tenants.select_for_update()
            .filter(token_hash=hash_refresh(raw))
            .first()
        )
        if token is None:
            raise TokenInvalidError("refresh token is not recognised")

        if token.revoked_at is not None:
            raise TokenInvalidError("refresh token has been revoked")

        if token.consumed_at is not None:
            reused = True
            RefreshToken.all_tenants.filter(
                family_id=token.family_id, revoked_at__isnull=True
            ).update(revoked_at=timezone.now())
        else:
            if token.expires_at <= timezone.now():
                raise TokenExpiredError("refresh token has expired")

            token.consumed_at = timezone.now()
            token.save(update_fields=["consumed_at", "updated_at"])

            new_raw, _new = issue_refresh(token.membership,
                                          family_id=token.family_id,
                                          device=token.device)

    # Raised outside the atomic block so the family-revocation write above is
    # not rolled back by the exception propagating through this function.
    if reused:
        raise RefreshReuseDetectedError(
            "refresh token replayed; the whole token family has been revoked"
        )

    return token.membership, new_raw


def revoke_family(family_id) -> int:
    from .models import RefreshToken

    return RefreshToken.all_tenants.filter(
        family_id=family_id, revoked_at__isnull=True
    ).update(revoked_at=timezone.now())
