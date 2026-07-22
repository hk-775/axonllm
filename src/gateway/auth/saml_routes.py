"""SAML 2.0 SSO endpoints: login redirect, ACS, and SP metadata."""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

if TYPE_CHECKING:
    from src.gateway.auth.saml_service import SamlService


class SamlAPI:
    """HTTP surface for SP-initiated SAML SSO."""

    def __init__(self, service: SamlService) -> None:
        self.service = service

    def _disabled(self) -> JSONResponse | None:
        if not self.service.enabled:
            return JSONResponse(
                status_code=503,
                content={"error": {"type": "sso_not_configured",
                                   "message": "SAML SSO is not configured."}})
        return None

    async def login(self, request: Request) -> Response:
        """GET /saml/login[?relay_state=] → 302 redirect to the IdP."""
        if (d := self._disabled()) is not None:
            return d
        relay = request.query_params.get("relay_state", "")
        return RedirectResponse(self.service.build_authn_request(relay), status_code=302)

    async def acs(self, request: Request) -> Response:
        """POST /saml/acs — Assertion Consumer Service (IdP POST binding)."""
        if (d := self._disabled()) is not None:
            return d
        from src.gateway.auth.saml_service import SamlError

        form = await request.form()
        saml_response = form.get("SAMLResponse")
        relay_state = form.get("RelayState", "")
        if not saml_response:
            return JSONResponse(
                status_code=400,
                content={"error": {"type": "invalid_request",
                                   "message": "Missing SAMLResponse"}})
        try:
            context = self.service.handle_acs(str(saml_response))
        except SamlError as e:
            return JSONResponse(
                status_code=401,
                content={"error": {"type": "authentication_error", "message": str(e)}})

        # Successful assertion → surface the resolved identity. A production SP
        # would mint a session here; we return the mapped context so the caller
        # (and tests) can confirm the mapping.
        return JSONResponse({
            "authenticated": True,
            "user_id": context.user_id,
            "email": context.email,
            "roles": context.roles,
            "project_id": context.project_id,
            "relay_state": relay_state,
        })

    async def metadata(self, request: Request) -> Response:
        """GET /saml/metadata — SP metadata XML for IdP configuration."""
        if (d := self._disabled()) is not None:
            return d
        return Response(self.service.sp_metadata(), media_type="application/xml")


def create_saml_routes(api: SamlAPI) -> list[Route]:
    return [
        Route("/saml/login", api.login, methods=["GET"]),
        Route("/saml/acs", api.acs, methods=["POST"]),
        Route("/saml/metadata", api.metadata, methods=["GET"]),
    ]
