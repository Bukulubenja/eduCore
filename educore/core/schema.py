"""OpenAPI extensions.

Without this, drf-spectacular cannot describe our authentication class, and
the published schema omits the security scheme entirely -- so every client
generated from it is built unable to authenticate (ADR-0006: the schema is
the contract, and the Flutter SDK is generated from it).
"""

from drf_spectacular.extensions import OpenApiAuthenticationExtension


class BearerTokenScheme(OpenApiAuthenticationExtension):
    target_class = "educore.core.authentication.BearerTokenAuthentication"
    name = "bearerAuth"

    def get_security_definition(self, auto_schema):
        return {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": (
                "Access token from POST /api/v1/auth/token. Carries the "
                "Membership claim; the tenant is derived from it and can "
                "never be supplied by the client."
            ),
        }
