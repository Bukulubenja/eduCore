"""Celery application.

Tasks are dispatched by the outbox relay, not directly from request handlers:
enqueueing inside a transaction that may still roll back publishes work for
changes that never happened (doc 02, transactional outbox).
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")

app = Celery("educore")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    print(f"Request: {self.request!r}")
