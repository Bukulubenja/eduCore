from django.db import migrations

from educore.core.db import composite_fks, rls

TENANT_TABLES = [
    "movement_passout",
    "movement_trip",
    "movement_tripparticipant",
    "movement_tripsupervisor",
]


class Migration(migrations.Migration):

    dependencies = [
        ("movement", "0001_initial"),
        ("students", "0003_tenant_isolation"),
        ("core", "0002_tenant_isolation"),
    ]

    operations = [
        composite_fks(
            ("movement_passout", "student_id", "students_student"),
            ("movement_passout", "requested_by_id", "core_membership"),
            ("movement_passout", "approved_by_id", "core_membership"),
            ("movement_passout", "departure_gate_event_id", "students_gateevent"),
            ("movement_passout", "return_gate_event_id", "students_gateevent"),
            ("movement_trip", "created_by_id", "core_membership"),
            ("movement_trip", "approved_by_id", "core_membership"),
            ("movement_tripparticipant", "trip_id", "movement_trip"),
            ("movement_tripparticipant", "student_id", "students_student"),
            ("movement_tripsupervisor", "trip_id", "movement_trip"),
            ("movement_tripsupervisor", "membership_id", "core_membership"),
        ),
        rls(*TENANT_TABLES),
    ]
