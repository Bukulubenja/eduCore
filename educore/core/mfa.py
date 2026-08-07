"""Multi-factor authentication and step-up.

Two distinct things share this module:

  * **Enrolment and login MFA** -- a TOTP secret per user, required for the
    privileged roles listed in doc 06.
  * **Step-up** -- a short-lived grant proving the person at the keyboard
    re-authenticated *just now*, required for a handful of irreversible acts:
    releasing marks, assigning roles, bulk export, approving a device.

Step-up is not the same as being logged in. A 15-minute access token in a
browser left open in a staff room is not evidence that the head teacher is
present, and releasing a term's results is exactly the act where that
distinction matters.
"""

from __future__ import annotations

import secrets
from datetime import timedelta

import pyotp
from django.utils import timezone

# Long enough to complete one action, short enough that a walked-away-from
# session cannot be used by the next person to sit down.
STEP_UP_TTL = timedelta(minutes=5)

# Accept the adjacent 30-second window either side. Phone clocks drift, and a
# rejected valid code trains people to disable MFA.
TOTP_DRIFT_WINDOWS = 1


class MfaError(Exception):
    pass


class MfaNotEnrolledError(MfaError):
    pass


class InvalidCodeError(MfaError):
    pass


class StepUpRequiredError(MfaError):
    """Raised by an action that needs a fresh step-up grant."""


def provision(user) -> dict:
    """Create (or replace) a user's TOTP secret and return enrolment details.

    The secret is returned exactly once. Re-provisioning invalidates the old
    authenticator, which is the recovery path when someone loses their phone.
    """
    secret = pyotp.random_base32()
    user.mfa_secret = secret
    user.mfa_enabled = False        # Not enabled until a code is confirmed.
    user.save(update_fields=["mfa_secret", "mfa_enabled"])

    uri = pyotp.TOTP(secret).provisioning_uri(
        name=user.email or user.phone_e164 or str(user.pk),
        issuer_name="eduCore",
    )
    return {"secret": secret, "otpauth_uri": uri}


def confirm(user, code: str) -> None:
    """Finish enrolment by proving the authenticator works."""
    if not user.mfa_secret:
        raise MfaNotEnrolledError("No enrolment in progress for this account.")
    if not verify_code(user, code):
        raise InvalidCodeError("That code is not valid.")

    user.mfa_enabled = True
    user.save(update_fields=["mfa_enabled"])


def verify_code(user, code: str) -> bool:
    if not user.mfa_secret or not code:
        return False
    return pyotp.TOTP(user.mfa_secret).verify(
        str(code).strip(), valid_window=TOTP_DRIFT_WINDOWS
    )


def grant_step_up(membership, code: str):
    """Exchange a fresh TOTP code for a short-lived step-up grant."""
    from .models import StepUpGrant

    user = membership.user
    if not user.mfa_enabled:
        raise MfaNotEnrolledError(
            "This action requires multi-factor authentication. "
            "Enrol an authenticator app first."
        )
    if not verify_code(user, code):
        raise InvalidCodeError("That code is not valid.")

    token = secrets.token_urlsafe(32)
    return StepUpGrant.objects.create(
        school_id=membership.school_id,
        membership=membership,
        token=token,
        expires_at=timezone.now() + STEP_UP_TTL,
    ), token


def consume_step_up(membership, token: str, *, action: str) -> None:
    """Spend a step-up grant, or refuse the action.

    Single use. A grant that could authorise several releases would let one
    code cover a whole afternoon of them, which defeats the purpose.
    """
    from .models import StepUpGrant

    if not token:
        raise StepUpRequiredError(
            f"{action} requires re-authentication. Provide a current "
            "authenticator code."
        )

    grant = StepUpGrant.objects.filter(
        membership=membership, token=token, consumed_at__isnull=True,
        expires_at__gt=timezone.now(),
    ).first()
    if grant is None:
        raise StepUpRequiredError(
            "That re-authentication has expired or was already used."
        )

    grant.consumed_at = timezone.now()
    grant.used_for = action[:100]
    grant.save(update_fields=["consumed_at", "used_for", "updated_at"])
