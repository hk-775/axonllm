#!/usr/bin/env python3
"""Mint a protected demo key and store its one-time value in Secrets Manager.

The raw AxonLLM key is never printed. Operators and automated checks should
resolve the ``api_key`` JSON field at runtime through ``asm-exec``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from src.gateway.auth.api_key_service import APIKeyService
from src.gateway.persistence import DynamoPersistence


def _secret_client(region: str):
    session = boto3.Session(region_name=region)
    return session.client(
        "secretsmanager",
        config=Config(
            retries={"total_max_attempts": 4, "mode": "adaptive"},
            connect_timeout=5,
            read_timeout=15,
        ),
    )


def _store_secret(
    client: Any,
    *,
    secret_name: str,
    raw_key: str,
) -> str:
    secret_string = json.dumps(
        {"api_key": raw_key},
        separators=(",", ":"),
    )
    try:
        response = client.create_secret(
            Name=secret_name,
            Description=(
                "AxonLLM disposable Fargate demo access credential"
            ),
            SecretString=secret_string,
            Tags=[
                {"Key": "Application", "Value": "AxonLLM"},
                {"Key": "Environment", "Value": "demo"},
                {
                    "Key": "Purpose",
                    "Value": "dashboard-and-codex-access",
                },
            ],
        )
        return str(response["ARN"])
    except client.exceptions.ResourceExistsException:
        client.put_secret_value(
            SecretId=secret_name,
            SecretString=secret_string,
        )
        return str(client.describe_secret(SecretId=secret_name)["ARN"])


async def _provision(
    *,
    persistence: DynamoPersistence,
    secret_client: Any,
    secret_name: str,
    project: str,
    key_name: str,
    scopes: list[str],
) -> dict[str, Any]:
    service = APIKeyService(persistence=persistence)
    previous = [
        key
        for key in await service.list_keys(project)
        if key.name == key_name and not key.revoked
    ]

    record, raw_key = await service.issue_key(
        project_id=project,
        name=key_name,
        scopes=scopes,
        created_by="demo-bootstrap",
    )
    try:
        secret_arn = _store_secret(
            secret_client,
            secret_name=secret_name,
            raw_key=raw_key,
        )
    except Exception:
        await service.revoke_key(
            record.key_id,
            revoked_by="demo-bootstrap-rollback",
        )
        raise
    finally:
        raw_key = ""

    revoked = 0
    for key in previous:
        if await service.revoke_key(
            key.key_id,
            revoked_by="demo-bootstrap-rotation",
        ):
            revoked += 1

    return {
        "key_id": record.key_id,
        "project": project,
        "revoked_previous_keys": revoked,
        "secret_arn": secret_arn,
        "secret_json_key": "api_key",
        "status": "ready",
    }


def _parse_scopes(value: str) -> list[str]:
    scopes = [scope.strip() for scope in value.split(",") if scope.strip()]
    if not scopes:
        raise ValueError("at least one scope is required")
    return scopes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--table", required=True)
    parser.add_argument(
        "--secret-name",
        default="axonllm/demo/access",
    )
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--project", default="proj-alpha")
    parser.add_argument("--key-name", default="fargate-demo-access")
    parser.add_argument("--scopes", default="chat,admin:*")
    args = parser.parse_args()

    os.environ["LLM_ROUTER_DYNAMODB_ENABLED"] = "true"
    os.environ["AXON_DYNAMODB_TABLE"] = args.table
    os.environ["AWS_DEFAULT_REGION"] = args.region

    try:
        result = asyncio.run(
            _provision(
                persistence=DynamoPersistence(region=args.region),
                secret_client=_secret_client(args.region),
                secret_name=args.secret_name,
                project=args.project,
                key_name=args.key_name,
                scopes=_parse_scopes(args.scopes),
            )
        )
    except (ClientError, RuntimeError, ValueError) as exc:
        print(f"Demo access bootstrap failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
