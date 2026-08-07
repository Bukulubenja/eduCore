from django.db import migrations

from educore.core.db import composite_fks, rls

TENANT_TABLES = [
    "delivery_schemeofwork",
    "delivery_syllabusunit",
    "delivery_lessonsession",
    "delivery_lessonqrredemption",
    "delivery_substitution",
    "delivery_coverageentry",
]


class Migration(migrations.Migration):

    dependencies = [
        ("delivery", "0002_initial"),
        ("timetable", "0002_tenant_isolation"),
    ]

    operations = [
        composite_fks(
            ("delivery_schemeofwork", "course_id", "academics_course"),
            ("delivery_schemeofwork", "term_id", "academics_term"),
            ("delivery_syllabusunit", "scheme_id", "delivery_schemeofwork"),
            ("delivery_syllabusunit", "parent_id", "delivery_syllabusunit"),
            ("delivery_lessonsession", "lesson_instance_id",
             "timetable_lessoninstance"),
            ("delivery_lessonsession", "actual_teacher_id", "core_membership"),
            ("delivery_lessonsession", "room_id", "timetable_room"),
            ("delivery_lessonqrredemption", "membership_id", "core_membership"),
            ("delivery_substitution", "lesson_instance_id",
             "timetable_lessoninstance"),
            ("delivery_substitution", "original_teacher_id", "core_membership"),
            ("delivery_substitution", "substitute_teacher_id", "core_membership"),
            ("delivery_coverageentry", "session_id", "delivery_lessonsession"),
            ("delivery_coverageentry", "unit_id", "delivery_syllabusunit"),
        ),
        rls(*TENANT_TABLES),
    ]
