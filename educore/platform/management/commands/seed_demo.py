"""Build a coherent demo school so every console page has something to show.

    manage.py seed_demo            # create / reset the demo school
    manage.py seed_demo --wipe     # delete it first, then recreate

Not for production. Uses the ORM directly rather than the service layer so it
can set states (released assessments, past attendance) that the services
deliberately gate behind approvals and step-up MFA.
"""

from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from educore.academics.models import (
    AcademicYear,
    ClassGroup,
    Course,
    Department,
    Level,
    StaffProfile,
    Subject,
    Term,
)
from educore.assessment.models import Assessment, AssessmentState, Score
from educore.core.models import Membership, Role, RoleAssignment, School, User
from educore.core.outbox import relay_pending
from educore.core.tenancy import TenantContext
from educore.delivery.models import (
    CoverageEntry,
    LessonSession,
    SchemeOfWork,
    SyllabusUnit,
)
from educore.movement import services as movement
from educore.movement.models import PassOut, Trip
from educore.presence.models import (
    AttendanceException,
    AttendancePolicy,
    AttendanceRecord,
    AttendanceStatus,
    Disposition,
)
from educore.presence.services import active_policy
from educore.students.models import (
    Enrolment,
    GuardianLink,
    Student,
    StudentAttendance,
    StudentAttendanceStatus,
)
from educore.students.services import enrol
from educore.timetable.models import (
    LessonInstance,
    PeriodGrid,
    PeriodSlot,
    Room,
    ScheduledLesson,
    TimetableVersion,
)
from educore.timetable.services import publish, materialise

SLUG = "demo-secondary"
PASSWORD = "demo-pass-1234"  # nosec B105 -- demo fixture, not a real credential

STAFF = [
    ("director@demo.school", "Grace Nakato", "director"),
    ("deputy@demo.school", "Samuel Okello", "deputy"),
    ("dos@demo.school", "Ruth Achieng", "dos"),
    ("bursar@demo.school", "Peter Mugisha", "bursar"),
    ("hod.sci@demo.school", "Daniel Kato", "hod"),
    ("teacher1@demo.school", "Mary Auma", "teacher"),
    ("teacher2@demo.school", "John Ssali", "teacher"),
    ("teacher3@demo.school", "Sarah Nabirye", "teacher"),
]

FIRST = ["Amos", "Brenda", "Charles", "Doreen", "Emma", "Faith", "Gerald",
         "Hope", "Ivan", "Joan", "Kevin", "Lydia", "Moses", "Nancy", "Owen",
         "Peace", "Quinter", "Robert", "Susan", "Timothy", "Umar", "Violet",
         "Wilson", "Yvonne"]
LAST = ["Bakia", "Chebet", "Draku", "Eryenyu", "Fungo", "Gimbo", "Hakiza",
        "Isabirye", "Juuko", "Kirabo", "Lubega", "Mbabazi", "Nsubuga",
        "Opio", "Piloya"]


class Command(BaseCommand):
    help = "Create a demo school with data on every dashboard."

    def add_arguments(self, parser):
        parser.add_argument("--wipe", action="store_true",
                            help="delete the demo school before recreating it")

    def handle(self, *args, **options):
        random.seed(42)
        existing = School.objects.filter(slug=SLUG).first()
        if existing and options["wipe"]:
            self.stdout.write("Wiping existing demo school...")
            existing.delete()
            existing = None
        if existing:
            raise SystemExit(
                f"Demo school '{SLUG}' already exists. Re-run with --wipe."
            )

        school = School.objects.create(
            name="Demo Secondary School", slug=SLUG, timezone="Africa/Kampala",
            country="UG", status=School.Status.ACTIVE,
        )
        with TenantContext.scope(school.id):
            self._build(school)

        self.stdout.write(self.style.SUCCESS(
            "\nDemo school ready.\n"
            f"  Console login:  director@demo.school  /  {PASSWORD}\n"
            "  Every staff account above uses the same password.\n"
        ))

    # ------------------------------------------------------------------

    def _build(self, school):
        today = timezone.localdate()
        now = timezone.now()

        # -- roles, policy, grading scale ----------------------------------
        from educore.platform.services import (
            ROLE_TEMPLATES,
            _seed_campus,
            _seed_grading_scale,
        )
        for code, label, perms in ROLE_TEMPLATES:
            Role.objects.create(school_id=school.id, code=code, name=label,
                                permissions=perms, is_system=True)
        campus = _seed_campus(school)
        scale = _seed_grading_scale(school)
        policy = active_policy(school.id)

        # -- staff --------------------------------------------------------
        science = Department.objects.create(school_id=school.id,
                                            name="Science", code="SCI")
        members = {}
        for email, name, role_code in STAFF:
            user = User.objects.create_user(email=email, full_name=name,
                                            password=PASSWORD)
            m = Membership.objects.create(school_id=school.id, user=user,
                                          status=Membership.Status.ACTIVE)
            RoleAssignment.objects.create(school_id=school.id, membership=m,
                                          role=Role.objects.get(code=role_code))
            if role_code in ("teacher", "hod"):
                StaffProfile.objects.create(
                    school_id=school.id, membership=m, department=science,
                    kind=StaffProfile.Kind.TEACHING, job_title=name.split()[0],
                )
            members[role_code if role_code != "teacher" else email] = m
        science.head = members["hod"]
        science.save(update_fields=["head", "updated_at"])
        teachers = [members["hod"], members["teacher1@demo.school"],
                    members["teacher2@demo.school"], members["teacher3@demo.school"]]

        # -- parents ----------------------------------------------------
        parents = []
        for i in range(6):
            u = User.objects.create_user(email=f"parent{i}@demo.school",
                                         full_name=f"{LAST[i]} Family",
                                         password=PASSWORD)
            pm = Membership.objects.create(school_id=school.id, user=u,
                                           status=Membership.Status.ACTIVE)
            RoleAssignment.objects.create(school_id=school.id, membership=pm,
                                          role=Role.objects.get(code="parent"))
            parents.append(pm)

        # -- academic structure --------------------------------------------
        year = AcademicYear.objects.create(
            school_id=school.id, name=str(today.year),
            starts_on=date(today.year, 1, 15), ends_on=date(today.year, 12, 5),
            is_current=True,
        )
        term = Term.objects.create(
            school_id=school.id, academic_year=year, name="Term 2", sequence=2,
            starts_on=today - timedelta(days=35), ends_on=today + timedelta(days=45),
            is_current=True,
        )
        levels = {}
        groups = []
        for n in (1, 2, 3, 4):
            lvl = Level.objects.create(school_id=school.id, name=f"Senior {n}",
                                       code=f"S{n}", sequence=n)
            levels[n] = lvl
            for stream in ("Blue", "Gold"):
                groups.append(ClassGroup.objects.create(
                    school_id=school.id, level=lvl, name=f"S{n} {stream}"))
        s4blue = next(g for g in groups if g.name == "S4 Blue")

        # -- students + guardians -----------------------------------------
        students = []
        for i in range(24):
            name = f"{FIRST[i]} {LAST[i % len(LAST)]}"
            res = (Student.Residency.BOARDING if i % 3 == 0
                   else Student.Residency.DAY)
            st = Student.objects.create(
                school_id=school.id, admission_number=f"D{i + 1:03d}",
                full_name=name, residency=res, status=Student.Status.ENROLLED,
                scan_code=f"SC{i + 1:04d}",
                date_of_birth=date(today.year - 16, 1 + i % 12, 1 + i % 27),
            )
            enrol(student=st, class_group=groups[i % len(groups)], term=term)
            students.append(st)
        for st, pm in zip(students[:6], parents):
            GuardianLink.objects.create(
                school_id=school.id, student=st, membership=pm,
                relationship=GuardianLink.Relationship.GUARDIAN,
                is_primary_contact=True, verified=True,
                receives_notifications=True,
            )

        # -- timetable + delivery ----------------------------------------
        grid = PeriodGrid.objects.create(school_id=school.id, name="Standard",
                                         is_default=True)
        slots = [
            PeriodSlot.objects.create(school_id=school.id, grid=grid,
                                      name=f"Period {p}", sequence=p,
                                      starts_at=time(8 + p, 0),
                                      ends_at=time(8 + p, 40))
            for p in range(1, 5)
        ]
        subjects = [
            Subject.objects.create(school_id=school.id, name=n, code=c)
            for n, c in [("Physics", "PHY"), ("Chemistry", "CHE"),
                         ("Mathematics", "MAT"), ("Biology", "BIO")]
        ]
        courses = [
            Course.objects.create(school_id=school.id, subject=s,
                                  level=levels[4], academic_year=year,
                                  periods_per_week=4)
            for s in subjects
        ]
        version = TimetableVersion.objects.create(
            school_id=school.id, name=f"{today.year} Term 2 v1",
            academic_year=year, grid=grid, effective_from=term.starts_on,
        )
        rooms = [
            Room.objects.create(school_id=school.id, campus=campus,
                                name=f"Lab {r}", code=f"L{r}")
            for r in range(1, 5)
        ]
        for idx, course in enumerate(courses):
            for weekday in range(5):
                ScheduledLesson.objects.create(
                    school_id=school.id, version=version, weekday=weekday,
                    slot=slots[idx], course=course, class_group=s4blue,
                    teacher=teachers[idx], room=rooms[idx],
                )
        publish(version, published_by=members["dos"])
        materialise(version, term.starts_on, today + timedelta(days=7))

        # scheme of work: some units covered, deliberately behind pace
        physics = courses[0]
        scheme = SchemeOfWork.objects.create(school_id=school.id,
                                             course=physics, term=term,
                                             author=teachers[0],
                                             approved_by=members["dos"],
                                             approved_at=now)
        units = [
            SyllabusUnit.objects.create(school_id=school.id, scheme=scheme,
                                        sequence=n, title=title,
                                        planned_periods=periods)
            for n, (title, periods) in enumerate(
                [("Measurement", 2), ("Mechanics", 5), ("Waves", 4),
                 ("Electricity", 3), ("Magnetism", 3), ("Modern physics", 3)],
                start=1)
        ]
        past_instances = list(
            LessonInstance.objects.filter(course=physics, class_group=s4blue,
                                          date__lt=today).order_by("date")
        )
        for n, inst in enumerate(past_instances):
            session = LessonSession.objects.create(
                school_id=school.id, lesson_instance=inst,
                actual_teacher=inst.expected_teacher, room=inst.expected_room,
                opened_at=datetime.combine(inst.date, time(8, 5),
                                           tzinfo=timezone.get_current_timezone()),
                closed_at=datetime.combine(inst.date, time(8, 45),
                                           tzinfo=timezone.get_current_timezone()),
                verification_confidence=100,
            )
            inst.status = LessonInstance.Status.DELIVERED
            inst.save(update_fields=["status", "updated_at"])
            # only the first two units get completed -> behind pace
            if n < 4:
                CoverageEntry.objects.create(
                    school_id=school.id, session=session,
                    unit=units[min(n // 2, 1)],
                    completion=CoverageEntry.Completion.COMPLETED,
                )

        # -- staff attendance (last 12 working days + today) --------------
        staff_members = list(members.values())
        for offset in range(12, -1, -1):
            d = today - timedelta(days=offset)
            if d.weekday() >= 5:
                continue
            for m in staff_members:
                roll = random.random()
                if roll < 0.08:
                    status, disp = AttendanceStatus.ABSENT, Disposition.PROVISIONAL
                    first_in = last_out = None
                elif roll < 0.22:
                    status, disp = AttendanceStatus.LATE, Disposition.VERIFIED
                    first_in = datetime.combine(d, time(8, 5), tzinfo=timezone.get_current_timezone())
                    last_out = datetime.combine(d, time(16, 30), tzinfo=timezone.get_current_timezone())
                else:
                    status, disp = AttendanceStatus.PRESENT, Disposition.VERIFIED
                    first_in = datetime.combine(d, time(7, 20), tzinfo=timezone.get_current_timezone())
                    last_out = datetime.combine(d, time(16, 40), tzinfo=timezone.get_current_timezone())
                AttendanceRecord.objects.create(
                    school_id=school.id, membership=m, date=d, status=status,
                    disposition=disp,
                    confidence=90 if disp == Disposition.VERIFIED else 30,
                    first_in_at=first_in, last_out_at=last_out,
                    minutes_on_site=540 if first_in and last_out else 0,
                    policy_version=policy.version,
                )
        # one open appeal for the review queue
        a_record = AttendanceRecord.objects.filter(
            status=AttendanceStatus.ABSENT).first()
        if a_record:
            AttendanceException.objects.create(
                school_id=school.id, record=a_record, raised_by=a_record.membership,
                reason_code=AttendanceException.Reason.NO_CONNECTIVITY,
                narrative="Checked in but the app could not reach the server; "
                          "have a screenshot with the timestamp.",
                requested_status=AttendanceStatus.PRESENT,
            )

        # -- student attendance (drives the at-risk list) ---------------
        lesson_days = sorted({
            i.date for i in LessonInstance.objects.filter(
                class_group=s4blue, date__lte=today)
        })
        disengaged = set(students[:3])
        for st in students:
            for d in lesson_days:
                if st in disengaged:
                    status = random.choice([
                        StudentAttendanceStatus.ABSENT,
                        StudentAttendanceStatus.ABSENT,
                        StudentAttendanceStatus.PRESENT,
                    ])
                else:
                    status = (StudentAttendanceStatus.ABSENT
                              if random.random() < 0.05
                              else StudentAttendanceStatus.PRESENT)
                StudentAttendance.objects.create(
                    school_id=school.id, student=st, date=d, status=status,
                    method=StudentAttendance.Method.TEACHER_MARKED,
                    marked_by=teachers[0],
                )

        # -- one released assessment -----------------------------------
        assessment = Assessment.objects.create(
            school_id=school.id, course=physics, term=term,
            title="Physics Mid-Term", kind=Assessment.Kind.MIDTERM,
            max_score=100, weight=2, grading_scale=scale,
            scheduled_for=today - timedelta(days=10),
            state=AssessmentState.RELEASED, author=teachers[0],
            approved_by=members["dos"], approved_at=now,
            moderated_by=members["hod"], moderated_at=now,
            released_by=members["head_teacher"] if "head_teacher" in members
            else members["director"],
            released_at=now,
        )
        assessment.class_groups.add(s4blue)
        s4blue_students = [
            e.student for e in Enrolment.objects.filter(
                class_group=s4blue, is_active=True).select_related("student")
        ]
        for st in s4blue_students:
            base = 25 if st in disengaged else random.randint(45, 88)
            Score.objects.create(
                school_id=school.id, assessment=assessment, student=st,
                raw_score=max(0, min(100, base + random.randint(-8, 8))),
                entered_by=teachers[0], entered_at=now,
            )

        # -- movement (the showcase) ----------------------------------
        boarders = [s for s in students
                    if s.residency == Student.Residency.BOARDING]
        office = members["deputy"]
        bursar = members["bursar"]

        po1 = movement.request_pass_out(
            student=boarders[0], requested_by=office,
            reason=PassOut.Reason.MEDICAL, destination="Nsambya Hospital",
            responsible_person="Aunt (Ms. Kirabo)",
            expected_return_at=now + timedelta(hours=5),
            narrative="Dental appointment booked for this afternoon.")
        # left one waiting for a decision -> shows on /movement

        po2 = movement.request_pass_out(
            student=boarders[1], requested_by=office,
            reason=PassOut.Reason.FAMILY, destination="Home (Ntinda)",
            responsible_person="Father",
            expected_return_at=now + timedelta(hours=8))
        movement.decide_pass_out(pass_out=po2, decided_by=bursar, approved=True)
        movement.record_departure(pass_out=po2)

        po3 = movement.request_pass_out(
            student=boarders[2], requested_by=office,
            reason=PassOut.Reason.OFFICIAL, destination="Regional science fair",
            responsible_person="Mr. Kato",
            expected_return_at=now - timedelta(hours=2))
        movement.decide_pass_out(pass_out=po3, decided_by=bursar, approved=True)
        movement.record_departure(pass_out=po3)   # now overdue

        po4 = movement.request_pass_out(
            student=boarders[3], requested_by=office,
            reason=PassOut.Reason.WEEKEND_HOME, destination="Home",
            responsible_person="Mother",
            expected_return_at=now - timedelta(days=2))
        movement.decide_pass_out(pass_out=po4, decided_by=bursar, approved=True)
        movement.record_departure(pass_out=po4)
        movement.record_return(pass_out=po4)

        trip1 = movement.create_trip(
            created_by=teachers[0], title="National Museum visit",
            purpose="S4 Physics & History - waves and heritage",
            destination="Uganda National Museum, Kampala",
            departs_at=now + timedelta(days=3),
            returns_at=now + timedelta(days=3, hours=6))
        movement.set_trip_roster(
            trip=trip1, student_ids=[s.id for s in students[:12]],
            supervisor_ids=[teachers[0].pk, teachers[1].pk],
            lead_id=teachers[0].pk)
        movement.submit_trip(trip=trip1, actor=teachers[0])   # awaiting approval

        trip2 = movement.create_trip(
            created_by=teachers[1], title="Inter-school athletics",
            purpose="Term 2 games - track team",
            destination="Mandela National Stadium",
            departs_at=now - timedelta(hours=3),
            returns_at=now + timedelta(hours=4))
        movement.set_trip_roster(
            trip=trip2, student_ids=[s.id for s in students[12:20]],
            supervisor_ids=[teachers[2].pk])
        movement.submit_trip(trip=trip2, actor=teachers[1])
        movement.decide_trip(trip=trip2, decided_by=members["director"],
                             approved=True)
        movement.record_trip_departure(trip=trip2)   # currently out

        # -- fan the outbox out into notifications --------------------
        relay_pending(limit=500)

        self.stdout.write(
            f"  {len(students)} students, {len(STAFF)} staff, "
            f"{PassOut.objects.count()} pass-outs, {Trip.objects.count()} trips, "
            f"{AttendanceRecord.objects.count()} staff-attendance records"
        )
