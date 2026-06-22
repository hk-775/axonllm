"""Admin API routes for API key management."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from src.gateway.auth.api_key_service import APIKeyService


class KeyManagementAPI:
    """Handles CRUD operations for project-scoped API keys."""

    def __init__(self, api_key_service: APIKeyService) -> None:
        self.api_key_service = api_key_service

    async def issue_key(self, request: Request) -> JSONResponse:
        """POST /admin/projects/{id}/keys"""
        project_id = request.path_params["id"]
        body = await request.json()

        name = body.get("name", "Unnamed key")
        scopes = body.get("scopes", ["chat:invoke"])
        created_by = body.get("created_by", "admin")
        expires_at_str = body.get("expires_at")

        expires_at = None
        if expires_at_str:
            expires_at = datetime.fromisoformat(expires_at_str)

        key_record, raw_key = await self.api_key_service.issue_key(
            project_id=project_id,
            name=name,
            scopes=scopes,
            created_by=created_by,
            expires_at=expires_at,
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
        keys = await self.api_key_service.list_keys(project_id)

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

    async def revoke_key(self, request: Request) -> JSONResponse:
        """DELETE /admin/keys/{key_id}"""
        key_id = request.path_params["key_id"]
        success = await self.api_key_service.revoke_key(key_id)

        if not success:
            return JSONResponse(
                status_code=404,
                content={"error": f"Key '{key_id}' not found."},
            )

        return JSONResponse(content={"status": "revoked", "key_id": key_id})

    async def rotate_key(self, request: Request) -> JSONResponse:
        """POST /admin/keys/{key_id}/rotate"""
        key_id = request.path_params["key_id"]
        body = await request.json() if await request.body() else {}
        rotated_by = body.get("rotated_by", "admin")

        result = await self.api_key_service.rotate_key(key_id, rotated_by)
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
