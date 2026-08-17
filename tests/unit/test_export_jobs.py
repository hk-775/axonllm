"""Durable asynchronous export contracts."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from src.gateway.export_jobs import (
    EXPORT_MESSAGE_SCHEMA,
    ExportFormat,
    ExportJob,
    ExportJobError,
    ExportJobNotFound,
    ExportJobNotReady,
    ExportJobService,
    ExportJobWorker,
    ExportKind,
    ExportLevel,
    ExportRenderer,
    ExportStatus,
    RenderedExport,
    export_job_public,
    export_message,
)
from src.gateway.models import UsageRecord


class _Store:
    def __init__(self) -> None:
        self.jobs: dict[tuple[str, str], ExportJob] = {}
        self.failures: list[tuple[str, bool]] = []

    async def create(self, job: ExportJob) -> None:
        self.jobs[(job.tenant_id, job.job_id)] = job

    async def get(
        self,
        tenant_id: str,
        job_id: str,
    ) -> ExportJob | None:
        return self.jobs.get((tenant_id, job_id))

    async def fail_queued(
        self,
        job: ExportJob,
        *,
        error_code: str,
    ) -> None:
        self.jobs[(job.tenant_id, job.job_id)] = replace(
            job,
            status=ExportStatus.FAILED,
            error_code=error_code,
        )

    async def claim(
        self,
        tenant_id: str,
        job_id: str,
    ) -> ExportJob | None:
        job = self.jobs.get((tenant_id, job_id))
        if job is None or job.status is not ExportStatus.QUEUED:
            return None
        claimed = replace(
            job,
            status=ExportStatus.PROCESSING,
            attempt_count=job.attempt_count + 1,
            claim_token="claim",
        )
        self.jobs[(tenant_id, job_id)] = claimed
        return claimed

    async def complete(
        self,
        job: ExportJob,
        rendered: RenderedExport,
        *,
        object_key: str,
    ) -> None:
        self.jobs[(job.tenant_id, job.job_id)] = replace(
            job,
            status=ExportStatus.COMPLETE,
            claim_token="",
            object_key=object_key,
            content_sha256=rendered.content_sha256,
            content_length=rendered.content_length,
            row_count=rendered.row_count,
        )

    async def release_or_fail(
        self,
        job: ExportJob,
        *,
        error_code: str,
        final: bool,
    ) -> None:
        self.failures.append((error_code, final))
        self.jobs[(job.tenant_id, job.job_id)] = replace(
            job,
            status=ExportStatus.FAILED if final else ExportStatus.QUEUED,
            claim_token="",
            error_code=error_code,
        )


class _Queue:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.jobs: list[ExportJob] = []

    async def enqueue(self, job: ExportJob) -> None:
        self.jobs.append(job)
        if self.fail:
            raise RuntimeError("queue unavailable")


class _Objects:
    def __init__(self, *, fail_upload: bool = False) -> None:
        self.fail_upload = fail_upload
        self.uploads: list[tuple[ExportJob, RenderedExport]] = []

    async def upload(
        self,
        job: ExportJob,
        rendered: RenderedExport,
    ) -> str:
        self.uploads.append((job, rendered))
        if self.fail_upload:
            raise RuntimeError("storage unavailable")
        return f"exports/hash/{job.job_id}/{rendered.filename}"

    async def download_url(self, job: ExportJob) -> str:
        return f"https://private.example/{job.object_key}?signed=1"


class _Reader:
    def __init__(
        self,
        *,
        usage: tuple[UsageRecord, ...] = (),
        audit: tuple[dict, ...] = (),
    ) -> None:
        self.usage = usage
        self.audit = audit

    def usage_records(self, _job: ExportJob):
        yield from self.usage

    def audit_records(self, _job: ExportJob):
        yield from self.audit


def _run(awaitable):
    return asyncio.run(awaitable)


def _usage(
    request_id: str,
    *,
    project_id: str = "project-a",
    user_id: str = "user-a",
    provider: str = "bedrock",
) -> UsageRecord:
    return UsageRecord(
        request_id=request_id,
        project_id=project_id,
        user_id=user_id,
        provider=provider,
        model="model-a",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        cost=0.25,
        timestamp=datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc),
        tenant_id="tenant-a",
        latency_ms=12.5,
    )


def _queued_job(
    *,
    kind: ExportKind = ExportKind.USAGE,
    format: ExportFormat = ExportFormat.JSON,
    level: ExportLevel = ExportLevel.RECORDS,
    attempts: int = 0,
    restricted: bool = False,
) -> ExportJob:
    now = datetime.now(timezone.utc)
    return ExportJob(
        job_id="exp_" + "a" * 32,
        tenant_id="tenant-a",
        requested_by="principal-a",
        kind=kind,
        format=format,
        level=level,
        filters=(),
        restricted=restricted,
        status=ExportStatus.QUEUED,
        created_at=now,
        expires_at=now + timedelta(days=1),
        attempt_count=attempts,
        filename=(
            "axonllm-audit-records.json" if kind is ExportKind.AUDIT else f"axonllm-usage-{level.value}.{format.value}"
        ),
        content_type=("text/csv" if format is ExportFormat.CSV else "application/json"),
    )


def test_control_service_creates_minimal_fifo_job() -> None:
    store = _Store()
    queue = _Queue()
    service = ExportJobService(
        store=store,
        queue=queue,
        objects=_Objects(),
    )

    job = _run(
        service.create_usage(
            tenant_id="tenant-a",
            requested_by="principal-a",
            format="csv",
            level="records",
            filters={"project_id": "project-a", "model": None},
        )
    )

    assert job.status is ExportStatus.QUEUED
    assert job.filters == (("project_id", "project-a"),)
    assert queue.jobs == [job]
    assert json.loads(export_message(job)) == {
        "jobId": job.job_id,
        "schema": EXPORT_MESSAGE_SCHEMA,
        "tenantId": "tenant-a",
    }
    public = export_job_public(job)
    assert public["jobId"] == job.job_id
    assert "requested_by" not in public
    assert "filters" not in public


def test_enqueue_failure_is_durable_and_secret_free() -> None:
    store = _Store()
    service = ExportJobService(
        store=store,
        queue=_Queue(fail=True),
        objects=_Objects(),
    )

    with pytest.raises(ExportJobError, match="could not be queued"):
        _run(
            service.create_audit(
                tenant_id="tenant-a",
                requested_by="principal-a",
                project_id=None,
                restricted=True,
            )
        )

    failed = next(iter(store.jobs.values()))
    assert failed.status is ExportStatus.FAILED
    assert failed.error_code == "enqueue_failed"


@pytest.mark.parametrize(
    "filters",
    [
        {"start_time": "not-a-date"},
        {
            "start_time": "2026-08-17T00:00:00Z",
            "end_time": "2026-08-16T00:00:00Z",
        },
    ],
)
def test_invalid_usage_window_fails_before_persistence(
    filters: dict[str, str],
) -> None:
    store = _Store()
    service = ExportJobService(
        store=store,
        queue=_Queue(),
        objects=_Objects(),
    )

    with pytest.raises(ValueError, match="export"):
        _run(
            service.create_usage(
                tenant_id="tenant-a",
                requested_by="principal-a",
                format="json",
                level="records",
                filters=filters,
            )
        )

    assert store.jobs == {}


def test_status_and_download_are_tenant_requester_and_kind_bound() -> None:
    store = _Store()
    objects = _Objects()
    service = ExportJobService(
        store=store,
        queue=_Queue(),
        objects=objects,
    )
    job = replace(
        _queued_job(),
        status=ExportStatus.COMPLETE,
        object_key="exports/hash/job/report.json",
    )
    store.jobs[(job.tenant_id, job.job_id)] = job

    assert _run(
        service.download_url(
            tenant_id="tenant-a",
            requested_by="principal-a",
            job_id=job.job_id,
            kind=ExportKind.USAGE,
        )
    ).startswith("https://private.example/")

    for tenant_id, requester, kind in (
        ("tenant-b", "principal-a", ExportKind.USAGE),
        ("tenant-a", "principal-b", ExportKind.USAGE),
        ("tenant-a", "principal-a", ExportKind.AUDIT),
    ):
        with pytest.raises(ExportJobNotFound):
            _run(
                service.get(
                    tenant_id=tenant_id,
                    requested_by=requester,
                    job_id=job.job_id,
                    kind=kind,
                )
            )


def test_incomplete_job_has_no_download() -> None:
    store = _Store()
    service = ExportJobService(
        store=store,
        queue=_Queue(),
        objects=_Objects(),
    )
    job = _queued_job()
    store.jobs[(job.tenant_id, job.job_id)] = job

    with pytest.raises(ExportJobNotReady):
        _run(
            service.download_url(
                tenant_id=job.tenant_id,
                requested_by=job.requested_by,
                job_id=job.job_id,
                kind=job.kind,
            )
        )


def test_usage_worker_streams_filtered_json_and_cleans_temp_file() -> None:
    store = _Store()
    job = replace(
        _queued_job(),
        filters=(("project_id", "project-a"),),
    )
    store.jobs[(job.tenant_id, job.job_id)] = job
    objects = _Objects()
    worker = ExportJobWorker(
        store=store,
        objects=objects,
        renderer=ExportRenderer(
            _Reader(
                usage=(
                    _usage("request-a"),
                    _usage("request-b", project_id="project-b"),
                )
            )
        ),
    )

    assert _run(worker.process(export_message(job))) == job.job_id

    completed = store.jobs[(job.tenant_id, job.job_id)]
    assert completed.status is ExportStatus.COMPLETE
    assert completed.row_count == 1
    assert completed.content_sha256
    rendered = objects.uploads[0][1]
    assert rendered.path.exists() is False


def test_csv_export_neutralizes_spreadsheet_formulas() -> None:
    job = _queued_job(
        format=ExportFormat.CSV,
        level=ExportLevel.RECORDS,
    )
    renderer = ExportRenderer(_Reader(usage=(_usage("request-a", user_id="=HYPERLINK(secret)"),)))

    rendered = renderer.render(job)
    try:
        content = rendered.path.read_text(encoding="utf-8")
    finally:
        rendered.path.unlink()

    assert "'=HYPERLINK(secret)" in content


def test_audit_worker_redacts_sensitive_and_restricted_fields() -> None:
    job = _queued_job(
        kind=ExportKind.AUDIT,
        restricted=True,
    )
    renderer = ExportRenderer(
        _Reader(
            audit=(
                {
                    "PK": "TENANT#tenant-a",
                    "SK": "AUDIT#RECORD#1",
                    "record_id": "record-a",
                    "data": json.dumps(
                        {
                            "authorization": "Bearer secret",
                            "destination_url": "https://private.example",
                            "marker": "visible",
                        }
                    ),
                },
            )
        )
    )

    rendered = renderer.render(job)
    try:
        payload = json.loads(rendered.path.read_text(encoding="utf-8"))
    finally:
        rendered.path.unlink()

    assert payload["count"] == 1
    data = payload["records"][0]["data"]
    assert data["authorization"] == "[REDACTED]"
    assert data["destination_url"] == "[REDACTED]"
    assert data["marker"] == "visible"
    assert "PK" not in payload["records"][0]
    assert "SK" not in payload["records"][0]


def test_worker_releases_transient_failure_then_marks_final_failure() -> None:
    store = _Store()
    objects = _Objects(fail_upload=True)
    worker = ExportJobWorker(
        store=store,
        objects=objects,
        renderer=ExportRenderer(_Reader(usage=(_usage("request-a"),))),
    )
    job = _queued_job(attempts=0)
    store.jobs[(job.tenant_id, job.job_id)] = job

    with pytest.raises(RuntimeError, match="storage unavailable"):
        _run(worker.process(export_message(job)))
    assert store.failures == [("retry_pending", False)]
    assert store.jobs[(job.tenant_id, job.job_id)].status is ExportStatus.QUEUED

    store.jobs[(job.tenant_id, job.job_id)] = replace(
        job,
        attempt_count=2,
    )
    assert _run(worker.process(export_message(job))) == job.job_id
    assert store.failures[-1] == ("generation_failed", True)
    assert store.jobs[(job.tenant_id, job.job_id)].status is ExportStatus.FAILED


@pytest.mark.parametrize(
    "body",
    [
        "",
        "{}",
        json.dumps(
            {
                "jobId": "bad",
                "schema": EXPORT_MESSAGE_SCHEMA,
                "tenantId": "tenant-a",
            }
        ),
        json.dumps(
            {
                "extra": True,
                "jobId": "exp_" + "a" * 32,
                "schema": EXPORT_MESSAGE_SCHEMA,
                "tenantId": "tenant-a",
            }
        ),
    ],
)
def test_invalid_worker_messages_fail_before_store_access(body: str) -> None:
    store = _Store()
    worker = ExportJobWorker(
        store=store,
        objects=_Objects(),
        renderer=ExportRenderer(_Reader()),
    )

    with pytest.raises(ValueError, match="export queue"):
        _run(worker.process(body))

    assert store.jobs == {}
