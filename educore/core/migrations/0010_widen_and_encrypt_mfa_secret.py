"""Widen mfa_secret and encrypt any existing plaintext values.

Two steps in one migration, in this order deliberately: the column must be
wide enough to hold Fernet ciphertext (~140 chars for a 32-char base32
secret) *before* anything is encrypted into it, or the encrypt step would
truncate. The field is still a plain CharField at this point in the
migration history -- the class swap to EncryptedCharField happens in
0011, once every row already holds ciphertext, so nothing is ever read
through the encrypted field's from_db_value() before it's actually
encrypted.
"""

from __future__ import annotations

from django.db import migrations, models


def encrypt_existing_secrets(apps, schema_editor):
    from educore.core.crypto import encrypt

    User = apps.get_model("core", "User")
    # nosec B106 -- bandit misreads this queryset filter as a hardcoded
    # password argument; it's an empty-string exclusion, not a credential.
    for user in User.objects.exclude(mfa_secret="").iterator():  # nosec B106
        user.mfa_secret = encrypt(user.mfa_secret)
        user.save(update_fields=["mfa_secret"])


def decrypt_existing_secrets(apps, schema_editor):
    """Reverse: only meaningful if 0011 has already been reversed first
    (migration order guarantees that), so every value here is still
    ciphertext at this point."""
    from educore.core.crypto import decrypt

    User = apps.get_model("core", "User")
    for user in User.objects.exclude(mfa_secret="").iterator():  # nosec B106
        user.mfa_secret = decrypt(user.mfa_secret)
        user.save(update_fields=["mfa_secret"])


class Migration(migrations.Migration):
    dependencies = [("core", "0009_invitetoken_isolation")]

    operations = [
        migrations.AlterField(
            model_name="user",
            name="mfa_secret",
            field=models.CharField(max_length=255, blank=True),
        ),
        migrations.RunPython(encrypt_existing_secrets, decrypt_existing_secrets),
    ]
