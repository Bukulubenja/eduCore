"""One local command that runs the same checks as CI, in the same order.

`README.md`'s "## Checks" section lists these by hand; CI enforces most of
them as separate jobs. This exists so a developer gets the same verdict on a
workstation before pushing, without memorising six invocations. Each phase
runs in its own subprocess so one crashing tool cannot take the rest of the
run down with it.
"""

from __future__ import annotations

import os
import subprocess  # nosec B404 (hardcoded argv only, see _run below)
import sys
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

MANAGE_PY = Path(settings.BASE_DIR) / "manage.py"


class Command(BaseCommand):
    help = (
        "Run the pre-PR verification pipeline: lint, security scan, module "
        "boundaries, migrations, API schema drift, tests+coverage, tenant "
        "isolation battery, deploy checks, audit chain integrity."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--skip",
            action="append",
            default=[],
            metavar="PHASE",
            choices=[
                "lint", "security", "boundaries", "migrations", "schema",
                "tests", "isolation", "deploy-check", "audit-chain",
            ],
            help="Skip a phase by name (repeatable).",
        )

    def handle(self, *args, **options):
        skip = set(options["skip"])
        phases = [
            ("lint", self._lint),
            ("security", self._security),
            ("boundaries", self._boundaries),
            ("migrations", self._migrations),
            ("schema", self._schema),
            ("tests", self._tests),
            ("isolation", self._isolation),
            ("deploy-check", self._deploy_check),
            ("audit-chain", self._audit_chain),
        ]

        results: list[tuple[str, bool]] = []
        for name, fn in phases:
            if name in skip:
                self.stdout.write(f"skip  {name}")
                continue
            self.stdout.write(f"\n== {name} ==")
            ok = fn()
            results.append((name, ok))
            style = self.style.SUCCESS if ok else self.style.ERROR
            self.stdout.write(style(f"{'ok' if ok else 'FAIL':<5} {name}"))

        failures = [name for name, ok in results if not ok]
        self.stdout.write("")
        if failures:
            self.stdout.write(self.style.ERROR(f"FAILED: {', '.join(failures)}"))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("All checks passed."))

    # -- phases ---------------------------------------------------------

    def _run(self, cmd: list[str], env: dict[str, str] | None = None) -> bool:
        # cmd is always one of this file's own hardcoded literals below, never
        # user input, so the dynamic-argument shape S603/B603 flags is a false
        # positive here.
        result = subprocess.run(cmd, cwd=settings.BASE_DIR, env=env)  # noqa: S603 # nosec B603
        return result.returncode == 0

    def _lint(self) -> bool:
        return self._run([sys.executable, "-m", "ruff", "check", "."])

    def _security(self) -> bool:
        # Doc 06's "Secure development" table: SAST via bandit, on every PR.
        # Findings get fixed or suppressed inline with `# nosec <id>` and a
        # reason -- this phase is not meant to stay green by attrition.
        return self._run([sys.executable, "-m", "bandit", "-r", "educore/"])

    def _boundaries(self) -> bool:
        # Console script lives next to the interpreter in the active venv
        # (Scripts/ on Windows, bin/ on Unix); fall back to PATH lookup.
        lint_imports = Path(sys.executable).with_name("lint-imports")
        cmd = [str(lint_imports)] if lint_imports.exists() else ["lint-imports"]
        return self._run(cmd)

    def _migrations(self) -> bool:
        return self._run(
            [sys.executable, str(MANAGE_PY), "makemigrations", "--check", "--dry-run"]
        )

    def _schema(self) -> bool:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_schema = Path(tmp) / "schema.yml"
            generated = self._run([
                sys.executable, str(MANAGE_PY), "spectacular",
                "--file", str(tmp_schema), "--fail-on-warn",
            ])
            if not generated:
                return False
            committed = Path(settings.BASE_DIR) / "schema.yml"
            if tmp_schema.read_text() != committed.read_text():
                self.stderr.write(self.style.ERROR(
                    "schema.yml is stale -- regenerate with "
                    "`manage.py spectacular --file schema.yml` in this commit."
                ))
                return False
        return True

    def _tests(self) -> bool:
        return self._run(
            [sys.executable, "-m", "pytest", "--cov", "--cov-report=term-missing"]
        )

    def _isolation(self) -> bool:
        engine = settings.DATABASES["default"]["ENGINE"]
        if "postgresql" not in engine:
            self.stdout.write(self.style.WARNING(
                "SQLite in use -- the row-level security battery is skipped, "
                "not passing. A green run here is not proof of tenant "
                "isolation; see README."
            ))
            return True

        # Hardcoded argv, never user input.
        result = subprocess.run(  # nosec B603
            [sys.executable, "-m", "pytest", "-m", "postgres", "--no-header", "-q"],
            cwd=settings.BASE_DIR,
            capture_output=True,
            text=True,
        )
        self.stdout.write(result.stdout)
        if result.returncode != 0:
            return False
        if "skipped" in result.stdout:
            self.stderr.write(self.style.ERROR(
                "a postgres-marked test was skipped -- the isolation battery "
                "did not fully run."
            ))
            return False
        return True

    def _deploy_check(self) -> bool:
        # config.settings.production raises ImproperlyConfigured at import
        # time on anything but PostgreSQL (see production.py) -- there is no
        # meaningful deploy check to run against a SQLite dev box, so treat
        # it the same way _isolation treats SQLite: an explicit pass-through
        # warning, not a guaranteed, uninformative FAIL.
        engine = settings.DATABASES["default"]["ENGINE"]
        if "postgresql" not in engine:
            self.stdout.write(self.style.WARNING(
                "SQLite in use -- production settings require PostgreSQL and "
                "refuse to boot without it. Run `docker compose up -d` and "
                "set DATABASE_URL to check deploy-readiness for real."
            ))
            return True

        deploy_env = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "config.settings.production",
            "DEBUG": "False",
            "SECRET_KEY": os.environ.get(
                "SECRET_KEY", "local-deploy-check-key-not-a-real-secret"
            ),
            "ALLOWED_HOSTS": os.environ.get("ALLOWED_HOSTS", "localhost"),
        }
        return self._run(
            [sys.executable, str(MANAGE_PY), "check", "--deploy", "--fail-level", "WARNING"],
            env=deploy_env,
        )

    def _audit_chain(self) -> bool:
        return self._run([sys.executable, str(MANAGE_PY), "verify_audit_chains"])
