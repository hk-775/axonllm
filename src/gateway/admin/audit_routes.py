"""Admin API routes for audit trail query and integrity verification."""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from src.gateway.security.audit_trail import AuditEventType

if TYPE_CHECKING:
    from src.gateway.security.audit_trail import AuditTrail


class AuditAPI:
    """Query and verify audit trail records."""

    def __init__(self, audit_trail: AuditTrail) -> None:
        self.audit_trail = audit_trail

    async def query_records(self, request: Request) -> JSONResponse:
        """GET /admin/audit/records?project_id=&event_type=&limit="""
        project_id = request.query_params.get("project_id")
        event_type_str = request.query_params.get("event_type")
        limit = int(request.query_params.get("limit", "100"))

        event_type = None
        if event_type_str:
            try:
                event_type = AuditEventType(event_type_str)
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={"error": f"Invalid event_type: {event_type_str}",
                             "valid_types": [e.value for e in AuditEventType]},
                )

        records = self.audit_trail.query_recent(
            project_id=project_id,
            event_type=event_type,
            limit=limit,
        )

        return JSONResponse(content={
            "count": len(records),
            "records": [
                {
                    "record_id": r.record_id,
                    "event_type": r.event_type.value,
                    "timestamp": r.timestamp.isoformat(),
                    "user_id": r.user_id,
                    "project_id": r.project_id,
                    "request_id": r.request_id,
                    "data": r.data,
                }
                for r in records
            ],
        })

    async def verify_integrity(self, request: Request) -> JSONResponse:
        """GET /admin/audit/verify"""
        is_valid = self.audit_trail.verify_chain()
        total_records = len(self.audit_trail._buffer)

        return JSONResponse(content={
            "chain_valid": is_valid,
            "total_records_in_buffer": total_records,
            "status": "intact" if is_valid else "TAMPERED",
        })

    async def get_stats(self, request: Request) -> JSONResponse:
        """GET /admin/audit/stats"""
        buffer = self.audit_trail._buffer
        if not buffer:
            return JSONResponse(content={"total": 0, "by_type": {}, "by_project": {}})

        by_type: dict[str, int] = {}
        by_project: dict[str, int] = {}
        for r in buffer:
            by_type[r.event_type.value] = by_type.get(r.event_type.value, 0) + 1
            by_project[r.project_id] = by_project.get(r.project_id, 0) + 1

        return JSONResponse(content={
            "total": len(buffer),
            "by_type": by_type,
            "by_project": by_project,
            "oldest": buffer[0].timestamp.isoformat(),
            "newest": buffer[-1].timestamp.isoformat(),
        })

    async def get_security_events(self, request: Request) -> JSONResponse:
        """GET /admin/audit/security?project_id=&limit="""
        project_id = request.query_params.get("project_id")
        limit = int(request.query_params.get("limit", "50"))

        security_types = {
            AuditEventType.INJECTION_DETECTED,
            AuditEventType.INJECTION_BLOCKED,
            AuditEventType.PII_REDACTION,
            AuditEventType.AUTH_FAILURE,
            AuditEventType.POLICY_DENY,
        }

        records = self.audit_trail._buffer
        if project_id:
            records = [r for r in records if r.project_id == project_id]
        records = [r for r in records if r.event_type in security_types]
        records = records[-limit:]

        return JSONResponse(content={
            "count": len(records),
            "records": [
                {
                    "record_id": r.record_id,
                    "event_type": r.event_type.value,
                    "timestamp": r.timestamp.isoformat(),
                    "user_id": r.user_id,
                    "project_id": r.project_id,
                    "data": r.data,
                }
                for r in records
            ],
        })


def create_audit_routes(audit_api: AuditAPI) -> list[Route]:
    """Create Starlette routes for audit trail admin API."""
    return [
        Route("/admin/audit/records", audit_api.query_records, methods=["GET"]),
        Route("/admin/audit/verify", audit_api.verify_integrity, methods=["GET"]),
        Route("/admin/audit/stats", audit_api.get_stats, methods=["GET"]),
        Route("/admin/audit/security", audit_api.get_security_events, methods=["GET"]),
    ]
