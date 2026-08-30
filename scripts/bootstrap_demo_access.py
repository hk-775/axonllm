#!/usr/bin/env python3
"""Mint tenant-admin demo personas without printing credential material.

Fargate stores the persona document in Secrets Manager. Local demo startup can
write the same document to a mode-0600 file mounted outside the container.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from src.gateway.auth.api_key_service import APIKeyService
from src.gateway.config_loader import load_demo_seed_config
from src.gateway.models import TenantRole
from src.gateway.persistence import DynamoPersistence

PERSONA_SCHEMA = "axonllm.demo-personas/v1"


@dataclass(frozen=True)
class DemoPersona:
    tenant_id: str
    project_id: str
    label: str

    @property
    def key_name(self) -> str:
        return f"demo-tenant-admin-{self.tenant_id}"


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
    document: dict[str, Any],
) -> str:
    secret_string = json.dumps(document, separators=(",", ":"))
    try:
        response = client.create_secret(
            Name=secret_name,
            Description="AxonLLM disposable multi-tenant demo personas",
            SecretString=secret_string,
            Tags=[
                {"Key": "Application", "Value": "AxonLLM"},
                {"Key": "Environment", "Value": "demo"},
                {
                    "Key": "Purpose",
                    "Value": "multi-tenant-dashboard-and-codex-access",
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


def _store_local_file(
    path: Path,
    *,
    document: dict[str, Any],
) -> str:
    target = path.expanduser().resolve()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        dir=target.parent,
        text=True,
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, target)
        target.chmod(0o600)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    return str(target)


def _personas_from_seed(path: str) -> list[DemoPersona]:
    seed = load_demo_seed_config(path)
    personas: list[DemoPersona] = []
    seen_tenants: set[str] = set()
    for tenant in seed.tenants:
        tenant_id = tenant.get("tenant_id")
        project_id = tenant.get("admin_project_id")
        label = tenant.get("persona_label") or tenant.get("display_name")
        if not all(
            isinstance(value, str) and value.strip()
            for value in (tenant_id, project_id, label)
        ):
            raise ValueError(
                "each demo tenant requires tenant_id, admin_project_id, "
                "and persona_label or display_name"
            )
        if tenant_id in seen_tenants:
            raise ValueError(
                f"duplicate demo tenant persona: {tenant_id}"
            )
        seen_tenants.add(tenant_id)
        personas.append(
            DemoPersona(
                tenant_id=tenant_id,
                project_id=project_id,
                label=label,
            )
        )
    if len(personas) < 2:
        raise ValueError(
            "the multi-tenant demo requires at least two personas"
        )
    return personas


async def _provision(
    *,
    persistence: DynamoPersistence,
    personas: list[DemoPersona],
    store_document,
) -> dict[str, Any]:
    service = APIKeyService(persistence=persistence)
    previous_by_persona: dict[DemoPersona, list] = {}
    issued: list[tuple[DemoPersona, Any, str]] = []

    try:
        for persona in personas:
            project = await persistence.get_project(
                persona.project_id,
                persona.tenant_id,
            )
            if project is None:
                raise RuntimeError(
                    "demo project is not seeded for "
                    f"{persona.tenant_id}/{persona.project_id}"
                )
            previous_by_persona[persona] = [
                key
                for key in await service.list_keys(
                    persona.project_id,
                    persona.tenant_id,
                )
                if key.name == persona.key_name and not key.revoked
            ]
            record, raw_key = await service.issue_key(
                project_id=persona.project_id,
                name=persona.key_name,
                scopes=[],
                created_by="demo-bootstrap",
                tenant_id=persona.tenant_id,
                principal_role=TenantRole.TENANT_ADMIN,
            )
            issued.append((persona, record, raw_key))
    except Exception:
        for persona, record, _raw_key in issued:
            await service.revoke_key(
                record.key_id,
                persona.tenant_id,
                revoked_by="demo-bootstrap-rollback",
            )
        raise

    document = {
        "schema": PERSONA_SCHEMA,
        "personas": [
            {
                "label": persona.label,
                "tenant_id": persona.tenant_id,
                "project_id": persona.project_id,
                "api_key": raw_key,
            }
            for persona, _record, raw_key in issued
        ],
    }

    try:
        location = store_document(document)
    except Exception:
        for persona, record, _raw_key in issued:
            await service.revoke_key(
                record.key_id,
                persona.tenant_id,
                revoked_by="demo-bootstrap-rollback",
            )
        raise
    finally:
        document = {}

    revoked = 0
    for persona, keys in previous_by_persona.items():
        for key in keys:
            if await service.revoke_key(
                key.key_id,
                persona.tenant_id,
                revoked_by="demo-bootstrap-rotation",
            ):
                revoked += 1

    return {
        "persona_count": len(issued),
        "personas": [
            {
                "key_id": record.key_id,
                "label": persona.label,
                "tenant_id": persona.tenant_id,
                "project_id": persona.project_id,
            }
            for persona, record, _raw_key in issued
        ],
        "revoked_previous_keys": revoked,
        "location": location,
        "schema": PERSONA_SCHEMA,
        "status": "ready",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--table", required=True)
    parser.add_argument(
        "--secret-name",
        default="axonllm/demo/access",
    )
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument(
        "--seed-config",
        default="config/demo_seed_multitenant.yaml",
    )
    parser.add_argument("--endpoint-url")
    parser.add_argument("--local-output", type=Path)
    args = parser.parse_args()

    os.environ["LLM_ROUTER_DYNAMODB_ENABLED"] = "true"
    os.environ["AXON_DYNAMODB_TABLE"] = args.table
    os.environ["AWS_DEFAULT_REGION"] = args.region
    if args.endpoint_url:
        os.environ["AXON_DEPLOYMENT_PROFILE"] = "development"
        os.environ["AXON_DYNAMODB_ENDPOINT_URL"] = args.endpoint_url

    try:
        personas = _personas_from_seed(args.seed_config)
        if args.local_output is not None:
            def store_document(document: dict) -> str:
                return _store_local_file(
                    args.local_output,
                    document=document,
                )

            location_type = "local_file"
        else:
            client = _secret_client(args.region)

            def store_document(document: dict) -> str:
                return _store_secret(
                    client,
                    secret_name=args.secret_name,
                    document=document,
                )

            location_type = "secrets_manager"

        result = asyncio.run(
            _provision(
                persistence=DynamoPersistence(region=args.region),
                personas=personas,
                store_document=store_document,
            )
        )
        result["location_type"] = location_type
    except (ClientError, RuntimeError, ValueError, OSError) as exc:
        print(f"Demo access bootstrap failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
