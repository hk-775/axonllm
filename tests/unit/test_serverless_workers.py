"""Lambda host contracts for request-independent AxonLLM workers."""

from __future__ import annotations

import asyncio

import pytest

from src.gateway.serverless_workers import (
    build_query_reconciler,
    process_export_sqs_batch,
    process_query_reconciliation,
    process_security_event_sqs_batch,
)
from src.gateway.query.reconciliation import QueryReconciliationResult

_QUEUE_ARN = "arn:aws:sqs:us-east-1:123456789012:axonllm-security-events.fifo"


class _Dispatcher:
    def __init__(
        self,
        *,
        fail_body: str | None = None,
        cancel_body: str | None = None,
    ) -> None:
        self.cancel_body = cancel_body
        self.delivered: list[str] = []
        self.fail_body = fail_body
        self.stopped = False

    async def deliver_outbox_body(self, body: object) -> str:
        assert isinstance(body, str)
        self.delivered.append(body)
        if body == self.cancel_body:
            raise asyncio.CancelledError
        if body == self.fail_body:
            raise RuntimeError("delivery refused")
        return "tenant-a"

    async def stop(self, timeout_seconds: float = 5.0) -> None:
        assert timeout_seconds == 5.0
        self.stopped = True


class _Reconciler:
    def __init__(self, result: QueryReconciliationResult) -> None:
        self.calls: list[dict[str, object]] = []
        self.result = result

    async def run(
        self,
        *,
        cursor: str | None = None,
        max_pages: int = 10,
    ) -> QueryReconciliationResult:
        self.calls.append({"cursor": cursor, "max_pages": max_pages})
        return self.result


class _ExportWorker:
    def __init__(self, *, fail: bool = False) -> None:
        self.bodies: list[str] = []
        self.fail = fail

    async def process(self, body: object) -> str:
        assert isinstance(body, str)
        self.bodies.append(body)
        if self.fail:
            raise RuntimeError("export failed")
        return "exp_" + "a" * 32


def _record(message_id: str, body: str) -> dict[str, str]:
    return {
        "body": body,
        "eventSource": "aws:sqs",
        "eventSourceARN": _QUEUE_ARN,
        "messageId": message_id,
    }


def _run(awaitable):
    return asyncio.run(awaitable)


def test_successful_fifo_batch_returns_no_failures_and_closes() -> None:
    dispatcher = _Dispatcher()

    result = _run(
        process_security_event_sqs_batch(
            {
                "Records": [
                    _record("message-1", "body-1"),
                    _record("message-2", "body-2"),
                ]
            },
            dispatcher_factory=lambda: dispatcher,
        )
    )

    assert result == {"batchItemFailures": []}
    assert dispatcher.delivered == ["body-1", "body-2"]
    assert dispatcher.stopped is True


def test_fifo_batch_stops_at_first_failure_and_retries_the_remainder(
    caplog: pytest.LogCaptureFixture,
) -> None:
    dispatcher = _Dispatcher(fail_body="body-2")

    result = _run(
        process_security_event_sqs_batch(
            {
                "Records": [
                    _record("message-1", "body-1"),
                    _record("message-2", "body-2"),
                    _record("message-3", "Bearer do-not-log"),
                ]
            },
            dispatcher_factory=lambda: dispatcher,
        )
    )

    assert result == {
        "batchItemFailures": [
            {"itemIdentifier": "message-2"},
            {"itemIdentifier": "message-3"},
        ]
    }
    assert dispatcher.delivered == ["body-1", "body-2"]
    assert dispatcher.stopped is True
    assert "message-2" in caplog.text
    assert "Bearer do-not-log" not in caplog.text


@pytest.mark.parametrize(
    "records",
    [
        [],
        [_record("duplicate", "one"), _record("duplicate", "two")],
        [
            {
                **_record("message-1", "body"),
                "eventSource": "aws:kinesis",
            }
        ],
        [
            {
                **_record("message-1", "body"),
                "eventSourceARN": _QUEUE_ARN.removesuffix(".fifo"),
            }
        ],
        [_record(f"message-{index}", "body") for index in range(11)],
    ],
)
def test_invalid_batch_is_rejected_before_dispatcher_construction(
    records: list[dict[str, str]],
) -> None:
    constructed = False

    def factory() -> _Dispatcher:
        nonlocal constructed
        constructed = True
        return _Dispatcher()

    with pytest.raises(ValueError, match="security event Lambda"):
        _run(
            process_security_event_sqs_batch(
                {"Records": records},
                dispatcher_factory=factory,
            )
        )

    assert constructed is False


def test_cancellation_is_propagated_after_cleanup() -> None:
    dispatcher = _Dispatcher(cancel_body="cancel")

    with pytest.raises(asyncio.CancelledError):
        _run(
            process_security_event_sqs_batch(
                {"Records": [_record("message-1", "cancel")]},
                dispatcher_factory=lambda: dispatcher,
            )
        )

    assert dispatcher.stopped is True


def test_export_fifo_message_returns_partial_batch_success() -> None:
    worker = _ExportWorker()

    result = _run(
        process_export_sqs_batch(
            {"Records": [_record("message-1", "export-body")]},
            worker_factory=lambda: worker,
        )
    )

    assert result == {"batchItemFailures": []}
    assert worker.bodies == ["export-body"]


def test_export_failure_retries_without_logging_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    worker = _ExportWorker(fail=True)

    result = _run(
        process_export_sqs_batch(
            {"Records": [_record("message-1", "Bearer do-not-log")]},
            worker_factory=lambda: worker,
        )
    )

    assert result == {"batchItemFailures": [{"itemIdentifier": "message-1"}]}
    assert "message-1" in caplog.text
    assert "Bearer do-not-log" not in caplog.text


@pytest.mark.parametrize(
    "event",
    [
        {},
        {"Records": []},
        {
            "Records": [
                _record("message-1", "one"),
                _record("message-2", "two"),
            ]
        },
        {
            "Records": [
                {
                    **_record("message-1", "body"),
                    "eventSourceARN": _QUEUE_ARN.removesuffix(".fifo"),
                }
            ]
        },
    ],
)
def test_invalid_export_event_is_rejected_before_worker_construction(
    event: dict,
) -> None:
    constructed = False

    def factory() -> _ExportWorker:
        nonlocal constructed
        constructed = True
        return _ExportWorker()

    with pytest.raises(ValueError, match="export Lambda"):
        _run(process_export_sqs_batch(event, worker_factory=factory))

    assert constructed is False


def _reconciliation_result(
    *,
    failed: int = 0,
) -> QueryReconciliationResult:
    return QueryReconciliationResult(
        claimed=3,
        finalized=1,
        audited=1,
        deferred=1,
        lost_claims=0,
        failed=failed,
        pages=1,
        next_cursor="next-page",
    )


def test_scheduled_reconciliation_returns_bounded_public_counters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AXON_QUERY_RECONCILIATION_MAX_PAGES", "2")
    reconciler = _Reconciler(_reconciliation_result())

    result = _run(
        process_query_reconciliation(
            {"schema": "axonllm.query-reconciliation/v1"},
            reconciler_factory=lambda: reconciler,
        )
    )

    assert reconciler.calls == [{"cursor": None, "max_pages": 2}]
    assert result == {
        "schema": "axonllm.query-reconciliation-result/v1",
        "audited": 1,
        "claimed": 3,
        "deferred": 1,
        "failed": 0,
        "finalized": 1,
        "lostClaims": 0,
        "nextCursor": "next-page",
        "pages": 1,
    }


def test_scheduled_reconciliation_raises_for_partial_failure() -> None:
    reconciler = _Reconciler(_reconciliation_result(failed=1))

    with pytest.raises(RuntimeError, match="1 query reconciliation"):
        _run(
            process_query_reconciliation(
                {"schema": "axonllm.query-reconciliation/v1"},
                reconciler_factory=lambda: reconciler,
            )
        )


def test_scheduled_reconciliation_validates_input_before_construction() -> None:
    constructed = False

    def factory() -> _Reconciler:
        nonlocal constructed
        constructed = True
        return _Reconciler(_reconciliation_result())

    with pytest.raises(ValueError, match="Lambda input"):
        _run(
            process_query_reconciliation(
                {"schema": "untrusted"},
                reconciler_factory=factory,
            )
        )

    assert constructed is False


def test_query_reconciler_builder_constructs_no_router_or_http_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AXON_DYNAMODB_TABLE", "axonllm-state")
    monkeypatch.setenv("LLM_ROUTER_DYNAMODB_ENABLED", "true")
    monkeypatch.setenv("AXON_ATHENA_QUERY_ENABLED", "true")
    monkeypatch.setenv(
        "AXON_ATHENA_QUERY_BINDINGS",
        ('[{"project_id":"project-a","role_arn":"arn:aws:iam::123456789012:role/axon-query","tenant_id":"tenant-a"}]'),
    )
    monkeypatch.setenv("AXON_QUERY_RECONCILIATION_PAGE_SIZE", "2")

    reconciler = build_query_reconciler()

    assert reconciler.page_size == 2
    assert reconciler.claim_seconds == 300
    assert reconciler.store.enabled is True
    assert reconciler.store._table_name == "axonllm-state"
    assert reconciler.bindings.allows(
        "tenant-a",
        "project-a",
        "arn:aws:iam::123456789012:role/axon-query",
    )
