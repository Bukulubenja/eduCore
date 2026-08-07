from django.urls import path

from . import views

app_name = "insights"

urlpatterns = [
    path("insights/today", views.TodayView.as_view(), name="today"),
    path("insights/punctuality", views.PunctualityView.as_view(),
         name="punctuality"),
    path("insights/workload", views.WorkloadView.as_view(), name="workload"),
    path("insights/at-risk-students", views.AtRiskStudentsView.as_view(),
         name="at-risk"),
    path("insights/coverage", views.CoverageOverviewView.as_view(),
         name="coverage-overview"),
]
