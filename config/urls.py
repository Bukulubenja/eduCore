from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from educore.core import views as core_views

api_v1 = [
    path("auth/token", core_views.TokenObtainView.as_view(), name="auth-token"),
    path("auth/refresh", core_views.TokenRefreshView.as_view(), name="auth-refresh"),
    path("auth/switch-school", core_views.SwitchSchoolView.as_view(),
         name="auth-switch-school"),
    path("auth/mfa/enrol", core_views.MfaEnrolView.as_view(), name="mfa-enrol"),
    path("auth/mfa/confirm", core_views.MfaConfirmView.as_view(),
         name="mfa-confirm"),
    path("auth/step-up", core_views.StepUpView.as_view(), name="step-up"),
    path("me", core_views.MeView.as_view(), name="me"),
    path("", include("educore.presence.urls")),
    path("", include("educore.delivery.urls")),
    path("", include("educore.students.urls")),
    path("", include("educore.assessment.urls")),
    path("", include("educore.comms.urls")),
    path("", include("educore.insights.urls")),
    path("", include("educore.platform.urls")),
]

urlpatterns = [
    # Operator portal. Doc 06 requires this on a separate hostname behind an
    # IP allowlist and hardware MFA before it is exposed anywhere real.
    path("operator/", admin.site.urls),
    path("api/v1/", include((api_v1, "v1"))),
    path("api/v1/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "api/v1/docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
]
