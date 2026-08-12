#!/usr/bin/env python3
"""Manage Cognito users and canonical AxonLLM authority."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
from typing import Iterator


if sys.version_info < (3, 11):
    print(
        "managed identity operations require Python 3.11 or newer; "
        "run with `uv run python`",
        file=sys.stderr,
    )
    raise SystemExit(2)


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.gateway.auth.managed_identity_lifecycle import (
    MANAGED_TENANT_ROLES,
    ManagedIdentityError,
    bootstrap_platform_operator,
    disable_managed_tenant_user,
    invite_managed_tenant_user,
    update_managed_tenant_user,
)
from src.gateway.persistence import DynamoPersistence


@contextmanager
def _dynamo_environment() -> Iterator[None]:
    previous = os.environ.get("LLM_ROUTER_DYNAMODB_ENABLED")
    os.environ["LLM_ROUTER_DYNAMODB_ENABLED"] = "true"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("LLM_ROUTER_DYNAMODB_ENABLED", None)
        else:
            os.environ["LLM_ROUTER_DYNAMODB_ENABLED"] = previous


def _common_user_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--user-name", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--display-name", default="")
    parser.add_argument("--tenant", required=True)
    parser.add_argument(
        "--role",
        required=True,
        choices=sorted(role.value for role in MANAGED_TENANT_ROLES),
    )
    parser.add_argument(
        "--project",
        required=True,
        action="append",
        dest="projects",
        help="Canonical project grant; repeat for multiple projects",
    )
    parser.add_argument(
        "--default-project",
        required=True,
        help="Project emitted as the Cognito project hint",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Coordinate managed Cognito identities with AxonLLM's canonical "
            "DynamoDB authority"
        )
    )
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--user-pool-id", required=True)
    parser.add_argument("--table-name", required=True)
    parser.add_argument(
        "--issuer",
        required=True,
        help="Exact OIDC issuer emitted by the identity stack",
    )
    actions = parser.add_subparsers(dest="operation", required=True)

    invite = actions.add_parser(
        "invite-user",
        help="Invite or finish provisioning an exact tenant user",
    )
    _common_user_arguments(invite)

    update = actions.add_parser(
        "update-user",
        help="Update an active tenant user's role, profile, and grants",
    )
    _common_user_arguments(update)

    disable = actions.add_parser(
        "disable-user",
        help="Disable a tenant user and revoke canonical project grants",
    )
    disable.add_argument("--user-name", required=True)
    disable.add_argument("--tenant", required=True)

    operator = actions.add_parser(
        "bootstrap-operator",
        help="Provision a dedicated platform_admin outside tenant SCIM",
    )
    operator.add_argument("--user-name", required=True)
    operator.add_argument("--email", required=True)
    operator.add_argument(
        "--platform-tenant",
        default="platform-home",
        help="Dedicated home tenant used only to resolve the operator",
    )
    operator.add_argument(
        "--project-hint",
        default="platform",
        help="Synthetic Cognito project hint; it grants no project access",
    )
    return parser


async def _run(args: argparse.Namespace, cognito_client) -> dict[str, object]:
    persistence = DynamoPersistence(
        table_name=args.table_name,
        region=args.region,
    )
    common = {
        "cognito_client": cognito_client,
        "persistence": persistence,
        "user_pool_id": args.user_pool_id,
        "issuer": args.issuer,
        "user_name": args.user_name,
    }
    if args.operation == "invite-user":
        result = await invite_managed_tenant_user(
            **common,
            email=args.email,
            display_name=args.display_name,
            tenant_id=args.tenant,
            role=args.role,
            project_ids=args.projects,
            default_project_id=args.default_project,
        )
    elif args.operation == "update-user":
        result = await update_managed_tenant_user(
            **common,
            email=args.email,
            display_name=args.display_name,
            tenant_id=args.tenant,
            role=args.role,
            project_ids=args.projects,
            default_project_id=args.default_project,
        )
    elif args.operation == "disable-user":
        result = await disable_managed_tenant_user(
            **common,
            tenant_id=args.tenant,
        )
    else:
        result = await bootstrap_platform_operator(
            **common,
            email=args.email,
            platform_tenant_id=args.platform_tenant,
            project_hint=args.project_hint,
        )
    return result.to_dict()


def main() -> int:
    args = build_parser().parse_args()
    try:
        import boto3

        session = boto3.Session(region_name=args.region)
        cognito_client = session.client(
            "cognito-idp",
            region_name=args.region,
        )
        with _dynamo_environment():
            result = asyncio.run(_run(args, cognito_client))
    except (ManagedIdentityError, RuntimeError, ValueError) as exc:
        print(f"managed identity operation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
