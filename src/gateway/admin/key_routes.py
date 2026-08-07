"""Admin API routes for API key management.

These four routes are the gateway's credential factory, which makes them the one
place where admin RBAC's resource-level granularity is not granular enough.
`AdminRBACMiddleware` authorizes on the first path segment, so `admin:projects`
grants *everything* under `/admin/projects/*` — including `POST
/admin/projects/{id}/keys`, and `admin:keys` grants `POST
/admin/keys/{key_id}/rotate`. Without the checks below, either scope is a full
privilege escalation: issue yourself a key with `scopes=['admin:*']`, or rotate
somebody else's `admin:*` key and read the replacement's raw value out of the
response. Both were reachable and confirmed; see
`tests/unit/test_admin_key_privilege_escalation.py`.

The rule enforced here is that a caller cannot mint authority it does not
already hold, and cannot operate on a credential outside its own project. That
has to live in the handlers rather than the middleware: the middleware sees a
path, while only the handler knows which scopes the *body* asked for and which
project the *target key* belongs to.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from src.gateway.middleware.admin_rbac import scope_implies
from src.gateway.models import RequestContext

if TYPE_CHECKING:
    from src.gateway.auth.api_key_service import APIKeyService

logger = logging.getLogger(__name__)


def _caller(request: Request) -> RequestContext | None:
    """The authenticated identity behind this request, if any."""
    return getattr(request.state, "context", None)


def _is_superadmin(ctx: RequestContext | None) -> bool:
    """Whether the caller holds unrestricted admin authority.

    Only these callers may grant arbitrary scopes or reach across projects. A
    holder of a narrow scope like ``admin:projects`` is deliberately not one:
    that scope reaches this route only because RBAC matches on the path's first
    segment, not because it was meant to confer key-minting power.
    """
    if ctx is None:
        return False
    return "admin" in ctx.roles or "admin:*" in ctx.scopes


def _may_grant(ctx: RequestContext | None, requested: list[str]) -> str | None:
    """Return a refusal reason if the caller cannot grant ``requested``.

    A caller may grant an admin scope only if something it holds already implies
    it — so ``admin:projects`` can hand out the narrower ``admin:projects:read``
    but not ``admin:quotas`` or ``admin:*``. Non-admin scopes (``chat`` and
    friends) are freely grantable: the concern is escalation of *admin* authority,
    not handing out ordinary gateway access.
    """
    if _is_superadmin(ctx):
        return None
    held = ctx.scopes if ctx else []
    escalating = [
        s
        for s in requested
        if s.startswith("admin:")
        and not any(scope_implies(h, s) for h in held)
    ]
    if escalating:
        return (
            "Cannot issue a key with scopes the caller does not hold: "
            + ", ".join(sorted(escalating))
        )
    return None


class KeyManagementAPI:
    """Handles CRUD operations for project-scoped API keys."""

    def __init__(self, api_key_service: APIKeyService, mode: str = "ENFORCE") -> None:
        self.api_key_service = api_key_service
        self.mode = mode

    def _forbid(self, message: str, ctx: RequestContext | None) -> JSONResponse | None:
        """Deny with 403, or in LOG_ONLY just record the attempt.

        Mirrors `AdminRBACMiddleware`: LOG_ONLY is the local-development default,
        where there is no authenticated context at all, and failing closed would
        make the key routes unusable before an operator has any credential to use
        them with.
        """
        user = ctx.user_id if ctx else "<no context>"
        if self.mode != "ENFORCE":
            logger.warning(
                "Would deny key operation for '%s' (LOG_ONLY): %s", user, message
            )
            return None
        logger.warning("Denied key operation for '%s': %s", user, message)
        return JSONResponse(
            status_code=403,
            content={"error": {"type": "authorization_error", "message": message}},
        )

    async def issue_key(self, request: Request) -> JSONResponse:
        """POST /admin/projects/{id}/keys"""
        project_id = request.path_params["id"]
        body = await request.json()

        name = body.get("name", "Unnamed key")
        scopes = body.get("scopes", ["chat:invoke"])
        created_by = body.get("created_by", "admin")
        expires_at_str = body.get("expires_at")

        ctx = _caller(request)
        if not _is_superadmin(ctx) and ctx is not None and ctx.project_id != project_id:
            denial = self._forbid(
                f"Caller scoped to project '{ctx.project_id}' cannot issue keys "
                f"for project '{project_id}'",
                ctx,
            )
            if denial is not None:
                return denial
        reason = _may_grant(ctx, list(scopes))
        if reason:
            denial = self._forbid(reason, ctx)
            if denial is not None:
                return denial

        expires_at = None
        if expires_at_str:
            expires_at = datetime.fromisoformat(expires_at_str)

        key_record, raw_key = await self.api_key_service.issue_key(
            project_id=project_id,
            name=name,
            scopes=scopes,
            created_by=created_by,
            expires_at=expires_at,
            tenant_id=ctx.tenant_id if ctx else None,
        )

        return JSONResponse(
            status_code=201,
            content={
                "key_id": key_record.key_id,
                "key": raw_key,
                "project_id": key_record.project_id,
                "name": key_record.name,
                "scopes": key_record.scopes,
                "created_at": key_record.created_at.isoformat(),
                "expires_at": key_record.expires_at.isoformat() if key_record.expires_at else None,
                "warning": "Store this key securely — it will not be shown again.",
            },
        )

    async def list_keys(self, request: Request) -> JSONResponse:
        """GET /admin/projects/{id}/keys"""
        project_id = request.path_params["id"]

        ctx = _caller(request)
        if not _is_superadmin(ctx) and ctx is not None and ctx.project_id != project_id:
            denial = self._forbid(
                f"Caller scoped to project '{ctx.project_id}' cannot list keys "
                f"for project '{project_id}'",
                ctx,
            )
            if denial is not None:
                return denial

        keys = await self.api_key_service.list_keys(
            project_id,
            ctx.tenant_id if ctx else None,
        )

        return JSONResponse(
            content=[
                {
                    "key_id": k.key_id,
                    "name": k.name,
                    "project_id": k.project_id,
                    "scopes": k.scopes,
                    "created_by": k.created_by,
                    "created_at": k.created_at.isoformat(),
                    "expires_at": k.expires_at.isoformat() if k.expires_at else None,
                    "revoked": k.revoked,
                    "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
                }
                for k in keys
            ]
        )

    async def _owns_key(self, ctx: RequestContext | None, key_id: str) -> bool:
        """Whether ``key_id`` belongs to the caller's own project.

        Asked via the public ``list_keys`` rather than a lookup by id, so this
        cannot become a way to confirm the existence of keys in other projects.
        """
        if ctx is None:
            return False
        keys = await self.api_key_service.list_keys(
            ctx.project_id,
            ctx.tenant_id,
        )
        return any(k.key_id == key_id for k in keys)

    async def revoke_key(self, request: Request) -> JSONResponse:
        """DELETE /admin/keys/{key_id}"""
        key_id = request.path_params["key_id"]

        ctx = _caller(request)
        if not _is_superadmin(ctx) and not await self._owns_key(ctx, key_id):
            denial = self._forbid(
                f"Caller cannot revoke key '{key_id}' outside its own project", ctx
            )
            if denial is not None:
                return denial

        success = await self.api_key_service.revoke_key(
            key_id,
            ctx.tenant_id if ctx else None,
        )

        if not success:
            return JSONResponse(
                status_code=404,
                content={"error": f"Key '{key_id}' not found."},
            )

        return JSONResponse(content={"status": "revoked", "key_id": key_id})

    async def rotate_key(self, request: Request) -> JSONResponse:
        """POST /admin/keys/{key_id}/rotate

        Rotation is an escalation primitive, not just a lifecycle operation: it
        returns the replacement's raw value, and ``APIKeyService.rotate_key``
        copies the *old* key's scopes onto it. So rotating a key you don't own is
        equivalent to being handed that key. An ownership check alone is not
        enough — the confirmed attack was entirely inside one project, where an
        ``admin:keys`` holder rotated a colleague's ``admin:*`` key and used the
        response. The caller must therefore also hold every admin scope the
        target carries.
        """
        key_id = request.path_params["key_id"]
        body = await request.json() if await request.body() else {}
        rotated_by = body.get("rotated_by", "admin")

        ctx = _caller(request)
        if not _is_superadmin(ctx):
            if not await self._owns_key(ctx, key_id):
                denial = self._forbid(
                    f"Caller cannot rotate key '{key_id}' outside its own project", ctx
                )
                if denial is not None:
                    return denial
            else:
                target = next(
                    (
                        k
                        for k in await self.api_key_service.list_keys(
                            ctx.project_id,
                            ctx.tenant_id,
                        )
                        if k.key_id == key_id
                    ),
                    None,
                )
                reason = _may_grant(ctx, list(target.scopes) if target else [])
                if reason:
                    denial = self._forbid(
                        f"Cannot rotate key '{key_id}': it carries admin scopes the "
                        "caller does not hold, and rotation would return its "
                        "replacement's raw value",
                        ctx,
                    )
                    if denial is not None:
                        return denial

        result = await self.api_key_service.rotate_key(
            key_id,
            rotated_by,
            ctx.tenant_id if ctx else None,
        )
        if result is None:
            return JSONResponse(
                status_code=404,
                content={"error": f"Key '{key_id}' not found."},
            )

        new_key, raw_key = result
        return JSONResponse(
            status_code=201,
            content={
                "old_key_id": key_id,
                "old_key_status": "revoked",
                "new_key_id": new_key.key_id,
                "key": raw_key,
                "project_id": new_key.project_id,
                "name": new_key.name,
                "scopes": new_key.scopes,
                "warning": "Store this key securely — it will not be shown again.",
            },
        )


def create_key_routes(key_api: KeyManagementAPI) -> list[Route]:
    """Create Starlette routes for API key management."""
    return [
        Route("/admin/projects/{id}/keys", key_api.issue_key, methods=["POST"]),
        Route("/admin/projects/{id}/keys", key_api.list_keys, methods=["GET"]),
        Route("/admin/keys/{key_id}", key_api.revoke_key, methods=["DELETE"]),
        Route("/admin/keys/{key_id}/rotate", key_api.rotate_key, methods=["POST"]),
    ]
