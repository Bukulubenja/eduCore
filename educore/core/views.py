"""Authentication and identity endpoints (doc 05)."""

from __future__ import annotations

from django.contrib.auth import authenticate
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import mfa, tokens
from .models import Membership
from .tenancy import TenantContext


class ProblemError(serializers.ValidationError):
    """ValidationError carrying an RFC 9457 `type` slug."""

    def __init__(self, detail, problem_type: str, code: int = 400):
        super().__init__(detail)
        self.problem_type = problem_type
        self.status_code = code


class TokenRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)
    membership_id = serializers.UUIDField(required=False)


class TokenObtainView(APIView):
    authentication_classes: list = []
    permission_classes = [AllowAny]

    @extend_schema(request=TokenRequestSerializer, responses=dict)
    def post(self, request):
        payload = TokenRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data

        user = authenticate(request, username=data["email"],
                            password=data["password"])
        if user is None or not user.is_active:
            # One message for both cases: distinguishing them tells an
            # attacker which addresses are registered.
            raise ProblemError("Email or password is incorrect.",
                               "invalid-credentials", status.HTTP_401_UNAUTHORIZED)

        memberships = list(
            Membership.all_tenants.filter(user=user,
                                          status=Membership.Status.ACTIVE)
            .exclude(school__status__in=["suspended", "closed"])
            .select_related("school")
        )
        if not memberships:
            raise ProblemError("This account has no active school membership.",
                               "no-active-membership", status.HTTP_403_FORBIDDEN)

        chosen = None
        if data.get("membership_id"):
            chosen = next((m for m in memberships
                           if str(m.pk) == str(data["membership_id"])), None)
            if chosen is None:
                raise ProblemError("Not a membership you hold.",
                                   "membership-not-found", status.HTTP_404_NOT_FOUND)
        elif len(memberships) == 1:
            chosen = memberships[0]

        if chosen is None:
            # Ask rather than guess. Picking for them is how a teacher ends up
            # marking the wrong school's register.
            return Response(
                {
                    "choose_membership": True,
                    "memberships": [
                        {"id": str(m.pk), "school": m.school.name,
                         "school_id": str(m.school_id)}
                        for m in memberships
                    ],
                },
                status=status.HTTP_300_MULTIPLE_CHOICES,
            )

        user.last_login_at = timezone.now()
        user.save(update_fields=["last_login_at"])
        return Response(_token_response(chosen))


class RefreshSerializer(serializers.Serializer):
    refresh_token = serializers.CharField()


class TokenRefreshView(APIView):
    authentication_classes: list = []
    permission_classes = [AllowAny]

    @extend_schema(request=RefreshSerializer, responses=dict)
    def post(self, request):
        payload = RefreshSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        try:
            membership, new_refresh = tokens.rotate_refresh(
                payload.validated_data["refresh_token"]
            )
        except tokens.RefreshReuseDetectedError as exc:
            raise ProblemError(str(exc), "refresh-token-replayed",
                               status.HTTP_401_UNAUTHORIZED) from exc
        except tokens.TokenError as exc:
            raise ProblemError(str(exc), "invalid-refresh-token",
                               status.HTTP_401_UNAUTHORIZED) from exc

        access, expires_in = tokens.issue_access(
            membership, role_codes=_role_codes(membership)
        )
        return Response({
            "access_token": access,
            "refresh_token": new_refresh,
            "token_type": "Bearer",
            "expires_in": expires_in,
        })


class SwitchSchoolSerializer(serializers.Serializer):
    membership_id = serializers.UUIDField()


class SwitchSchoolView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=SwitchSchoolSerializer, responses=dict)
    def post(self, request):
        payload = SwitchSchoolSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        membership = Membership.all_tenants.filter(
            pk=payload.validated_data["membership_id"],
            user=request.user,
            status=Membership.Status.ACTIVE,
        ).first()
        if membership is None:
            raise ProblemError("Not a membership you hold.",
                               "membership-not-found", status.HTTP_404_NOT_FOUND)

        return Response(_token_response(membership))


class MfaEnrolView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=None, responses=dict)
    def post(self, request):
        details = mfa.provision(request.user)
        # The secret is returned exactly once. Re-provisioning is also the
        # recovery path when someone loses their phone.
        return Response({"secret": details["secret"],
                         "otpauth_uri": details["otpauth_uri"]})


class MfaCodeSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=10)


class MfaConfirmView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=MfaCodeSerializer, responses=dict)
    def post(self, request):
        payload = MfaCodeSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            mfa.confirm(request.user, payload.validated_data["code"])
        except mfa.MfaError as exc:
            raise ProblemError(str(exc), "mfa-code-invalid",
                               status.HTTP_400_BAD_REQUEST) from exc
        return Response({"mfa_enabled": True})


class StepUpView(APIView):
    """Exchange a current authenticator code for a single-use grant.

    Separate from login because being logged in is not evidence of presence: a
    session left open in a staff room must not be able to release a term's
    results.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(request=MfaCodeSerializer, responses=dict)
    def post(self, request):
        payload = MfaCodeSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        membership = getattr(request, "membership", None)
        if membership is None:
            raise ProblemError("No active school membership for this session.",
                               "no-active-membership", status.HTTP_403_FORBIDDEN)

        try:
            grant, token = mfa.grant_step_up(membership,
                                             payload.validated_data["code"])
        except mfa.MfaNotEnrolledError as exc:
            raise ProblemError(str(exc), "mfa-not-enrolled",
                               status.HTTP_403_FORBIDDEN) from exc
        except mfa.MfaError as exc:
            raise ProblemError(str(exc), "mfa-code-invalid",
                               status.HTTP_400_BAD_REQUEST) from exc

        return Response({"step_up_token": token, "expires_at": grant.expires_at})


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=dict)
    def get(self, request):
        memberships = Membership.all_tenants.filter(
            user=request.user, status=Membership.Status.ACTIVE
        ).select_related("school")

        active = getattr(request, "membership", None)
        return Response({
            "id": str(request.user.pk),
            "full_name": request.user.full_name,
            "email": request.user.email,
            "mfa_enabled": request.user.mfa_enabled,
            "active_membership_id": str(active.pk) if active else None,
            "active_school_id": str(TenantContext.get()) if TenantContext.get() else None,
            "memberships": [
                {"id": str(m.pk), "school": m.school.name,
                 "school_id": str(m.school_id), "roles": _role_codes(m)}
                for m in memberships
            ],
        })


def _role_codes(membership) -> list[str]:
    today = timezone.localdate()
    with TenantContext.scope(membership.school_id):
        return sorted({
            ra.role.code
            for ra in membership.role_assignments.select_related("role")
            if ra.is_valid_on(today)
        })


def _token_response(membership) -> dict:
    access, expires_in = tokens.issue_access(
        membership, role_codes=_role_codes(membership)
    )
    raw_refresh, _token = tokens.issue_refresh(membership)
    return {
        "access_token": access,
        "refresh_token": raw_refresh,
        "token_type": "Bearer",
        "expires_in": expires_in,
        "membership_id": str(membership.pk),
        "school_id": str(membership.school_id),
    }
