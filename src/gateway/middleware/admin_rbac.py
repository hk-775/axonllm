"""Admin RBAC middleware — restricts /admin/* endpoints to authorized users.

Checks that the authenticated context has either:
- The 'admin' role, OR
- A scope matching 'admin:*' or the specific admin action

In LOG_ONLY mode (default), denials are logged but not enforced.
In ENFORCE mode, returns 403.
"""

from __future__ import annotations

import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)


class AdminRBACMiddleware(BaseHTTPMiddleware):
    """Enforces role/scope requirements on admin endpoints."""

    def __init__(self, app, mode: str = "LOG_ONLY"):
        super().__init__(app)
        self.mode = mode

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        if not path.startswith("/admin/"):
            return await call_next(request)

        # Static assets and dashboard page are public
        if path.startswith("/admin/static") or path == "/admin/dashboard":
            return await call_next(request)

        ctx = getattr(request.state, "context", None)
        if ctx is None:
            if self.mode == "ENFORCE":
                return self._deny("No authentication context")
            return await call_next(request)

        if self._is_authorized(ctx):
            return await call_next(request)

        if self.mode == "ENFORCE":
            return self._deny(
                f"User '{ctx.user_id}' lacks admin access. "
                f"Required: 'admin' role or 'admin:*' scope."
            )

        logger.warning(
            "Admin RBAC DENY (LOG_ONLY) user=%s path=%s roles=%s scopes=%s",
            ctx.user_id, path, ctx.roles, ctx.scopes,
        )
        return await call_next(request)

    def _is_authorized(self, ctx) -> bool:
        if "admin" in ctx.roles:
            return True
        for scope in ctx.scopes:
            if scope == "admin:*" or scope.startswith("admin:"):
                return True
        return False

    def _deny(self, message: str) -> JSONResponse:
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "type": "authorization_error",
                    "message": message,
                    "code": "admin_access_denied",
                }
            },
        )
