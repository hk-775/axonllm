"""Validated first-adopter configuration for AxonLLM AgentCore."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


SCHEMA_VERSION = 1
MANAGED_COGNITO = "managed-cognito"
EXTERNAL_OIDC = "external-oidc"
IDENTITY_MODES = (MANAGED_COGNITO, EXTERNAL_OIDC)
DEFAULT_TENANT_CLAIM = "custom:tenant_id"
DEFAULT_PROJECT_CLAIM = "custom:project_id"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DOMAIN_PREFIX_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])$")
_PREFIX_LIST_PATTERN = re.compile(r"^pl-[0-9a-fA-F]+$")
_IMAGE_PATTERN = re.compile(
    r"^(?P<account>[0-9]{12})\.dkr\.ecr\."
    r"(?P<region>[a-z0-9-]+)\.amazonaws\.com/"
    r"[a-z0-9]+(?:[._/-][a-z0-9]+)*@sha256:[0-9a-f]{64}$"
)
_BEDROCK_ARN_PATTERN = re.compile(
    r"^arn:(?:aws|aws-us-gov|aws-cn):bedrock:"
    r"(?P<region>[a-z0-9-]+):(?:[0-9]{12})?:"
    r"(?:foundation-model|inference-profile|"
    r"application-inference-profile|custom-model|provisioned-model|"
    r"imported-model)/[A-Za-z0-9][A-Za-z0-9._:/+-]*$"
)
_SENSITIVE_KEY_PARTS = (
    "api_key",
    "access_token",
    "client_secret",
    "credential",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "token",
)


class AgentCoreSetupError(ValueError):
    """Raised when first-adopter configuration is unsafe or incomplete."""


def _required_string(value: Any, name: str, *, max_length: int = 2048) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > max_length
        or any(ord(character) < 32 for character in value)
    ):
        raise AgentCoreSetupError(
            f"{name} must be a non-empty string without surrounding whitespace or control characters"
        )
    return value


def _identifier(value: Any, name: str) -> str:
    value = _required_string(value, name, max_length=128)
    if _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise AgentCoreSetupError(
            f"{name} must start with a letter or number and contain only "
            "letters, numbers, dots, underscores, or hyphens"
        )
    return value


def _claim_name(value: Any, name: str) -> str:
    value = _required_string(value, name, max_length=256)
    if any(character.isspace() for character in value):
        raise AgentCoreSetupError(f"{name} must not contain whitespace")
    return value


def _https_url(
    value: Any,
    name: str,
    *,
    discovery: bool = False,
    issuer: bool = False,
) -> str:
    value = _required_string(value, name)
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise AgentCoreSetupError(f"{name} must be a valid HTTPS URL") from exc
    hostname = parsed.hostname or ""
    try:
        ipaddress.ip_address(hostname)
        is_ip_literal = True
    except ValueError:
        is_ip_literal = False
    if (
        parsed.scheme != "https"
        or not hostname
        or hostname.casefold() == "localhost"
        or hostname.casefold().endswith(".localhost")
        or is_ip_literal
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or any(character.isspace() for character in value)
        or port is not None
        and not 1 <= port <= 65535
    ):
        raise AgentCoreSetupError(f"{name} must be an HTTPS URL without userinfo, whitespace, or a fragment")
    if issuer and (parsed.query or value.endswith("/")):
        raise AgentCoreSetupError(f"{name} must not contain a query or trailing slash")
    if discovery and not parsed.path.endswith("/.well-known/openid-configuration"):
        raise AgentCoreSetupError(f"{name} must end with /.well-known/openid-configuration")
    return value


def _oauth_identifier(value: Any, name: str) -> str:
    value = _required_string(value, name, max_length=512)
    if any(character.isspace() for character in value):
        raise AgentCoreSetupError(f"{name} must not contain whitespace")
    return value


def _email(value: Any, name: str) -> str:
    value = _required_string(value, name, max_length=320)
    local, separator, domain = value.rpartition("@")
    if (
        separator != "@"
        or not local
        or not domain
        or "." not in domain
        or any(character.isspace() for character in value)
    ):
        raise AgentCoreSetupError(f"{name} must be a valid email address")
    return value


def _strict_object(
    value: Any,
    name: str,
    *,
    required: set[str],
    optional: set[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AgentCoreSetupError(f"{name} must be a JSON object")
    optional = optional or set()
    missing = required.difference(value)
    unknown = set(value).difference(required | optional)
    if missing:
        raise AgentCoreSetupError(f"{name} is missing required fields: {', '.join(sorted(missing))}")
    if unknown:
        raise AgentCoreSetupError(f"{name} contains unsupported fields: {', '.join(sorted(unknown))}")
    return value


def _optional_display_name(value: Any) -> str:
    if value in (None, ""):
        return ""
    return _required_string(value, "admin.display_name", max_length=256)


def _budget_limit(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AgentCoreSetupError("tenant.budget_limit must be a number or null")
    normalized = float(value)
    if normalized < 0 or normalized != normalized or normalized == float("inf"):
        raise AgentCoreSetupError("tenant.budget_limit must be a finite non-negative number")
    return normalized


@dataclass(frozen=True)
class TenantSetup:
    tenant_id: str
    project_id: str
    project_name: str
    budget_limit: float | None = None

    @classmethod
    def from_mapping(cls, raw: Any) -> TenantSetup:
        value = _strict_object(
            raw,
            "tenant",
            required={"tenant_id", "project_id", "project_name"},
            optional={"budget_limit"},
        )
        return cls(
            tenant_id=_identifier(value["tenant_id"], "tenant.tenant_id"),
            project_id=_identifier(value["project_id"], "tenant.project_id"),
            project_name=_required_string(
                value["project_name"],
                "tenant.project_name",
                max_length=256,
            ),
            budget_limit=_budget_limit(value.get("budget_limit")),
        )


@dataclass(frozen=True)
class AdminSetup:
    user_name: str
    email: str
    display_name: str = ""
    subject: str | None = None

    @classmethod
    def from_mapping(
        cls,
        raw: Any,
        *,
        subject_required: bool,
    ) -> AdminSetup:
        value = _strict_object(
            raw,
            "admin",
            required={"user_name", "email"},
            optional={"display_name", "subject"},
        )
        subject = value.get("subject")
        if subject_required:
            subject = _required_string(subject, "admin.subject")
        elif subject is not None:
            raise AgentCoreSetupError("admin.subject is assigned by managed Cognito and must be omitted")
        return cls(
            user_name=_email(value["user_name"], "admin.user_name"),
            email=_email(value["email"], "admin.email"),
            display_name=_optional_display_name(value.get("display_name")),
            subject=subject,
        )


@dataclass(frozen=True)
class RuntimeSetup:
    verified_image_uri: str
    bedrock_invoke_resource_arns: tuple[str, ...]
    approved_https_prefix_list_id: str

    @classmethod
    def from_mapping(cls, raw: Any, *, aws_region: str) -> RuntimeSetup:
        value = _strict_object(
            raw,
            "runtime",
            required={
                "verified_image_uri",
                "bedrock_invoke_resource_arns",
                "approved_https_prefix_list_id",
            },
        )
        image = _required_string(
            value["verified_image_uri"],
            "runtime.verified_image_uri",
        )
        match = _IMAGE_PATTERN.fullmatch(image)
        if match is None or match.group("region") != aws_region:
            raise AgentCoreSetupError(
                f"runtime.verified_image_uri must be an immutable private ECR digest URI in {aws_region}"
            )
        raw_arns = value["bedrock_invoke_resource_arns"]
        if not isinstance(raw_arns, list) or not raw_arns:
            raise AgentCoreSetupError("runtime.bedrock_invoke_resource_arns must be a non-empty array")
        arns: list[str] = []
        for index, raw_arn in enumerate(raw_arns):
            arn = _required_string(
                raw_arn,
                f"runtime.bedrock_invoke_resource_arns[{index}]",
            )
            arn_match = _BEDROCK_ARN_PATTERN.fullmatch(arn)
            if arn_match is None or arn_match.group("region") != aws_region or "*" in arn:
                raise AgentCoreSetupError(
                    f"each Bedrock resource must be a concrete model or inference-profile ARN in {aws_region}"
                )
            if arn in arns:
                raise AgentCoreSetupError("runtime.bedrock_invoke_resource_arns must not contain duplicates")
            arns.append(arn)
        prefix_list_id = _required_string(
            value["approved_https_prefix_list_id"],
            "runtime.approved_https_prefix_list_id",
        )
        if _PREFIX_LIST_PATTERN.fullmatch(prefix_list_id) is None:
            raise AgentCoreSetupError("runtime.approved_https_prefix_list_id must be an EC2 managed prefix list ID")
        return cls(
            verified_image_uri=image,
            bedrock_invoke_resource_arns=tuple(arns),
            approved_https_prefix_list_id=prefix_list_id,
        )


@dataclass(frozen=True)
class ManagedCognitoSetup:
    hosted_ui_domain_prefix: str
    oauth_callback_urls: tuple[str, ...]

    @classmethod
    def from_mapping(cls, raw: Any) -> ManagedCognitoSetup:
        value = _strict_object(
            raw,
            "managed_cognito",
            required={"hosted_ui_domain_prefix", "oauth_callback_urls"},
        )
        prefix = _required_string(
            value["hosted_ui_domain_prefix"],
            "managed_cognito.hosted_ui_domain_prefix",
            max_length=63,
        )
        if _DOMAIN_PREFIX_PATTERN.fullmatch(prefix) is None:
            raise AgentCoreSetupError(
                "managed_cognito.hosted_ui_domain_prefix must be 3-63 "
                "lowercase letters, numbers, or hyphens and cannot start or "
                "end with a hyphen"
            )
        raw_urls = value["oauth_callback_urls"]
        if not isinstance(raw_urls, list) or not raw_urls:
            raise AgentCoreSetupError("managed_cognito.oauth_callback_urls must be a non-empty array")
        urls: list[str] = []
        for index, raw_url in enumerate(raw_urls):
            url = _https_url(
                raw_url,
                f"managed_cognito.oauth_callback_urls[{index}]",
            )
            if "," in url:
                raise AgentCoreSetupError("managed Cognito callback URLs must not contain commas")
            if url in urls:
                raise AgentCoreSetupError("managed Cognito callback URLs must not contain duplicates")
            urls.append(url)
        return cls(
            hosted_ui_domain_prefix=prefix,
            oauth_callback_urls=tuple(urls),
        )


@dataclass(frozen=True)
class ExternalOidcSetup:
    issuer: str
    discovery_url: str
    client_id: str
    audience: str
    tenant_claim: str
    project_claim: str

    @classmethod
    def from_mapping(cls, raw: Any) -> ExternalOidcSetup:
        value = _strict_object(
            raw,
            "external_oidc",
            required={
                "issuer",
                "discovery_url",
                "client_id",
                "audience",
                "tenant_claim",
                "project_claim",
            },
        )
        issuer = _https_url(
            value["issuer"],
            "external_oidc.issuer",
            issuer=True,
        )
        discovery_url = _https_url(
            value["discovery_url"],
            "external_oidc.discovery_url",
            discovery=True,
        )
        if discovery_url != f"{issuer}/.well-known/openid-configuration":
            raise AgentCoreSetupError(
                "external_oidc.discovery_url must be the configured issuer "
                "followed by /.well-known/openid-configuration"
            )
        return cls(
            issuer=issuer,
            discovery_url=discovery_url,
            client_id=_oauth_identifier(
                value["client_id"],
                "external_oidc.client_id",
            ),
            audience=_oauth_identifier(
                value["audience"],
                "external_oidc.audience",
            ),
            tenant_claim=_claim_name(
                value["tenant_claim"],
                "external_oidc.tenant_claim",
            ),
            project_claim=_claim_name(
                value["project_claim"],
                "external_oidc.project_claim",
            ),
        )


@dataclass(frozen=True)
class AgentCoreSetupConfig:
    schema_version: int
    target: str
    identity_mode: str
    aws_region: str
    tenant: TenantSetup
    admin: AdminSetup
    runtime: RuntimeSetup
    managed_cognito: ManagedCognitoSetup | None = None
    external_oidc: ExternalOidcSetup | None = None

    @classmethod
    def from_mapping(cls, raw: Any) -> AgentCoreSetupConfig:
        value = _strict_object(
            raw,
            "configuration",
            required={
                "schema_version",
                "target",
                "identity_mode",
                "aws_region",
                "tenant",
                "admin",
                "runtime",
            },
            optional={"managed_cognito", "external_oidc"},
        )
        if (
            isinstance(value["schema_version"], bool)
            or not isinstance(value["schema_version"], int)
            or value["schema_version"] != SCHEMA_VERSION
        ):
            raise AgentCoreSetupError(f"schema_version must be {SCHEMA_VERSION}")
        if value["target"] != "agentcore":
            raise AgentCoreSetupError("target must be 'agentcore'")
        identity_mode = value["identity_mode"]
        if identity_mode not in IDENTITY_MODES:
            raise AgentCoreSetupError(
                "identity_mode must be 'managed-cognito' or 'external-oidc'; "
                "AgentCore has no unauthenticated production mode"
            )
        aws_region = _identifier(value["aws_region"], "aws_region")
        managed_raw = value.get("managed_cognito")
        external_raw = value.get("external_oidc")
        if identity_mode == MANAGED_COGNITO:
            if managed_raw is None or external_raw is not None:
                raise AgentCoreSetupError("managed-cognito requires managed_cognito and forbids external_oidc")
        elif external_raw is None or managed_raw is not None:
            raise AgentCoreSetupError("external-oidc requires external_oidc and forbids managed_cognito")
        return cls(
            schema_version=SCHEMA_VERSION,
            target="agentcore",
            identity_mode=identity_mode,
            aws_region=aws_region,
            tenant=TenantSetup.from_mapping(value["tenant"]),
            admin=AdminSetup.from_mapping(
                value["admin"],
                subject_required=identity_mode == EXTERNAL_OIDC,
            ),
            runtime=RuntimeSetup.from_mapping(
                value["runtime"],
                aws_region=aws_region,
            ),
            managed_cognito=(ManagedCognitoSetup.from_mapping(managed_raw) if managed_raw is not None else None),
            external_oidc=(ExternalOidcSetup.from_mapping(external_raw) if external_raw is not None else None),
        )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        if self.admin.subject is None:
            value["admin"].pop("subject")
        if self.managed_cognito is None:
            value.pop("managed_cognito")
        else:
            value["managed_cognito"]["oauth_callback_urls"] = list(self.managed_cognito.oauth_callback_urls)
        if self.external_oidc is None:
            value.pop("external_oidc")
        value["runtime"]["bedrock_invoke_resource_arns"] = list(self.runtime.bedrock_invoke_resource_arns)
        return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise AgentCoreSetupError(f"configuration contains duplicate JSON field {key!r}")
        value[key] = item
    return value


def _reject_non_finite_number(value: str) -> None:
    raise AgentCoreSetupError(f"configuration contains non-finite number {value}")


def load_agentcore_setup(path: str | Path) -> AgentCoreSetupConfig:
    config_path = Path(path)
    try:
        raw_bytes = config_path.read_bytes()
    except OSError as exc:
        raise AgentCoreSetupError(f"cannot read AgentCore setup file {config_path}: {exc}") from exc
    if len(raw_bytes) > 128 * 1024:
        raise AgentCoreSetupError("AgentCore setup file exceeds 128 KiB")
    try:
        raw = json.loads(
            raw_bytes,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite_number,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AgentCoreSetupError(f"AgentCore setup file is not valid UTF-8 JSON: {exc}") from exc
    return AgentCoreSetupConfig.from_mapping(raw)


def write_agentcore_setup(
    config: AgentCoreSetupConfig,
    path: str | Path,
) -> Path:
    output = Path(path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    payload = json.dumps(config.to_dict(), indent=2, sort_keys=True) + "\n"
    try:
        temporary.write_text(payload, encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return output


def redact_sensitive(value: Any) -> Any:
    """Return a log-safe copy of nested setup data."""
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).casefold()
            if any(part in normalized for part in _SENSITIVE_KEY_PARTS):
                redacted[key] = "<redacted>"
            else:
                redacted[key] = redact_sensitive(item)
        return redacted
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    return value


def _comma_values(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def config_from_args(args: argparse.Namespace) -> AgentCoreSetupConfig:
    mode = args.identity_mode
    mapping: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "target": "agentcore",
        "identity_mode": mode,
        "aws_region": args.aws_region,
        "tenant": {
            "tenant_id": args.tenant,
            "project_id": args.project,
            "project_name": args.project_name,
            "budget_limit": args.budget_limit,
        },
        "admin": {
            "user_name": args.admin_user_name,
            "email": args.admin_email,
            "display_name": args.admin_display_name,
        },
        "runtime": {
            "verified_image_uri": args.verified_image_uri,
            "bedrock_invoke_resource_arns": _comma_values(args.bedrock_invoke_resource_arns),
            "approved_https_prefix_list_id": (args.approved_https_prefix_list_id),
        },
    }
    if mode == MANAGED_COGNITO:
        callback_urls = list(args.oauth_callback_url or [])
        if not callback_urls:
            callback_urls = _comma_values(os.environ.get("AXON_OAUTH_CALLBACK_URLS"))
        mapping["managed_cognito"] = {
            "hosted_ui_domain_prefix": args.hosted_ui_domain_prefix,
            "oauth_callback_urls": callback_urls,
        }
    elif mode == EXTERNAL_OIDC:
        mapping["admin"]["subject"] = args.admin_subject
        mapping["external_oidc"] = {
            "issuer": args.oidc_issuer,
            "discovery_url": args.oidc_discovery_url,
            "client_id": args.oidc_client_id,
            "audience": args.oidc_audience,
            "tenant_claim": args.oidc_tenant_claim,
            "project_claim": args.oidc_project_claim,
        }
    return AgentCoreSetupConfig.from_mapping(mapping)


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


def add_setup_subcommands(subparsers: argparse._SubParsersAction) -> None:
    setup = subparsers.add_parser(
        "setup",
        help="Configure a local demo or authenticated AgentCore deployment",
    )
    targets = setup.add_subparsers(dest="setup_target")

    local = targets.add_parser(
        "local-demo",
        help="Start the anonymous seeded demo (non-production only)",
    )
    local.add_argument("--start", action="store_true")
    local.add_argument(
        "--acknowledge-non-production",
        action="store_true",
        help="Required with --start because this mode accepts anonymous traffic",
    )

    agentcore = targets.add_parser(
        "agentcore",
        help="Create or deploy an authenticated AgentCore setup file",
    )
    agentcore.add_argument("--config", help="Use an existing setup JSON file")
    agentcore.add_argument(
        "--output",
        default="axonllm-agentcore.json",
        help="Generated setup JSON path",
    )
    agentcore.add_argument("--deploy", action="store_true")
    agentcore.add_argument("--yes", action="store_true")
    agentcore.add_argument("--bootstrap-cdk", action="store_true")
    agentcore.add_argument("--show-config", action="store_true")
    agentcore.add_argument(
        "--identity-mode",
        choices=IDENTITY_MODES,
        default=_env("AXON_IDENTITY_MODE"),
    )
    agentcore.add_argument(
        "--aws-region",
        default=_env("AWS_DEFAULT_REGION", "us-east-1"),
    )
    agentcore.add_argument("--tenant", default=_env("AXON_TENANT_ID"))
    agentcore.add_argument("--project", default=_env("AXON_PROJECT_ID"))
    agentcore.add_argument(
        "--project-name",
        default=_env("AXON_PROJECT_NAME", "Production"),
    )
    agentcore.add_argument(
        "--budget-limit",
        type=float,
        default=_env("AXON_PROJECT_BUDGET_LIMIT"),
    )
    agentcore.add_argument(
        "--admin-user-name",
        default=_env("AXON_ADMIN_USER_NAME"),
    )
    agentcore.add_argument(
        "--admin-email",
        default=_env("AXON_ADMIN_EMAIL"),
    )
    agentcore.add_argument(
        "--admin-display-name",
        default=_env("AXON_ADMIN_DISPLAY_NAME", ""),
    )
    agentcore.add_argument(
        "--admin-subject",
        default=_env("AXON_ADMIN_SUBJECT"),
    )
    agentcore.add_argument(
        "--verified-image-uri",
        default=_env("AXON_VERIFIED_IMAGE_URI"),
    )
    agentcore.add_argument(
        "--bedrock-invoke-resource-arns",
        default=_env("AXON_BEDROCK_INVOKE_RESOURCE_ARNS"),
    )
    agentcore.add_argument(
        "--approved-https-prefix-list-id",
        default=_env("AXON_APPROVED_HTTPS_PREFIX_LIST_ID"),
    )
    agentcore.add_argument(
        "--hosted-ui-domain-prefix",
        default=_env("AXON_COGNITO_DOMAIN_PREFIX"),
    )
    agentcore.add_argument(
        "--oauth-callback-url",
        action="append",
        help="Repeat for every managed Cognito PKCE callback URL",
    )
    agentcore.add_argument(
        "--oidc-issuer",
        default=_env("AXON_OIDC_ISSUER"),
    )
    agentcore.add_argument(
        "--oidc-discovery-url",
        default=_env("AXON_OIDC_DISCOVERY_URL"),
    )
    agentcore.add_argument(
        "--oidc-client-id",
        default=_env("AXON_OIDC_CLIENT_ID"),
    )
    agentcore.add_argument(
        "--oidc-audience",
        default=_env("AXON_OIDC_AUDIENCE"),
    )
    agentcore.add_argument(
        "--oidc-tenant-claim",
        default=_env("AXON_OIDC_TENANT_CLAIM"),
    )
    agentcore.add_argument(
        "--oidc-project-claim",
        default=_env("AXON_OIDC_PROJECT_CLAIM"),
    )


def cmd_setup_agentcore(args: argparse.Namespace) -> None:
    if args.config:
        config_path = Path(args.config).expanduser().resolve()
        config = load_agentcore_setup(config_path)
    else:
        config = config_from_args(args)
        config_path = write_agentcore_setup(config, args.output)
        print(f"Wrote authenticated AgentCore setup: {config_path}")

    print(
        "Validated AgentCore setup: "
        f"{config.identity_mode}, {config.aws_region}, "
        f"tenant {config.tenant.tenant_id}, "
        f"project {config.tenant.project_id}"
    )
    if args.show_config:
        print(
            json.dumps(
                redact_sensitive(config.to_dict()),
                indent=2,
                sort_keys=True,
            )
        )
    if not args.deploy:
        print(f"Deploy with: ./deploy-agentcore.sh --config {config_path}")
        return

    command = [
        str(_REPO_ROOT / "deploy-agentcore.sh"),
        "--config",
        str(config_path),
    ]
    if args.yes:
        command.append("--yes")
    if args.bootstrap_cdk:
        command.append("--bootstrap-cdk")
    try:
        subprocess.run(command, cwd=_REPO_ROOT, check=True)
    except subprocess.CalledProcessError as exc:
        raise AgentCoreSetupError(f"AgentCore deployment failed with exit code {exc.returncode}") from exc


def local_demo_environment(
    source: dict[str, str] | None = None,
) -> dict[str, str]:
    environment = dict(source if source is not None else os.environ)
    environment.update(
        {
            "AXON_DEPLOYMENT_PROFILE": "development",
            "AXON_REQUIRE_CANONICAL_IDENTITY": "false",
            "LLM_ROUTER_DYNAMODB_ENABLED": "false",
            "AXON_AUTH_MODE": "LOG_ONLY",
            "AXON_LOAD_DEMO_DATA": "true",
        }
    )
    return environment


def cmd_setup_local_demo(args: argparse.Namespace) -> None:
    warning = "NON-PRODUCTION LOCAL DEMO: seeded fictional data and LOG_ONLY authentication accept anonymous requests."
    print(warning, file=os.sys.stderr)
    if not args.start:
        print("Start it explicitly with: uv run axon setup local-demo --start --acknowledge-non-production")
        return
    if not args.acknowledge_non_production:
        raise AgentCoreSetupError("--start requires --acknowledge-non-production")
    os.chdir(_REPO_ROOT)
    os.execvpe(
        "uv",
        ["uv", "run", "python", "serve_dashboard.py"],
        local_demo_environment(),
    )
