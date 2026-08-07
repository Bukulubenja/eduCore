from django.urls import path

from . import views

app_name = "platform"

urlpatterns = [
    path("imports/staff", views.StaffImportView.as_view(), name="import-staff"),
    path("imports/students", views.StudentImportView.as_view(),
         name="import-students"),
    path("imports/guardians", views.GuardianImportView.as_view(),
         name="import-guardians"),
    path("imports", views.ImportHistoryView.as_view(), name="import-history"),
]
