"""Focused immutable auditing tests for API-key lifecycle routes."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.testclient import TestClient

from src.gateway.admin.key_routes import KeyManagementAPI, create_key_routes
from src.gateway.auth.api_key_service import APIKeyService
from src.gateway.models import APIKey, RequestContext
from src.gateway.security.audit_trail import AuditEventType, AuditTrail


class _Persistence:
    def __init__(self, *, enabled: bool = False) -> None:
        self.enabled = enabled
        self.keys: dict[str, APIKey] = {}
        self.epoch = 0
        self.audit_heads: dict[str, str] = {}
        self.audit_rows: dict[str, list[dict]] = {}

    async def save_api_key(self, key: APIKey) -> None:
        self.keys[key.key_id] = key

    async def get_api_key(self, key_id: str, tenant_id=None):
        key = self.keys.get(key_id)
        return key if key is not None and key.tenant_id == tenant_id else None

    async def get_api_key_by_hash(self, key_hash: str):
        return next(
            (key for key in self.keys.values() if key.key_hash == key_hash),
            None,
        )

    async def list_api_keys_for_project(self, project_id: str, tenant_id=None):
        return [
            key
            for key in self.keys.values()
            if key.project_id == project_id and key.tenant_id == tenant_id
        ]

    async def update_api_key(self, key: APIKey) -> None:
        self.keys[key.key_id] = key

    async def bump_revocation_epoch(self, tenant_id=None) -> None:
        self.epoch += 1

    async def get_revocation_epoch(self, tenant_id=None) -> int:
        return self.epoch

    async def append_tenant_audit_record(
        self,
        tenant_id: str,
        record: dict,
        expected_prev_hash: str,
    ) -> bool:
        current = self.audit_heads.get(tenant_id, "genesis")
        if current != expected_prev_hash:
            return False
        self.audit_rows.setdefault(tenant_id, []).append(dict(record))
        self.audit_heads[tenant_id] = record["record_hash"]
        return True

    async def get_latest_tenant_audit_hash(self, tenant_id: str):
        return self.audit_heads.get(tenant_id)

    async def load_tenant_audit_records(
        self,
        tenant_id: str,
        project_id: str | None = None,
    ) -> list[dict]:
        records = list(self.audit_rows.get(tenant_id, []))
        if project_id is not None:
            records = [
                record
                for record in records
                if record["project_id"] == project_id
            ]
        return records


def _client(
    service: APIKeyService,
    audit_trail: AuditTrail,
) -> TestClient:
    async def add_context(request, call_next):
        request.state.context = RequestContext(
            user_id="principal:tenant-admin",
            principal_id="principal:tenant-admin",
            project_id="project-a",
            tenant_id="tenant-a",
            roles=["tenant_admin"],
            scopes=[],
        )
        return await call_next(request)

    app = Starlette(
        routes=create_key_routes(
            KeyManagementAPI(
                service,
                mode="ENFORCE",
                audit_trail=audit_trail,
            )
        )
    )
    app.add_middleware(BaseHTTPMiddleware, dispatch=add_context)
    return TestClient(app)


def test_issue_list_rotate_and_revoke_emit_attributed_events() -> None:
    persistence = _Persistence(enabled=True)
    service = APIKeyService(persistence)
    audit_trail = AuditTrail(persistence)
    client = _client(service, audit_trail)

    issued = client.post(
        "/admin/projects/project-a/keys",
        headers={"x-request-id": "request-issue"},
        json={"name": "application", "scopes": ["inference.invoke"]},
    )
    assert issued.status_code == 201
    old_key_id = issued.json()["key_id"]

    listed = client.get(
        "/admin/projects/project-a/keys",
        headers={"x-request-id": "request-list"},
    )
    assert listed.status_code == 200

    rotated = client.post(
        f"/admin/keys/{old_key_id}/rotate",
        headers={"x-request-id": "request-rotate"},
    )
    assert rotated.status_code == 201
    new_key_id = rotated.json()["new_key_id"]

    revoked = client.delete(
        f"/admin/keys/{new_key_id}",
        headers={"x-request-id": "request-revoke"},
    )
    assert revoked.status_code == 200

    records = audit_trail.buffered_records("tenant-a")
    assert [record.event_type for record in records] == [
        AuditEventType.KEY_ISSUED,
        AuditEventType.KEY_LISTED,
        AuditEventType.KEY_ROTATED,
        AuditEventType.KEY_REVOKED,
    ]
    assert [record.request_id for record in records] == [
        "request-issue",
        "request-list",
        "request-rotate",
        "request-revoke",
    ]
    assert all(
        record.user_id == "principal:tenant-admin"
        and record.tenant_id == "tenant-a"
        and record.project_id == "project-a"
        and record.data["actor_id"] == "principal:tenant-admin"
        and record.data["outcome"] == "success"
        for record in records
    )
    assert records[0].data["key_id"] == old_key_id
    assert records[1].data["key_ids"] == [old_key_id]
    assert records[2].data["old_key_id"] == old_key_id
    assert records[2].data["new_key_id"] == new_key_id
    assert records[3].data["key_id"] == new_key_id
    assert audit_trail.verify_chain(tenant_id="tenant-a") is True

    keys = persistence.keys
    assert keys[old_key_id].revoked_by == "principal:tenant-admin"
    assert keys[new_key_id].revoked_by == "principal:tenant-admin"


def test_durable_canonical_issue_fails_closed_without_durable_audit() -> None:
    persistence = _Persistence(enabled=True)
    service = APIKeyService(persistence)
    client = _client(service, AuditTrail())

    response = client.post(
        "/admin/projects/project-a/keys",
        json={"name": "application", "scopes": ["inference.invoke"]},
    )

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "audit_store_unavailable"
    assert len(persistence.keys) == 1
    contained = next(iter(persistence.keys.values()))
    assert contained.revoked is True
    assert contained.revoked_by == "principal:tenant-admin"
