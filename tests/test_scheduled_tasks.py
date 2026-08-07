"""The Celery wrapper around scheduled management commands."""

from __future__ import annotations

import pytest
from django.conf import settings

from educore.core.tasks import SCHEDULABLE, run_command

pytestmark = pytest.mark.django_db


def test_a_scheduled_command_runs(school_a):
    assert run_command("relay_outbox") == "relay_outbox"


def test_an_arbitrary_command_is_refused():
    """The task takes a command name; a compromised broker must not turn that
    into arbitrary management-command execution."""
    with pytest.raises(ValueError, match="not a schedulable command"):
        run_command("flush")


def test_every_scheduled_job_names_an_allowlisted_command():
    """A beat entry pointing at a command the task will refuse is a job that
    fails every time it fires, and only in production."""
    for name, entry in settings.CELERY_BEAT_SCHEDULE.items():
        command = entry["args"][0]
        assert command in SCHEDULABLE, f"{name} schedules {command!r}"


def test_every_scheduled_command_exists():
    from django.core.management import get_commands

    available = get_commands()
    for command in SCHEDULABLE:
        assert command in available, f"{command} is not a registered command"


def test_a_failing_command_surfaces_as_a_task_failure(school_a, monkeypatch):
    """verify_audit_chains exits non-zero on a broken chain. That must reach
    the alerting path, not a log line nobody reads."""
    from educore.core import tasks

    def exit_one(*args, **kwargs):
        raise SystemExit(1)

    monkeypatch.setattr(tasks, "call_command", exit_one)

    with pytest.raises(RuntimeError, match="exited with status 1"):
        run_command("verify_audit_chains")
