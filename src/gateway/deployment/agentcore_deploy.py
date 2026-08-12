#!/usr/bin/env python3
"""Deploy an authenticated first-adopter AgentCore configuration."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Callable

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
from src.gateway.deployment.provider_secret import (
    ALLOWED_SECRET_FIELDS,
    ProviderSecretError,
    ProviderSecretVersion,
    collect_provider_secret,
    load_provider_environment_file,
    rollback_provider_secret,
    synchronize_provider_secret,
)


def _infra_resource_digest() -> str:
    digest = hashlib.sha256()
    resource_root = files("src.gateway.deployment").joinpath("infra")
    for resource in sorted(resource_root.iterdir(), key=lambda item: item.name):
        if resource.is_file():
            digest.update(resource.name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(resource.read_bytes())
    return digest.hexdigest()[:16]


def _infra_cache_root() -> Path:
    cache_home = Path(
        os.environ.get(
            "XDG_CACHE_HOME",
            Path.home() / ".cache",
        )
    ).expanduser()
    return cache_home / "axonllm" / "agentcore-infra" / _infra_resource_digest()


INFRA_ROOT = _infra_cache_root()


def _materialize_infra() -> None:
    """Copy immutable CDK support files out of the installed wheel."""
    marker = INFRA_ROOT / ".complete"
    if marker.is_file():
        return
    INFRA_ROOT.mkdir(parents=True, exist_ok=True)
    resource_root = files("src.gateway.deployment").joinpath("infra")
    for resource in resource_root.iterdir():
        if resource.is_file():
            target = INFRA_ROOT / resource.name
            target.write_bytes(resource.read_bytes())
            target.chmod(0o600)
    marker.write_text(_infra_resource_digest() + "\n", encoding="ascii")
    marker.chmod(0o600)


def _ensure_cdk_environment() -> None:
    """Install hash-pinned CDK dependencies into the per-version cache."""
    _materialize_infra()
    python = INFRA_ROOT / ".venv" / "bin" / "python3"
    if python.is_file():
        return
    uv = shutil.which("uv")
    if uv is None:
        raise AgentCoreDeploymentError(
            "uv is required to install the hash-pinned AgentCore CDK environment"
        )
    try:
        subprocess.run(
            [uv, "venv", "--python", "3.12", str(INFRA_ROOT / ".venv")],
            check=True,
        )
        subprocess.run(
            [
                uv,
                "pip",
                "sync",
                "--python",
                str(INFRA_ROOT / ".venv" / "bin" / "python"),
                "--require-hashes",
                str(INFRA_ROOT / "requirements.txt"),
            ],
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise AgentCoreDeploymentError(
            "could not install the hash-pinned AgentCore CDK environment"
        ) from exc


IDENTITY_STACK = "AxonLLMIdentityStack"
AGENTCORE_STACK = "AxonLLMAgentCoreStack"
CONTROL_PLANE_STACK = "AxonLLMControlPlaneStack"
_MAX_AGENTCORE_ENVIRONMENT_VALUE_CHARACTERS = 2_048
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


def managed_ses_sender(
    config: AgentCoreSetupConfig,
) -> tuple[str, str]:
    """Return an explicit SES sender/domain pair for the identity stack."""
    managed = config.managed_cognito
    if managed is None:
        raise AgentCoreDeploymentError(
            "managed Cognito settings are missing"
        )
    if managed.ses_from_email is not None:
        if managed.ses_verified_domain is None:
            raise AgentCoreDeploymentError(
                "managed Cognito SES domain is missing"
            )
        return managed.ses_from_email, managed.ses_verified_domain
    local, separator, domain = config.admin.email.rpartition("@")
    if separator != "@" or not local or not domain:
        raise AgentCoreDeploymentError(
            "administrator email cannot be used as the SES sender"
        )
    domain = domain.casefold()
    return f"{local}@{domain}", domain


def identity_deploy_command(
    config: AgentCoreSetupConfig,
    *,
    outputs_file: Path,
    assume_yes: bool,
) -> list[str]:
    if config.identity_mode != MANAGED_COGNITO:
        raise AgentCoreDeploymentError("the identity stack is only used for managed-cognito")
    managed = config.managed_cognito
    control_plane = config.control_plane
    if managed is None or control_plane is None:
        raise AgentCoreDeploymentError(
            "managed Cognito or control-plane settings are missing"
        )
    ses_from_email, ses_verified_domain = managed_ses_sender(config)
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
        _parameter(
            "ControlPlaneDomainName",
            control_plane.domain_name,
            stack=IDENTITY_STACK,
        )
    )
    command.extend(
        _parameter(
            "SesFromEmail",
            ses_from_email,
            stack=IDENTITY_STACK,
        )
    )
    command.extend(
        _parameter(
            "SesVerifiedDomain",
            ses_verified_domain,
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


def _athena_contexts(
    config: AgentCoreSetupConfig,
) -> dict[str, str]:
    athena = config.runtime.athena_query
    if athena is None:
        return {}
    bindings = [
        {
            "tenant_id": config.tenant.tenant_id,
            "project_id": config.tenant.project_id,
            "role_arn": role_arn,
        }
        for role_arn in athena.role_arns
    ]
    bindings_json = json.dumps(
        bindings,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(bindings_json) > _MAX_AGENTCORE_ENVIRONMENT_VALUE_CHARACTERS:
        raise AgentCoreDeploymentError(
            "athena_query_bindings exceeds the AgentCore "
            "2,048-character environment value limit"
        )
    return {
        "athena_query_bindings": bindings_json,
        "athena_query_timeout_seconds": f"{athena.timeout_seconds:g}",
        "athena_query_max_rows": str(athena.max_rows),
        "athena_query_max_result_bytes": str(
            athena.max_result_bytes
        ),
        "athena_query_max_bytes_scanned": str(
            athena.max_bytes_scanned
        ),
        "athena_query_poll_interval_seconds": (
            f"{athena.poll_interval_seconds:g}"
        ),
        "athena_query_project_rpm": str(athena.project_rpm),
        "athena_query_principal_rpm": str(athena.principal_rpm),
        "athena_query_project_concurrency": str(
            athena.project_concurrency
        ),
        "athena_query_principal_concurrency": str(
            athena.principal_concurrency
        ),
        "athena_query_project_scan_bytes_per_minute": str(
            athena.project_scan_bytes_per_minute
        ),
        "athena_query_principal_scan_bytes_per_minute": str(
            athena.principal_scan_bytes_per_minute
        ),
        "athena_query_max_datasources_per_tenant": str(
            athena.max_datasources_per_tenant
        ),
    }


def _append_athena_contexts(
    command: list[str],
    config: AgentCoreSetupConfig,
) -> None:
    for name, value in _athena_contexts(config).items():
        command.extend(["-c", f"{name}={value}"])


def agentcore_deploy_command(
    config: AgentCoreSetupConfig,
    identity: IdentityValues,
    *,
    outputs_file: Path,
    assume_yes: bool,
    provider_secret_version: str = "bootstrap",
    publish_runtime_endpoint: bool = True,
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
        "EnabledProviders": ",".join(
            config.runtime.enabled_providers
        ),
        "ProviderSecretVersion": provider_secret_version,
        "PublishRuntimeEndpoint": (
            "true" if publish_runtime_endpoint else "false"
        ),
    }
    for name, value in parameters.items():
        command.extend(_parameter(name, value, stack=AGENTCORE_STACK))
    _append_athena_contexts(command, config)
    command.extend(
        [
            "--require-approval",
            "never" if assume_yes else "broadening",
            "--outputs-file",
            str(outputs_file),
        ]
    )
    return command


def control_plane_deploy_command(
    config: AgentCoreSetupConfig,
    *,
    primary_state_table_name: str,
    outputs_file: Path,
    assume_yes: bool,
    runtime_state_table_name: str = "",
    recovery_approval_id: str = "",
) -> list[str]:
    if config.identity_mode != MANAGED_COGNITO:
        raise AgentCoreDeploymentError(
            "the web control plane currently requires managed-cognito"
        )
    control_plane = config.control_plane
    if control_plane is None:
        raise AgentCoreDeploymentError(
            "control-plane settings are missing"
        )
    command = [
        "npx",
        "cdk",
        "deploy",
        CONTROL_PLANE_STACK,
        "-c",
        "deployment_target=control-plane",
        "-c",
        f"region={config.aws_region}",
    ]
    parameters = {
        "AgentCoreStackName": AGENTCORE_STACK,
        "IdentityStackName": IDENTITY_STACK,
        "ControlPlaneVerifiedImageUri": (
            control_plane.verified_image_uri
        ),
        "CertificateArn": control_plane.certificate_arn,
        "PublicHostedZoneId": (
            control_plane.public_hosted_zone_id
        ),
        "ApprovedIngressPrefixListId": (
            control_plane.approved_ingress_prefix_list_id
        ),
        "ApprovedHttpsPrefixListId": (
            control_plane.approved_https_prefix_list_id
        ),
        "SamlLoginPath": control_plane.saml_login_path,
        "PrimaryStateTableName": primary_state_table_name,
        "RuntimeStateTableName": runtime_state_table_name,
        "RecoveryCutoverMode": "normal",
        "RecoveryApprovalId": recovery_approval_id,
    }
    for name, value in parameters.items():
        command.extend(
            _parameter(name, value, stack=CONTROL_PLANE_STACK)
        )
    if control_plane.scim_tenants_secret_arn is not None:
        command.extend(
            [
                "-c",
                (
                    "scim_tenants_secret_arn="
                    f"{control_plane.scim_tenants_secret_arn}"
                ),
            ]
        )
    _append_athena_contexts(command, config)
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


def _existing_agentcore_outputs(
    cloudformation_client: Any,
) -> dict[str, str] | None:
    """Return stable existing outputs, or None when this is a first deployment."""
    try:
        response = cloudformation_client.describe_stacks(
            StackName=AGENTCORE_STACK,
        )
    except Exception as exc:
        if _aws_error_code(exc) == "ValidationError":
            return None
        raise AgentCoreDeploymentError(
            "could not inspect the existing AgentCore stack"
        ) from exc
    stacks = response.get("Stacks")
    if not isinstance(stacks, list) or len(stacks) != 1:
        raise AgentCoreDeploymentError(
            "CloudFormation returned an ambiguous AgentCore stack"
        )
    stack = stacks[0]
    if not isinstance(stack, dict):
        raise AgentCoreDeploymentError(
            "CloudFormation returned a malformed AgentCore stack"
        )
    status = stack.get("StackStatus")
    if (
        not isinstance(status, str)
        or status.endswith("_IN_PROGRESS")
        or status.endswith("_FAILED")
        or "ROLLBACK" in status
    ):
        raise AgentCoreDeploymentError(
            "the existing AgentCore stack is not in a stable successful state"
        )
    raw_outputs = stack.get("Outputs", [])
    if not isinstance(raw_outputs, list):
        raise AgentCoreDeploymentError(
            "the existing AgentCore stack outputs are malformed"
        )
    outputs: dict[str, str] = {}
    for item in raw_outputs:
        if not isinstance(item, dict):
            raise AgentCoreDeploymentError(
                "the existing AgentCore stack outputs are malformed"
            )
        name = item.get("OutputKey")
        value = item.get("OutputValue")
        if (
            not isinstance(name, str)
            or not isinstance(value, str)
            or name in outputs
        ):
            raise AgentCoreDeploymentError(
                "the existing AgentCore stack outputs are malformed"
            )
        outputs[name] = value
    return outputs


def _write_private_json(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(
                dict(value),
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _provider_environment(
    env_file: Path | None,
) -> dict[str, str]:
    values = (
        load_provider_environment_file(env_file)
        if env_file is not None
        else {}
    )
    for field_name in ALLOWED_SECRET_FIELDS:
        if field_name in os.environ:
            values[field_name] = os.environ[field_name]
    return values


def _sync_provider_credentials(
    session: Any,
    *,
    config: AgentCoreSetupConfig,
    provider_environment: Mapping[str, str],
    secret_arn: str,
) -> ProviderSecretVersion:
    try:
        return synchronize_provider_secret(
            session.client(
                "secretsmanager",
                region_name=config.aws_region,
            ),
            secret_arn=secret_arn,
            environ=provider_environment,
            enabled_providers=config.runtime.enabled_providers,
        )
    except ProviderSecretError as exc:
        raise AgentCoreDeploymentError(str(exc)) from exc


def _rollback_provider_credentials(
    session: Any,
    *,
    config: AgentCoreSetupConfig,
    secret_arn: str,
    version_id: str,
) -> ProviderSecretVersion:
    try:
        return rollback_provider_secret(
            session.client(
                "secretsmanager",
                region_name=config.aws_region,
            ),
            secret_arn=secret_arn,
            version_id=version_id,
        )
    except ProviderSecretError as exc:
        raise AgentCoreDeploymentError(str(exc)) from exc


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
    _ensure_cdk_environment()
    if shutil.which("npx") is None:
        raise AgentCoreDeploymentError("npx is required for CDK deployment")
    if not (INFRA_ROOT / ".venv" / "bin" / "python3").is_file():
        raise AgentCoreDeploymentError(
            "the hash-pinned AgentCore CDK environment is incomplete"
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
    provider_environment: Mapping[str, str] | None = None,
    provider_secret_rollback_version: str | None = None,
) -> None:
    _assert_deployment_prerequisites()
    provider_environment = dict(
        os.environ
        if provider_environment is None
        else provider_environment
    )
    if provider_secret_rollback_version is None:
        try:
            collect_provider_secret(
                provider_environment,
                config.runtime.enabled_providers,
            )
        except ProviderSecretError as exc:
            raise AgentCoreDeploymentError(str(exc)) from exc

    outputs_dir.mkdir(parents=True, exist_ok=True)
    outputs_dir.chmod(0o700)
    if boto3_session is None:
        import boto3

        boto3_session = boto3.Session(region_name=config.aws_region)

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

    cloudformation_client = boto3_session.client(
        "cloudformation",
        region_name=config.aws_region,
    )
    existing_outputs = _existing_agentcore_outputs(
        cloudformation_client
    )
    if (
        provider_secret_rollback_version is not None
        and existing_outputs is None
    ):
        raise AgentCoreDeploymentError(
            "provider secret rollback requires an existing AgentCore stack"
        )
    if (
        existing_outputs is not None
        and existing_outputs.get("RecoveryCutoverMode") != "normal"
    ):
        raise AgentCoreDeploymentError(
            "AgentCore recovery is active; complete or abort it before "
            "running the first-adopter deployment"
        )

    runtime_output_path = outputs_dir / "agentcore-outputs.json"
    if existing_outputs is None:
        runtime_output_path.unlink(missing_ok=True)
        print(
            "Creating the AgentCore runtime with its public endpoint held back..."
        )
        runner(
            agentcore_deploy_command(
                config,
                identity,
                outputs_file=runtime_output_path,
                assume_yes=assume_yes,
                provider_secret_version="bootstrap",
                publish_runtime_endpoint=False,
            ),
            INFRA_ROOT,
        )
        bootstrap_outputs = _stack_outputs(
            runtime_output_path,
            AGENTCORE_STACK,
        )
        if (
            _required_output(
                bootstrap_outputs,
                "RecoveryCutoverMode",
            )
            != "normal"
        ):
            raise AgentCoreDeploymentError(
                "AgentCore recovery became active during bootstrap"
            )
        secret_arn = _required_output(
            bootstrap_outputs,
            "ProviderSecretArn",
        )
    else:
        secret_arn = _required_output(
            existing_outputs,
            "ProviderSecretArn",
        )

    if provider_secret_rollback_version is None:
        print("Synchronizing the allowlisted provider credential secret...")
        secret_version = _sync_provider_credentials(
            boto3_session,
            config=config,
            provider_environment=provider_environment,
            secret_arn=secret_arn,
        )
    else:
        print("Rolling back to the reviewed provider secret version...")
        secret_version = _rollback_provider_credentials(
            boto3_session,
            config=config,
            secret_arn=secret_arn,
            version_id=provider_secret_rollback_version,
        )
    _write_private_json(
        outputs_dir / "provider-secret-version.json",
        secret_version.to_dict(),
    )
    print(
        "Provider secret version synchronized for fields: "
        + ", ".join(secret_version.configured_fields)
    )

    runtime_output_path.unlink(missing_ok=True)
    print(
        "Deploying the credential-bound authenticated AgentCore runtime..."
    )
    runner(
        agentcore_deploy_command(
            config,
            identity,
            outputs_file=runtime_output_path,
            assume_yes=assume_yes,
            provider_secret_version=secret_version.version_id,
            publish_runtime_endpoint=True,
        ),
        INFRA_ROOT,
    )
    runtime_outputs = _stack_outputs(runtime_output_path, AGENTCORE_STACK)
    recovery_mode = _required_output(
        runtime_outputs,
        "RecoveryCutoverMode",
    )
    if recovery_mode != "normal":
        raise AgentCoreDeploymentError(
            "AgentCore recovery is active; complete or abort it before "
            "running the first-adopter deployment"
        )
    if (
        _required_output(
            runtime_outputs,
            "ProviderSecretVersion",
        )
        != secret_version.version_id
    ):
        raise AgentCoreDeploymentError(
            "deployed AgentCore runtime is not bound to the synchronized "
            "provider secret version"
        )
    _required_output(runtime_outputs, "RuntimeEndpointArn")
    _required_output(runtime_outputs, "RuntimeVersion")
    primary_table_name = _required_output(
        runtime_outputs,
        "StateTableName",
    )
    table_name = _required_output(
        runtime_outputs,
        "SelectedRuntimeStateTableName",
    )
    recovery_approval_id = runtime_outputs.get("RecoveryApprovalId")
    if not isinstance(recovery_approval_id, str):
        raise AgentCoreDeploymentError(
            "AgentCore recovery approval output is missing"
        )

    print("Creating or verifying canonical tenant authority...")
    result = bootstrap_canonical_admin(
        config,
        table_name=table_name,
        issuer=identity.issuer,
        subject=subject,
    )
    print(f"Canonical administrator verified: {result['principal_id']} on {result['project_id']}.")
    control_outputs: dict[str, str] | None = None
    if config.identity_mode == MANAGED_COGNITO:
        control_output_path = outputs_dir / "control-plane-outputs.json"
        control_output_path.unlink(missing_ok=True)
        print("Deploying the authenticated shared-state web control plane...")
        runner(
            control_plane_deploy_command(
                config,
                primary_state_table_name=primary_table_name,
                outputs_file=control_output_path,
                assume_yes=assume_yes,
                runtime_state_table_name=(
                    ""
                    if table_name == primary_table_name
                    else table_name
                ),
                recovery_approval_id=recovery_approval_id,
            ),
            INFRA_ROOT,
        )
        control_outputs = _stack_outputs(
            control_output_path,
            CONTROL_PLANE_STACK,
        )
        expected_control_outputs = {
            "AgentCoreStackName": AGENTCORE_STACK,
            "PrimaryStateTableName": primary_table_name,
            "SelectedRuntimeStateTableName": table_name,
            "RecoveryCutoverMode": "normal",
            "RecoveryApprovalId": recovery_approval_id,
        }
        actual_control_outputs = {
            name: control_outputs.get(name)
            for name in expected_control_outputs
        }
        if actual_control_outputs != expected_control_outputs:
            raise AgentCoreDeploymentError(
                "control-plane recovery outputs do not match AgentCore: "
                f"expected {expected_control_outputs}, "
                f"found {actual_control_outputs}"
            )
    if identity.hosted_ui_domain:
        print(f"Managed login: {identity.hosted_ui_domain}")
        print(f"OIDC client ID: {identity.client_id}")
    if control_outputs is not None:
        if config.control_plane is None:
            raise AgentCoreDeploymentError(
                "control-plane settings disappeared after validation"
            )
        print(
            f"Control plane: https://{config.control_plane.domain_name}"
        )
    else:
        print(
            "Web control plane: not deployed; external OIDC is currently "
            "supported on the AgentCore invocation surface only."
        )
    print(
        "Runtime execution role: "
        f"{_required_output(runtime_outputs, 'RuntimeExecutionRoleArn')}"
    )
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
    parser.add_argument(
        "--provider-env-file",
        help=(
            "Owner-only env file read as data for allowlisted provider "
            "credentials; process environment values take precedence"
        ),
    )
    parser.add_argument(
        "--rollback-provider-secret-version",
        help=(
            "Move AWSCURRENT to a reviewed prior version and publish a fresh "
            "runtime version"
        ),
    )
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
            provider_environment=_provider_environment(
                (
                    Path(args.provider_env_file)
                    if args.provider_env_file
                    else None
                )
            ),
            provider_secret_rollback_version=(
                args.rollback_provider_secret_version
            ),
        )
    except (
        AgentCoreSetupError,
        AgentCoreDeploymentError,
        ProviderSecretError,
    ) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
