"""Admin RBAC middleware — restricts /admin/* endpoints to authorized users.

Checks that the authenticated context has either:
- The 'admin' role, OR
- A scope matching 'admin:*' or the specific admin action

Scopes name a resource and, optionally, an access level:

    admin:*                 everything
    admin:*:read            read every resource, write none
    admin:quotas            read and write /admin/quotas/*   (no suffix = both)
    admin:quotas:read       read /admin/quotas/* only
    admin:quotas:write      read and write /admin/quotas/*

A bare ``admin:<resource>`` grants both, so scopes issued before ``:read`` existed
keep the access they had — the suffix narrows, it never silently widens or
downgrades. ``:write`` implies read: an operator who can reset a quota can
already see the value they are resetting, and splitting them would only produce
keys that mutate blind.

**Read and write are classified by effect, not by HTTP method.** Four admin
POSTs are named like inspections but mutate state, and are treated as writes:
``/admin/quotas/simulate`` consumes the project's rate-limit budget,
``/admin/regions/health/check`` updates spoke status (and so changes routing),
``/admin/regions/route`` exercises the live router, and
``/admin/webhooks/{name}/test`` sends a real HTTP request to an external
endpoint. ``POST /admin/pii/preview`` is the one POST that genuinely persists
nothing, so a ``:read`` scope reaches it. Classifying those four by method would
hand a nominally read-only credential the ability to exhaust a rate limit or ping
an outside host.

In LOG_ONLY mode (default), denials are logged but not enforced.
In ENFORCE mode, returns 403.
"""

from __future__ import annotations

import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

READ_ONLY_WRITE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
"""Methods that are reads by default, before the by-effect overrides below."""

WRITE_EFFECT_PATHS = frozenset({
    "/admin/quotas/simulate",
    "/admin/regions/health/check",
    "/admin/regions/route",
})
"""Non-GET paths that a ``:read`` scope must not reach, despite reading like
inspections. Each mutates: consumes rate-limit budget, updates spoke status,
or exercises the live router. ``/admin/webhooks/{name}/test`` is matched
separately since it carries a path parameter."""

READ_EFFECT_PATHS = frozenset({
    "/admin/pii/preview",
})
"""Non-GET paths that ``:read`` may reach because they persist nothing. Kept as
an explicit allowlist rather than a naming convention — ``preview``, ``test``
and ``simulate`` are used by both kinds of route here, so the name is not
evidence."""


def classify_access(method: str, path: str) -> str:
    """Return ``"read"`` or ``"write"`` for a request, by effect.

    Method is the default signal; the two path sets above override it where the
    method lies about what the handler does.
    """
    if path in WRITE_EFFECT_PATHS:
        return "write"
    if path.startswith("/admin/webhooks/") and path.endswith("/test"):
        return "write"  # fires a real HTTP request at an external endpoint
    if path in READ_EFFECT_PATHS:
        return "read"
    return "read" if method.upper() in READ_ONLY_WRITE_METHODS else "write"


def parse_admin_scope(scope: str) -> tuple[str, str]:
    """Split ``admin:<resource>[:<access>]`` into ``(resource, access)``.

    A missing suffix yields ``"write"``, which is what keeps pre-existing
    ``admin:quotas`` keys working exactly as they did. An unrecognised suffix is
    treated as part of the resource name rather than as an access level, so a typo
    like ``admin:quotas:raed`` fails closed (it matches no resource) instead of
    quietly granting write.
    """
    body = scope[len("admin:"):] if scope.startswith("admin:") else scope
    resource, sep, suffix = body.rpartition(":")
    if sep and suffix in ("read", "write"):
        return resource, suffix
    return body, "write"


def scope_implies(held: str, requested: str) -> bool:
    """Whether holding ``held`` confers everything ``requested`` grants.

    Used by the key-issuance guard so a caller can delegate a *narrower* slice of
    what it holds: ``admin:projects`` may grant ``admin:projects:read``, and
    ``admin:*`` may grant anything. Without this, exact string comparison would
    refuse to hand out a subset of one's own authority — the one delegation that
    is unambiguously safe.
    """
    if held == requested:
        return True
    if not held.startswith("admin:") or not requested.startswith("admin:"):
        return False
    held_resource, held_access = parse_admin_scope(held)
    req_resource, req_access = parse_admin_scope(requested)
    if held_resource != "*" and held_resource != req_resource:
        return False
    return held_access == "write" or held_access == req_access


class AdminRBACMiddleware(BaseHTTPMiddleware):
    """Enforces role/scope requirements on admin endpoints."""

    def __init__(self, app, mode: str = "ENFORCE"):
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

        access = classify_access(request.method, path)

        if self._is_authorized(ctx, path, access):
            return await call_next(request)

        if self.mode == "ENFORCE":
            resource = self._extract_resource(path)
            return self._deny(
                f"User '{ctx.user_id}' lacks {access} access to '{resource}'. "
                f"Required: 'admin' role, 'admin:*', or "
                f"'admin:{resource}:{access}' scope."
            )

        logger.warning(
            "Admin RBAC DENY (LOG_ONLY) user=%s path=%s access=%s roles=%s scopes=%s",
            ctx.user_id, path, access, ctx.roles, ctx.scopes,
        )
        return await call_next(request)

    def _is_authorized(self, ctx, path: str = "", access: str = "write") -> bool:
        """Whether ``ctx`` may perform ``access`` on ``path``.

        ``access`` defaults to ``"write"`` so that a caller which forgets to pass
        it gets the stricter check rather than silently authorizing mutations.
        """
        if "admin" in ctx.roles:
            return True
        resource = self._extract_resource(path)
        for scope in ctx.scopes:
            if not scope.startswith("admin:"):
                continue
            scope_resource, scope_access = parse_admin_scope(scope)
            if scope_resource not in ("*", resource):
                continue
            if scope_access == "write" or scope_access == access:
                return True
        return False

    def _extract_resource(self, path: str) -> str:
        """Extract the admin resource from path, e.g. /admin/quotas/proj:x -> quotas."""
        parts = path.strip("/").split("/")
        if len(parts) >= 2:
            return parts[1]
        return ""

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
