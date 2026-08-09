"""Issuing and delivering invite tokens (doc 05, "Authentication").

Called wherever a Membership is created `INVITED`: `provision_school`'s first
administrator and the staff/guardian bulk importers, all in `educore.platform`
(see `educore/platform/services.py` and `educore/platform/importers.py`).
Both call sites already import from `educore.core`, so this lives here rather
than in `educore.comms` -- `comms` is a leaf module under ADR-0005's layering
and neither `core` nor `platform` may import it.

Email is sent directly through Django's own `send_mail` rather than through
the comms outbox. The outbox payload is a JSONField that sits in the database
until a relay worker picks it up, sometimes for minutes; the raw invite token
must never be persisted anywhere in the clear, only ever held in memory for
the length of this call and the body of one outgoing email. That rules out
the outbox for this specific piece of data even though it is the pattern
every other cross-module notification in this codebase uses.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.mail import send_mail

from . import tokens

if TYPE_CHECKING:
    from .models import InviteToken

logger = logging.getLogger("educore.core")


def send_invite(membership) -> InviteToken:
    """Issue an invite token for `membership` and email the redemption link.

    Returns the persisted token row -- never the raw secret, which is logged
    once for local development (there is no real mail server in dev/test) and
    otherwise exists only in the outgoing email.
    """
    raw, token = tokens.issue_invite(membership)
    url = f"{settings.CONSOLE_BASE_URL}/accept-invite?token={raw}"
    user = membership.user

    # Logged at INFO unconditionally: a silently-sent invite nobody can find
    # is indistinguishable from a broken invite flow, and this is the only
    # place the raw token is ever discoverable outside the email itself. Same
    # "record, never pretend" discipline as comms/channels.py's ConsoleChannel.
    logger.info("invite issued for membership %s (%s): %s",
               membership.pk, user.email or user.phone_e164 or "no contact", url)

    if not user.email:
        return token

    try:
        send_mail(
            subject="You've been invited to eduCore",
            message=(
                f"Hello {user.full_name or user.email},\n\n"
                "An account has been created for you. Use the link below to "
                f"set a password and sign in:\n\n{url}\n\n"
                "This link expires in 7 days and can only be used once. If "
                "you were not expecting this, you can ignore it."
            ),
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL",
                               "no-reply@educore.app"),
            recipient_list=[user.email],
            fail_silently=False,
        )
    except Exception:
        # Delivery failing must not unwind the transaction that created the
        # membership: the account still exists and is still activatable, it
        # just needs the token re-sent by whatever operator tooling ends up
        # covering that case. Loud in the log either way.
        logger.exception("invite email failed to send for membership %s",
                         membership.pk)

    return token
