from django.apps import AppConfig


class CommsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "educore.comms"
    label = "comms"

    def ready(self):
        # Registering here is what keeps `core` free of any dependency on a
        # domain module: the relay learns about handlers, never the reverse.
        from . import handlers  # noqa: F401
