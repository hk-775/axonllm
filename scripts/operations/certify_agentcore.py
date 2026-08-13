#!/usr/bin/env python3
"""Certify a deployed AxonLLM AgentCore runtime through its JWT HTTPS API."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlencode
from uuid import uuid4

import sqlglot
from sqlglot import exp


REPORT_SCHEMA = "axonllm.agentcore-certification/v1"
ENABLED_PROVIDERS_PROFILE = "enabled-providers"
PRODUCTION_LAUNCH_PROFILE = "production-launch"
PRODUCTION_LAUNCH_PROVIDERS = frozenset(
    {
        "anthropic",
        "bedrock",
        "bedrock-mantle",
        "fireworks",
        "google_ai",
        "groq",
        "openai",
        "together",
        "xai",
    }
)
PRODUCTION_OPTIONAL_PROVIDERS = frozenset(
    {
        "ai21",
        "azure_openai",
        "cohere",
        "vertex_ai",
    }
)
PRODUCTION_ALLOWED_PROVIDERS = (
    PRODUCTION_LAUNCH_PROVIDERS | PRODUCTION_OPTIONAL_PROVIDERS
)
SUPPORTED_PROVIDER_FEATURES = frozenset(
    {"completion", "stream", "tool_calling"}
)
PRODUCTION_REQUIRED_PROVIDER_FEATURES = frozenset(
    {"completion", "stream"}
)
PRODUCTION_PROVIDER_FEATURES_BY_PROVIDER = {
    provider: (
        PRODUCTION_REQUIRED_PROVIDER_FEATURES
        if provider == "fireworks"
        else SUPPORTED_PROVIDER_FEATURES
    )
    for provider in PRODUCTION_ALLOWED_PROVIDERS
}
_ARN_PATTERN = re.compile(
    r"^arn:(?P<partition>aws(?:-[a-z]+)?):bedrock-agentcore:"
    r"(?P<region>[a-z0-9-]+):(?P<account>[0-9]{12}):"
    r"runtime/(?P<runtime>[A-Za-z][A-Za-z0-9_]{0,99}-[A-Za-z0-9]{10})$"
)
_ENV_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_QUALIFIER_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,47}$")
_ROLE_ARN_PATTERN = re.compile(
    r"^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]{1,512}$"
)
_MAX_CONFIG_BYTES = 256 * 1024
_DEFAULT_MAX_RESPONSE_BYTES = 10 * 1024 * 1024
_CANARY_CONTENT = "AXON_CANARY_OK"
_TOOL_NAME = "axon_launch_probe"
_TOOL_VALUE = "AXON_TOOL_OK"
_TOOL_RESULT_VALUE = "AXON_TOOL_RESULT_OK"
_TOOL_CONTINUATION_CONTENT = "AXON_TOOL_CONTINUATION_OK"
_UNSUPPORTED_PROVIDER_FEATURE = "unsupported_provider_feature"
_TENANT_CONFIG_FIELDS = frozenset(
    {
        "name",
        "budget_limit",
        "alert_threshold",
        "allowed_models",
        "guardrail_rules",
        "cache_enabled",
        "cache_ttl_seconds",
        "semantic_cache_enabled",
        "semantic_cache_threshold",
        "log_level",
        "log_destination",
        "prompt_caching_enabled",
        "ltm_enabled",
        "retention_period_hours",
        "rate_limit_rpm",
    }
)


class CertificationError(RuntimeError):
    """A credential-safe AgentCore certification failure."""


@dataclass(frozen=True)
class IdentityCases:
    active_env: str
    inactive_env: str
    ungranted_env: str
    cross_tenant_env: str
    admin_env: str | None
    viewer_env: str | None


@dataclass(frozen=True)
class ProviderCase:
    provider: str
    model: str
    features: frozenset[str] = frozenset({"completion", "stream"})


@dataclass(frozen=True)
class QueryCase:
    datasource_id: str
    sql: str
    max_rows: int
    role_arn: str
    region: str
    catalog: str
    database: str
    workgroup: str


@dataclass(frozen=True)
class TenantConfigCase:
    tenant_id: str
    project_id: str


@dataclass(frozen=True)
class CertificationConfig:
    profile: str
    region: str
    runtime_arn: str
    qualifier: str
    timeout_seconds: float
    max_response_bytes: int
    identities: IdentityCases
    providers: tuple[ProviderCase, ...]
    query: QueryCase
    tenant_config: TenantConfigCase | None


@dataclass(frozen=True)
class InvocationRequest:
    url: str
    payload: bytes
    headers: Mapping[str, str]


@dataclass(frozen=True)
class InvocationObservation:
    status_code: int | None
    latency_ms: float
    content_type: str
    body: bytes
    error_type: str | None = None


class Transport(Protocol):
    def __call__(
        self,
        request: InvocationRequest,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> InvocationObservation: ...


def _object(value: Any, location: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise CertificationError(f"{location} must be a JSON object")
    return value


def _strict_fields(
    value: dict[str, Any],
    location: str,
    *,
    required: set[str],
    optional: set[str] = set(),
) -> None:
    missing = sorted(required.difference(value))
    unexpected = sorted(set(value).difference(required | optional))
    if missing:
        raise CertificationError(f"{location} is missing required fields: {', '.join(missing)}")
    if unexpected:
        raise CertificationError(f"{location} contains unsupported fields: {', '.join(unexpected)}")


def _string(
    value: Any,
    location: str,
    *,
    maximum: int = 2048,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise CertificationError(f"{location} must be a non-empty safe string")
    return value


def _environment_name(value: Any, location: str) -> str:
    name = _string(value, location, maximum=128)
    if _ENV_PATTERN.fullmatch(name) is None:
        raise CertificationError(f"{location} must be an uppercase environment variable name")
    return name


def _identifier(value: Any, location: str) -> str:
    name = _string(value, location, maximum=128)
    if _NAME_PATTERN.fullmatch(name) is None:
        raise CertificationError(f"{location} contains unsupported characters")
    return name


def _positive_integer(
    value: Any,
    location: str,
    *,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise CertificationError(f"{location} must be an integer between 1 and {maximum}")
    return value


def _positive_number(value: Any, location: str, maximum: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0.1 <= float(value) <= maximum
    ):
        raise CertificationError(f"{location} must be between 0.1 and {maximum}")
    return float(value)


def _select_sql(value: Any) -> str:
    sql = _string(value, "query.sql", maximum=64 * 1024)
    try:
        statements = sqlglot.parse(
            sql,
            read="athena",
            error_level=sqlglot.ErrorLevel.RAISE,
            error_message_context=0,
        )
    except Exception as exc:
        raise CertificationError("query.sql must be valid Athena SQL") from exc
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        raise CertificationError("query.sql must contain exactly one SELECT")
    if any(isinstance(node, (exp.DDL, exp.DML, exp.Command, exp.Into)) for node in statements[0].walk()):
        raise CertificationError("query.sql must be read-only")
    return sql


def parse_config(value: Any) -> CertificationConfig:
    """Parse a complete launch-certification scenario without accepting secrets."""
    raw = _object(value, "configuration")
    _strict_fields(
        raw,
        "configuration",
        required={
            "schemaVersion",
            "region",
            "runtimeArn",
            "qualifier",
            "identities",
            "providers",
            "query",
        },
        optional={
            "timeoutSeconds",
            "maxResponseBytes",
            "profile",
            "tenantConfig",
        },
    )
    if raw["schemaVersion"] != 1:
        raise CertificationError("configuration.schemaVersion must be 1")
    profile = raw.get("profile", ENABLED_PROVIDERS_PROFILE)
    if profile not in {
        ENABLED_PROVIDERS_PROFILE,
        PRODUCTION_LAUNCH_PROFILE,
    }:
        raise CertificationError(
            "configuration.profile must be enabled-providers or "
            "production-launch"
        )
    runtime_arn = _string(raw["runtimeArn"], "runtimeArn")
    arn_match = _ARN_PATTERN.fullmatch(runtime_arn)
    if arn_match is None:
        raise CertificationError("runtimeArn is not an AgentCore runtime ARN")
    region = _identifier(raw["region"], "region")
    if arn_match.group("region") != region:
        raise CertificationError("runtimeArn region does not match region")
    qualifier = _string(raw["qualifier"], "qualifier", maximum=48)
    if _QUALIFIER_PATTERN.fullmatch(qualifier) is None:
        raise CertificationError("qualifier is not a valid endpoint name")

    raw_identities = _object(raw["identities"], "identities")
    _strict_fields(
        raw_identities,
        "identities",
        required={
            "activeCredentialEnv",
            "inactiveCredentialEnv",
            "ungrantedCredentialEnv",
            "crossTenantCredentialEnv",
        },
        optional={"adminCredentialEnv", "viewerCredentialEnv"},
    )
    has_admin = "adminCredentialEnv" in raw_identities
    has_viewer = "viewerCredentialEnv" in raw_identities
    has_tenant_config = "tenantConfig" in raw
    if len({has_admin, has_viewer, has_tenant_config}) != 1:
        raise CertificationError(
            "adminCredentialEnv, viewerCredentialEnv, and tenantConfig "
            "must be configured together"
        )
    if not has_tenant_config and profile != PRODUCTION_LAUNCH_PROFILE:
        raise CertificationError(
            "managed certification requires adminCredentialEnv, "
            "viewerCredentialEnv, and tenantConfig"
        )
    identities = IdentityCases(
        active_env=_environment_name(
            raw_identities["activeCredentialEnv"],
            "identities.activeCredentialEnv",
        ),
        inactive_env=_environment_name(
            raw_identities["inactiveCredentialEnv"],
            "identities.inactiveCredentialEnv",
        ),
        ungranted_env=_environment_name(
            raw_identities["ungrantedCredentialEnv"],
            "identities.ungrantedCredentialEnv",
        ),
        cross_tenant_env=_environment_name(
            raw_identities["crossTenantCredentialEnv"],
            "identities.crossTenantCredentialEnv",
        ),
        admin_env=(
            _environment_name(
                raw_identities["adminCredentialEnv"],
                "identities.adminCredentialEnv",
            )
            if has_admin
            else None
        ),
        viewer_env=(
            _environment_name(
                raw_identities["viewerCredentialEnv"],
                "identities.viewerCredentialEnv",
            )
            if has_viewer
            else None
        ),
    )
    identity_names = {
        identities.active_env,
        identities.inactive_env,
        identities.ungranted_env,
        identities.cross_tenant_env,
        identities.admin_env,
        identities.viewer_env,
    }
    identity_names.discard(None)
    expected_identity_count = 6 if has_admin else 4
    if len(identity_names) != expected_identity_count:
        raise CertificationError("identity credential environment names must be distinct")

    tenant_config: TenantConfigCase | None = None
    if has_tenant_config:
        raw_tenant_config = _object(
            raw["tenantConfig"],
            "tenantConfig",
        )
        _strict_fields(
            raw_tenant_config,
            "tenantConfig",
            required={"tenantId", "projectId"},
        )
        tenant_config = TenantConfigCase(
            tenant_id=_identifier(
                raw_tenant_config["tenantId"],
                "tenantConfig.tenantId",
            ),
            project_id=_identifier(
                raw_tenant_config["projectId"],
                "tenantConfig.projectId",
            ),
        )

    raw_providers = raw["providers"]
    if not isinstance(raw_providers, list) or not raw_providers:
        raise CertificationError("providers must be a non-empty JSON array")
    providers: list[ProviderCase] = []
    provider_names: set[str] = set()
    for index, raw_case in enumerate(raw_providers):
        location = f"providers[{index}]"
        case = _object(raw_case, location)
        _strict_fields(
            case,
            location,
            required={"provider", "model"},
            optional={"features"},
        )
        provider = _identifier(case["provider"], f"{location}.provider")
        if provider in provider_names:
            raise CertificationError("providers must contain exactly one case per provider")
        provider_names.add(provider)
        raw_features = case.get(
            "features",
            ["completion", "stream"],
        )
        if (
            not isinstance(raw_features, list)
            or not raw_features
            or any(
                not isinstance(feature, str)
                or feature not in SUPPORTED_PROVIDER_FEATURES
                for feature in raw_features
            )
            or len(raw_features) != len(set(raw_features))
        ):
            raise CertificationError(
                f"{location}.features must contain unique supported "
                "provider features"
            )
        features = frozenset(raw_features)
        if not PRODUCTION_REQUIRED_PROVIDER_FEATURES.issubset(features):
            raise CertificationError(
                f"{location}.features must include completion and stream"
            )
        expected_features = (
            PRODUCTION_PROVIDER_FEATURES_BY_PROVIDER.get(provider)
            if profile == PRODUCTION_LAUNCH_PROFILE
            else None
        )
        if expected_features is not None and features != expected_features:
            expected = ", ".join(sorted(expected_features))
            raise CertificationError(
                f"{location}.features must exactly match the production "
                f"launch contract for {provider}: {expected}"
            )
        providers.append(
            ProviderCase(
                provider=provider,
                model=_identifier(case["model"], f"{location}.model"),
                features=features,
            )
        )
    if profile == PRODUCTION_LAUNCH_PROFILE:
        missing_providers = (
            PRODUCTION_LAUNCH_PROVIDERS - provider_names
        )
        if missing_providers:
            raise CertificationError(
                "production-launch providers are missing mandatory "
                f"providers: {', '.join(sorted(missing_providers))}"
            )
        unsupported_providers = (
            provider_names - PRODUCTION_ALLOWED_PROVIDERS
        )
        if unsupported_providers:
            raise CertificationError(
                "production-launch providers contain unsupported "
                f"providers: {', '.join(sorted(unsupported_providers))}"
            )

    raw_query = _object(raw["query"], "query")
    _strict_fields(
        raw_query,
        "query",
        required={
            "catalog",
            "database",
            "datasourceId",
            "region",
            "roleArn",
            "sql",
            "workgroup",
        },
        optional={"maxRows"},
    )
    role_arn = _string(
        raw_query["roleArn"],
        "query.roleArn",
        maximum=600,
    )
    if _ROLE_ARN_PATTERN.fullmatch(role_arn) is None:
        raise CertificationError(
            "query.roleArn must be a concrete IAM role ARN"
        )
    query_region = _identifier(
        raw_query["region"],
        "query.region",
    )
    if query_region != region:
        raise CertificationError(
            "query.region must match the AgentCore region"
        )
    query = QueryCase(
        datasource_id=_identifier(
            raw_query["datasourceId"],
            "query.datasourceId",
        ),
        sql=_select_sql(raw_query["sql"]),
        max_rows=_positive_integer(
            raw_query.get("maxRows", 10),
            "query.maxRows",
            maximum=10_000,
        ),
        role_arn=role_arn,
        region=query_region,
        catalog=_identifier(
            raw_query["catalog"],
            "query.catalog",
        ),
        database=_identifier(
            raw_query["database"],
            "query.database",
        ),
        workgroup=_identifier(
            raw_query["workgroup"],
            "query.workgroup",
        ),
    )
    return CertificationConfig(
        profile=profile,
        region=region,
        runtime_arn=runtime_arn,
        qualifier=qualifier,
        timeout_seconds=_positive_number(
            raw.get("timeoutSeconds", 60),
            "timeoutSeconds",
            300,
        ),
        max_response_bytes=_positive_integer(
            raw.get("maxResponseBytes", _DEFAULT_MAX_RESPONSE_BYTES),
            "maxResponseBytes",
            maximum=100 * 1024 * 1024,
        ),
        identities=identities,
        providers=tuple(providers),
        query=query,
        tenant_config=tenant_config,
    )


def load_config(path: str | Path) -> CertificationConfig:
    config_path = Path(path)
    try:
        raw = config_path.read_bytes()
    except OSError as exc:
        raise CertificationError(f"cannot read configuration {config_path}") from exc
    if len(raw) > _MAX_CONFIG_BYTES:
        raise CertificationError("configuration exceeds 256 KiB")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CertificationError("configuration is not valid UTF-8 JSON") from exc
    return parse_config(value)


def invocation_url(config: CertificationConfig) -> str:
    """Build the documented OAuth invocation URL with an encoded runtime ARN."""
    encoded_arn = quote(config.runtime_arn, safe="")
    query = urlencode({"qualifier": config.qualifier})
    return f"https://bedrock-agentcore.{config.region}.amazonaws.com/runtimes/{encoded_arn}/invocations?{query}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read_bounded(response: Any, maximum: int) -> bytes:
    body = response.read(maximum + 1)
    if len(body) > maximum:
        raise CertificationError("response_too_large")
    return body


def urllib_transport(
    request: InvocationRequest,
    timeout_seconds: float,
    max_response_bytes: int,
) -> InvocationObservation:
    """Invoke AgentCore directly without SDK signing or redirects."""
    started = time.perf_counter()
    url_request = urllib.request.Request(
        request.url,
        data=request.payload,
        headers=dict(request.headers),
        method="POST",
    )
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(url_request, timeout=timeout_seconds) as response:
            body = _read_bounded(response, max_response_bytes)
            return InvocationObservation(
                status_code=response.getcode(),
                latency_ms=(time.perf_counter() - started) * 1000,
                content_type=response.headers.get("Content-Type", ""),
                body=body,
            )
    except urllib.error.HTTPError as exc:
        try:
            body = _read_bounded(exc, max_response_bytes)
            content_type = exc.headers.get("Content-Type", "")
        except CertificationError:
            body = b""
            content_type = ""
        finally:
            exc.close()
        return InvocationObservation(
            status_code=exc.code,
            latency_ms=(time.perf_counter() - started) * 1000,
            content_type=content_type,
            body=body,
        )
    except CertificationError:
        return InvocationObservation(
            status_code=None,
            latency_ms=(time.perf_counter() - started) * 1000,
            content_type="",
            body=b"",
            error_type="response_too_large",
        )
    except Exception:
        return InvocationObservation(
            status_code=None,
            latency_ms=(time.perf_counter() - started) * 1000,
            content_type="",
            body=b"",
            error_type="transport_error",
        )


def _credential(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if not isinstance(value, str) or not value or value != value.strip() or "\r" in value or "\n" in value:
        raise CertificationError(f"credential environment variable {name} is unavailable")
    return value


def _request(
    config: CertificationConfig,
    payload: Mapping[str, Any],
    *,
    token: str | None,
) -> InvocationRequest:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "User-Agent": "axonllm-agentcore-certification/1",
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": (f"axonllm-certification-{uuid4().hex}"),
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return InvocationRequest(
        url=invocation_url(config),
        payload=json.dumps(
            dict(payload),
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8"),
        headers=headers,
    )


def _safe_send(
    transport: Transport,
    request: InvocationRequest,
    config: CertificationConfig,
) -> InvocationObservation:
    try:
        result = transport(
            request,
            config.timeout_seconds,
            config.max_response_bytes,
        )
    except Exception:
        return InvocationObservation(
            None,
            0,
            "",
            b"",
            "transport_error",
        )
    if (
        not isinstance(result, InvocationObservation)
        or isinstance(result.status_code, bool)
        or (
            result.status_code is not None
            and (not isinstance(result.status_code, int) or not 100 <= result.status_code <= 599)
        )
        or isinstance(result.latency_ms, bool)
        or not isinstance(result.latency_ms, (int, float))
        or not math.isfinite(result.latency_ms)
        or result.latency_ms < 0
        or not isinstance(result.body, bytes)
        or len(result.body) > config.max_response_bytes
    ):
        return InvocationObservation(
            None,
            0,
            "",
            b"",
            "invalid_transport_result",
        )
    return result


def _json_body(observation: InvocationObservation) -> dict[str, Any] | None:
    try:
        value = json.loads(observation.body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if type(value) is dict else None


def _sse_events(observation: InvocationObservation) -> list[Any] | None:
    try:
        text = observation.body.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if text.startswith("\ufeff"):
        text = text.removeprefix("\ufeff")

    events: list[Any] = []
    data_lines: list[str] = []
    for line in text.splitlines():
        if not line:
            if not data_lines:
                continue
            try:
                events.append(json.loads("\n".join(data_lines)))
            except json.JSONDecodeError:
                return None
            data_lines.clear()
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "data":
            data_lines.append(value)
    if data_lines:
        return None
    return events or None


def _valid_usage(value: Any) -> bool:
    if type(value) is not dict:
        return False
    token_counts = (
        value.get("prompt_tokens"),
        value.get("completion_tokens"),
        value.get("total_tokens"),
    )
    if any(type(count) is not int or count < 0 for count in token_counts):
        return False
    prompt_tokens, completion_tokens, total_tokens = token_counts
    return total_tokens == prompt_tokens + completion_tokens


def _valid_completion_content(
    body: dict[str, Any] | None,
    case: ProviderCase,
    expected_content: str,
) -> bool:
    if (
        body is None
        or body.get("provider") != case.provider
        or body.get("model") != case.model
        or not _valid_usage(body.get("usage"))
    ):
        return False
    choices = body.get("choices")
    if type(choices) is not list or len(choices) != 1:
        return False
    choice = choices[0]
    if type(choice) is not dict:
        return False
    message = choice.get("message")
    return (
        type(message) is dict
        and message.get("role") == "assistant"
        and message.get("content") == expected_content
        and not message.get("tool_calls")
        and message.get("function_call") is None
    )


def _valid_completion_canary(
    body: dict[str, Any] | None,
    case: ProviderCase,
) -> bool:
    return _valid_completion_content(body, case, _CANARY_CONTENT)


def _valid_error_code(
    body: dict[str, Any] | None,
    expected_code: str,
) -> bool:
    if body is None:
        return False
    for container_name in ("detail", "error"):
        container = body.get(container_name)
        if (
            type(container) is dict
            and container.get("code") == expected_code
            and isinstance(container.get("message"), str)
            and bool(container["message"])
        ):
            return True
    return False


def _valid_stream_canary(
    events: list[Any] | None,
    case: ProviderCase,
) -> bool:
    if not events:
        return False

    provider_seen = False
    model_seen = False
    content_parts: list[str] = []
    done_seen = False
    for index, event in enumerate(events):
        if type(event) is not dict or "data" not in event:
            return False
        data = event["data"]
        if data == "[DONE]":
            if done_seen or index != len(events) - 1:
                return False
            done_seen = True
            continue
        if done_seen or type(data) is not dict or "error" in data:
            return False

        if "provider" in data:
            if data["provider"] != case.provider:
                return False
            provider_seen = True
        if "model" in data:
            if data["model"] != case.model:
                return False
            model_seen = True

        choices = data.get("choices")
        if type(choices) is not list or len(choices) != 1:
            return False
        choice = choices[0]
        if type(choice) is not dict:
            return False
        delta = choice.get("delta")
        if type(delta) is not dict:
            return False
        if delta.get("tool_calls") or delta.get("function_call") is not None:
            return False
        if "content" in delta:
            content = delta["content"]
            if not isinstance(content, str):
                return False
            content_parts.append(content)

    return done_seen and provider_seen and model_seen and "".join(content_parts) == _CANARY_CONTENT


def _tool_canary_call(
    body: dict[str, Any] | None,
    case: ProviderCase,
) -> dict[str, Any] | None:
    if (
        body is None
        or body.get("provider") != case.provider
        or body.get("model") != case.model
        or not _valid_usage(body.get("usage"))
    ):
        return None
    choices = body.get("choices")
    if type(choices) is not list or len(choices) != 1:
        return None
    choice = choices[0]
    if (
        type(choice) is not dict
        or choice.get("finish_reason") != "tool_calls"
    ):
        return None
    message = choice.get("message")
    calls = (
        message.get("tool_calls")
        if type(message) is dict
        else None
    )
    if type(calls) is not list or len(calls) != 1:
        return None
    call = calls[0]
    if type(call) is not dict:
        return None
    function = call.get("function")
    arguments = function.get("arguments") if type(function) is dict else None
    try:
        parsed_arguments = json.loads(arguments)
    except (TypeError, json.JSONDecodeError):
        return None
    valid = (
        isinstance(call.get("id"), str)
        and bool(call["id"])
        and call.get("type") == "function"
        and function.get("name") == _TOOL_NAME
        and parsed_arguments == {"token": _TOOL_VALUE}
        and message.get("function_call") is None
    )
    return call if valid else None


def _valid_tool_canary(
    body: dict[str, Any] | None,
    case: ProviderCase,
) -> bool:
    return _tool_canary_call(body, case) is not None


def _valid_stream_tool_canary(
    events: list[Any] | None,
    case: ProviderCase,
) -> bool:
    if not events:
        return False

    provider_seen = False
    model_seen = False
    done_seen = False
    finish_seen = False
    calls: dict[int, dict[str, Any]] = {}
    for event_index, event in enumerate(events):
        if type(event) is not dict or "data" not in event:
            return False
        data = event["data"]
        if data == "[DONE]":
            if done_seen or event_index != len(events) - 1:
                return False
            done_seen = True
            continue
        if done_seen or type(data) is not dict or "error" in data:
            return False
        if "provider" in data:
            if data["provider"] != case.provider:
                return False
            provider_seen = True
        if "model" in data:
            if data["model"] != case.model:
                return False
            model_seen = True

        choices = data.get("choices")
        if type(choices) is not list or len(choices) != 1:
            return False
        choice = choices[0]
        delta = choice.get("delta") if type(choice) is dict else None
        if type(delta) is not dict or delta.get("function_call") is not None:
            return False
        content = delta.get("content")
        if content not in (None, ""):
            return False
        finish_reason = choice.get("finish_reason")
        if finish_reason is not None:
            if finish_seen or finish_reason != "tool_calls":
                return False
            finish_seen = True

        raw_calls = delta.get("tool_calls", [])
        if type(raw_calls) is not list:
            return False
        for raw_call in raw_calls:
            if type(raw_call) is not dict:
                return False
            index = raw_call.get("index", 0)
            if type(index) is not int or index < 0:
                return False
            call = calls.setdefault(
                index,
                {"id": None, "type": None, "name": None, "arguments": []},
            )
            for source, target in (
                (raw_call.get("id"), "id"),
                (raw_call.get("type"), "type"),
            ):
                if source is not None:
                    if not isinstance(source, str) or (
                        call[target] is not None
                        and call[target] != source
                    ):
                        return False
                    call[target] = source
            function = raw_call.get("function", {})
            if type(function) is not dict:
                return False
            name = function.get("name")
            if name is not None:
                if not isinstance(name, str) or (
                    call["name"] is not None
                    and call["name"] != name
                ):
                    return False
                call["name"] = name
            arguments = function.get("arguments")
            if arguments is not None:
                if not isinstance(arguments, str):
                    return False
                call["arguments"].append(arguments)

    if (
        not done_seen
        or not finish_seen
        or not provider_seen
        or not model_seen
        or set(calls) != {0}
    ):
        return False
    call = calls[0]
    try:
        arguments = json.loads("".join(call["arguments"]))
    except json.JSONDecodeError:
        return False
    return (
        isinstance(call["id"], str)
        and bool(call["id"])
        and call["type"] == "function"
        and call["name"] == _TOOL_NAME
        and arguments == {"token": _TOOL_VALUE}
    )


def _body_evidence(observation: InvocationObservation) -> dict[str, Any]:
    return {
        "statusCode": observation.status_code,
        "latencyMs": round(float(observation.latency_ms), 3),
        "contentType": observation.content_type.split(";", 1)[0].strip().lower(),
        "responseBytes": len(observation.body),
        "responseSha256": hashlib.sha256(observation.body).hexdigest(),
        "transportError": observation.error_type,
    }


def _check(
    *,
    name: str,
    category: str,
    observation: InvocationObservation,
    passed: bool,
    validation: str,
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    result = {
        "name": name,
        "category": category,
        "passed": passed,
        "validation": validation,
        **_body_evidence(observation),
    }
    if provider is not None:
        result["provider"] = provider
    if model is not None:
        result["model"] = model
    return result


def _invoke_check(
    config: CertificationConfig,
    transport: Transport,
    *,
    name: str,
    category: str,
    payload: Mapping[str, Any],
    token: str | None,
    expected_statuses: set[int],
) -> tuple[InvocationObservation, dict[str, Any] | None]:
    observation = _safe_send(
        transport,
        _request(config, payload, token=token),
        config,
    )
    body = _json_body(observation)
    return observation, (body if observation.status_code in expected_statuses else None)


def _error_code(body: dict[str, Any] | None) -> str | None:
    if body is None:
        return None
    for container_name in ("detail", "error"):
        container = body.get(container_name)
        if type(container) is dict and isinstance(
            container.get("code"),
            str,
        ):
            return container["code"]
    return None


def _tenant_config_snapshot(
    body: dict[str, Any] | None,
    case: TenantConfigCase,
) -> tuple[int, str] | None:
    if (
        body is None
        or set(body) != {
            "tenant_id",
            "project_id",
            "revision",
            "config",
        }
        or body.get("tenant_id") != case.tenant_id
        or body.get("project_id") != case.project_id
    ):
        return None
    revision = body.get("revision")
    project_config = body.get("config")
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 0
        or type(project_config) is not dict
        or set(project_config) != _TENANT_CONFIG_FIELDS
    ):
        return None
    name = project_config.get("name")
    if (
        not isinstance(name, str)
        or not name
        or name != name.strip()
        or len(name) > 256
    ):
        return None
    return revision, name


def _failed_observation(error_type: str) -> InvocationObservation:
    return InvocationObservation(
        status_code=None,
        latency_ms=0,
        content_type="",
        body=b"",
        error_type=error_type,
    )


def _restore_tenant_config(
    config: CertificationConfig,
    transport: Transport,
    *,
    token: str,
    original_name: str,
    canary_name: str,
) -> bool:
    """Restore only a canary-owned name after an ambiguous mutation result."""
    case = config.tenant_config
    if case is None:
        return False
    for _attempt in range(3):
        read, read_body = _invoke_check(
            config,
            transport,
            name="tenant-config-cleanup-read",
            category="tenant_config_cleanup",
            payload={"action": "get_tenant_config"},
            token=token,
            expected_statuses={200},
        )
        snapshot = (
            _tenant_config_snapshot(read_body, case)
            if read.status_code == 200
            else None
        )
        if snapshot is None:
            continue
        revision, current_name = snapshot
        if current_name == original_name:
            return True
        if current_name != canary_name:
            return False
        rollback, rollback_body = _invoke_check(
            config,
            transport,
            name="tenant-config-cleanup-rollback",
            category="tenant_config_cleanup",
            payload={
                "action": "update_tenant_config",
                "expected_revision": revision,
                "config": {"name": original_name},
            },
            token=token,
            expected_statuses={200},
        )
        restored = (
            _tenant_config_snapshot(rollback_body, case)
            if rollback.status_code == 200
            else None
        )
        if (
            rollback.error_type is None
            and restored == (revision + 1, original_name)
        ):
            return True
    return False


def _tenant_config_checks(
    config: CertificationConfig,
    transport: Transport,
    *,
    admin: str,
    viewer: str,
    ungranted: str,
    cross_tenant: str,
) -> list[dict[str, Any]]:
    case = config.tenant_config
    if case is None:
        return []
    checks: list[dict[str, Any]] = []

    admin_read, admin_body = _invoke_check(
        config,
        transport,
        name="admin-tenant-config-read",
        category="admin_tenant_config_read",
        payload={"action": "get_tenant_config"},
        token=admin,
        expected_statuses={200},
    )
    original = _tenant_config_snapshot(admin_body, case)
    checks.append(
        _check(
            name="admin-tenant-config-read",
            category="admin_tenant_config_read",
            observation=admin_read,
            passed=original is not None,
            validation="canonical_tenant_project_config_snapshot",
        )
    )

    viewer_read, viewer_body = _invoke_check(
        config,
        transport,
        name="viewer-tenant-config-read",
        category="viewer_tenant_config_read",
        payload={"action": "get_tenant_config"},
        token=viewer,
        expected_statuses={200},
    )
    checks.append(
        _check(
            name="viewer-tenant-config-read",
            category="viewer_tenant_config_read",
            observation=viewer_read,
            passed=(
                original is not None
                and _tenant_config_snapshot(viewer_body, case) == original
                and viewer_body == admin_body
            ),
            validation="viewer_read_matches_admin_snapshot",
        )
    )

    for name, category, token in (
        (
            "tenant-config-project-isolation",
            "tenant_config_project_isolation",
            ungranted,
        ),
        (
            "tenant-config-tenant-isolation",
            "tenant_config_tenant_isolation",
            cross_tenant,
        ),
    ):
        isolation, _ = _invoke_check(
            config,
            transport,
            name=name,
            category=category,
            payload={"action": "get_tenant_config"},
            token=token,
            expected_statuses={404},
        )
        checks.append(
            _check(
                name=name,
                category=category,
                observation=isolation,
                passed=isolation.status_code == 404,
                validation="resource_existence_concealed",
            )
        )

    mutation_check_names = (
        (
            "viewer-tenant-config-mutation-denied",
            "viewer_tenant_config_mutation_denied",
            "viewer_config_mutation_denied_by_rbac",
        ),
        (
            "admin-tenant-config-cas-mutation",
            "admin_tenant_config_cas_mutation",
            "admin_config_cas_mutation_committed",
        ),
        (
            "admin-tenant-config-mutation-confirmed",
            "admin_tenant_config_mutation_confirmed",
            "admin_config_mutation_visible_on_strong_read",
        ),
        (
            "admin-tenant-config-cas-rollback",
            "admin_tenant_config_cas_rollback",
            "admin_config_cas_rollback_committed",
        ),
        (
            "admin-tenant-config-rollback-confirmed",
            "admin_tenant_config_rollback_confirmed",
            "admin_config_rollback_visible_on_strong_read",
        ),
    )

    def append_skipped(start: int, reason: str) -> None:
        for name, category, validation in mutation_check_names[start:]:
            checks.append(
                _check(
                    name=name,
                    category=category,
                    observation=_failed_observation(reason),
                    passed=False,
                    validation=validation,
                )
            )

    if original is None:
        append_skipped(0, "tenant_config_precondition_failed")
        return checks

    original_revision, original_name = original
    viewer_canary = f"Axon viewer denial {uuid4().hex[:24]}"
    viewer_write, viewer_write_body = _invoke_check(
        config,
        transport,
        name=mutation_check_names[0][0],
        category=mutation_check_names[0][1],
        payload={
            "action": "update_tenant_config",
            "expected_revision": original_revision,
            "config": {"name": viewer_canary},
        },
        token=viewer,
        expected_statuses={403},
    )
    viewer_denied = (
        viewer_write.status_code == 403
        and _error_code(viewer_write_body) == "authorization_denied"
    )
    checks.append(
        _check(
            name=mutation_check_names[0][0],
            category=mutation_check_names[0][1],
            observation=viewer_write,
            passed=viewer_denied,
            validation=mutation_check_names[0][2],
        )
    )
    if not viewer_denied:
        if not _restore_tenant_config(
            config,
            transport,
            token=admin,
            original_name=original_name,
            canary_name=viewer_canary,
        ):
            raise CertificationError(
                "viewer mutation denial failed and tenant configuration "
                "rollback is incomplete"
            )
        append_skipped(1, "viewer_mutation_denial_failed")
        return checks

    admin_canary = f"Axon admin CAS {uuid4().hex[:24]}"
    mutation, mutation_body = _invoke_check(
        config,
        transport,
        name=mutation_check_names[1][0],
        category=mutation_check_names[1][1],
        payload={
            "action": "update_tenant_config",
            "expected_revision": original_revision,
            "config": {"name": admin_canary},
        },
        token=admin,
        expected_statuses={200},
    )
    mutated = _tenant_config_snapshot(mutation_body, case)
    mutation_passed = mutated == (
        original_revision + 1,
        admin_canary,
    )
    checks.append(
        _check(
            name=mutation_check_names[1][0],
            category=mutation_check_names[1][1],
            observation=mutation,
            passed=mutation_passed,
            validation=mutation_check_names[1][2],
        )
    )
    if not mutation_passed or mutated is None:
        if not _restore_tenant_config(
            config,
            transport,
            token=admin,
            original_name=original_name,
            canary_name=admin_canary,
        ):
            raise CertificationError(
                "admin mutation failed and tenant configuration rollback "
                "is incomplete"
            )
        append_skipped(2, "admin_mutation_failed")
        return checks

    mutated_revision = mutated[0]
    confirmation, confirmation_body = _invoke_check(
        config,
        transport,
        name=mutation_check_names[2][0],
        category=mutation_check_names[2][1],
        payload={"action": "get_tenant_config"},
        token=admin,
        expected_statuses={200},
    )
    confirmation_passed = (
        _tenant_config_snapshot(confirmation_body, case) == mutated
    )
    checks.append(
        _check(
            name=mutation_check_names[2][0],
            category=mutation_check_names[2][1],
            observation=confirmation,
            passed=confirmation_passed,
            validation=mutation_check_names[2][2],
        )
    )
    if not confirmation_passed:
        if not _restore_tenant_config(
            config,
            transport,
            token=admin,
            original_name=original_name,
            canary_name=admin_canary,
        ):
            raise CertificationError(
                "admin mutation confirmation failed and tenant "
                "configuration rollback is incomplete"
            )
        append_skipped(3, "admin_mutation_confirmation_failed")
        return checks

    rollback, rollback_body = _invoke_check(
        config,
        transport,
        name=mutation_check_names[3][0],
        category=mutation_check_names[3][1],
        payload={
            "action": "update_tenant_config",
            "expected_revision": mutated_revision,
            "config": {"name": original_name},
        },
        token=admin,
        expected_statuses={200},
    )
    rolled_back = _tenant_config_snapshot(rollback_body, case)
    rollback_passed = rolled_back == (
        mutated_revision + 1,
        original_name,
    )
    checks.append(
        _check(
            name=mutation_check_names[3][0],
            category=mutation_check_names[3][1],
            observation=rollback,
            passed=rollback_passed,
            validation=mutation_check_names[3][2],
        )
    )
    if not rollback_passed or rolled_back is None:
        if not _restore_tenant_config(
            config,
            transport,
            token=admin,
            original_name=original_name,
            canary_name=admin_canary,
        ):
            raise CertificationError(
                "tenant configuration rollback is incomplete"
            )
        append_skipped(4, "admin_rollback_evidence_failed")
        return checks

    rollback_read, rollback_read_body = _invoke_check(
        config,
        transport,
        name=mutation_check_names[4][0],
        category=mutation_check_names[4][1],
        payload={"action": "get_tenant_config"},
        token=admin,
        expected_statuses={200},
    )
    rollback_confirmed = (
        _tenant_config_snapshot(rollback_read_body, case)
        == rolled_back
    )
    checks.append(
        _check(
            name=mutation_check_names[4][0],
            category=mutation_check_names[4][1],
            observation=rollback_read,
            passed=rollback_confirmed,
            validation=mutation_check_names[4][2],
        )
    )
    if not rollback_confirmed and not _restore_tenant_config(
        config,
        transport,
        token=admin,
        original_name=original_name,
        canary_name=admin_canary,
    ):
        raise CertificationError(
            "tenant configuration rollback confirmation failed and "
            "cleanup is incomplete"
        )
    return checks


def resolve_endpoint_metadata(
    control_client: Any,
    config: CertificationConfig,
) -> dict[str, str]:
    """Prove the qualifier points to one stable READY runtime version."""
    runtime_id = config.runtime_arn.rsplit("/", 1)[-1]
    try:
        response = control_client.get_agent_runtime_endpoint(
            agentRuntimeId=runtime_id,
            endpointName=config.qualifier,
        )
    except Exception as exc:
        raise CertificationError("unable to resolve the AgentCore runtime endpoint") from exc
    required = {
        "agentRuntimeArn": config.runtime_arn,
        "name": config.qualifier,
        "status": "READY",
    }
    for field_name, expected in required.items():
        if response.get(field_name) != expected:
            raise CertificationError(f"AgentCore endpoint metadata has unexpected {field_name}")
    live_version = response.get("liveVersion")
    target_version = response.get("targetVersion")
    endpoint_arn = response.get("agentRuntimeEndpointArn")
    if (
        not isinstance(live_version, str)
        or live_version != target_version
        or not live_version.isdigit()
        or not isinstance(endpoint_arn, str)
        or not endpoint_arn.startswith(f"{config.runtime_arn}/runtime-endpoint/")
    ):
        raise CertificationError("AgentCore endpoint is not on one stable runtime version")
    return {
        "runtimeArn": config.runtime_arn,
        "endpointArn": endpoint_arn,
        "endpointName": config.qualifier,
        "status": "READY",
        "runtimeVersion": live_version,
        "invocationUrl": invocation_url(config),
    }


def run_certification(
    config: CertificationConfig,
    *,
    environ: Mapping[str, str],
    transport: Transport = urllib_transport,
    endpoint_metadata: Mapping[str, str],
) -> dict[str, Any]:
    """Run auth, RBAC, provider, streaming, and query launch canaries."""
    active = _credential(environ, config.identities.active_env)
    inactive = _credential(environ, config.identities.inactive_env)
    ungranted = _credential(environ, config.identities.ungranted_env)
    cross_tenant = _credential(
        environ,
        config.identities.cross_tenant_env,
    )
    admin = (
        _credential(environ, config.identities.admin_env)
        if config.identities.admin_env is not None
        else None
    )
    viewer = (
        _credential(environ, config.identities.viewer_env)
        if config.identities.viewer_env is not None
        else None
    )
    checks: list[dict[str, Any]] = []

    denial_cases = (
        ("missing-jwt", "missing_jwt_denied", None, {401, 403}),
        (
            "invalid-jwt",
            "invalid_jwt_denied",
            "invalid.jwt.signature",
            {401, 403},
        ),
        (
            "inactive-membership",
            "inactive_membership_denied",
            inactive,
            {403},
        ),
        (
            "missing-project-grant",
            "missing_project_grant_denied",
            ungranted,
            {404},
        ),
        (
            "cross-tenant",
            "cross_tenant_denied",
            cross_tenant,
            {404},
        ),
    )
    for name, category, token, expected in denial_cases:
        observation, _ = _invoke_check(
            config,
            transport,
            name=name,
            category=category,
            payload={"action": "list_models"},
            token=token,
            expected_statuses=expected,
        )
        checks.append(
            _check(
                name=name,
                category=category,
                observation=observation,
                passed=observation.status_code in expected,
                validation="expected_denial_status",
            )
        )

    observation, body = _invoke_check(
        config,
        transport,
        name="payload-identity-rejected",
        category="payload_identity_rejected",
        payload={
            "action": "list_models",
            "tenant_id": "attacker-controlled",
        },
        token=active,
        expected_statuses={400},
    )
    checks.append(
        _check(
            name="payload-identity-rejected",
            category="payload_identity_rejected",
            observation=observation,
            passed=(observation.status_code == 400 and body is not None),
            validation="identity_field_rejected",
        )
    )

    if config.tenant_config is not None:
        if admin is None or viewer is None:
            raise CertificationError(
                "managed tenant configuration identities are unavailable"
            )
        checks.extend(
            _tenant_config_checks(
                config,
                transport,
                admin=admin,
                viewer=viewer,
                ungranted=ungranted,
                cross_tenant=cross_tenant,
            )
        )

    observation, body = _invoke_check(
        config,
        transport,
        name="health",
        category="liveness",
        payload={"action": "health"},
        token=active,
        expected_statuses={200},
    )
    checks.append(
        _check(
            name="health",
            category="liveness",
            observation=observation,
            passed=(body is not None and body.get("status") == "alive" and body.get("ready") is False),
            validation="liveness_contract",
        )
    )

    observation, body = _invoke_check(
        config,
        transport,
        name="readiness",
        category="dependency_readiness",
        payload={"action": "readiness"},
        token=active,
        expected_statuses={200},
    )
    checks.append(
        _check(
            name="readiness",
            category="dependency_readiness",
            observation=observation,
            passed=(
                body is not None
                and body.get("status") == "ready"
                and body.get("ready") is True
                and isinstance(body.get("dependencies"), dict)
                and all(value == "ready" for value in body["dependencies"].values())
            ),
            validation="all_runtime_dependencies_ready",
        )
    )

    observation, body = _invoke_check(
        config,
        transport,
        name="list-models",
        category="model_listing",
        payload={"action": "list_models"},
        token=active,
        expected_statuses={200},
    )
    advertised: set[str] = set()
    if body is not None and isinstance(body.get("models"), list):
        for model in body["models"]:
            if isinstance(model, dict) and isinstance(
                model.get("providers"),
                list,
            ):
                advertised.update(provider for provider in model["providers"] if isinstance(provider, str))
    expected_providers = {case.provider for case in config.providers}
    checks.append(
        _check(
            name="list-models",
            category="model_listing",
            observation=observation,
            passed=(
                body is not None
                and isinstance(body.get("models"), list)
                and bool(body["models"])
                and expected_providers <= advertised
            ),
            validation="all_certified_providers_advertised",
        )
    )

    for case in config.providers:
        payload = {
            "action": "chat",
            "model": case.model,
            "provider": case.provider,
            "messages": [
                {
                    "role": "user",
                    "content": "Reply with exactly AXON_CANARY_OK.",
                }
            ],
            "max_tokens": 32,
            "temperature": 0,
            "stream": False,
        }
        observation, body = _invoke_check(
            config,
            transport,
            name=f"{case.provider}-completion",
            category="provider_completion",
            payload=payload,
            token=active,
            expected_statuses={200},
        )
        checks.append(
            _check(
                name=f"{case.provider}-completion",
                category="provider_completion",
                observation=observation,
                passed=_valid_completion_canary(body, case),
                validation="exact_provider_model_canary_and_usage",
                provider=case.provider,
                model=case.model,
            )
        )

        streaming_payload = dict(payload)
        streaming_payload["stream"] = True
        stream_observation, _ = _invoke_check(
            config,
            transport,
            name=f"{case.provider}-stream",
            category="provider_stream",
            payload=streaming_payload,
            token=active,
            expected_statuses={200},
        )
        events = _sse_events(stream_observation) if stream_observation.status_code == 200 else None
        checks.append(
            _check(
                name=f"{case.provider}-stream",
                category="provider_stream",
                observation=stream_observation,
                passed=_valid_stream_canary(events, case),
                validation="exact_provider_model_canary_sse_and_done",
                provider=case.provider,
                model=case.model,
            )
        )

        if "tool_calling" in case.features:
            tool_definition = {
                "type": "function",
                "function": {
                    "name": _TOOL_NAME,
                    "description": (
                        "Return the production launch probe token."
                    ),
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "token": {
                                "type": "string",
                                "enum": [_TOOL_VALUE],
                            }
                        },
                        "required": ["token"],
                    },
                },
            }
            tool_payload = {
                **payload,
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            f"Call {_TOOL_NAME} exactly once with token "
                            f"{_TOOL_VALUE}. Do not answer directly. After the "
                            "tool result, reply with exactly "
                            f"{_TOOL_CONTINUATION_CONTENT}."
                        ),
                    }
                ],
                "max_tokens": 64,
                "tools": [tool_definition],
                "tool_choice": "auto",
            }
            tool_observation, tool_body = _invoke_check(
                config,
                transport,
                name=f"{case.provider}-tool-call",
                category="provider_tool_call",
                payload=tool_payload,
                token=active,
                expected_statuses={200},
            )
            tool_call = _tool_canary_call(tool_body, case)
            checks.append(
                _check(
                    name=f"{case.provider}-tool-call",
                    category="provider_tool_call",
                    observation=tool_observation,
                    passed=tool_call is not None,
                    validation=(
                        "automatic_exact_provider_model_tool_call_and_arguments"
                    ),
                    provider=case.provider,
                    model=case.model,
                )
            )

            required_tool_payload = {
                **tool_payload,
                "tool_choice": "required",
            }
            required_statuses = (
                {400} if case.provider == "cohere" else {200}
            )
            required_observation, required_body = _invoke_check(
                config,
                transport,
                name=f"{case.provider}-tool-required",
                category="provider_tool_required",
                payload=required_tool_payload,
                token=active,
                expected_statuses=required_statuses,
            )
            required_passed = (
                required_observation.status_code == 400
                and _valid_error_code(
                    required_body,
                    _UNSUPPORTED_PROVIDER_FEATURE,
                )
                if case.provider == "cohere"
                else _tool_canary_call(required_body, case) is not None
            )
            checks.append(
                _check(
                    name=f"{case.provider}-tool-required",
                    category="provider_tool_required",
                    observation=required_observation,
                    passed=required_passed,
                    validation=(
                        "required_tool_selection_explicitly_unsupported"
                        if case.provider == "cohere"
                        else (
                            "required_exact_provider_model_tool_call_and_"
                            "arguments"
                        )
                    ),
                    provider=case.provider,
                    model=case.model,
                )
            )

            if tool_call is None:
                continuation_observation = InvocationObservation(
                    status_code=None,
                    latency_ms=0,
                    content_type="",
                    body=b"",
                    error_type="required_tool_call_failed",
                )
                continuation_body = None
            else:
                continuation_payload = {
                    **tool_payload,
                    "messages": [
                        *tool_payload["messages"],
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [tool_call],
                        },
                        {
                            "role": "tool",
                            "tool_call_id": tool_call["id"],
                            "content": json.dumps(
                                {"result": _TOOL_RESULT_VALUE},
                                separators=(",", ":"),
                            ),
                        },
                    ],
                }
                continuation_observation, continuation_body = _invoke_check(
                    config,
                    transport,
                    name=f"{case.provider}-tool-continuation",
                    category="provider_tool_continuation",
                    payload=continuation_payload,
                    token=active,
                    expected_statuses={200},
                )
            checks.append(
                _check(
                    name=f"{case.provider}-tool-continuation",
                    category="provider_tool_continuation",
                    observation=continuation_observation,
                    passed=_valid_completion_content(
                        continuation_body,
                        case,
                        _TOOL_CONTINUATION_CONTENT,
                    ),
                    validation=(
                        "provider_tool_result_round_trip_and_exact_continuation"
                    ),
                    provider=case.provider,
                    model=case.model,
                )
            )

            no_tool_payload = {
                **payload,
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "Do not call any tool. Reply with exactly "
                            f"{_CANARY_CONTENT}."
                        ),
                    }
                ],
                "tools": [tool_definition],
                "tool_choice": "none",
            }
            no_tool_observation, no_tool_body = _invoke_check(
                config,
                transport,
                name=f"{case.provider}-tool-none",
                category="provider_tool_none",
                payload=no_tool_payload,
                token=active,
                expected_statuses={200},
            )
            checks.append(
                _check(
                    name=f"{case.provider}-tool-none",
                    category="provider_tool_none",
                    observation=no_tool_observation,
                    passed=_valid_completion_canary(no_tool_body, case),
                    validation="tool_choice_none_prevents_tool_calls",
                    provider=case.provider,
                    model=case.model,
                )
            )

            stream_tool_payload = {
                **tool_payload,
                "stream": True,
                "tool_choice": "auto",
            }
            stream_tool_observation, _ = _invoke_check(
                config,
                transport,
                name=f"{case.provider}-tool-stream",
                category="provider_tool_stream",
                payload=stream_tool_payload,
                token=active,
                expected_statuses={200},
            )
            stream_tool_events = (
                _sse_events(stream_tool_observation)
                if stream_tool_observation.status_code == 200
                else None
            )
            checks.append(
                _check(
                    name=f"{case.provider}-tool-stream",
                    category="provider_tool_stream",
                    observation=stream_tool_observation,
                    passed=_valid_stream_tool_canary(
                        stream_tool_events,
                        case,
                    ),
                    validation=(
                        "automatic_streamed_tool_call_and_arguments"
                    ),
                    provider=case.provider,
                    model=case.model,
                )
            )

    request_id = f"cert-{uuid4().hex}"
    observation, body = _invoke_check(
        config,
        transport,
        name="query-select",
        category="query_select",
        payload={
            "action": "query",
            "datasource_id": config.query.datasource_id,
            "sql": config.query.sql,
            "max_rows": config.query.max_rows,
            "request_id": request_id,
        },
        token=viewer or active,
        expected_statuses={200},
    )
    checks.append(
        _check(
            name="query-select",
            category="query_select",
            observation=observation,
            passed=(
                body is not None
                and body.get("request_id") == request_id
                and body.get("datasource_id") == config.query.datasource_id
                and isinstance(body.get("rows"), list)
                and isinstance(body.get("statistics"), dict)
            ),
            validation="bounded_query_response_contract",
        )
    )

    observation, _ = _invoke_check(
        config,
        transport,
        name="query-mutation-denied",
        category="query_mutation_denied",
        payload={
            "action": "query",
            "datasource_id": config.query.datasource_id,
            "sql": "DELETE FROM launch_canary",
            "max_rows": 1,
        },
        token=viewer or active,
        expected_statuses={400, 403},
    )
    checks.append(
        _check(
            name="query-mutation-denied",
            category="query_mutation_denied",
            observation=observation,
            passed=observation.status_code in {400, 403},
            validation="mutation_rejected",
        )
    )

    passed = all(check["passed"] for check in checks)
    return {
        "schema": REPORT_SCHEMA,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "overallStatus": "PASS" if passed else "FAIL",
        "endpoint": dict(endpoint_metadata),
        "summary": {
            "checkCount": len(checks),
            "passed": sum(1 for check in checks if check["passed"]),
            "failed": sum(1 for check in checks if not check["passed"]),
            "providerCount": len(config.providers),
            "profile": config.profile,
            "providerFeatures": {
                case.provider: sorted(case.features)
                for case in sorted(
                    config.providers,
                    key=lambda item: item.provider,
                )
            },
            "queryBackendExercised": True,
            "tenantConfigRbacExercised": (
                config.tenant_config is not None
            ),
            "agentcoreHttpsInvoked": True,
        },
        "checks": checks,
    }


def _write_report(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(dict(report), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Certify a deployed AxonLLM AgentCore runtime",
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        import boto3

        endpoint = resolve_endpoint_metadata(
            boto3.client(
                "bedrock-agentcore-control",
                region_name=config.region,
            ),
            config,
        )
        report = run_certification(
            config,
            environ=os.environ,
            endpoint_metadata=endpoint,
        )
        _write_report(
            Path(args.output).expanduser().resolve(),
            report,
        )
    except CertificationError as exc:
        print(f"AgentCore certification failed: {exc}", file=sys.stderr)
        return 2
    print(
        f"AgentCore certification: {report['overallStatus']} "
        f"({report['summary']['passed']}/{report['summary']['checkCount']})"
    )
    return 0 if report["overallStatus"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
