from django.urls import path

from . import views

app_name = "movement"

urlpatterns = [
    # Pass-outs
    path("pass-outs", views.PassOutListView.as_view(), name="pass-outs"),
    path("pass-outs/<uuid:pass_out_id>", views.PassOutDetailView.as_view(),
         name="pass-out-detail"),
    path("pass-outs/<uuid:pass_out_id>/decision",
         views.PassOutDecisionView.as_view(), name="pass-out-decision"),
    path("pass-outs/<uuid:pass_out_id>/departure",
         views.PassOutDepartureView.as_view(), name="pass-out-departure"),
    path("pass-outs/<uuid:pass_out_id>/return",
         views.PassOutReturnView.as_view(), name="pass-out-return"),
    path("pass-outs/<uuid:pass_out_id>/cancel",
         views.PassOutCancelView.as_view(), name="pass-out-cancel"),

    # Trips
    path("trips", views.TripListView.as_view(), name="trips"),
    path("trips/<uuid:trip_id>", views.TripDetailView.as_view(),
         name="trip-detail"),
    path("trips/<uuid:trip_id>/roster", views.TripRosterView.as_view(),
         name="trip-roster"),
    path("trips/<uuid:trip_id>/submit", views.TripSubmitView.as_view(),
         name="trip-submit"),
    path("trips/<uuid:trip_id>/decision", views.TripDecisionView.as_view(),
         name="trip-decision"),
    path("trips/<uuid:trip_id>/departure", views.TripDepartureView.as_view(),
         name="trip-departure"),
    path("trips/<uuid:trip_id>/return", views.TripReturnView.as_view(),
         name="trip-return"),
    path("trips/<uuid:trip_id>/cancel", views.TripCancelView.as_view(),
         name="trip-cancel"),

    # Movement read models
    path("movement/eligible-students", views.EligibleStudentsView.as_view(),
         name="eligible-students"),
    path("movement/off-campus", views.StudentsOffCampusView.as_view(),
         name="off-campus"),
]
