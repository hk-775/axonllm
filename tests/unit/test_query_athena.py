"""Focused boundary tests for bounded Athena execution."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import time
from collections.abc import Callable
from typing import Any

import boto3
import pytest

from src.gateway.query.athena import (
    AWS_CLIENT_CONFIG,
    AthenaExecutionError,
    AthenaExecutor,
    AthenaQueryLimits,
    BotoAthenaClientFactory,
)
from src.gateway.query.models import AthenaDatasource
from src.gateway.query.sql_policy import ValidatedQuery


ROLE_ARN = "arn:aws:iam::123456789012:role/axon-athena-project-a"


def _datasource() -> AthenaDatasource:
    return AthenaDatasource(
        datasource_id="warehouse",
        tenant_id="tenant-a",
        project_id="project-a",
        name="Analytics warehouse",
        role_arn=ROLE_ARN,
        region="us-east-1",
        catalog="AwsDataCatalog",
        database="analytics",
        workgroup="axon_read_only",
    )


def _query() -> ValidatedQuery:
    sql = "SELECT order_id, note FROM orders"
    return ValidatedQuery(
        sql=sql,
        sha256=hashlib.sha256(sql.encode()).hexdigest(),
        table_count=1,
    )


def _safe_workgroup(*, cutoff: int = 4096) -> dict[str, Any]:
    return {
        "WorkGroup": {
            "Name": "axon_read_only",
            "State": "ENABLED",
            "Configuration": {
                "EnforceWorkGroupConfiguration": True,
                "PublishCloudWatchMetricsEnabled": True,
                "BytesScannedCutoffPerQuery": cutoff,
                "ResultConfiguration": {
                    "OutputLocation": "s3://axon-results/tenant-a/",
                    "EncryptionConfiguration": {
                        "EncryptionOption": "SSE_KMS",
                        "KmsKey": (
                            "arn:aws:kms:us-east-1:123456789012:"
                            "key/11111111-2222-3333-4444-555555555555"
                        ),
                    },
                },
            },
        }
    }


def _execution(
    *,
    state: str = "SUCCEEDED",
    scanned: int = 2048,
    engine_ms: int = 25,
) -> dict[str, Any]:
    return {
        "QueryExecution": {
            "Status": {"State": state},
            "Statistics": {
                "DataScannedInBytes": scanned,
                "EngineExecutionTimeInMillis": engine_ms,
            },
        }
    }


def _result_page(
    rows: list[list[str | None]],
    *,
    columns: list[tuple[str, str]] | None = None,
    next_token: object | None = None,
) -> dict[str, Any]:
    column_values = columns or [
        ("order_id", "varchar"),
        ("note", "varchar"),
    ]
    response: dict[str, Any] = {
        "ResultSet": {
            "ResultSetMetadata": {
                "ColumnInfo": [
                    {"Name": name, "Type": column_type}
                    for name, column_type in column_values
                ]
            },
            "Rows": [
                {
                    "Data": [
                        {} if value is None else {"VarCharValue": value}
                        for value in row
                    ]
                }
                for row in rows
            ],
        }
    }
    if next_token is not None:
        response["NextToken"] = next_token
    return response


class _AthenaClient:
    def __init__(
        self,
        *,
        workgroup: dict[str, Any] | None = None,
        executions: list[dict[str, Any]] | None = None,
        result_pages: list[dict[str, Any]] | None = None,
    ) -> None:
        self.workgroup = workgroup or _safe_workgroup()
        self.executions = executions or [_execution()]
        self.result_pages = result_pages or [
            _result_page(
                [
                    ["order_id", "note"],
                    ["order-1", "ready"],
                ]
            )
        ]
        self.workgroup_calls: list[dict[str, Any]] = []
        self.start_calls: list[dict[str, Any]] = []
        self.status_calls: list[dict[str, Any]] = []
        self.result_calls: list[dict[str, Any]] = []
        self.stop_calls: list[dict[str, Any]] = []

    def get_work_group(self, **kwargs: Any) -> dict[str, Any]:
        self.workgroup_calls.append(kwargs)
        return copy.deepcopy(self.workgroup)

    def start_query_execution(self, **kwargs: Any) -> dict[str, str]:
        self.start_calls.append(kwargs)
        return {"QueryExecutionId": "execution-123"}

    def get_query_execution(self, **kwargs: Any) -> dict[str, Any]:
        self.status_calls.append(kwargs)
        if len(self.executions) > 1:
            response = self.executions.pop(0)
        else:
            response = self.executions[0]
        return copy.deepcopy(response)

    def get_query_results(self, **kwargs: Any) -> dict[str, Any]:
        self.result_calls.append(kwargs)
        if not self.result_pages:
            raise AssertionError("unexpected result page request")
        return copy.deepcopy(self.result_pages.pop(0))

    def stop_query_execution(self, **kwargs: Any) -> None:
        self.stop_calls.append(kwargs)


class _ClientFactory:
    def __init__(
        self,
        client: _AthenaClient,
        *,
        error: Exception | None = None,
    ) -> None:
        self.client = client
        self.error = error
        self.calls: list[tuple[AthenaDatasource, dict[str, Any]]] = []

    def __call__(
        self,
        datasource: AthenaDatasource,
        **kwargs: Any,
    ) -> _AthenaClient:
        self.calls.append((datasource, kwargs))
        if self.error is not None:
            raise self.error
        return self.client


async def _execute(
    executor: AthenaExecutor,
    *,
    datasource: AthenaDatasource | None = None,
    max_rows: int | None = None,
    on_started: Callable[[str], Any] | None = None,
) -> Any:
    return await executor.execute(
        _query(),
        datasource or _datasource(),
        tenant_id="tenant-a",
        project_id="project-a",
        principal_id="principal:analyst",
        request_id="request-123",
        max_rows=max_rows,
        on_started=on_started,
    )


async def test_executor_enforces_context_and_parses_paginated_results() -> None:
    datasource = _datasource()
    client = _AthenaClient(
        result_pages=[
            _result_page(
                [
                    ["order_id", "note"],
                    ["order-1", None],
                ],
                next_token="page-2",
            ),
            _result_page([["order-2", "ready"]]),
        ]
    )
    factory = _ClientFactory(client)
    executor = AthenaExecutor(
        client_factory=factory,
        limits=AthenaQueryLimits(
            max_rows=10,
            max_result_bytes=4096,
            max_bytes_scanned=4096,
        ),
    )

    result = await _execute(executor, datasource=datasource)

    assert factory.calls[0][0] is datasource
    assert factory.calls[0][1] == {
        "tenant_id": "tenant-a",
        "project_id": "project-a",
        "principal_id": "principal:analyst",
        "request_id": "request-123",
    }
    assert client.workgroup_calls == [{"WorkGroup": "axon_read_only"}]
    assert len(client.start_calls) == 1
    started = client.start_calls[0]
    assert started["QueryString"] == _query().sql
    assert started["QueryExecutionContext"] == {
        "Catalog": "AwsDataCatalog",
        "Database": "analytics",
    }
    assert started["WorkGroup"] == "axon_read_only"
    assert re.fullmatch(
        r"[0-9a-f]{64}",
        started["ClientRequestToken"],
    )
    assert client.result_calls == [
        {"QueryExecutionId": "execution-123", "MaxResults": 12},
        {
            "QueryExecutionId": "execution-123",
            "MaxResults": 12,
            "NextToken": "page-2",
        },
    ]
    assert result.columns == (
        {"name": "order_id", "type": "varchar"},
        {"name": "note", "type": "varchar"},
    )
    assert result.rows == (
        ("order-1", None),
        ("order-2", "ready"),
    )
    assert result.row_count == 2
    assert result.truncated is False
    assert result.data_scanned_bytes == 2048
    assert result.engine_execution_ms == 25
    assert result.result_bytes == len(
        json.dumps(
            {
                "columns": [
                    {"name": "order_id", "type": "varchar"},
                    {"name": "note", "type": "varchar"},
                ],
                "rows": [
                    ["order-1", None],
                    ["order-2", "ready"],
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    )


async def test_result_page_size_is_bounded_across_pagination() -> None:
    client = _AthenaClient(
        result_pages=[
            _result_page(
                [["order_id", "note"]],
                next_token="page-2",
            ),
            _result_page([]),
        ]
    )
    executor = AthenaExecutor(
        client_factory=_ClientFactory(client),
        limits=AthenaQueryLimits(max_rows=1000),
    )

    result = await _execute(executor)

    assert result.rows == ()
    assert client.result_calls == [
        {"QueryExecutionId": "execution-123", "MaxResults": 100},
        {
            "QueryExecutionId": "execution-123",
            "MaxResults": 100,
            "NextToken": "page-2",
        },
    ]


async def test_identical_request_retries_use_the_same_athena_token() -> None:
    tokens: list[str] = []
    for _ in range(2):
        client = _AthenaClient()
        executor = AthenaExecutor(
            client_factory=_ClientFactory(client),
        )
        await _execute(executor)
        tokens.append(client.start_calls[0]["ClientRequestToken"])

    assert len(set(tokens)) == 1
    assert re.fullmatch(r"[0-9a-f]{64}", tokens[0])


async def test_execution_id_is_persisted_before_status_polling() -> None:
    client = _AthenaClient()
    observed: list[str] = []

    async def on_started(execution_id: str) -> None:
        assert client.status_calls == []
        observed.append(execution_id)

    executor = AthenaExecutor(client_factory=_ClientFactory(client))

    await _execute(executor, on_started=on_started)

    assert observed == ["execution-123"]


async def test_lifecycle_start_failure_cancels_started_query() -> None:
    client = _AthenaClient()

    async def on_started(_execution_id: str) -> None:
        raise RuntimeError("lifecycle unavailable")

    executor = AthenaExecutor(client_factory=_ClientFactory(client))

    with pytest.raises(RuntimeError, match="lifecycle unavailable"):
        await _execute(executor, on_started=on_started)

    assert client.status_calls == []
    assert client.stop_calls == [
        {"QueryExecutionId": "execution-123"}
    ]


@pytest.mark.parametrize(
    "values",
    [
        {"timeout_seconds": 0},
        {"timeout_seconds": float("inf")},
        {"timeout_seconds": 301},
        {"max_rows": True},
        {"max_rows": 0},
        {"max_rows": 10_001},
        {"max_result_bytes": 1023},
        {"max_result_bytes": 16 * 1024 * 1024 + 1},
        {"max_bytes_scanned": False},
        {"max_bytes_scanned": 0},
        {"poll_interval_seconds": 0.01},
        {"poll_interval_seconds": 5.01},
    ],
)
def test_query_limits_reject_unsafe_configuration(
    values: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        AthenaQueryLimits(**values)


@pytest.mark.parametrize("max_rows", [True, 0, -1, 6, "5"])
async def test_invalid_row_limit_is_rejected_before_role_assumption(
    max_rows: Any,
) -> None:
    factory = _ClientFactory(_AthenaClient())
    executor = AthenaExecutor(
        client_factory=factory,
        limits=AthenaQueryLimits(max_rows=5),
    )

    with pytest.raises(AthenaExecutionError) as raised:
        await _execute(executor, max_rows=max_rows)

    assert raised.value.code == "invalid_query_limit"
    assert factory.calls == []


async def test_datasource_owner_mismatch_is_rejected_before_role_assumption() -> None:
    factory = _ClientFactory(_AthenaClient())
    executor = AthenaExecutor(client_factory=factory)
    datasource = AthenaDatasource(
        datasource_id="warehouse",
        tenant_id="tenant-b",
        project_id="project-a",
        name="Analytics warehouse",
        role_arn=ROLE_ARN,
        region="us-east-1",
        catalog="AwsDataCatalog",
        database="analytics",
        workgroup="axon_read_only",
    )

    with pytest.raises(AthenaExecutionError) as raised:
        await _execute(executor, datasource=datasource)

    assert raised.value.code == "datasource_identity_mismatch"
    assert factory.calls == []


def _unsafe_workgroup(case: str) -> dict[str, Any]:
    value = _safe_workgroup(cutoff=4096)
    workgroup = value["WorkGroup"]
    configuration = workgroup["Configuration"]
    result_configuration = configuration["ResultConfiguration"]
    encryption = result_configuration["EncryptionConfiguration"]
    if case == "disabled":
        workgroup["State"] = "DISABLED"
    elif case == "not_enforced":
        configuration["EnforceWorkGroupConfiguration"] = False
    elif case == "metrics_disabled":
        configuration["PublishCloudWatchMetricsEnabled"] = False
    elif case == "invalid_output":
        result_configuration["OutputLocation"] = "s3://"
    elif case == "output_query":
        result_configuration["OutputLocation"] = "s3://bucket/path?version=1"
    elif case == "output_fragment":
        result_configuration["OutputLocation"] = "s3://bucket/path#fragment"
    elif case == "output_userinfo":
        result_configuration["OutputLocation"] = (
            "s3://user:password@bucket/path"
        )
    elif case == "unencrypted":
        encryption["EncryptionOption"] = "SSE_S3"
    elif case == "missing_kms":
        encryption["KmsKey"] = ""
    elif case == "excessive_cutoff":
        configuration["BytesScannedCutoffPerQuery"] = 4097
    elif case == "boolean_cutoff":
        configuration["BytesScannedCutoffPerQuery"] = True
    else:
        raise AssertionError(f"unknown workgroup case: {case}")
    return value


@pytest.mark.parametrize(
    "case",
    [
        "disabled",
        "not_enforced",
        "metrics_disabled",
        "invalid_output",
        "output_query",
        "output_fragment",
        "output_userinfo",
        "unencrypted",
        "missing_kms",
        "excessive_cutoff",
        "boolean_cutoff",
    ],
)
async def test_unsafe_workgroup_is_rejected_before_query_start(
    case: str,
) -> None:
    client = _AthenaClient(workgroup=_unsafe_workgroup(case))
    executor = AthenaExecutor(
        client_factory=_ClientFactory(client),
        limits=AthenaQueryLimits(max_bytes_scanned=4096),
    )

    with pytest.raises(AthenaExecutionError) as raised:
        await _execute(executor)

    assert raised.value.code == "athena_workgroup_unsafe"
    assert client.start_calls == []


async def test_scan_limit_stops_result_retrieval() -> None:
    client = _AthenaClient(
        workgroup=_safe_workgroup(cutoff=4096),
        executions=[_execution(scanned=4097)],
    )
    executor = AthenaExecutor(
        client_factory=_ClientFactory(client),
        limits=AthenaQueryLimits(max_bytes_scanned=4096),
    )

    with pytest.raises(AthenaExecutionError) as raised:
        await _execute(executor)

    assert raised.value.code == "athena_scan_limit_exceeded"
    assert client.result_calls == []


@pytest.mark.parametrize(
    "statistics",
    [
        {"DataScannedInBytes": -1, "EngineExecutionTimeInMillis": 1},
        {"DataScannedInBytes": True, "EngineExecutionTimeInMillis": 1},
        {"DataScannedInBytes": 1, "EngineExecutionTimeInMillis": "1"},
    ],
)
async def test_malformed_execution_statistics_fail_closed(
    statistics: dict[str, object],
) -> None:
    execution = _execution()
    execution["QueryExecution"]["Statistics"] = statistics
    client = _AthenaClient(executions=[execution])
    executor = AthenaExecutor(client_factory=_ClientFactory(client))

    with pytest.raises(AthenaExecutionError) as raised:
        await _execute(executor)

    assert raised.value.code == "athena_status_failed"
    assert client.result_calls == []


async def test_row_limit_truncates_without_returning_extra_row() -> None:
    client = _AthenaClient(
        result_pages=[
            _result_page(
                [
                    ["order_id", "note"],
                    ["order-1", "one"],
                    ["order-2", "two"],
                    ["order-3", "three"],
                ]
            )
        ]
    )
    executor = AthenaExecutor(
        client_factory=_ClientFactory(client),
        limits=AthenaQueryLimits(max_rows=5),
    )

    result = await _execute(executor, max_rows=2)

    assert result.rows == (
        ("order-1", "one"),
        ("order-2", "two"),
    )
    assert result.row_count == 2
    assert result.truncated is True


async def test_result_byte_limit_truncates_before_oversized_row() -> None:
    accepted = "a" * 900
    client = _AthenaClient(
        result_pages=[
            _result_page(
                [["value"], [accepted], ["b" * 200]],
                columns=[("value", "varchar")],
            )
        ]
    )
    executor = AthenaExecutor(
        client_factory=_ClientFactory(client),
        limits=AthenaQueryLimits(max_result_bytes=1024),
    )

    result = await _execute(executor)

    assert result.rows == ((accepted,),)
    assert result.result_bytes == len(
        json.dumps(
            {
                "columns": [
                    {"name": "value", "type": "varchar"}
                ],
                "rows": [[accepted]],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    )
    assert result.truncated is True


async def test_null_heavy_results_cannot_bypass_result_byte_limit() -> None:
    client = _AthenaClient(
        result_pages=[
            _result_page(
                [["value"], *([[None]] * 1000)],
                columns=[("value", "varchar")],
            )
        ]
    )
    executor = AthenaExecutor(
        client_factory=_ClientFactory(client),
        limits=AthenaQueryLimits(
            max_rows=1000,
            max_result_bytes=1024,
        ),
    )

    result = await _execute(executor)

    assert 0 < result.row_count < 1000
    assert result.result_bytes <= 1024
    assert result.truncated is True


@pytest.mark.parametrize(
    "page",
    [
        {
            "ResultSet": {
                "ResultSetMetadata": {"ColumnInfo": ["not-an-object"]},
                "Rows": [],
            }
        },
        _result_page(
            [["order_id", "note", "extra"]],
        ),
        _result_page(
            [["order_id", "note"]],
            next_token="",
        ),
        {"ResultSet": {"ResultSetMetadata": {}, "Rows": "not-a-list"}},
        {
            "ResultSet": {
                "ResultSetMetadata": {
                    "ColumnInfo": [
                        {"Name": "value", "Type": "varchar"}
                    ]
                },
                "Rows": [{"Data": [{"VarCharValue": 123}]}],
            }
        },
    ],
)
async def test_malformed_result_pages_fail_closed(
    page: dict[str, Any],
) -> None:
    client = _AthenaClient(result_pages=[page])
    executor = AthenaExecutor(client_factory=_ClientFactory(client))

    with pytest.raises(AthenaExecutionError) as raised:
        await _execute(executor)

    assert raised.value.code == "athena_results_failed"


async def test_repeated_pagination_token_fails_closed() -> None:
    client = _AthenaClient(
        result_pages=[
            _result_page(
                [["order_id", "note"]],
                next_token="same-token",
            ),
            _result_page([], next_token="same-token"),
        ]
    )
    executor = AthenaExecutor(client_factory=_ClientFactory(client))

    with pytest.raises(AthenaExecutionError) as raised:
        await _execute(executor)

    assert raised.value.code == "athena_results_failed"


async def test_timeout_cancels_nonterminal_query() -> None:
    client = _AthenaClient(executions=[_execution(state="RUNNING")])
    executor = AthenaExecutor(
        client_factory=_ClientFactory(client),
        limits=AthenaQueryLimits(
            timeout_seconds=0.005,
            poll_interval_seconds=0.05,
        ),
    )

    with pytest.raises(AthenaExecutionError) as raised:
        await _execute(executor)

    assert raised.value.code == "athena_query_timeout"
    assert client.stop_calls == [
        {"QueryExecutionId": "execution-123"}
    ]


async def test_deadline_also_bounds_result_retrieval() -> None:
    class _SlowResultClient(_AthenaClient):
        def get_query_results(self, **kwargs: Any) -> dict[str, Any]:
            time.sleep(0.05)
            return super().get_query_results(**kwargs)

    client = _SlowResultClient()
    executor = AthenaExecutor(
        client_factory=_ClientFactory(client),
        limits=AthenaQueryLimits(
            timeout_seconds=0.005,
            poll_interval_seconds=0.05,
        ),
    )

    with pytest.raises(AthenaExecutionError) as raised:
        await _execute(executor)

    assert raised.value.code == "athena_query_timeout"
    assert client.stop_calls == []


async def test_readiness_returns_false_for_role_or_workgroup_failure() -> None:
    datasource = _datasource()
    unavailable = AthenaExecutor(
        client_factory=_ClientFactory(
            _AthenaClient(),
            error=RuntimeError("STS unavailable"),
        )
    )
    unsafe = AthenaExecutor(
        client_factory=_ClientFactory(
            _AthenaClient(workgroup=_unsafe_workgroup("unencrypted"))
        )
    )

    for executor in (unavailable, unsafe):
        assert (
            await executor.check_ready(
                datasource,
                tenant_id="tenant-a",
                project_id="project-a",
                principal_id="principal:analyst",
                request_id="readiness-123",
            )
            is False
        )


class _StsClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def assume_role(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "Credentials": {
                "AccessKeyId": "temporary-access-key",
                "SecretAccessKey": "temporary-secret",
                "SessionToken": "temporary-token",
            }
        }


class _BaseSession:
    def __init__(self, sts: _StsClient) -> None:
        self.sts = sts

    def client(self, service: str, **kwargs: Any) -> _StsClient:
        assert service == "sts"
        assert kwargs == {
            "region_name": "us-east-1",
            "config": AWS_CLIENT_CONFIG,
        }
        return self.sts


class _AssumedSession:
    def __init__(
        self,
        athena_client: object,
        calls: list[dict[str, Any]],
        **kwargs: Any,
    ) -> None:
        self.athena_client = athena_client
        calls.append(kwargs)

    def client(self, service: str, **kwargs: Any) -> object:
        assert service == "athena"
        assert kwargs == {
            "region_name": "us-east-1",
            "config": AWS_CLIENT_CONFIG,
        }
        return self.athena_client


def test_role_session_and_tags_are_bounded_and_principal_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sts = _StsClient()
    athena_client = object()
    session_calls: list[dict[str, Any]] = []
    principal_id = "principal:\n" + ("sensitive-user@example.test" * 20)

    def session_factory(**kwargs: Any) -> _AssumedSession:
        return _AssumedSession(
            athena_client,
            session_calls,
            **kwargs,
        )

    monkeypatch.setattr(boto3, "Session", session_factory)
    factory = BotoAthenaClientFactory(_BaseSession(sts))

    result = factory(
        _datasource(),
        tenant_id="tenant-a",
        project_id="project-a",
        principal_id=principal_id,
        request_id="request-123",
    )

    assert result is athena_client
    assert len(sts.calls) == 1
    assume_role = sts.calls[0]
    assert assume_role["RoleArn"] == ROLE_ARN
    assert assume_role["DurationSeconds"] == 900
    assert re.fullmatch(
        r"axon-query-[0-9a-f]{32}",
        assume_role["RoleSessionName"],
    )
    assert assume_role["SourceIdentity"] == assume_role["RoleSessionName"]
    tags = {
        tag["Key"]: tag["Value"]
        for tag in assume_role["Tags"]
    }
    assert tags["AxonTenant"] == "tenant-a"
    assert tags["AxonProject"] == "project-a"
    assert tags["AxonPrincipal"] == hashlib.sha256(
        principal_id.encode()
    ).hexdigest()
    assert principal_id not in repr(assume_role)
    assert all(
        len(value) <= 256
        and not any(ord(character) < 32 for character in value)
        for value in tags.values()
    )
    assert session_calls == [
        {
            "aws_access_key_id": "temporary-access-key",
            "aws_secret_access_key": "temporary-secret",
            "aws_session_token": "temporary-token",
            "region_name": "us-east-1",
        }
    ]
