"""Data-plane authorization for canonical tenant principals."""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from src.gateway.auth.authorization import Action, ResourceRef, authorize


DATA_PLANE_ACTIONS: dict[tuple[str, str], Action] = {
    ("GET", "/api/models"): Action.MODEL_LIST,
    ("GET", "/v1/models"): Action.MODEL_LIST,
    ("POST", "/api/chat"): Action.INFERENCE_INVOKE,
    ("POST", "/api/chat/stream"): Action.INFERENCE_INVOKE,
    ("POST", "/v1/chat/completions"): Action.INFERENCE_INVOKE,
}

_CANONICAL_API_PREFIXES = ("/api/", "/v1/")


class TenantAuthorizationMiddleware(BaseHTTPMiddleware):
    """Enforce the baseline RBAC floor after canonical authentication."""

    async def dispatch(self, request: Request, call_next) -> Response:
        # Legacy migration mode has no Principal. AuthMiddleware is responsible
        # for requiring one once AXON_REQUIRE_CANONICAL_IDENTITY is enabled.
        principal = getattr(request.state, "principal", None)
        if principal is None:
            return await call_next(request)

        method = request.method.upper()
        if method == "HEAD":
            method = "GET"
        action = DATA_PLANE_ACTIONS.get((method, request.url.path))
        if action is None:
            if request.url.path.startswith(_CANONICAL_API_PREFIXES):
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": {
                            "type": "authorization_error",
                            "message": (
                                "No canonical authorization action is mapped "
                                "for this endpoint."
                            ),
                            "code": "canonical_action_required",
                        }
                    },
                )
            return await call_next(request)

        context = request.state.context
        project_id = context.project_id or None
        if project_id is None:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "type": "invalid_request",
                        "message": (
                            "An explicit project context is required for "
                            "canonical data-plane requests."
                        ),
                        "code": "project_context_required",
                    }
                },
            )
        resource = ResourceRef(
            resource_type="project",
            resource_id=project_id,
            tenant_id=principal.tenant_id,
            project_id=project_id,
        )
        decision = authorize(principal, action, resource)
        if decision.allowed:
            request.state.authorization_decision = decision
            return await call_next(request)

        return JSONResponse(
            status_code=decision.status_code,
            content={
                "error": {
                    "type": "authorization_error",
                    "message": "The principal is not authorized for this action.",
                    "code": decision.reason,
                }
            },
        )
