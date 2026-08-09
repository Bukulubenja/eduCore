"""CSV bulk import with validation.

Always two passes: validate everything, then apply only if the whole file is
clean. A half-applied import is the worst outcome available -- the operator
cannot tell what landed, and re-running produces duplicates. Refusing the file
and returning row-numbered errors is slower for the operator and far cheaper
for everyone.

Row numbers are 1-based and count the header, so they match what the person
sees in their spreadsheet.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

from django.db import transaction

from educore.core.invites import send_invite
from educore.core.models import Membership, Role, RoleAssignment, User
from educore.core.tenancy import TenantContext

from .models import ImportBatch


@dataclass
class ImportResult:
    kind: str
    rows_total: int = 0
    rows_ok: int = 0
    errors: list[dict] = field(default_factory=list)
    applied: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, row: int, column: str, message: str) -> None:
        self.errors.append({"row": row, "column": column, "message": message})


def _read(content: str | bytes) -> list[dict]:
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(content)))


def _require(result, row_number, row, columns) -> bool:
    ok = True
    for column in columns:
        if not (row.get(column) or "").strip():
            result.error(row_number, column, "This column is required.")
            ok = False
    return ok


# -- Staff -------------------------------------------------------------------

STAFF_COLUMNS = ["full_name", "email", "role", "staff_number"]


def import_staff(content, *, school_id, actor=None, dry_run: bool = True):
    """Columns: full_name, email, role, staff_number (optional)."""
    result = ImportResult(kind=ImportBatch.Kind.STAFF)
    rows = _read(content)
    result.rows_total = len(rows)

    with TenantContext.scope(school_id):
        known_roles = {r.code: r for r in Role.objects.all()}
        seen_emails: set[str] = set()
        prepared = []

        for index, row in enumerate(rows, start=2):
            if not _require(result, index, row, ["full_name", "email", "role"]):
                continue

            email = row["email"].strip().lower()
            role_code = row["role"].strip().lower()

            if email in seen_emails:
                result.error(index, "email", "Duplicated within this file.")
                continue
            seen_emails.add(email)

            if role_code not in known_roles:
                result.error(index, "role",
                             f"Unknown role '{role_code}'. Known roles: "
                             f"{', '.join(sorted(known_roles))}.")
                continue

            existing = User.objects.filter(email=email).first()
            if existing and Membership.all_tenants.filter(
                user=existing, school_id=school_id
            ).exists():
                result.error(index, "email",
                             "Already a member of staff at this school.")
                continue

            prepared.append({
                "full_name": row["full_name"].strip(),
                "email": email,
                "role": known_roles[role_code],
                "staff_number": (row.get("staff_number") or "").strip(),
                "user": existing,
            })

        result.rows_ok = len(prepared)
        if dry_run or not result.ok:
            _record_batch(result, school_id, actor, dry_run)
            return result

        with transaction.atomic():
            for entry in prepared:
                user = entry["user"]
                if user is None:
                    user = User.objects.create_user(
                        email=entry["email"], full_name=entry["full_name"],
                        password=None,
                    )
                    user.set_unusable_password()
                    user.save(update_fields=["password"])

                membership = Membership.objects.create(
                    school_id=school_id, user=user,
                    staff_number=entry["staff_number"],
                    status=Membership.Status.INVITED,
                )
                RoleAssignment.objects.create(
                    school_id=school_id, membership=membership,
                    role=entry["role"],
                )
                send_invite(membership)
            result.applied = True

        _record_batch(result, school_id, actor, dry_run)
    return result


# -- Students ----------------------------------------------------------------

STUDENT_COLUMNS = ["full_name", "admission_number", "class_group", "residency"]


def import_students(content, *, school_id, term, actor=None, dry_run: bool = True):
    """Columns: full_name, admission_number, class_group, residency, scan_code."""
    from educore.academics.models import ClassGroup
    from educore.students.models import Student
    from educore.students.services import enrol

    result = ImportResult(kind=ImportBatch.Kind.STUDENTS)
    rows = _read(content)
    result.rows_total = len(rows)

    with TenantContext.scope(school_id):
        groups = {g.name.lower(): g for g in ClassGroup.objects.all()}
        existing_numbers = set(
            Student.objects.values_list("admission_number", flat=True)
        )
        seen: set[str] = set()
        prepared = []

        for index, row in enumerate(rows, start=2):
            if not _require(result, index, row,
                            ["full_name", "admission_number", "class_group"]):
                continue

            number = row["admission_number"].strip()
            group_name = row["class_group"].strip().lower()
            residency = (row.get("residency") or "day").strip().lower()

            if number in seen:
                result.error(index, "admission_number",
                             "Duplicated within this file.")
                continue
            seen.add(number)

            if number in existing_numbers:
                result.error(index, "admission_number",
                             "Already used by another student at this school.")
                continue

            if group_name not in groups:
                result.error(index, "class_group",
                             f"Unknown class '{row['class_group'].strip()}'. "
                             f"Create it before importing students.")
                continue

            if residency not in dict(Student.Residency.choices):
                result.error(index, "residency",
                             "Must be 'day' or 'boarding'.")
                continue

            prepared.append({
                "full_name": row["full_name"].strip(),
                "admission_number": number,
                "class_group": groups[group_name],
                "residency": residency,
                "scan_code": (row.get("scan_code") or "").strip(),
            })

        result.rows_ok = len(prepared)
        if dry_run or not result.ok:
            _record_batch(result, school_id, actor, dry_run)
            return result

        with transaction.atomic():
            for entry in prepared:
                student = Student.objects.create(
                    school_id=school_id,
                    full_name=entry["full_name"],
                    admission_number=entry["admission_number"],
                    residency=entry["residency"],
                    scan_code=entry["scan_code"],
                )
                enrol(student=student, class_group=entry["class_group"],
                      term=term)
            result.applied = True

        _record_batch(result, school_id, actor, dry_run)
    return result


# -- Guardians ---------------------------------------------------------------


def import_guardians(content, *, school_id, actor=None, dry_run: bool = True):
    """Columns: admission_number, guardian_name, email, phone, relationship.

    Links are created **unverified**. Verification is a deliberate human act:
    an import file is not evidence that this adult is entitled to a child's
    attendance, location and grades (doc 06, threat 5).
    """
    from educore.students.models import GuardianLink, Student

    result = ImportResult(kind=ImportBatch.Kind.GUARDIANS)
    rows = _read(content)
    result.rows_total = len(rows)

    with TenantContext.scope(school_id):
        students = {s.admission_number: s for s in Student.objects.all()}
        relationships = dict(GuardianLink.Relationship.choices)
        prepared = []

        for index, row in enumerate(rows, start=2):
            if not _require(result, index, row,
                            ["admission_number", "guardian_name",
                             "relationship"]):
                continue

            number = row["admission_number"].strip()
            email = (row.get("email") or "").strip().lower()
            phone = (row.get("phone") or "").strip()
            relationship = row["relationship"].strip().lower()

            if number not in students:
                result.error(index, "admission_number", "No such student.")
                continue
            if relationship not in relationships:
                result.error(index, "relationship",
                             f"Must be one of: {', '.join(relationships)}.")
                continue
            if not email and not phone:
                result.error(index, "email",
                             "A guardian needs either an email or a phone number.")
                continue

            prepared.append({
                "student": students[number],
                "full_name": row["guardian_name"].strip(),
                "email": email or None,
                "phone": phone or None,
                "relationship": relationship,
            })

        result.rows_ok = len(prepared)
        if dry_run or not result.ok:
            _record_batch(result, school_id, actor, dry_run)
            return result

        with transaction.atomic():
            for entry in prepared:
                user = None
                if entry["email"]:
                    user = User.objects.filter(email=entry["email"]).first()
                if user is None and entry["phone"]:
                    user = User.objects.filter(phone_e164=entry["phone"]).first()
                if user is None:
                    user = User.objects.create_user(
                        email=entry["email"], phone_e164=entry["phone"],
                        full_name=entry["full_name"], password=None,
                    )
                    user.set_unusable_password()
                    user.save(update_fields=["password"])

                membership, created = Membership.objects.get_or_create(
                    school_id=school_id, user=user,
                    defaults={"status": Membership.Status.INVITED},
                )
                if created:
                    send_invite(membership)
                parent_role = Role.objects.filter(code="parent").first()
                if parent_role:
                    RoleAssignment.objects.get_or_create(
                        school_id=school_id, membership=membership,
                        role=parent_role, scope_type="", scope_id=None,
                    )
                GuardianLink.objects.get_or_create(
                    school_id=school_id, student=entry["student"],
                    membership=membership,
                    defaults={"relationship": entry["relationship"],
                              "verified": False},
                )
            result.applied = True

        _record_batch(result, school_id, actor, dry_run)
    return result


def _record_batch(result: ImportResult, school_id, actor, dry_run: bool):
    status = (ImportBatch.Status.VALIDATED if dry_run
              else (ImportBatch.Status.APPLIED if result.applied
                    else ImportBatch.Status.REJECTED))
    ImportBatch.objects.create(
        school_id=school_id, kind=result.kind, status=status,
        uploaded_by=actor, rows_total=result.rows_total,
        rows_ok=result.rows_ok, rows_failed=len(result.errors),
        errors=result.errors[:200],
    )
