"""Developer machine settings. Never used outside a workstation."""

from .base import *
from .base import env

DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]"]
SECRET_KEY = env("SECRET_KEY", default="insecure-development-key-override-me")

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# Run tasks inline so a developer does not need Redis and a worker to click
# through the app. Anything relying on real async behaviour must be covered by
# a test that sets CELERY_TASK_ALWAYS_EAGER = False explicitly.
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

INTERNAL_IPS = ["127.0.0.1"]
