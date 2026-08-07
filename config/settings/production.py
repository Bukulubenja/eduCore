"""Production settings.

Deliberately fails fast. A misconfigured production process must refuse to
start rather than serve traffic with isolation or transport security disabled
(doc 06 -- "secure defaults ... enforced by a startup check").
"""

from django.core.exceptions import ImproperlyConfigured

from .base import *
from .base import DATABASES, env

DEBUG = False
SECRET_KEY = env("SECRET_KEY")           # No default: absent means no boot.
ALLOWED_HOSTS = env("ALLOWED_HOSTS")

if not ALLOWED_HOSTS:
    raise ImproperlyConfigured("ALLOWED_HOSTS must be set in production.")

if "postgresql" not in DATABASES["default"]["ENGINE"]:
    raise ImproperlyConfigured(
        "PostgreSQL is required in production: row-level security, the "
        "isolation backstop of ADR-0001, has no equivalent on other engines."
    )

# -- Transport ---------------------------------------------------------------

SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31_536_000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = True
CSRF_COOKIE_HTTPONLY = True
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

# -- Database ----------------------------------------------------------------

DATABASES["default"]["CONN_MAX_AGE"] = 0        # PgBouncer owns pooling.
DATABASES["default"].setdefault("OPTIONS", {})
DATABASES["default"]["OPTIONS"].update({
    "sslmode": env("PGSSLMODE", default="require"),
    # Bounds a runaway query so one tenant cannot degrade the rest (ADR-0001,
    # accepted cost: noisy neighbours).
    "options": "-c statement_timeout=30000",
})

CELERY_TASK_ALWAYS_EAGER = False

# -- Error reporting ---------------------------------------------------------

SENTRY_DSN = env("SENTRY_DSN", default="")
if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[DjangoIntegration()],
        traces_sample_rate=env.float("SENTRY_TRACES_SAMPLE_RATE", default=0.1),
        # PII must never leave the estate (doc 06, data classification).
        send_default_pii=False,
        release=env("RELEASE_TAG", default=""),
    )
