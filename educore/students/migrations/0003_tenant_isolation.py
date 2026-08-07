from django.db import migrations

from educore.core.db import append_only, composite_fks, rls

TENANT_TABLES = [
    "students_student",
    "students_enrolment",
    "students_guardianlink",
    "students_studentattendance",
    "students_gateevent",
]


class Migration(migrations.Migration):

    dependencies = [
        ("students", "0002_initial"),
        ("timetable", "0002_tenant_isolation"),
    ]

    operations = [
        composite_fks(
            ("students_enrolment", "student_id", "students_student"),
            ("students_enrolment", "class_group_id", "academics_classgroup"),
            ("students_enrolment", "term_id", "academics_term"),
            ("students_guardianlink", "student_id", "students_student"),
            ("students_guardianlink", "membership_id", "core_membership"),
            ("students_studentattendance", "student_id", "students_student"),
            ("students_studentattendance", "lesson_instance_id",
             "timetable_lessoninstance"),
            ("students_studentattendance", "marked_by_id", "core_membership"),
            ("students_gateevent", "student_id", "students_student"),
            ("students_gateevent", "campus_id", "core_campus"),
        ),
        rls(*TENANT_TABLES),
        # A gate scan is an observation, not a record to be tidied later.
        append_only("students_gateevent"),
    ]
