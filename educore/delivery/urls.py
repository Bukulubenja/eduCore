from django.urls import path

from . import views

app_name = "delivery"

urlpatterns = [
    path("rooms/<uuid:room_id>/qr-token", views.RoomTokenView.as_view(),
         name="room-token"),
    path("lesson-sessions", views.OpenSessionView.as_view(), name="open-session"),
    path("lesson-sessions/<uuid:session_id>/close", views.CloseSessionView.as_view(),
         name="close-session"),
    path("me/timetable", views.MyDayView.as_view(), name="my-day"),
    path("coverage", views.CoverageView.as_view(), name="coverage"),
]
