"""Swap mfa_secret to EncryptedCharField.

A Python-level-only change -- EncryptedCharField doesn't alter db_type or
max_length, so this produces no schema-level ALTER on either backend. By the
time this runs, 0010 has already encrypted every existing value, so nothing
is ever misread as plaintext.
"""

from __future__ import annotations

from django.db import migrations

import educore.core.crypto


class Migration(migrations.Migration):
    dependencies = [("core", "0010_widen_and_encrypt_mfa_secret")]

    operations = [
        migrations.AlterField(
            model_name="user",
            name="mfa_secret",
            field=educore.core.crypto.EncryptedCharField(max_length=255, blank=True),
        ),
    ]
