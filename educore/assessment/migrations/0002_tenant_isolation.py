from django.db import migrations

from educore.core.db import composite_fks, rls

TENANT_TABLES = [
    "assessment_gradingscale",
    "assessment_gradeband",
    "assessment_assessment",
    "assessment_score",
    "assessment_reportcard",
]


class Migration(migrations.Migration):

    dependencies = [
        ("assessment", "0001_initial"),
        ("students", "0003_tenant_isolation"),
        ("core", "0007_stepup_isolation"),
    ]

    operations = [
        composite_fks(
            ("assessment_gradingscale", "level_id", "academics_level"),
            ("assessment_gradeband", "scale_id", "assessment_gradingscale"),
            ("assessment_assessment", "course_id", "academics_course"),
            ("assessment_assessment", "term_id", "academics_term"),
            ("assessment_assessment", "grading_scale_id",
             "assessment_gradingscale"),
            ("assessment_assessment", "author_id", "core_membership"),
            ("assessment_assessment", "approved_by_id", "core_membership"),
            ("assessment_assessment", "moderated_by_id", "core_membership"),
            ("assessment_assessment", "released_by_id", "core_membership"),
            ("assessment_score", "assessment_id", "assessment_assessment"),
            ("assessment_score", "student_id", "students_student"),
            ("assessment_score", "entered_by_id", "core_membership"),
            ("assessment_reportcard", "student_id", "students_student"),
            ("assessment_reportcard", "term_id", "academics_term"),
            ("assessment_reportcard", "class_group_id", "academics_classgroup"),
            ("assessment_reportcard", "issued_by_id", "core_membership"),
        ),
        rls(*TENANT_TABLES),
    ]
