"""Field-level encryption for restricted data at rest (doc 06).

Fernet (AES128-CBC + HMAC-SHA256, authenticated) rather than anything
hand-rolled -- the one rule for cryptography in this codebase is to use a
library that has been audited, never to write our own.

`FIELD_ENCRYPTION_KEY` is deliberately a separate setting from `SECRET_KEY`:
the two protect different things and must be rotatable independently. Losing
`SECRET_KEY` invalidates sessions and tokens; losing `FIELD_ENCRYPTION_KEY`
makes every encrypted column permanently unreadable, which is a far more
serious failure mode and should never be entangled with the more casual
history a Django `SECRET_KEY` tends to accumulate.
"""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet
from django.conf import settings
from django.db import models


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    return Fernet(settings.FIELD_ENCRYPTION_KEY.encode()
                  if isinstance(settings.FIELD_ENCRYPTION_KEY, str)
                  else settings.FIELD_ENCRYPTION_KEY)


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str) -> str:
    return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")


class EncryptedCharField(models.CharField):
    """A `CharField` that is encrypted in the database and plaintext in Python.

    Transparent on purpose: every existing read (`user.mfa_secret`) and write
    (`user.mfa_secret = secret`) in `educore/core/mfa.py` needed no change at
    all. A field a caller has to remember to encrypt is a field that
    eventually gets forgotten; making the storage layer responsible removes
    that failure mode entirely.

    Not filterable on plaintext value by design -- Fernet ciphertext includes
    a random IV, so `.filter(mfa_secret=...)` could never match by definition,
    and encrypted-at-rest data has no business being queried by its cleartext
    value anyway. Nothing in this codebase does that today (grep confirms
    `mfa_secret` is only ever read or written as an attribute, never filtered
    on), and this field intentionally offers no way to start.
    """

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if not value:
            return value
        return encrypt(value)

    def from_db_value(self, value, expression, connection):
        if not value:
            return value
        return decrypt(value)
