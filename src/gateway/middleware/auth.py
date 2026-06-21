"""Authentication and authorization middleware for the LLM-Router.

Validates JWT tokens via AgentCore Identity and evaluates Cedar policies
via AgentCore Policy. Extracts JWT claims into RequestContext on request.state.
"""

import logging
from typing import Protocol

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from src.gateway.models import RequestContext

logger = logging.getLogger(__name__)


class IdentityService(Protocol):
    """Interface for JWT token validation (AgentCore Identity)."""

    async def validate_token(self, token: str) -> dict | None:
        """Validate a JWT token and return claims dict, or None if invalid."""
        ...


class PolicyService(Protocol):
    """Interface for Cedar policy evaluation (AgentCore Policy)."""

    async def evaluate(self, context: RequestContext, action: str, resource: str) -> str:
        """Evaluate a Cedar policy. Returns 'ALLOW' or 'DENY'."""
        ...


class AuthMiddleware(BaseHTTPMiddleware):
    """Starlette middleware that validates JWT via AgentCore Identity
    and extracts claims into RequestContext.

    Supports ENFORCE and LOG_ONLY modes for Cedar policy evaluation.
    """

    def __init__(
        self,
        app,
        identity_service: IdentityService,
        policy_service: PolicyService,
        mode: str = "ENFORCE",
    ):
        super().__init__(app)
        self.identity_service = identity_service
        self.policy_service = policy_service
        self.mode = mode

    async def dispatch(self, request: Request, call_next) -> Response:
        """
        1. Extract Bearer token from Authorization header
        2. Validate token via AgentCore Identity
        3. Extract JWT_Claims (sub, project_id, roles, scopes)
        4. Attach RequestContext to request.state
        5. Evaluate Cedar policy via AgentCore Policy
        6. Return 401 if token invalid, 403 if policy denies
        """
        # 1. Extract Bearer token
        auth_header = request.headers.get("authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={
                    "error": {
                        "type": "authentication_error",
                        "message": "Missing or malformed Authorization header. Expected 'Bearer <token>'.",
                    }
                },
            )

        token = auth_header[len("Bearer "):]

        # 2. Validate token via identity service
        claims = await self.identity_service.validate_token(token)
        if claims is None:
            return JSONResponse(
                status_code=401,
                content={
                    "error": {
                        "type": "authentication_error",
                        "message": "Invalid or expired token.",
                    }
                },
            )

        # 3. Extract JWT claims into RequestContext
        context = RequestContext(
            user_id=claims.get("sub", ""),
            project_id=claims.get("project_id", ""),
            roles=claims.get("roles", []),
            scopes=claims.get("scopes", []),
        )

        # 4. Attach to request.state
        request.state.context = context

        # 5. Evaluate Cedar policy
        action = request.method.lower()
        resource = request.url.path
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
                # LOG_ONLY mode — log the denial but allow through
                logger.warning(
                    "Policy DENY (LOG_ONLY mode) for user=%s project=%s action=%s resource=%s",
                    context.user_id,
                    context.project_id,
                    action,
                    resource,
                )

        # 6. Continue to the next middleware / route handler
        return await call_next(request)
