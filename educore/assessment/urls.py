from django.urls import path

from . import views

app_name = "assessment"

urlpatterns = [
    path("assessments", views.AssessmentListView.as_view(), name="list"),
    path("assessments/<uuid:assessment_id>/submit", views.SubmitView.as_view(),
         name="submit"),
    path("assessments/<uuid:assessment_id>/approve", views.ApproveView.as_view(),
         name="approve"),
    path("assessments/<uuid:assessment_id>/return", views.ReturnView.as_view(),
         name="return"),
    path("assessments/<uuid:assessment_id>/lock", views.LockView.as_view(),
         name="lock"),
    path("assessments/<uuid:assessment_id>/begin-scoring",
         views.BeginScoringView.as_view(), name="begin-scoring"),
    path("assessments/<uuid:assessment_id>/scores", views.ScoresView.as_view(),
         name="scores"),
    path("assessments/<uuid:assessment_id>/submit-marks",
         views.SubmitMarksView.as_view(), name="submit-marks"),
    path("assessments/<uuid:assessment_id>/moderate", views.ModerateView.as_view(),
         name="moderate"),
    path("assessments/<uuid:assessment_id>/release", views.ReleaseView.as_view(),
         name="release"),
    path("report-cards", views.ReportCardListView.as_view(), name="report-cards"),
    path("report-cards/generate", views.GenerateReportCardsView.as_view(),
         name="generate-report-cards"),
    path("me/results", views.MyResultsView.as_view(), name="my-results"),
    path("students", views.StudentLookupView.as_view(), name="student-lookup"),
]
