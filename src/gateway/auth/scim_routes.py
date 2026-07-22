"""SCIM 2.0 provisioning endpoints (RFC 7643/7644).

Exposes ``/scim/v2/Users`` and ``/scim/v2/Groups`` so an IdP can drive the
joiner/mover/leaver lifecycle. Supports the subset every major IdP (Okta, Entra
ID, OneLogin) uses to reconcile: list with ``userName eq`` filter + pagination,
GET/POST/PUT/DELETE, and PATCH (notably ``active=false`` to deprovision).

Auth: protected by a bearer token equal to ``AXON_SCIM_TOKEN`` (the secret the
IdP is configured with). When the token isn't set, SCIM is disabled (503) rather
than open.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from src.gateway.models import ScimGroup, ScimUser

if TYPE_CHECKING:
    from src.gateway.auth.scim_service import ScimStore

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"


def _scim_error(status: int, detail: str, scim_type: str | None = None) -> JSONResponse:
    body: dict[str, Any] = {"schemas": [ERROR_SCHEMA], "status": str(status), "detail": detail}
    if scim_type:
        body["scimType"] = scim_type
    return JSONResponse(body, status_code=status)


def _user_to_scim(u: ScimUser, store: ScimStore) -> dict:
    return {
        "schemas": [USER_SCHEMA],
        "id": u.id,
        "externalId": u.external_id,
        "userName": u.user_name,
        "active": u.active,
        "displayName": u.display_name,
        "emails": u.emails,
        "groups": [{"value": g} for g in u.groups],
        "roles": [{"value": r} for r in store.roles_for_user(u)],
        "meta": {"resourceType": "User", "created": u.created_at.isoformat(),
                 "lastModified": u.updated_at.isoformat()},
    }


def _group_to_scim(g: ScimGroup) -> dict:
    return {
        "schemas": [GROUP_SCHEMA],
        "id": g.id,
        "externalId": g.external_id,
        "displayName": g.display_name,
        "members": [{"value": m} for m in g.members],
        "roles": [{"value": r} for r in g.roles],
        "meta": {"resourceType": "Group", "created": g.created_at.isoformat(),
                 "lastModified": g.updated_at.isoformat()},
    }


def _parse_filter_username(filt: str | None) -> str | None:
    """Parse the one filter IdPs use to reconcile: ``userName eq "value"``."""
    if not filt:
        return None
    parts = filt.split(None, 2)
    if len(parts) == 3 and parts[0].lower() == "username" and parts[1].lower() == "eq":
        return parts[2].strip().strip('"')
    return None


class ScimAPI:
    """SCIM 2.0 Users + Groups handlers over a ScimStore."""

    def __init__(self, store: ScimStore) -> None:
        self.store = store

    def _authorized(self, request: Request) -> bool:
        token = os.environ.get("AXON_SCIM_TOKEN", "").strip()
        if not token:
            return False  # SCIM disabled unless a token is configured
        auth = request.headers.get("authorization", "")
        return auth.startswith("Bearer ") and auth[7:] == token

    def _guard(self, request: Request) -> JSONResponse | None:
        if not os.environ.get("AXON_SCIM_TOKEN", "").strip():
            return _scim_error(503, "SCIM provisioning is not enabled (AXON_SCIM_TOKEN unset)")
        if not self._authorized(request):
            return _scim_error(401, "Invalid or missing SCIM bearer token")
        return None

    # -- Users ---------------------------------------------------------------

    async def list_users(self, request: Request) -> JSONResponse:
        if (g := self._guard(request)) is not None:
            return g
        user_name = _parse_filter_username(request.query_params.get("filter"))
        start = int(request.query_params.get("startIndex", "1") or "1")
        count = int(request.query_params.get("count", "100") or "100")
        page, total = self.store.list_users(user_name=user_name, start=start, count=count)
        return JSONResponse({
            "schemas": [LIST_SCHEMA],
            "totalResults": total,
            "startIndex": start,
            "itemsPerPage": len(page),
            "Resources": [_user_to_scim(u, self.store) for u in page],
        })

    async def get_user(self, request: Request) -> JSONResponse:
        if (g := self._guard(request)) is not None:
            return g
        u = self.store.get_user(request.path_params["id"])
        if u is None:
            return _scim_error(404, "User not found")
        return JSONResponse(_user_to_scim(u, self.store))

    async def create_user(self, request: Request) -> JSONResponse:
        if (g := self._guard(request)) is not None:
            return g
        from src.gateway.auth.scim_service import ScimConflictError
        body = await request.json()
        user = ScimUser(
            id="", user_name=body["userName"], active=bool(body.get("active", True)),
            external_id=body.get("externalId"), display_name=body.get("displayName", ""),
            emails=body.get("emails", []), roles=[r.get("value") for r in body.get("roles", [])],
            groups=[grp.get("value") for grp in body.get("groups", [])],
            project_id=body.get("projectId", ""),
        )
        try:
            created = await self.store.create_user(user)
        except ScimConflictError as e:
            return _scim_error(409, str(e), scim_type="uniqueness")
        except KeyError:
            return _scim_error(400, "userName is required", scim_type="invalidValue")
        return JSONResponse(_user_to_scim(created, self.store), status_code=201)

    async def replace_user(self, request: Request) -> JSONResponse:
        if (g := self._guard(request)) is not None:
            return g
        from src.gateway.auth.scim_service import ScimNotFoundError
        body = await request.json()
        user = ScimUser(
            id="", user_name=body["userName"], active=bool(body.get("active", True)),
            external_id=body.get("externalId"), display_name=body.get("displayName", ""),
            emails=body.get("emails", []), roles=[r.get("value") for r in body.get("roles", [])],
            groups=[grp.get("value") for grp in body.get("groups", [])],
            project_id=body.get("projectId", ""),
        )
        try:
            updated = await self.store.replace_user(request.path_params["id"], user)
        except ScimNotFoundError:
            return _scim_error(404, "User not found")
        return JSONResponse(_user_to_scim(updated, self.store))

    async def patch_user(self, request: Request) -> JSONResponse:
        """PATCH — primarily the deprovision toggle (active=false)."""
        if (g := self._guard(request)) is not None:
            return g
        from src.gateway.auth.scim_service import ScimNotFoundError
        user_id = request.path_params["id"]
        body = await request.json()
        active: bool | None = None
        for op in body.get("Operations", []):
            if op.get("op", "").lower() not in ("replace", "add"):
                continue
            value = op.get("value")
            path = (op.get("path") or "").lower()
            if path == "active" and value is not None:
                active = value if isinstance(value, bool) else str(value).lower() == "true"
            elif isinstance(value, dict) and "active" in value:
                active = bool(value["active"])
        if active is None:
            return _scim_error(400, "Only the 'active' attribute is patchable",
                               scim_type="invalidValue")
        try:
            updated = await self.store.set_user_active(user_id, active)
        except ScimNotFoundError:
            return _scim_error(404, "User not found")
        return JSONResponse(_user_to_scim(updated, self.store))

    async def delete_user(self, request: Request) -> JSONResponse | Any:
        if (g := self._guard(request)) is not None:
            return g
        from src.gateway.auth.scim_service import ScimNotFoundError
        try:
            await self.store.delete_user(request.path_params["id"])
        except ScimNotFoundError:
            return _scim_error(404, "User not found")
        return JSONResponse(None, status_code=204)

    # -- Groups --------------------------------------------------------------

    async def list_groups(self, request: Request) -> JSONResponse:
        if (g := self._guard(request)) is not None:
            return g
        start = int(request.query_params.get("startIndex", "1") or "1")
        count = int(request.query_params.get("count", "100") or "100")
        page, total = self.store.list_groups(start=start, count=count)
        return JSONResponse({
            "schemas": [LIST_SCHEMA], "totalResults": total, "startIndex": start,
            "itemsPerPage": len(page), "Resources": [_group_to_scim(g) for g in page],
        })

    async def get_group(self, request: Request) -> JSONResponse:
        if (g := self._guard(request)) is not None:
            return g
        grp = self.store.get_group(request.path_params["id"])
        if grp is None:
            return _scim_error(404, "Group not found")
        return JSONResponse(_group_to_scim(grp))

    async def create_group(self, request: Request) -> JSONResponse:
        if (g := self._guard(request)) is not None:
            return g
        body = await request.json()
        group = ScimGroup(
            id="", display_name=body["displayName"], external_id=body.get("externalId"),
            members=[m.get("value") for m in body.get("members", [])],
            roles=[r.get("value") for r in body.get("roles", [])],
        )
        created = await self.store.create_group(group)
        return JSONResponse(_group_to_scim(created), status_code=201)

    async def replace_group(self, request: Request) -> JSONResponse:
        if (g := self._guard(request)) is not None:
            return g
        from src.gateway.auth.scim_service import ScimNotFoundError
        body = await request.json()
        group = ScimGroup(
            id="", display_name=body["displayName"], external_id=body.get("externalId"),
            members=[m.get("value") for m in body.get("members", [])],
            roles=[r.get("value") for r in body.get("roles", [])],
        )
        try:
            updated = await self.store.replace_group(request.path_params["id"], group)
        except ScimNotFoundError:
            return _scim_error(404, "Group not found")
        return JSONResponse(_group_to_scim(updated))

    async def delete_group(self, request: Request) -> JSONResponse:
        if (g := self._guard(request)) is not None:
            return g
        from src.gateway.auth.scim_service import ScimNotFoundError
        try:
            await self.store.delete_group(request.path_params["id"])
        except ScimNotFoundError:
            return _scim_error(404, "Group not found")
        return JSONResponse(None, status_code=204)


def create_scim_routes(api: ScimAPI) -> list[Route]:
    return [
        Route("/scim/v2/Users", api.list_users, methods=["GET"]),
        Route("/scim/v2/Users", api.create_user, methods=["POST"]),
        Route("/scim/v2/Users/{id}", api.get_user, methods=["GET"]),
        Route("/scim/v2/Users/{id}", api.replace_user, methods=["PUT"]),
        Route("/scim/v2/Users/{id}", api.patch_user, methods=["PATCH"]),
        Route("/scim/v2/Users/{id}", api.delete_user, methods=["DELETE"]),
        Route("/scim/v2/Groups", api.list_groups, methods=["GET"]),
        Route("/scim/v2/Groups", api.create_group, methods=["POST"]),
        Route("/scim/v2/Groups/{id}", api.get_group, methods=["GET"]),
        Route("/scim/v2/Groups/{id}", api.replace_group, methods=["PUT"]),
        Route("/scim/v2/Groups/{id}", api.delete_group, methods=["DELETE"]),
    ]
