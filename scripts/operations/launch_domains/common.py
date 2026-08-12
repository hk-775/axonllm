"""Shared fenced-ledger and runtime transport for launch domains."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

import launch_activity_domains as framework
import launch_activity_worker as worker
from src.gateway.agentcore.schemas import REHEARSAL_SCHEMA
from src.gateway.rehearsal_control import (
    TABLE_ENV,
    RehearsalBinding,
    RehearsalControlLedger,
    RehearsalEvidenceUnavailable,
)


CONTROL_TABLE_ENV = TABLE_ENV
IDENTITY_SECRET_ENV = "AXON_LAUNCH_REHEARSAL_IDENTITY_SECRET_ARN"
MAX_RUNTIME_RESPONSE_BYTES = 2 * 1024 * 1024
RUNTIME_TIMEOUT_SECONDS = 30.0
AWS_TIMEOUT_SECONDS = 8.0

_TABLE_ARN = re.compile(
    r"^arn:aws:dynamodb:(?P<region>[a-z0-9-]+):"
    r"(?P<account>[0-9]{12}):table/"
    r"axonllm-rehearsal-control-ledger$"
)
_SECRET_ARN = re.compile(
    r"^arn:aws:secretsmanager:(?P<region>[a-z0-9-]+):"
    r"(?P<account>[0-9]{12}):secret:"
    r"axonllm/launch/runtime-identity(?:-[A-Za-z0-9]{6})?$"
)
_RUNTIME_ARN = re.compile(
    r"^arn:aws:bedrock-agentcore:(?P<region>[a-z0-9-]+):"
    r"(?P<account>[0-9]{12}):runtime/"
    r"(?P<runtime>[A-Za-z][A-Za-z0-9_]{0,99}-[A-Za-z0-9]{10})$"
)
_ENDPOINT_ARN = re.compile(
    r"^arn:aws:bedrock-agentcore:(?P<region>[a-z0-9-]+):"
    r"(?P<account>[0-9]{12}):runtime/"
    r"(?P<runtime>[A-Za-z][A-Za-z0-9_]{0,99}-[A-Za-z0-9]{10})/"
    r"runtime-endpoint/(?P<endpoint>[A-Za-z0-9_-]{1,128})$"
)


class _ConditionalFailure(RuntimeError):
    def __init__(self) -> None:
        self.response = {
            "Error": {
                "Code": "ConditionalCheckFailedException",
            }
        }
        super().__init__("conditional write failed")


class TransportTable:
    """DynamoDB resource-style adapter over the worker's bounded transport."""

    def __init__(
        self,
        *,
        aws: worker.AwsTransport,
        region: str,
        table_arn: str,
    ) -> None:
        self.aws = aws
        self.region = region
        self.table_arn = table_arn
        self.serializer = TypeSerializer()
        self.deserializer = TypeDeserializer()

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        key = kwargs.get("Key")
        if type(key) is not dict or kwargs.get("ConsistentRead") is not True:
            raise worker.HandlerContractError from None
        response = self.aws.call(
            "dynamodb",
            "get_item",
            region=self.region,
            parameters={
                "TableName": self.table_arn,
                "Key": {name: self.serializer.serialize(value) for name, value in key.items()},
                "ConsistentRead": True,
            },
            timeout_seconds=AWS_TIMEOUT_SECONDS,
        )
        item = response.get("Item")
        if item is None:
            return {}
        if type(item) is not dict:
            raise worker.AwsTransportError(
                "dynamodb",
                "get_item",
                "InvalidResponse",
            )
        return {"Item": {name: self.deserializer.deserialize(value) for name, value in item.items()}}

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        item = kwargs.get("Item")
        condition = kwargs.get("ConditionExpression")
        names = kwargs.get("ExpressionAttributeNames")
        values = kwargs.get("ExpressionAttributeValues")
        if (
            type(item) is not dict
            or not isinstance(condition, str)
            or type(names) is not dict
            or (values is not None and type(values) is not dict)
        ):
            raise worker.HandlerContractError from None
        parameters: dict[str, Any] = {
            "TableName": self.table_arn,
            "Item": {name: self.serializer.serialize(value) for name, value in item.items()},
            "ConditionExpression": condition,
            "ExpressionAttributeNames": dict(names),
        }
        if values is not None:
            parameters["ExpressionAttributeValues"] = {
                name: self.serializer.serialize(value) for name, value in values.items()
            }
        try:
            self.aws.call(
                "dynamodb",
                "put_item",
                region=self.region,
                parameters=parameters,
                timeout_seconds=AWS_TIMEOUT_SECONDS,
            )
        except worker.AwsTransportError as exc:
            if exc.aws_code == "ConditionalCheckFailedException":
                raise _ConditionalFailure from None
            raise
        return {}


@dataclass(frozen=True)
class RuntimeObservation:
    status_code: int | None
    body: Mapping[str, Any] | None
    transport_error: bool = False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def _strict_object(raw: bytes) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    return value if type(value) is dict else None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError
        result[name] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError


def _read_bounded(response: Any) -> bytes:
    value = response.read(MAX_RUNTIME_RESPONSE_BYTES + 1)
    if len(value) > MAX_RUNTIME_RESPONSE_BYTES:
        raise ValueError
    return value


def _valid_bearer_token(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        return False
    return 0 < len(encoded) <= 16_384 and all(0x21 <= byte <= 0x7E for byte in encoded)


class LaunchSession:
    """One action's exact ledger binding and finite-time runtime client."""

    def __init__(
        self,
        *,
        task: worker.ActionTask,
        context: worker.HandlerContext,
        environ: Mapping[str, str] | None = None,
        opener: Any | None = None,
        now: Any | None = None,
        control_gate: str | None = None,
        fence_token: int | None = None,
    ) -> None:
        environment = os.environ if environ is None else environ
        table_arn = environment.get(CONTROL_TABLE_ENV, "")
        secret_arn = environment.get(IDENTITY_SECRET_ENV, "")
        table_match = _TABLE_ARN.fullmatch(table_arn)
        secret_match = _SECRET_ARN.fullmatch(secret_arn)
        release = task.payload.get("release")
        parameters = task.payload.get("parameters")
        binding = task.payload.get("binding")
        if (
            table_match is None
            or secret_match is None
            or table_match.group("region") != context.region
            or secret_match.group("region") != context.region
            or table_match.group("account") != secret_match.group("account")
            or type(release) is not dict
            or type(parameters) is not dict
            or type(binding) is not dict
            or not isinstance(release.get("commit"), str)
        ):
            raise worker.ConfigurationError from None
        tenant_id = binding.get("tenantId")
        project_id = binding.get("projectId")
        if "tenantId" in parameters and parameters["tenantId"] != tenant_id:
            raise worker.HandlerContractError from None
        if "projectId" in parameters and parameters["projectId"] != project_id:
            raise worker.HandlerContractError from None
        if not isinstance(tenant_id, str) or not isinstance(project_id, str):
            raise worker.HandlerContractError from None
        effective_fence = context.fence_token if fence_token is None else fence_token
        gate = task.gate if control_gate is None else control_gate
        if not isinstance(gate, str) or worker.SAFE_ID.fullmatch(gate) is None or effective_fence is None:
            raise worker.HandlerContractError from None
        control_correlation_id = hashlib.sha256(f"{task.owner_id}:{gate}:runtime-control".encode("ascii")).hexdigest()[
            :32
        ]
        self.task = task
        self.context = context
        self.secret_arn = secret_arn
        self.binding_payload = binding
        self.binding = RehearsalBinding(
            tenant_id=tenant_id,
            project_id=project_id,
            correlation_id=control_correlation_id,
            owner_id=task.owner_id,
            release_commit=release["commit"],
            fence_token=effective_fence,
            expires_at_epoch=int(task.expires_at.timestamp()),
        )
        table = TransportTable(
            aws=context.aws,
            region=context.region,
            table_arn=table_arn,
        )
        self.ledger = RehearsalControlLedger(
            table=table,
            environ={CONTROL_TABLE_ENV: table_arn},
            now=now,
        )
        self._opener = opener or urllib.request.build_opener(_NoRedirect())

    def claim(self) -> int:
        revision = self.ledger.claim(self.binding)
        if revision is None:
            raise worker.DomainTaskFailure(
                "RehearsalControlUnavailable",
                retryable=True,
            )
        return revision

    def write_control(
        self,
        *,
        control_type: str,
        name: str,
        parameters: Mapping[str, Any],
        active: bool,
    ) -> int:
        revision = self.claim()
        updated = self.ledger.write_control(
            self.binding,
            control_type=control_type,
            name=name,
            parameters=parameters,
            active=active,
            expected_revision=revision,
        )
        if updated is None:
            raise worker.DomainTaskFailure(
                "RehearsalControlUnavailable",
                retryable=True,
            )
        return updated

    def observations(
        self,
        *required_kinds: str,
    ) -> tuple[Any, ...]:
        try:
            return self.ledger.collect_observations(
                self.binding,
                required_kinds=required_kinds,
            )
        except RehearsalEvidenceUnavailable as exc:
            raise worker.DomainTaskFailure(
                "RehearsalEvidenceUnavailable",
                retryable=True,
            ) from exc

    def _token(self) -> str:
        response = self.context.aws.call(
            "secretsmanager",
            "get_secret_value",
            region=self.context.region,
            parameters={
                "SecretId": self.secret_arn,
                "VersionStage": "AWSCURRENT",
            },
            timeout_seconds=AWS_TIMEOUT_SECONDS,
        )
        raw = response.get("SecretString")
        if raw is None:
            encoded = response.get("SecretBinary")
            if not isinstance(encoded, (str, bytes)):
                raise worker.DomainTaskFailure("RuntimeIdentityUnavailable", retryable=True)
            try:
                raw = base64.b64decode(encoded, validate=True).decode("utf-8")
            except (ValueError, UnicodeDecodeError) as exc:
                raise worker.DomainTaskFailure("RuntimeIdentityUnavailable", retryable=True) from exc
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > 32 * 1024:
            raise worker.DomainTaskFailure("RuntimeIdentityUnavailable", retryable=True)
        value = _strict_object(raw.encode("utf-8"))
        if value is None or set(value) != {"token", "expiresAtEpoch"}:
            raise worker.DomainTaskFailure("RuntimeIdentityUnavailable", retryable=True)
        token = value["token"]
        expires = value["expiresAtEpoch"]
        now_epoch = int(datetime.now(timezone.utc).timestamp())
        if (
            not _valid_bearer_token(token)
            or isinstance(expires, bool)
            or not isinstance(expires, int)
            or expires <= now_epoch + 60
            or expires > self.binding.expires_at_epoch
        ):
            raise worker.DomainTaskFailure("RuntimeIdentityUnavailable", retryable=True)
        return token

    def _runtime_url(self) -> str:
        runtime_arn = self.binding_payload.get("runtimeArn")
        endpoint_arn = self.binding_payload.get("runtimeEndpointArn")
        runtime = _RUNTIME_ARN.fullmatch(runtime_arn) if isinstance(runtime_arn, str) else None
        endpoint = _ENDPOINT_ARN.fullmatch(endpoint_arn) if isinstance(endpoint_arn, str) else None
        if (
            runtime is None
            or endpoint is None
            or runtime.group("region") != self.context.region
            or endpoint.group("region") != self.context.region
            or runtime.group("account") != endpoint.group("account")
            or runtime.group("runtime") != endpoint.group("runtime")
        ):
            raise worker.HandlerContractError from None
        encoded = urllib.parse.quote(runtime_arn, safe="")
        query = urllib.parse.urlencode({"qualifier": endpoint.group("endpoint")})
        return f"https://bedrock-agentcore.{self.context.region}.amazonaws.com/runtimes/{encoded}/invocations?{query}"

    def invoke(
        self,
        payload: Mapping[str, Any],
        *,
        operation: str,
        routing_strategy: str | None = None,
        dependency: str | None = None,
        timeout_seconds: float = RUNTIME_TIMEOUT_SECONDS,
    ) -> RuntimeObservation:
        self.context.cancellation.raise_if_cancelled()
        rehearsal: dict[str, Any] = {
            "schema": REHEARSAL_SCHEMA,
            "correlation_id": self.binding.correlation_id,
            "owner_id": self.binding.owner_id,
            "release_commit": self.binding.release_commit,
            "fence_token": self.binding.fence_token,
            "expires_at_epoch": self.binding.expires_at_epoch,
            "operation": operation,
        }
        if routing_strategy is not None:
            rehearsal["routing_strategy"] = routing_strategy
        if dependency is not None:
            rehearsal["dependency"] = dependency
        request_payload = dict(payload)
        request_payload["rehearsal"] = rehearsal
        encoded = json.dumps(
            request_payload,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            self._runtime_url(),
            data=encoded,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._token()}",
                "Content-Type": "application/json",
                "User-Agent": "axonllm-launch-rehearsal/1",
                "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": (f"axonllm-launch-{uuid.uuid4().hex}"),
            },
            method="POST",
        )
        started = time.monotonic()
        try:
            with self._opener.open(
                request,
                timeout=timeout_seconds,
            ) as response:
                raw = _read_bounded(response)
                status = response.getcode()
        except urllib.error.HTTPError as exc:
            try:
                raw = _read_bounded(exc)
            except ValueError:
                raw = b""
            finally:
                exc.close()
            status = exc.code
        except Exception:
            if time.monotonic() - started > timeout_seconds + 5:
                raise worker.DomainTaskFailure(
                    "RuntimeInvocationTimedOut",
                    retryable=True,
                ) from None
            return RuntimeObservation(
                status_code=None,
                body=None,
                transport_error=True,
            )
        self.context.cancellation.raise_if_cancelled()
        return RuntimeObservation(
            status_code=status,
            body=_strict_object(raw),
        )


def copied_ownership(
    value: Mapping[str, worker.JsonValue],
) -> dict[str, worker.JsonValue]:
    return json.loads(
        json.dumps(
            dict(value),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def owned_id(task: worker.ActionTask, suffix: str) -> str:
    value = f"{task.owner_id}:{suffix}"
    if worker.SAFE_ID.fullmatch(value) is None:
        raise worker.HandlerContractError from None
    return value


def completed_state(
    state: Mapping[str, worker.JsonValue],
    *,
    operations: tuple[str, ...],
    operation: str,
    extra: Mapping[str, worker.JsonValue] | None = None,
) -> dict[str, worker.JsonValue]:
    value = dict(state)
    completed = value.get("completed", [])
    if type(completed) is not list or completed != list(operations[: len(completed)]):
        raise worker.HandlerContractError from None
    index = len(completed)
    if index >= len(operations) or operations[index] != operation:
        raise worker.DomainTaskFailure("DomainActionOutOfOrder")
    value["completed"] = [*completed, operation]
    if extra is not None:
        value.update(extra)
    return value


def empty_cleanup(
    *,
    state: Mapping[str, worker.JsonValue],
    ownership: Mapping[str, worker.JsonValue],
    primary_state_selected: bool | None = None,
    production_endpoint_status: str | None = None,
) -> framework.DomainCleanupResult:
    snapshots = [item["ref"] for item in ownership["snapshots"].values() if isinstance(item, Mapping)]
    return framework.DomainCleanupResult(
        state=dict(state),
        ownership={
            "faultIds": [],
            "fixtureIds": [],
            "dlqCorrelationIds": [],
            "snapshots": {"model": None, "tenantConfig": None},
        },
        verified_complete=True,
        restored_snapshot_refs=sorted(snapshots),
        cleared_fault_ids=list(ownership["faultIds"]),
        cleared_fixture_ids=list(ownership["fixtureIds"]),
        removed_dlq_correlation_ids=list(ownership["dlqCorrelationIds"]),
        primary_state_selected=primary_state_selected,
        production_endpoint_status=production_endpoint_status,
    )
