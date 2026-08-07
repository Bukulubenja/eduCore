"""Test settings. Fast, deterministic, no external services."""

from .base import *

DEBUG = False
# Length matters even here: PyJWT warns below 32 bytes for HS256, and a
# suite full of warnings is a suite nobody reads.
SECRET_KEY = "test-only-key-not-a-secret-padded-to-a-sane-length"
ALLOWED_HOSTS = ["testserver"]

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# Tests drive transactions explicitly; per-request atomicity would nest
# confusingly inside pytest-django's own transaction handling.
DATABASES["default"]["ATOMIC_REQUESTS"] = False
