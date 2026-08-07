from django.db import migrations

from educore.core.db import composite_fks, rls

TENANT_TABLES = [
    "timetable_room",
    "timetable_periodgrid",
    "timetable_periodslot",
    "timetable_timetableversion",
    "timetable_scheduledlesson",
    "timetable_calendarexception",
    "timetable_lessoninstance",
]


class Migration(migrations.Migration):

    dependencies = [
        ("timetable", "0001_initial"),
        ("academics", "0004_curriculum_isolation"),
    ]

    operations = [
        composite_fks(
            ("timetable_room", "campus_id", "core_campus"),
            ("timetable_periodslot", "grid_id", "timetable_periodgrid"),
            ("timetable_timetableversion", "academic_year_id",
             "academics_academicyear"),
            ("timetable_timetableversion", "grid_id", "timetable_periodgrid"),
            ("timetable_timetableversion", "published_by_id", "core_membership"),
            ("timetable_scheduledlesson", "version_id",
             "timetable_timetableversion"),
            ("timetable_scheduledlesson", "slot_id", "timetable_periodslot"),
            ("timetable_scheduledlesson", "course_id", "academics_course"),
            ("timetable_scheduledlesson", "class_group_id",
             "academics_classgroup"),
            ("timetable_scheduledlesson", "teacher_id", "core_membership"),
            ("timetable_scheduledlesson", "room_id", "timetable_room"),
            ("timetable_lessoninstance", "scheduled_lesson_id",
             "timetable_scheduledlesson"),
            ("timetable_lessoninstance", "expected_teacher_id", "core_membership"),
            ("timetable_lessoninstance", "expected_room_id", "timetable_room"),
            ("timetable_lessoninstance", "course_id", "academics_course"),
            ("timetable_lessoninstance", "class_group_id", "academics_classgroup"),
        ),
        rls(*TENANT_TABLES),
    ]
