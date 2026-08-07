from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "educore.core"
    label = "core"

    def ready(self):
        from . import (
            checks,  # noqa: F401  registers the system checks
            schema,  # noqa: F401  registers the OpenAPI auth extension
        )
