"""Celery entry points.

Scheduled work is expressed as management commands and invoked through this
one task rather than as a task per job. The commands are the real interface:
they can be run by hand during an incident, they are what the runbooks name,
and keeping a single Celery wrapper means a scheduled job cannot drift away
from the thing an operator actually runs.
"""

from __future__ import annotations

import logging

from celery import shared_task
from django.core.management import call_command

logger = logging.getLogger("educore.tasks")

# Only these may be invoked from the scheduler. An allowlist because the task
# takes a command name as an argument, and a broker that is ever compromised
# should not hand an attacker arbitrary management commands.
SCHEDULABLE = {
    "relay_outbox",
    "roll_timetable",
    "verify_audit_chains",
    "estate_report",
}


@shared_task(name="educore.core.tasks.run_command", ignore_result=True)
def run_command(command: str, *args, **options) -> str:
    if command not in SCHEDULABLE:
        raise ValueError(f"{command!r} is not a schedulable command.")

    logger.info("running scheduled command: %s", command)
    try:
        call_command(command, *args, **options)
    except SystemExit as exc:
        # verify_audit_chains and relay_outbox exit non-zero to signal a
        # problem worth waking someone for. Surface it as a task failure so
        # the alerting path sees it rather than a log line nobody reads.
        if exc.code:
            raise RuntimeError(f"{command} exited with status {exc.code}") from exc

    return command
