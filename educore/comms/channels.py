"""Channel adapters.

One narrow interface per delivery channel, selected by setting. Email works
today through Django. Push and SMS need a real provider before production and
default to a console adapter that records rather than sends -- deliberately
loud in the delivery record rather than silently pretending to have sent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.conf import settings
from django.core.mail import send_mail
from django.utils.module_loading import import_string

logger = logging.getLogger("educore.comms")


@dataclass
class SendResult:
    ok: bool
    provider_message_id: str = ""
    error: str = ""
    # True when the provider confirms handover, not merely acceptance. Push
    # providers accept immediately and deliver later, which is exactly why
    # escalation to SMS is time-based rather than response-based.
    confirmed: bool = False


class BaseChannel:
    code = ""

    def address_for(self, membership) -> str:
        raise NotImplementedError

    def send(self, *, membership, notification) -> SendResult:
        raise NotImplementedError


class ConsoleChannel(BaseChannel):
    """Development and test default. Records, never transmits."""

    code = "console"

    def address_for(self, membership) -> str:
        return str(membership.pk)

    def send(self, *, membership, notification) -> SendResult:
        logger.info("[%s] to %s: %s", self.code, membership, notification.title)
        return SendResult(ok=True, provider_message_id=f"console:{notification.pk}")


class InAppChannel(BaseChannel):
    """No transport: the Notification row itself is the delivery."""

    code = "in_app"

    def address_for(self, membership) -> str:
        return str(membership.pk)

    def send(self, *, membership, notification) -> SendResult:
        return SendResult(ok=True, confirmed=True,
                          provider_message_id=f"in_app:{notification.pk}")


class EmailChannel(BaseChannel):
    code = "email"

    def address_for(self, membership) -> str:
        return membership.user.email or ""

    def send(self, *, membership, notification) -> SendResult:
        address = self.address_for(membership)
        if not address:
            return SendResult(ok=False, error="no email address on file")

        try:
            send_mail(
                subject=notification.title,
                message=notification.body,
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL",
                                   "no-reply@educore.app"),
                recipient_list=[address],
                fail_silently=False,
            )
        except Exception as exc:
            return SendResult(ok=False, error=str(exc)[:300])

        return SendResult(ok=True, provider_message_id=f"email:{notification.pk}")


class PushChannel(ConsoleChannel):
    """Placeholder for FCM/APNs.

    Left as a console adapter on purpose. A stub that claimed success would
    put `sent` rows in the delivery table for messages nobody received, and
    the whole reason that table exists is to stop us believing that.
    """

    code = "push"

    def address_for(self, membership) -> str:
        # Queryset filter value, not a credential.
        device = membership.devices.filter(status="active").exclude(
            push_token=""  # nosec B106
        ).first()
        return device.push_token if device else ""

    def send(self, *, membership, notification) -> SendResult:
        if not self.address_for(membership):
            return SendResult(ok=False, error="no registered device with a push token")
        return super().send(membership=membership, notification=notification)


class SmsChannel(ConsoleChannel):
    """Placeholder for a gateway. See PushChannel for why it is not a stub."""

    code = "sms"

    def address_for(self, membership) -> str:
        return membership.user.phone_e164 or ""

    def send(self, *, membership, notification) -> SendResult:
        if not self.address_for(membership):
            return SendResult(ok=False, error="no phone number on file")
        return super().send(membership=membership, notification=notification)


DEFAULT_BACKENDS = {
    "in_app": "educore.comms.channels.InAppChannel",
    "push": "educore.comms.channels.PushChannel",
    "sms": "educore.comms.channels.SmsChannel",
    "email": "educore.comms.channels.EmailChannel",
}

_cache: dict[str, BaseChannel] = {}


def get_channel(code: str) -> BaseChannel:
    if code not in _cache:
        configured = getattr(settings, "COMMS_CHANNEL_BACKENDS", {})
        path = {**DEFAULT_BACKENDS, **configured}[code]
        _cache[code] = import_string(path)()
    return _cache[code]


def reset_channel_cache() -> None:
    """Test seam, so a settings override is not defeated by memoisation."""
    _cache.clear()
