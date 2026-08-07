from django.urls import path

from . import views

app_name = "students"

urlpatterns = [
    path("lesson-instances/<uuid:lesson_id>/register", views.RegisterView.as_view(),
         name="register"),
    path("gate-events", views.GateBatchView.as_view(), name="gate-events"),
    path("students/<uuid:student_id>/attendance",
         views.StudentAttendanceView.as_view(), name="student-attendance"),
]
