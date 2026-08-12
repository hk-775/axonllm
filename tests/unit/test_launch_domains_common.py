"""Contracts for the launch-domain runtime transport."""

from __future__ import annotations

import hashlib
import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "operations"))

import launch_activity_worker as worker
from launch_domains import common


REGION = "us-east-1"
ACCOUNT = "123456789012"
OWNER = "a" * 64
TABLE_ARN = f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/axonllm-rehearsal-control-ledger"
SECRET_ARN = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:axonllm/launch/runtime-identity-Ab12Cd"
RUNTIME_NAME = "AxonLLMRuntime-abcdefghij"


class FakeAws:
    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.response = response or {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def call(
        self,
        service: str,
        operation: str,
        *,
        region: str,
        parameters: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        assert region == REGION
        assert timeout_seconds == common.AWS_TIMEOUT_SECONDS
        self.calls.append((service, operation, dict(parameters)))
        return self.response


def _task(*, expires_at: datetime) -> worker.ActionTask:
    operation = "verify-control-plane-recovery"
    gate = worker.ACTION_TO_GATE[operation]
    return worker.ActionTask(
        payload={
            "owner": {
                "id": OWNER,
                "expiresAt": expires_at.isoformat(timespec="seconds"),
            },
            "release": {"commit": "b" * 40},
            "binding": {
                "tenantId": "tenant-a",
                "projectId": "project-a",
                "runtimeArn": (f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/{RUNTIME_NAME}"),
                "runtimeEndpointArn": (
                    f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/{RUNTIME_NAME}/runtime-endpoint/production"
                ),
            },
            "parameters": {
                "tenantId": "tenant-a",
                "projectId": "project-a",
                "dependency": "dynamodb",
                "faultTtlSeconds": 60,
            },
        },
        gate=gate,
        operation=operation,
        owner_id=OWNER,
        correlation_id=hashlib.sha256(f"{OWNER}:{gate}:{operation}".encode("ascii")).hexdigest()[:32],
        idempotency_key="c" * 64,
        expires_at=expires_at,
        fence_token=7,
        request_sha256="d" * 64,
    )


def _session(
    aws: FakeAws,
    *,
    environment: dict[str, str] | None = None,
) -> common.LaunchSession:
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    return common.LaunchSession(
        task=_task(expires_at=expires_at),
        context=worker.HandlerContext(
            aws=aws,
            region=REGION,
            state_store=SimpleNamespace(),
            owner_state=None,
            cancellation=worker.CancellationToken(threading.Event()),
            fence_token=7,
        ),
        environ=environment
        or {
            common.CONTROL_TABLE_ENV: TABLE_ARN,
            common.IDENTITY_SECRET_ENV: SECRET_ARN,
        },
    )


def test_runtime_identity_is_read_at_use_time_and_accepts_header_safe_token() -> None:
    expires = int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp())
    token = "opaque:credential!with=padding=="
    aws = FakeAws(
        {
            "SecretString": json.dumps(
                {
                    "token": token,
                    "expiresAtEpoch": expires,
                }
            )
        }
    )
    session = _session(aws)

    assert session._token() == token
    assert aws.calls == [
        (
            "secretsmanager",
            "get_secret_value",
            {
                "SecretId": SECRET_ARN,
                "VersionStage": "AWSCURRENT",
            },
        )
    ]


@pytest.mark.parametrize(
    "token",
    [
        "",
        "contains space",
        "line\r\ninjection",
        "non-ascii-\N{SNOWMAN}",
        "x" * 16_385,
    ],
)
def test_bearer_token_rejects_unsafe_header_values(token: str) -> None:
    assert common._valid_bearer_token(token) is False


@pytest.mark.parametrize(
    ("table_arn", "secret_arn"),
    [
        (
            TABLE_ARN.replace(ACCOUNT, "999999999999"),
            SECRET_ARN,
        ),
        (
            TABLE_ARN,
            SECRET_ARN.replace(REGION, "us-west-2"),
        ),
        (
            TABLE_ARN,
            SECRET_ARN.replace(
                "axonllm/launch/runtime-identity",
                "axonllm/foreign/runtime-identity",
            ),
        ),
    ],
)
def test_session_rejects_cross_boundary_or_foreign_resources(
    table_arn: str,
    secret_arn: str,
) -> None:
    with pytest.raises(worker.ConfigurationError):
        _session(
            FakeAws(),
            environment={
                common.CONTROL_TABLE_ENV: table_arn,
                common.IDENTITY_SECRET_ENV: secret_arn,
            },
        )
