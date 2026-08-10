#!/usr/bin/env python3
"""Deploy an authenticated first-adopter AgentCore configuration."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Callable


REPO_ROOT = Path(__file__).resolve().parents[2]
INFRA_ROOT = REPO_ROOT / "infra"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.gateway.agentcore_setup import (
    DEFAULT_PROJECT_CLAIM,
    DEFAULT_TENANT_CLAIM,
    EXTERNAL_OIDC,
    MANAGED_COGNITO,
    AgentCoreSetupConfig,
    AgentCoreSetupError,
    load_agentcore_setup,
    redact_sensitive,
)


IDENTITY_STACK = "AxonLLMIdentityStack"
AGENTCORE_STACK = "AxonLLMAgentCoreStack"
CommandRunner = Callable[[list[str], Path], None]


class AgentCoreDeploymentError(RuntimeError):
    """Raised when deployment cannot prove a safe resulting configuration."""


@dataclass(frozen=True)
class IdentityValues:
    issuer: str
    discovery_url: str
    client_id: str
    audience: str
    tenant_claim: str
    project_claim: str
    user_pool_id: str | None = None
    hosted_ui_domain: str | None = None


@dataclass(frozen=True)
class ManagedAdminResult:
    subject: str
    created: bool


def _parameter(name: str, value: str, *, stack: str) -> list[str]:
    return ["--parameters", f"{stack}:{name}={value}"]


def identity_deploy_command(
    config: AgentCoreSetupConfig,
    *,
    outputs_file: Path,
    assume_yes: bool,
) -> list[str]:
    if config.identity_mode != MANAGED_COGNITO:
        raise AgentCoreDeploymentError("the identity stack is only used for managed-cognito")
    managed = config.managed_cognito
    if managed is None:
        raise AgentCoreDeploymentError("managed Cognito settings are missing")
    command = [
        "npx",
        "cdk",
        "deploy",
        IDENTITY_STACK,
        "-c",
        "deployment_target=identity",
        "-c",
        f"region={config.aws_region}",
    ]
    command.extend(
        _parameter(
            "HostedUiDomainPrefix",
            managed.hosted_ui_domain_prefix,
            stack=IDENTITY_STACK,
        )
    )
    command.extend(
        _parameter(
            "OAuthCallbackUrls",
            ",".join(managed.oauth_callback_urls),
            stack=IDENTITY_STACK,
        )
    )
    command.extend(
        [
            "--require-approval",
            "never" if assume_yes else "broadening",
            "--outputs-file",
            str(outputs_file),
        ]
    )
    return command


def agentcore_deploy_command(
    config: AgentCoreSetupConfig,
    identity: IdentityValues,
    *,
    outputs_file: Path,
    assume_yes: bool,
) -> list[str]:
    command = [
        "npx",
        "cdk",
        "deploy",
        AGENTCORE_STACK,
        "-c",
        "deployment_target=agentcore",
        "-c",
        f"region={config.aws_region}",
    ]
    parameters = {
        "VerifiedImageUri": config.runtime.verified_image_uri,
        "OidcIssuer": identity.issuer,
        "OidcDiscoveryUrl": identity.discovery_url,
        "OidcClientId": identity.client_id,
        "OidcAudience": identity.audience,
        "OidcTenantClaim": identity.tenant_claim,
        "OidcProjectClaim": identity.project_claim,
        "ApprovedHttpsPrefixListId": (config.runtime.approved_https_prefix_list_id),
        "BedrockInvokeResourceArns": ",".join(config.runtime.bedrock_invoke_resource_arns),
    }
    for name, value in parameters.items():
        command.extend(_parameter(name, value, stack=AGENTCORE_STACK))
    command.extend(
        [
            "--require-approval",
            "never" if assume_yes else "broadening",
            "--outputs-file",
            str(outputs_file),
        ]
    )
    return command


def cdk_bootstrap_command(config: AgentCoreSetupConfig) -> list[str]:
    return [
        "npx",
        "cdk",
        "bootstrap",
        "-c",
        "deployment_target=identity",
        "-c",
        f"region={config.aws_region}",
    ]


def _run_command(command: list[str], cwd: Path) -> None:
    environment = os.environ.copy()
    environment.setdefault(
        "JSII_RUNTIME_PACKAGE_CACHE_ROOT",
        str(Path(os.getenv("TMPDIR", "/tmp")) / "axonllm-jsii-cache"),
    )
    environment.setdefault(
        "PYTHONPYCACHEPREFIX",
        str(Path(os.getenv("TMPDIR", "/tmp")) / "axonllm-pycache"),
    )
    try:
        subprocess.run(command, cwd=cwd, env=environment, check=True)
    except subprocess.CalledProcessError as exc:
        raise AgentCoreDeploymentError(f"command failed with exit code {exc.returncode}: {command[0]}") from exc


def _stack_outputs(path: Path, stack_name: str) -> dict[str, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AgentCoreDeploymentError(f"cannot read CDK outputs from {path}: {exc}") from exc
    outputs = payload.get(stack_name) if isinstance(payload, dict) else None
    if not isinstance(outputs, dict):
        raise AgentCoreDeploymentError(f"CDK outputs do not contain {stack_name}")
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in outputs.items()):
        raise AgentCoreDeploymentError(f"{stack_name} outputs must be string values")
    return outputs


def _required_output(outputs: dict[str, str], name: str) -> str:
    value = outputs.get(name)
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(character.isspace() for character in value)
    ):
        raise AgentCoreDeploymentError(f"deployment output {name} is missing or invalid")
    return value


def managed_identity_from_outputs(
    outputs: dict[str, str],
) -> IdentityValues:
    tenant_claim = _required_output(outputs, "TenantClaimName")
    project_claim = _required_output(outputs, "ProjectClaimName")
    if tenant_claim != DEFAULT_TENANT_CLAIM:
        raise AgentCoreDeploymentError("managed identity emitted an unexpected tenant claim name")
    if project_claim != DEFAULT_PROJECT_CLAIM:
        raise AgentCoreDeploymentError("managed identity emitted an unexpected project claim name")
    client_id = _required_output(outputs, "OidcClientId")
    audience = _required_output(outputs, "OidcAudience")
    if client_id != audience:
        raise AgentCoreDeploymentError("managed Cognito client and ID-token audience must match")
    issuer = _required_output(outputs, "OidcIssuer")
    discovery_url = _required_output(outputs, "OidcDiscoveryUrl")
    if discovery_url != f"{issuer}/.well-known/openid-configuration":
        raise AgentCoreDeploymentError("managed identity discovery URL does not match its issuer")
    hosted_ui = _required_output(outputs, "HostedUiDomain")
    if not issuer.startswith("https://") or not hosted_ui.startswith("https://"):
        raise AgentCoreDeploymentError("managed Cognito identity outputs must use HTTPS")
    return IdentityValues(
        issuer=issuer,
        discovery_url=discovery_url,
        client_id=client_id,
        audience=audience,
        tenant_claim=tenant_claim,
        project_claim=project_claim,
        user_pool_id=_required_output(outputs, "UserPoolId"),
        hosted_ui_domain=hosted_ui,
    )


def external_identity(config: AgentCoreSetupConfig) -> IdentityValues:
    if config.identity_mode != EXTERNAL_OIDC or config.external_oidc is None:
        raise AgentCoreDeploymentError("external OIDC settings are missing")
    oidc = config.external_oidc
    return IdentityValues(
        issuer=oidc.issuer,
        discovery_url=oidc.discovery_url,
        client_id=oidc.client_id,
        audience=oidc.audience,
        tenant_claim=oidc.tenant_claim,
        project_claim=oidc.project_claim,
    )


def _aws_error_code(exc: Exception) -> str | None:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    error = response.get("Error")
    if not isinstance(error, dict):
        return None
    code = error.get("Code")
    return code if isinstance(code, str) else None


def _attributes(items: Any) -> dict[str, str]:
    if not isinstance(items, list):
        raise AgentCoreDeploymentError("Cognito returned malformed user attributes")
    attributes: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            raise AgentCoreDeploymentError("Cognito returned malformed user attributes")
        name = item.get("Name")
        value = item.get("Value")
        if not isinstance(name, str) or not isinstance(value, str) or name in attributes:
            raise AgentCoreDeploymentError("Cognito returned malformed or duplicate user attributes")
        attributes[name] = value
    return attributes


def _verify_managed_admin(
    user: dict[str, Any],
    *,
    tenant_id: str,
    project_id: str,
    email: str,
) -> str:
    if user.get("Enabled") is not True:
        raise AgentCoreDeploymentError("the managed Cognito administrator is disabled")
    if user.get("UserStatus") in {"ARCHIVED", "UNKNOWN", "RESET_REQUIRED"}:
        raise AgentCoreDeploymentError("the managed Cognito administrator has an unusable status")
    attributes = _attributes(user.get("UserAttributes"))
    expected = {
        "email": email,
        "email_verified": "true",
        DEFAULT_TENANT_CLAIM: tenant_id,
        DEFAULT_PROJECT_CLAIM: project_id,
    }
    mismatches = [name for name, expected_value in expected.items() if attributes.get(name) != expected_value]
    if mismatches:
        raise AgentCoreDeploymentError(
            "existing Cognito administrator has conflicting attributes: " + ", ".join(sorted(mismatches))
        )
    subject = attributes.get("sub")
    if not isinstance(subject, str) or not subject or subject != subject.strip():
        raise AgentCoreDeploymentError("managed Cognito administrator has no stable subject")
    return subject


def ensure_managed_cognito_admin(
    cognito_client: Any,
    *,
    user_pool_id: str,
    user_name: str,
    email: str,
    tenant_id: str,
    project_id: str,
) -> ManagedAdminResult:
    """Invite the first administrator once and verify every idempotent rerun."""
    created = False
    try:
        user = cognito_client.admin_get_user(
            UserPoolId=user_pool_id,
            Username=user_name,
        )
    except Exception as exc:
        if _aws_error_code(exc) != "UserNotFoundException":
            raise AgentCoreDeploymentError("could not resolve the managed Cognito administrator") from exc
        try:
            cognito_client.admin_create_user(
                UserPoolId=user_pool_id,
                Username=user_name,
                DesiredDeliveryMediums=["EMAIL"],
                UserAttributes=[
                    {"Name": "email", "Value": email},
                    {"Name": "email_verified", "Value": "true"},
                    {"Name": DEFAULT_TENANT_CLAIM, "Value": tenant_id},
                    {"Name": DEFAULT_PROJECT_CLAIM, "Value": project_id},
                ],
            )
            created = True
        except Exception as create_exc:
            if _aws_error_code(create_exc) != "UsernameExistsException":
                raise AgentCoreDeploymentError("could not invite the managed Cognito administrator") from create_exc
        try:
            user = cognito_client.admin_get_user(
                UserPoolId=user_pool_id,
                Username=user_name,
            )
        except Exception as get_exc:
            raise AgentCoreDeploymentError("invited Cognito administrator could not be resolved") from get_exc

    subject = _verify_managed_admin(
        user,
        tenant_id=tenant_id,
        project_id=project_id,
        email=email,
    )
    return ManagedAdminResult(subject=subject, created=created)


@contextmanager
def _bootstrap_environment(region: str):
    updates = {
        "AWS_DEFAULT_REGION": region,
        "LLM_ROUTER_DYNAMODB_ENABLED": "true",
    }
    previous = {name: os.environ.get(name) for name in updates}
    os.environ.update(updates)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def bootstrap_canonical_admin(
    config: AgentCoreSetupConfig,
    *,
    table_name: str,
    issuer: str,
    subject: str,
) -> dict[str, object]:
    from src.gateway.auth.tenant_bootstrap import bootstrap_tenant
    from src.gateway.persistence import DynamoPersistence

    with _bootstrap_environment(config.aws_region):
        persistence = DynamoPersistence(
            table_name=table_name,
            region=config.aws_region,
        )
        result = asyncio.run(
            bootstrap_tenant(
                persistence,
                tenant_id=config.tenant.tenant_id,
                project_id=config.tenant.project_id,
                project_name=config.tenant.project_name,
                issuer=issuer,
                subject=subject,
                user_name=config.admin.user_name,
                display_name=config.admin.display_name,
                email=config.admin.email,
                budget_limit=config.tenant.budget_limit,
            )
        )
    return result.to_dict()


def _assert_deployment_prerequisites() -> None:
    if shutil.which("npx") is None:
        raise AgentCoreDeploymentError("npx is required for CDK deployment")
    if not (INFRA_ROOT / ".venv" / "bin" / "python3").is_file():
        raise AgentCoreDeploymentError(
            "infra/.venv is missing; run deploy-agentcore.sh so it can install the hash-pinned CDK dependencies"
        )
    try:
        version = subprocess.run(
            ["node", "--version"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        ).stdout.strip()
        major = int(version.removeprefix("v").split(".", 1)[0])
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        raise AgentCoreDeploymentError("Node.js 22 or newer is required for CDK deployment") from exc
    if major < 22:
        raise AgentCoreDeploymentError(f"Node.js 22 or newer is required; found {version}")


def deploy(
    config: AgentCoreSetupConfig,
    *,
    outputs_dir: Path,
    assume_yes: bool,
    bootstrap_cdk: bool,
    runner: CommandRunner = _run_command,
    boto3_session: Any | None = None,
) -> None:
    _assert_deployment_prerequisites()
    outputs_dir.mkdir(parents=True, exist_ok=True)
    outputs_dir.chmod(0o700)

    if bootstrap_cdk:
        print(f"Bootstrapping CDK in {config.aws_region}...")
        runner(cdk_bootstrap_command(config), INFRA_ROOT)

    if config.identity_mode == MANAGED_COGNITO:
        identity_output_path = outputs_dir / "identity-outputs.json"
        identity_output_path.unlink(missing_ok=True)
        print("Deploying retained managed Cognito identity...")
        runner(
            identity_deploy_command(
                config,
                outputs_file=identity_output_path,
                assume_yes=assume_yes,
            ),
            INFRA_ROOT,
        )
        identity = managed_identity_from_outputs(_stack_outputs(identity_output_path, IDENTITY_STACK))
        if boto3_session is None:
            import boto3

            boto3_session = boto3.Session(region_name=config.aws_region)
        cognito_client = boto3_session.client(
            "cognito-idp",
            region_name=config.aws_region,
        )
        if identity.user_pool_id is None:
            raise AgentCoreDeploymentError("managed identity did not return a user pool")
        admin = ensure_managed_cognito_admin(
            cognito_client,
            user_pool_id=identity.user_pool_id,
            user_name=config.admin.user_name,
            email=config.admin.email,
            tenant_id=config.tenant.tenant_id,
            project_id=config.tenant.project_id,
        )
        action = "invited" if admin.created else "verified"
        print(f"Managed Cognito administrator {action}.")
        subject = admin.subject
    else:
        identity = external_identity(config)
        if config.admin.subject is None:
            raise AgentCoreDeploymentError("external OIDC administrator subject is missing")
        subject = config.admin.subject

    runtime_output_path = outputs_dir / "agentcore-outputs.json"
    runtime_output_path.unlink(missing_ok=True)
    print("Deploying authenticated AxonLLM AgentCore runtime...")
    runner(
        agentcore_deploy_command(
            config,
            identity,
            outputs_file=runtime_output_path,
            assume_yes=assume_yes,
        ),
        INFRA_ROOT,
    )
    runtime_outputs = _stack_outputs(runtime_output_path, AGENTCORE_STACK)
    table_name = _required_output(runtime_outputs, "StateTableName")

    print("Creating or verifying canonical tenant authority...")
    result = bootstrap_canonical_admin(
        config,
        table_name=table_name,
        issuer=identity.issuer,
        subject=subject,
    )
    print(f"Canonical administrator verified: {result['principal_id']} on {result['project_id']}.")
    if identity.hosted_ui_domain:
        print(f"Managed login: {identity.hosted_ui_domain}")
        print(f"OIDC client ID: {identity.client_id}")
    print(f"Runtime ARN: {_required_output(runtime_outputs, 'RuntimeArn')}")
    print(f"Deployment outputs: {outputs_dir}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deploy a validated AxonLLM AgentCore setup",
    )
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--outputs-dir",
        default=".axonllm/agentcore",
    )
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--bootstrap-cdk", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--show-config", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_agentcore_setup(args.config)
        print(f"Validated authenticated AgentCore configuration: {config.identity_mode}, {config.aws_region}.")
        if args.show_config:
            print(
                json.dumps(
                    redact_sensitive(config.to_dict()),
                    indent=2,
                    sort_keys=True,
                )
            )
        if args.validate_only:
            return 0
        if not args.yes and not sys.stdin.isatty():
            raise AgentCoreDeploymentError("non-interactive deployment requires --yes after reviewing the CDK diff")
        deploy(
            config,
            outputs_dir=Path(args.outputs_dir).expanduser().resolve(),
            assume_yes=args.yes,
            bootstrap_cdk=args.bootstrap_cdk,
        )
    except (AgentCoreSetupError, AgentCoreDeploymentError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
