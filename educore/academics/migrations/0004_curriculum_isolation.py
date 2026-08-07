from django.db import migrations

from educore.core.db import composite_fks, rls


class Migration(migrations.Migration):

    dependencies = [("academics", "0003_subject_course_and_more")]

    operations = [
        composite_fks(
            ("academics_subject", "department_id", "academics_department"),
            ("academics_course", "subject_id", "academics_subject"),
            ("academics_course", "level_id", "academics_level"),
            ("academics_course", "academic_year_id", "academics_academicyear"),
        ),
        rls("academics_subject", "academics_course"),
    ]
