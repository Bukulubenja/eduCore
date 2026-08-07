from django.urls import path

from . import views

app_name = "presence"

urlpatterns = [
    path("attendance/check-in", views.CheckInView.as_view(), name="check-in"),
    path("attendance/check-out", views.CheckOutView.as_view(), name="check-out"),
    path("attendance/qr-token", views.QrTokenView.as_view(), name="qr-token"),
    path("attendance/records", views.AttendanceRecordListView.as_view(),
         name="records"),
    path("attendance/records/<uuid:record_id>/appeal", views.AppealView.as_view(),
         name="appeal"),
    path("attendance/reviews", views.ReviewQueueView.as_view(), name="reviews"),
    path("attendance/reviews/<uuid:exception_id>/decide",
         views.DecideAppealView.as_view(), name="decide"),
    path("attendance/health", views.HealthMetricsView.as_view(), name="health"),
    path("me/attendance", views.MyAttendanceView.as_view(), name="my-attendance"),
    path("sync", views.SyncView.as_view(), name="sync"),
]
