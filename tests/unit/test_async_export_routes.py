"""HTTP contracts for serverless asynchronous exports."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.testclient import TestClient

from src.gateway.admin.audit_routes import AuditAPI, create_audit_routes
from src.gateway.admin.routes import AdminAPI, create_admin_routes
from src.gateway.cost_tracker import CostTracker
from src.gateway.export_jobs import (
    ExportFormat,
    ExportJob,
    ExportJobError,
    ExportKind,
    ExportLevel,
    ExportStatus,
)
from src.gateway.health_tracker import ProviderHealthTracker
from src.gateway.model_registry import ModelRegistry
from src.gateway.security.audit_trail import AuditTrail


class _ContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        request.state.context = SimpleNamespace(
            tenant_id="tenant-a",
            principal_id="principal-a",
            user_id="principal-a",
            roles=["tenant_auditor"],
            scopes=[],
        )
        return await call_next(request)


def _job(
    kind: ExportKind,
    *,
    status: ExportStatus = ExportStatus.QUEUED,
) -> ExportJob:
    now = datetime.now(timezone.utc)
    return ExportJob(
        job_id="exp_" + "a" * 32,
        tenant_id="tenant-a",
        requested_by="principal-a",
        kind=kind,
        format=(ExportFormat.JSON if kind is ExportKind.AUDIT else ExportFormat.CSV),
        level=ExportLevel.RECORDS,
        filters=(),
        restricted=kind is ExportKind.AUDIT,
        status=status,
        created_at=now,
        expires_at=now + timedelta(hours=24),
        object_key=("exports/hash/job/report.json" if status is ExportStatus.COMPLETE else ""),
        filename=("axonllm-audit-records.json" if kind is ExportKind.AUDIT else "axonllm-usage-records.csv"),
        content_type=("application/json" if kind is ExportKind.AUDIT else "text/csv"),
        content_sha256=("b" * 64 if status is ExportStatus.COMPLETE else ""),
        content_length=(42 if status is ExportStatus.COMPLETE else 0),
        row_count=3 if status is ExportStatus.COMPLETE else 0,
    )


class _Exports:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.jobs = {
            ExportKind.USAGE: _job(ExportKind.USAGE),
            ExportKind.AUDIT: _job(ExportKind.AUDIT),
        }

    async def create_usage(self, **kwargs):
        self.calls.append(("create_usage", kwargs))
        return self.jobs[ExportKind.USAGE]

    async def create_audit(self, **kwargs):
        self.calls.append(("create_audit", kwargs))
        return self.jobs[ExportKind.AUDIT]

    async def get(self, **kwargs):
        self.calls.append(("get", kwargs))
        return self.jobs[kwargs["kind"]]

    async def download_url(self, **kwargs):
        self.calls.append(("download", kwargs))
        return "https://private.example/export?signed=1"


class _FailingExports(_Exports):
    async def get(self, **kwargs):
        raise ExportJobError("store unavailable")

    async def download_url(self, **kwargs):
        raise ExportJobError("signer unavailable")


def _admin_client(exports: _Exports) -> TestClient:
    app = Starlette(
        routes=create_admin_routes(
            AdminAPI(
                cost_tracker=CostTracker(pricing_config={}),
                health_tracker=ProviderHealthTracker(),
                model_registry=ModelRegistry(),
                export_jobs=exports,
            )
        )
    )
    app.add_middleware(_ContextMiddleware)
    return TestClient(app)


def _audit_client(exports: _Exports) -> TestClient:
    app = Starlette(
        routes=create_audit_routes(
            AuditAPI(
                AuditTrail(persistence=None),
                export_jobs=exports,
            )
        )
    )
    app.add_middleware(_ContextMiddleware)
    return TestClient(app)


def test_usage_export_returns_job_status_and_short_lived_download() -> None:
    exports = _Exports()
    client = _admin_client(exports)

    created = client.get("/admin/usage/export?format=csv&level=records&project_id=project-a")
    assert created.status_code == 202
    assert created.headers["retry-after"] == "2"
    assert created.json()["statusUrl"] == ("/admin/usage/exports/exp_" + "a" * 32)
    assert exports.calls[0] == (
        "create_usage",
        {
            "tenant_id": "tenant-a",
            "requested_by": "principal-a",
            "format": "csv",
            "level": "records",
            "filters": {
                "start_time": None,
                "end_time": None,
                "provider": None,
                "model": None,
                "project_id": "project-a",
                "user_id": None,
            },
        },
    )

    exports.jobs[ExportKind.USAGE] = _job(
        ExportKind.USAGE,
        status=ExportStatus.COMPLETE,
    )
    status = client.get("/admin/usage/exports/exp_" + "a" * 32)
    assert status.status_code == 200
    assert status.json()["downloadUrl"].endswith("/download")

    download = client.get(
        "/admin/usage/exports/exp_" + "a" * 32 + "/download",
        follow_redirects=False,
    )
    assert download.status_code == 303
    assert download.headers["location"].startswith("https://private.example/")


def test_audit_export_preserves_restricted_reader_scope() -> None:
    exports = _Exports()
    client = _audit_client(exports)

    response = client.get("/admin/audit/export?project_id=project-a")

    assert response.status_code == 202
    assert response.json()["kind"] == "audit"
    assert response.json()["statusUrl"] == ("/admin/audit/exports/exp_" + "a" * 32)
    assert exports.calls == [
        (
            "create_audit",
            {
                "tenant_id": "tenant-a",
                "requested_by": "principal-a",
                "project_id": "project-a",
                "restricted": True,
            },
        )
    ]


def test_export_backend_failures_are_sanitized() -> None:
    exports = _FailingExports()
    usage = _admin_client(exports)
    audit = _audit_client(exports)
    job_id = "exp_" + "a" * 32

    usage_status = usage.get(f"/admin/usage/exports/{job_id}")
    audit_status = audit.get(f"/admin/audit/exports/{job_id}")
    usage_download = usage.get(
        f"/admin/usage/exports/{job_id}/download",
        follow_redirects=False,
    )
    audit_download = audit.get(
        f"/admin/audit/exports/{job_id}/download",
        follow_redirects=False,
    )

    for response in (
        usage_status,
        audit_status,
        usage_download,
        audit_download,
    ):
        assert response.status_code == 503
        assert response.json()["error"]["type"] == "export_unavailable"
        assert "store unavailable" not in response.text
        assert "signer unavailable" not in response.text
