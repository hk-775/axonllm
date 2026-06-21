"""Multi-strategy authentication middleware for AxonLLM.

Priority chain:
1. X-Amzn-Oidc-Data header (ALB OIDC JWT)
2. Authorization: Bearer <token> (OIDC JWT or API key if prefixed axon_)
3. X-Api-Key header
4. Anonymous -> 401
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from src.gateway.models import AuthMethod, RequestContext

if TYPE_CHECKING:
    from src.gateway.auth.api_key_service import APIKeyService
    from src.gateway.auth.oidc_service import OIDCService

logger = logging.getLogger(__name__)

PUBLIC_PATHS = frozenset({
    "/health",
    "/admin/dashboard",
    "/chat",
    "/playground",
    "/routing",
})


class PolicyService(Protocol):
    """Interface for policy evaluation."""

    async def evaluate(self, context: RequestContext, action: str, resource: str) -> str:
        ...


class AuthMiddleware(BaseHTTPMiddleware):
    """Authenticates requests via OIDC JWT, API key, or rejects as anonymous."""

    def __init__(
        self,
        app,
        oidc_service: OIDCService | None = None,
        api_key_service: APIKeyService | None = None,
        policy_service: PolicyService | None = None,
        mode: str = "LOG_ONLY",
        public_paths: frozenset[str] | None = None,
    ):
        super().__init__(app)
        self.oidc_service = oidc_service
        self.api_key_service = api_key_service
        self.policy_service = policy_service
        self.mode = mode
        self.public_paths = public_paths or PUBLIC_PATHS

    async def dispatch(self, request: Request, call_next) -> Response:
        # Skip auth for public paths and static assets
        path = request.url.path
        if path in self.public_paths or path.startswith("/admin/static") or path.startswith("/chat/static"):
            request.state.context = RequestContext(
                user_id="anonymous",
                project_id="",
                roles=[],
                scopes=[],
                auth_method=AuthMethod.ANONYMOUS,
            )
            return await call_next(request)

        context = None

        # 1. ALB OIDC header
        if self.oidc_service:
            alb_token = request.headers.get("x-amzn-oidc-data")
            if alb_token:
                context = await self.oidc_service.validate_alb_jwt(alb_token)

        # 2. Authorization: Bearer <token>
        if context is None:
            auth_header = request.headers.get("authorization")
            if auth_header and auth_header.startswith("Bearer "):
                token = auth_header[7:]
                if token.startswith("axon_"):
                    context = await self._authenticate_api_key(token)
                elif self.oidc_service:
                    context = await self.oidc_service.validate_oidc_jwt(token)

        # 3. X-Api-Key header
        if context is None:
            api_key_header = request.headers.get("x-api-key")
            if api_key_header:
                context = await self._authenticate_api_key(api_key_header)

        # 4. No credentials — reject (or allow in LOG_ONLY mode)
        if context is None:
            if self.mode == "ENFORCE":
                return JSONResponse(
                    status_code=401,
                    content={
                        "error": {
                            "type": "authentication_error",
                            "message": "Missing or invalid credentials. Provide a Bearer token or X-Api-Key header.",
                        }
                    },
                )
            else:
                context = RequestContext(
                    user_id="anonymous",
                    project_id="",
                    roles=[],
                    scopes=[],
                    auth_method=AuthMethod.ANONYMOUS,
                )

        request.state.context = context

        # Policy evaluation
        if self.policy_service:
            action = request.method.lower()
            resource = path
            decision = await self.policy_service.evaluate(context, action, resource)

            if decision == "DENY":
                if self.mode == "ENFORCE":
                    return JSONResponse(
                        status_code=403,
                        content={
                            "error": {
                                "type": "authorization_error",
                                "message": "Access denied by policy.",
                            }
                        },
                    )
                else:
                    logger.warning(
                        "Policy DENY (LOG_ONLY) user=%s project=%s action=%s resource=%s",
                        context.user_id,
                        context.project_id,
                        action,
                        resource,
                    )

        return await call_next(request)

    async def _authenticate_api_key(self, raw_key: str) -> RequestContext | None:
        """Validate API key and return context."""
        if not self.api_key_service:
            return None

        key_record = await self.api_key_service.validate_key(raw_key)
        if key_record is None:
            return None

        return RequestContext(
            user_id=f"apikey:{key_record.key_id}",
            project_id=key_record.project_id,
            roles=["service"],
            scopes=key_record.scopes,
            auth_method=AuthMethod.API_KEY,
            api_key_id=key_record.key_id,
        )
