"""User.mfa_secret is encrypted at rest, not just plumbed through mfa.py.

A test that only calls mfa.provision()/verify_code() and checks the result
would pass even if encryption silently no-ops -- the ORM round-trip hides
that. These read the actual stored bytes with a raw cursor to prove the
column genuinely never holds the plaintext secret.
"""

from __future__ import annotations

import pyotp
import pytest
from django.db import connection

from educore.core import mfa
from educore.core.crypto import decrypt, encrypt
from educore.core.models import User

pytestmark = pytest.mark.django_db


def _raw_mfa_secret(email: str) -> str:
    # Filtered by email, not id: SQLite stores UUIDField as a bare hex
    # string with no dashes, so a manually str()-formatted UUID in raw SQL
    # never matches -- email sidesteps the mismatch entirely.
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT mfa_secret FROM core_user WHERE email = %s", [email]
        )
        return cursor.fetchone()[0]


@pytest.fixture
def user():
    return User.objects.create_user(
        email="mfa-encryption@example.com", full_name="MFA Test", password="x"
    )


def test_the_stored_value_is_not_the_plaintext_secret(user):
    details = mfa.provision(user)
    raw = _raw_mfa_secret(user.email)

    assert raw != details["secret"]
    assert details["secret"] not in raw


def test_the_stored_value_decrypts_back_to_the_real_secret(user):
    details = mfa.provision(user)
    raw = _raw_mfa_secret(user.email)

    assert decrypt(raw) == details["secret"]


def test_a_fresh_read_from_the_database_still_verifies_codes(user):
    details = mfa.provision(user)
    code = pyotp.TOTP(details["secret"]).now()

    reloaded = User.objects.get(pk=user.pk)
    assert mfa.verify_code(reloaded, code)


def test_blank_secret_is_not_encrypted_into_an_empty_ciphertext(user):
    """An unenrolled user's blank secret should stay a plain empty string in
    the database, not an encrypted representation of nothing -- there's no
    secret to protect, and it keeps "not enrolled" visually obvious in the
    raw column."""
    assert user.mfa_secret == ""
    assert _raw_mfa_secret(user.email) == ""


def test_encrypting_the_same_secret_twice_produces_different_ciphertext(user):
    """Fernet includes a random IV -- this is expected, not a bug, and
    confirms the encryption is using real randomised crypto rather than
    something deterministic (which would leak whether two users share a
    secret by comparing ciphertext)."""
    secret = pyotp.random_base32()
    assert encrypt(secret) != encrypt(secret)
