"""Base settings shared by every environment.

Environment-specific modules import * from here and override. Nothing in this
file may enable a development convenience -- see config/settings/local.py.
"""

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, []),
)
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("SECRET_KEY", default="insecure-development-key-override-me")
# Separate from SECRET_KEY on purpose (educore/core/crypto.py) -- protects
# encrypted-at-rest columns (User.mfa_secret) and must be rotatable on its
# own schedule. The dev default below is fixed only so a fresh clone runs
# without extra setup; production must override it via env.
FIELD_ENCRYPTION_KEY = env(
    "FIELD_ENCRYPTION_KEY",
    default="NBbyyekox3qI5ZHmgnXJSUYeNAFvpb_9hGhSdTr5USo=",
)
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")

# Base URL of the Next.js console. Used only to build the link inside an
# invite email (educore/core/invites.py) -- never trusted as input, never
# used for anything security-sensitive itself.
CONSOLE_BASE_URL = env("CONSOLE_BASE_URL", default="http://localhost:3000")

# -- Applications ------------------------------------------------------------

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "drf_spectacular",
]

# Domain modules. Order mirrors the dependency layering in ADR-0005: a module
# may only import from those above it in this list.
LOCAL_APPS = [
    "educore.core",
    "educore.academics",
    "educore.timetable",
    "educore.presence",
    "educore.delivery",
    "educore.students",
    "educore.assessment",
    "educore.comms",
    "educore.insights",
    "educore.platform",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Must run after authentication: it resolves the tenant from the request's
    # Membership and binds it for the duration of the request.
    "educore.core.middleware.TenantMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# -- Database ----------------------------------------------------------------
#
# PostgreSQL is the only supported production engine: row-level security,
# range partitioning and composite foreign keys all depend on it (ADR-0001).
# SQLite is permitted locally so the suite runs without a server, at the cost
# of leaving isolation layer 3 unexercised.

DATABASES = {
    "default": env.db_url(
        "DATABASE_URL",
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
    )
}
DATABASES["default"]["ATOMIC_REQUESTS"] = True

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "core.User"

# -- Authentication ----------------------------------------------------------

# Argon2id first (doc 06). Django ships the rest for verifying legacy hashes.
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.ScryptPasswordHasher",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
]

# -- Internationalisation ----------------------------------------------------

LANGUAGE_CODE = "en-gb"
TIME_ZONE = "UTC"          # Always UTC in storage; rendered per School timezone.
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# -- REST framework ----------------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "educore.core.authentication.BearerTokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "EXCEPTION_HANDLER": "educore.core.api.exception_handler",
    "DEFAULT_PAGINATION_CLASS": "educore.core.api.CursorPagination",
    "PAGE_SIZE": 50,
    "UNAUTHENTICATED_USER": None,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "eduCore API",
    "DESCRIPTION": "School operations and accountability platform.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SCHEMA_PATH_PREFIX": "/api/v1",
    # Several models carry a `status` field with different choices; without
    # explicit names drf-spectacular collapses them into one StatusEnum and
    # generated clients get the wrong value set.
    "ENUM_NAME_OVERRIDES": {
        "AttendanceStatusEnum": "educore.presence.models.AttendanceStatus.choices",
        "DispositionEnum": "educore.presence.models.Disposition.choices",
        "VerdictEnum": "educore.presence.models.Verdict.choices",
        "SignalTypeEnum": "educore.presence.models.SignalType.choices",
    },
}

# -- Celery ------------------------------------------------------------------

CELERY_BROKER_URL = env("REDIS_URL", default="redis://localhost:6379/0")
CELERY_RESULT_BACKEND = None          # Results go to the database, not Redis.
CELERY_TASK_ALWAYS_EAGER = False
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_TASK_DEFAULT_QUEUE = "default"
CELERY_TASK_ROUTES = {
    "educore.comms.*": {"queue": "notify"},
    "educore.insights.*": {"queue": "reports"},
}

# Scheduled work. These are not optional extras -- two of the product's
# headline facts exist only because a scheduler produces them:
#   * a lesson is "missed" only because roll_timetable said so;
#   * a notification reaches anyone only because relay_outbox drained the queue.
# Without them the system fails silently, which is the worst way to fail.
CELERY_BEAT_SCHEDULE = {
    "relay-outbox": {
        "task": "educore.core.tasks.run_command",
        "schedule": 60.0,
        "args": ("relay_outbox",),
        "options": {"queue": "notify", "expires": 55},
    },
    "roll-timetable": {
        "task": "educore.core.tasks.run_command",
        "schedule": 300.0,
        "args": ("roll_timetable",),
        "options": {"expires": 280},
    },
    "verify-audit-chains": {
        "task": "educore.core.tasks.run_command",
        "schedule": 24 * 60 * 60.0,
        "args": ("verify_audit_chains",),
        "options": {"queue": "reports"},
    },
    "estate-report": {
        "task": "educore.core.tasks.run_command",
        "schedule": 24 * 60 * 60.0,
        "args": ("estate_report",),
        "options": {"queue": "reports"},
    },
    "alert-staff-absences": {
        "task": "educore.core.tasks.run_command",
        "schedule": 300.0,
        "args": ("alert_staff_absences",),
        "options": {"queue": "notify", "expires": 280},
    },
}

# -- Domain policy defaults --------------------------------------------------
# Platform-enforced bounds. Schools tune within these; they cannot escape them.

ATTENDANCE_MIN_ACCEPT_THRESHOLD = 40    # ADR-0002: nothing below this is "verified".
ATTENDANCE_DEFAULT_ACCEPT = 75
ATTENDANCE_DEFAULT_REVIEW = 45
ATTENDANCE_MIN_EVIDENCE_WEIGHT = 40
ATTENDANCE_MAX_CLOCK_SKEW_SECONDS = 300
SYNC_MAX_BACKDATE_DAYS = 14

# Minutes after a school's duty-start time (plus its own late-arrival grace)
# before a staff member with no check-in event becomes a leadership alert
# rather than just "not in yet". See alert_staff_absences.
STAFF_ABSENCE_ALERT_GRACE_MINUTES = 120

# -- Logging -----------------------------------------------------------------

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "()": "django.utils.log.ServerFormatter",
            "format": "{levelname} {asctime} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "json"},
    },
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", default="INFO")},
    "loggers": {
        "educore": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "educore.audit": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
