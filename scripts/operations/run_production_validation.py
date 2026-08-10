#!/usr/bin/env python3
"""Run AxonLLM production RBAC canaries and read-only HTTP load.

The scenario file contains request shapes and credential *environment variable*
names. Credential values are read only at request time and are never included in
the JSON report. The load request is restricted to GET or HEAD so a validation
run cannot intentionally generate mutation traffic.

This tool validates HTTP behavior and the checked-out authorization contract. It
does not exercise a SQL/query backend and does not validate AgentCore cutover.

Example:
    python scripts/operations/run_production_validation.py \
      --config scripts/operations/production_validation.example.json \
      --base-url https://task-a.example.test \
      --base-url https://task-b.example.test \
      --output production-validation.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.gateway.auth.authorization import Action, ResourceRef, authorize
from src.gateway.models import (
    AuthMethod,
    MembershipStatus,
    Principal,
    TenantRole,
)


REPORT_SCHEMA = "axonllm.production-validation/v1"
REQUIRED_CANARY_CATEGORIES = frozenset(
    {
        "authenticated_read_allowed",
        "viewer_mutation_denied",
        "cross_tenant_denied",
        "ungranted_project_denied",
    }
)
SUPPORTED_TARGETS = frozenset({"fargate", "agentcore-http", "generic"})
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
READ_METHODS = frozenset({"GET", "HEAD"})
SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "cookie",
        "host",
        "proxy-authorization",
        "x-api-key",
    }
)
SENSITIVE_BODY_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "authorization",
        "password",
        "refresh_token",
        "secret",
        "token",
    }
)
NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
ENV_PATTERN = re.compile(r"[A-Z_][A-Z0-9_]{0,127}")
HEADER_PATTERN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")


class ConfigurationError(ValueError):
    """A safe-to-report configuration failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CredentialUnavailable(RuntimeError):
    """Raised without retaining a credential value."""


@dataclass(frozen=True)
class RequestSpec:
    method: str
    path: str
    expected_statuses: tuple[int, ...]
    credential_env: str
    credential_type: str
    headers: tuple[tuple[str, str], ...]
    body: bytes | None


@dataclass(frozen=True)
class CanarySpec:
    name: str
    category: str
    request: RequestSpec


@dataclass(frozen=True)
class LoadSpec:
    request: RequestSpec
    request_count: int
    concurrency: int
    minimum_endpoints: int
    max_error_rate: float
    max_p95_latency_ms: float


@dataclass(frozen=True)
class ValidationConfig:
    target: str
    timeout_seconds: float
    canaries: tuple[CanarySpec, ...]
    load: LoadSpec


@dataclass(frozen=True)
class HttpRequest:
    url: str
    method: str
    headers: Mapping[str, str]
    body: bytes | None


@dataclass(frozen=True)
class HttpObservation:
    status_code: int | None
    latency_ms: float
    error_type: str | None = None


class Transport(Protocol):
    def __call__(
        self,
        request: HttpRequest,
        timeout_seconds: float,
    ) -> HttpObservation: ...


def _mapping(value: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigurationError(
            "invalid_configuration",
            f"{location} must be a JSON object",
        )
    return value


def _integer(
    value: Any,
    location: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(
            "invalid_configuration",
            f"{location} must be an integer",
        )
    if not minimum <= value <= maximum:
        raise ConfigurationError(
            "invalid_configuration",
            f"{location} must be between {minimum} and {maximum}",
        )
    return value


def _number(
    value: Any,
    location: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(
            "invalid_configuration",
            f"{location} must be a number",
        )
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ConfigurationError(
            "invalid_configuration",
            f"{location} must be between {minimum} and {maximum}",
        )
    return result


def _status_codes(value: Any, location: str) -> tuple[int, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigurationError(
            "invalid_configuration",
            f"{location} must be a non-empty JSON array",
        )
    statuses = tuple(
        sorted(
            {
                _integer(
                    status,
                    f"{location} entry",
                    minimum=100,
                    maximum=599,
                )
                for status in value
            }
        )
    )
    if len(statuses) != len(value):
        raise ConfigurationError(
            "invalid_configuration",
            f"{location} must not contain duplicates",
        )
    return statuses


def _contains_sensitive_body_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in SENSITIVE_BODY_KEYS:
                return True
            if _contains_sensitive_body_key(child):
                return True
    elif isinstance(value, list):
        return any(_contains_sensitive_body_key(child) for child in value)
    return False


def _parse_headers(
    value: Any,
    location: str,
) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    headers = _mapping(value, location)
    parsed: list[tuple[str, str]] = []
    for key, header_value in headers.items():
        if not isinstance(key, str) or HEADER_PATTERN.fullmatch(key) is None:
            raise ConfigurationError(
                "invalid_configuration",
                f"{location} contains an invalid header name",
            )
        if key.lower() in SENSITIVE_HEADERS:
            raise ConfigurationError(
                "secret_in_configuration",
                f"{location} must not contain credential or routing headers",
            )
        if not isinstance(header_value, str):
            raise ConfigurationError(
                "invalid_configuration",
                f"{location} values must be strings",
            )
        if (
            not header_value
            or len(header_value) > 4096
            or "\r" in header_value
            or "\n" in header_value
        ):
            raise ConfigurationError(
                "invalid_configuration",
                f"{location} contains an invalid header value",
            )
        parsed.append((key, header_value))
    return tuple(sorted(parsed, key=lambda item: item[0].lower()))


def _parse_request(
    value: Any,
    location: str,
) -> RequestSpec:
    request = _mapping(value, location)
    method = request.get("method")
    if not isinstance(method, str) or not method.isalpha():
        raise ConfigurationError(
            "invalid_configuration",
            f"{location}.method must contain only letters",
        )
    method = method.upper()

    path = request.get("path")
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or path.startswith("//")
        or len(path) > 2048
        or any(ord(character) < 32 for character in path)
    ):
        raise ConfigurationError(
            "invalid_configuration",
            f"{location}.path must be an absolute HTTP path",
        )
    parsed_path = urlsplit(path)
    if (
        parsed_path.scheme
        or parsed_path.netloc
        or parsed_path.query
        or parsed_path.fragment
    ):
        raise ConfigurationError(
            "invalid_configuration",
            f"{location}.path must not contain a host, query, or fragment",
        )

    credential_env = request.get("credentialEnv")
    if (
        not isinstance(credential_env, str)
        or ENV_PATTERN.fullmatch(credential_env) is None
    ):
        raise ConfigurationError(
            "invalid_configuration",
            f"{location}.credentialEnv must be an uppercase environment name",
        )
    credential_type = request.get("credentialType", "bearer")
    if credential_type not in {
        "alb-session-cookie",
        "bearer",
        "x-api-key",
    }:
        raise ConfigurationError(
            "invalid_configuration",
            f"{location}.credentialType must be alb-session-cookie, bearer, "
            "or x-api-key",
        )

    body: bytes | None = None
    if "jsonBody" in request:
        json_body = request["jsonBody"]
        if _contains_sensitive_body_key(json_body):
            raise ConfigurationError(
                "secret_in_configuration",
                f"{location}.jsonBody contains a sensitive field name",
            )
        try:
            body = json.dumps(
                json_body,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(
                "invalid_configuration",
                f"{location}.jsonBody must be valid JSON",
            ) from exc
        if len(body) > 65536:
            raise ConfigurationError(
                "invalid_configuration",
                f"{location}.jsonBody exceeds 64 KiB",
            )

    return RequestSpec(
        method=method,
        path=path,
        expected_statuses=_status_codes(
            request.get("expectedStatuses"),
            f"{location}.expectedStatuses",
        ),
        credential_env=credential_env,
        credential_type=credential_type,
        headers=_parse_headers(request.get("headers"), f"{location}.headers"),
        body=body,
    )


def _validate_canary_semantics(canary: CanarySpec) -> None:
    request = canary.request
    location = f"canary {canary.name}"
    if canary.category == "authenticated_read_allowed":
        if request.method not in READ_METHODS or not all(
            200 <= status < 300 for status in request.expected_statuses
        ):
            raise ConfigurationError(
                "invalid_canary_contract",
                f"{location} must be a read expecting only 2xx statuses",
            )
    elif canary.category == "viewer_mutation_denied":
        if (
            request.method not in WRITE_METHODS
            or request.expected_statuses != (403,)
        ):
            raise ConfigurationError(
                "invalid_canary_contract",
                f"{location} must be a mutation expecting exactly 403",
            )
    elif (
        request.method not in READ_METHODS
        or request.expected_statuses != (403,)
    ):
        raise ConfigurationError(
            "invalid_canary_contract",
            f"{location} must be a read expecting exactly 403",
        )


def parse_config(value: Any) -> ValidationConfig:
    """Parse and fail closed on an incomplete production validation scenario."""
    raw = _mapping(value, "configuration")
    if raw.get("schemaVersion") != 1:
        raise ConfigurationError(
            "unsupported_schema",
            "configuration.schemaVersion must be 1",
        )
    target = raw.get("target")
    if target not in SUPPORTED_TARGETS:
        raise ConfigurationError(
            "invalid_configuration",
            "configuration.target must be fargate, agentcore-http, or generic",
        )
    timeout_seconds = _number(
        raw.get("timeoutSeconds", 10),
        "configuration.timeoutSeconds",
        minimum=0.1,
        maximum=120,
    )

    raw_canaries = raw.get("canaries")
    if not isinstance(raw_canaries, list) or not raw_canaries:
        raise ConfigurationError(
            "missing_required_canaries",
            "configuration.canaries must be a non-empty JSON array",
        )
    canaries: list[CanarySpec] = []
    names: set[str] = set()
    for index, value in enumerate(raw_canaries):
        location = f"configuration.canaries[{index}]"
        raw_canary = _mapping(value, location)
        name = raw_canary.get("name")
        if not isinstance(name, str) or NAME_PATTERN.fullmatch(name) is None:
            raise ConfigurationError(
                "invalid_configuration",
                f"{location}.name is invalid",
            )
        if name in names:
            raise ConfigurationError(
                "invalid_configuration",
                "canary names must be unique",
            )
        names.add(name)
        category = raw_canary.get("category")
        if category not in REQUIRED_CANARY_CATEGORIES:
            raise ConfigurationError(
                "invalid_configuration",
                f"{location}.category is unsupported",
            )
        canary = CanarySpec(
            name=name,
            category=category,
            request=_parse_request(raw_canary, location),
        )
        _validate_canary_semantics(canary)
        canaries.append(canary)

    categories = {canary.category for canary in canaries}
    missing_categories = REQUIRED_CANARY_CATEGORIES.difference(categories)
    if missing_categories:
        raise ConfigurationError(
            "missing_required_canaries",
            "configuration does not cover every required canary category",
        )

    raw_load = _mapping(raw.get("load"), "configuration.load")
    load_request = _parse_request(
        raw_load.get("request"),
        "configuration.load.request",
    )
    if load_request.method not in READ_METHODS or load_request.body is not None:
        raise ConfigurationError(
            "unsafe_load_request",
            "configuration.load.request must be a bodyless GET or HEAD",
        )
    if not all(200 <= status < 300 for status in load_request.expected_statuses):
        raise ConfigurationError(
            "invalid_configuration",
            "configuration.load.request must expect only 2xx statuses",
        )
    request_count = _integer(
        raw_load.get("requestCount"),
        "configuration.load.requestCount",
        minimum=2,
        maximum=100000,
    )
    concurrency = _integer(
        raw_load.get("concurrency"),
        "configuration.load.concurrency",
        minimum=1,
        maximum=1000,
    )
    if concurrency > request_count:
        raise ConfigurationError(
            "invalid_configuration",
            "configuration.load.concurrency cannot exceed requestCount",
        )
    load = LoadSpec(
        request=load_request,
        request_count=request_count,
        concurrency=concurrency,
        minimum_endpoints=_integer(
            raw_load.get("minimumEndpoints", 2),
            "configuration.load.minimumEndpoints",
            minimum=1,
            maximum=100,
        ),
        max_error_rate=_number(
            raw_load.get("maxErrorRate"),
            "configuration.load.maxErrorRate",
            minimum=0,
            maximum=1,
        ),
        max_p95_latency_ms=_number(
            raw_load.get("maxP95LatencyMs"),
            "configuration.load.maxP95LatencyMs",
            minimum=0.001,
            maximum=3600000,
        ),
    )
    return ValidationConfig(
        target=target,
        timeout_seconds=timeout_seconds,
        canaries=tuple(canaries),
        load=load,
    )


def normalize_base_urls(values: list[str]) -> tuple[str, ...]:
    """Normalize one or more distinct HTTPS endpoints."""
    normalized: list[str] = []
    for index, value in enumerate(values):
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or any(ord(character) < 32 for character in value)
        ):
            raise ConfigurationError(
                "invalid_base_url",
                f"base URL {index + 1} is invalid",
            )
        parsed = urlsplit(value)
        try:
            parsed.port
        except ValueError as exc:
            raise ConfigurationError(
                "invalid_base_url",
                f"base URL {index + 1} has an invalid port",
            ) from exc
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ConfigurationError(
                "invalid_base_url",
                f"base URL {index + 1} must be an HTTPS origin without credentials",
            )
        clean = value.rstrip("/")
        if clean in normalized:
            raise ConfigurationError(
                "insufficient_endpoints",
                "base URLs must be distinct",
            )
        normalized.append(clean)
    if not normalized:
        raise ConfigurationError(
            "insufficient_endpoints",
            "production validation requires at least one base URL",
        )
    return tuple(normalized)


def validate_endpoint_count(
    config: ValidationConfig,
    endpoints: tuple[str, ...],
) -> None:
    if len(endpoints) < config.load.minimum_endpoints:
        raise ConfigurationError(
            "insufficient_endpoints",
            "configuration.load.minimumEndpoints exceeds the number of "
            "distinct base URLs",
        )


def _credential_headers(
    request: RequestSpec,
    environ: Mapping[str, str],
) -> dict[str, str]:
    credential = environ.get(request.credential_env)
    if (
        not isinstance(credential, str)
        or not credential
        or credential != credential.strip()
        or "\r" in credential
        or "\n" in credential
    ):
        raise CredentialUnavailable("credential_unavailable")
    headers = dict(request.headers)
    headers.setdefault("Accept", "application/json")
    headers.setdefault("User-Agent", "axonllm-production-validation/1")
    if request.body is not None:
        headers.setdefault("Content-Type", "application/json")
    if request.credential_type == "bearer":
        headers["Authorization"] = f"Bearer {credential}"
    elif request.credential_type == "x-api-key":
        headers["X-Api-Key"] = credential
    else:
        headers["Cookie"] = credential
    return headers


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def urllib_transport(
    request: HttpRequest,
    timeout_seconds: float,
) -> HttpObservation:
    """Send one request without following redirects or retaining its body."""
    started = time.perf_counter()
    url_request = urllib.request.Request(
        request.url,
        data=request.body,
        headers=dict(request.headers),
        method=request.method,
    )
    opener = urllib.request.build_opener(_NoRedirectHandler())
    try:
        with opener.open(url_request, timeout=timeout_seconds) as response:
            response.read(65536)
            status_code = response.getcode()
        return HttpObservation(
            status_code=status_code,
            latency_ms=(time.perf_counter() - started) * 1000,
        )
    except urllib.error.HTTPError as exc:
        try:
            exc.read(65536)
        finally:
            exc.close()
        return HttpObservation(
            status_code=exc.code,
            latency_ms=(time.perf_counter() - started) * 1000,
        )
    except Exception:
        return HttpObservation(
            status_code=None,
            latency_ms=(time.perf_counter() - started) * 1000,
            error_type="transport_error",
        )


def _send(
    transport: Transport,
    request: HttpRequest,
    timeout_seconds: float,
) -> HttpObservation:
    try:
        observation = transport(request, timeout_seconds)
    except Exception:
        return HttpObservation(None, 0.0, "transport_error")
    if (
        not isinstance(observation, HttpObservation)
        or not isinstance(observation.latency_ms, (int, float))
        or isinstance(observation.latency_ms, bool)
        or not math.isfinite(observation.latency_ms)
        or observation.latency_ms < 0
        or (
            observation.status_code is not None
            and (
                isinstance(observation.status_code, bool)
                or not isinstance(observation.status_code, int)
                or not 100 <= observation.status_code <= 599
            )
        )
    ):
        return HttpObservation(None, 0.0, "invalid_transport_result")
    return observation


def _request_for_endpoint(
    endpoint: str,
    spec: RequestSpec,
    headers: Mapping[str, str],
) -> HttpRequest:
    return HttpRequest(
        url=f"{endpoint}{spec.path}",
        method=spec.method,
        headers=headers,
        body=spec.body,
    )


def evaluate_authorization_contract() -> dict[str, Any]:
    """Evaluate the local default-deny policy without claiming remote SQL."""

    def principal(
        role: TenantRole,
        *,
        projects: frozenset[str] = frozenset({"project-a"}),
    ) -> Principal:
        return Principal(
            principal_id=f"validation:{role.value}",
            tenant_id="tenant-a",
            subject=f"validation:{role.value}",
            issuer="urn:axonllm:production-validation",
            roles=frozenset({role}),
            auth_method=AuthMethod.OIDC_JWT,
            membership_status=MembershipStatus.ACTIVE,
            project_ids=projects,
            scopes=frozenset({Action.QUERY_SELECT.value}),
        )

    resource = ResourceRef(
        resource_type="project",
        resource_id="project-a",
        tenant_id="tenant-a",
        project_id="project-a",
    )
    checks: list[dict[str, Any]] = []

    def record(
        check_id: str,
        role: TenantRole,
        action: Action,
        decision,
        *,
        expected_allowed: bool,
        expected_status: int,
        expected_reason: str | None = None,
    ) -> None:
        passed = (
            decision.allowed is expected_allowed
            and decision.status_code == expected_status
            and (
                expected_reason is None
                or decision.reason == expected_reason
            )
        )
        checks.append(
            {
                "id": check_id,
                "role": role.value,
                "action": action.value,
                "expectedAllowed": expected_allowed,
                "allowed": decision.allowed,
                "statusCode": decision.status_code,
                "reason": decision.reason,
                "passed": passed,
            }
        )

    tenant_roles = (
        TenantRole.TENANT_ADMIN,
        TenantRole.TENANT_MEMBER,
        TenantRole.TENANT_AUDITOR,
    )
    for role in tenant_roles:
        decision = authorize(principal(role), Action.QUERY_SELECT, resource)
        record(
            f"{role.value}_query_select_allowed",
            role,
            Action.QUERY_SELECT,
            decision,
            expected_allowed=True,
            expected_status=200,
        )
    service_decision = authorize(
        principal(TenantRole.SERVICE),
        Action.QUERY_SELECT,
        resource,
    )
    record(
        "service_query_select_allowed",
        TenantRole.SERVICE,
        Action.QUERY_SELECT,
        service_decision,
        expected_allowed=True,
        expected_status=200,
    )

    for role in TenantRole:
        decision = authorize(principal(role), Action.QUERY_MUTATE, resource)
        record(
            f"{role.value}_query_mutate_denied",
            role,
            Action.QUERY_MUTATE,
            decision,
            expected_allowed=False,
            expected_status=403,
            expected_reason="query_mutation_not_supported",
        )

    for role, expected_allowed in (
        (TenantRole.TENANT_ADMIN, True),
        (TenantRole.TENANT_MEMBER, False),
        (TenantRole.TENANT_AUDITOR, False),
    ):
        decision = authorize(
            principal(role),
            Action.TENANT_CONFIG_WRITE,
            resource,
        )
        record(
            f"{role.value}_tenant_config_write",
            role,
            Action.TENANT_CONFIG_WRITE,
            decision,
            expected_allowed=expected_allowed,
            expected_status=200 if expected_allowed else 403,
        )

    cross_tenant = authorize(
        principal(TenantRole.TENANT_MEMBER),
        Action.QUERY_SELECT,
        ResourceRef(
            resource_type="project",
            resource_id="project-b",
            tenant_id="tenant-b",
            project_id="project-b",
        ),
    )
    record(
        "cross_tenant_query_select_concealed",
        TenantRole.TENANT_MEMBER,
        Action.QUERY_SELECT,
        cross_tenant,
        expected_allowed=False,
        expected_status=404,
        expected_reason="resource_not_found",
    )

    ungranted = authorize(
        principal(TenantRole.TENANT_MEMBER, projects=frozenset()),
        Action.QUERY_SELECT,
        resource,
    )
    record(
        "ungranted_project_query_select_concealed",
        TenantRole.TENANT_MEMBER,
        Action.QUERY_SELECT,
        ungranted,
        expected_allowed=False,
        expected_status=404,
        expected_reason="resource_not_found",
    )
    passed = all(check["passed"] for check in checks)
    return {
        "status": "PASS" if passed else "FAIL",
        "policyModule": "src.gateway.auth.authorization",
        "sourcePolicyContractExercised": True,
        "queryBackendExercised": False,
        "checks": checks,
    }


def run_canaries(
    config: ValidationConfig,
    endpoints: tuple[str, ...],
    *,
    environ: Mapping[str, str],
    transport: Transport,
) -> dict[str, Any]:
    """Run every required canary against every configured endpoint."""
    results: list[dict[str, Any]] = []
    for canary in config.canaries:
        try:
            headers = _credential_headers(canary.request, environ)
        except CredentialUnavailable:
            headers = None
        for endpoint in endpoints:
            if headers is None:
                observation = HttpObservation(
                    status_code=None,
                    latency_ms=0,
                    error_type="credential_unavailable",
                )
            else:
                observation = _send(
                    transport,
                    _request_for_endpoint(
                        endpoint,
                        canary.request,
                        headers,
                    ),
                    config.timeout_seconds,
                )
            passed = (
                observation.error_type is None
                and observation.status_code
                in canary.request.expected_statuses
            )
            failure_reason = None
            if not passed:
                failure_reason = (
                    observation.error_type or "unexpected_status"
                )
            results.append(
                {
                    "name": canary.name,
                    "category": canary.category,
                    "baseUrl": endpoint,
                    "method": canary.request.method,
                    "path": canary.request.path,
                    "expectedStatuses": list(
                        canary.request.expected_statuses
                    ),
                    "statusCode": observation.status_code,
                    "latencyMs": round(observation.latency_ms, 3),
                    "passed": passed,
                    "failureReason": failure_reason,
                }
            )
    passed = all(result["passed"] for result in results)
    expected_result_count = len(config.canaries) * len(endpoints)
    return {
        "status": "PASS" if passed else "FAIL",
        "requiredCategories": sorted(REQUIRED_CANARY_CATEGORIES),
        "configuredCategories": sorted(
            {canary.category for canary in config.canaries}
        ),
        "scenarioCount": len(config.canaries),
        "requestCount": len(results),
        "allEndpointsCovered": (
            len(results) == expected_result_count
            and {result["baseUrl"] for result in results}
            == set(endpoints)
        ),
        "allRequiredCanariesPassedOnAllEndpoints": passed,
        "results": results,
    }


def _percentile(values: list[float], percentile: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(0, math.ceil((percentile / 100) * len(ordered)) - 1)
    return round(ordered[rank], 3)


def _latency_summary(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {
            "min": None,
            "mean": None,
            "p50": None,
            "p95": None,
            "p99": None,
            "max": None,
        }
    return {
        "min": round(min(values), 3),
        "mean": round(sum(values) / len(values), 3),
        "p50": _percentile(values, 50),
        "p95": _percentile(values, 95),
        "p99": _percentile(values, 99),
        "max": round(max(values), 3),
    }


def _status_counts(
    observations: list[HttpObservation],
) -> dict[str, int]:
    counts = Counter(
        (
            observation.error_type
            if observation.error_type is not None
            else (
                str(observation.status_code)
                if observation.status_code is not None
                else "transport_error"
            )
        )
        for observation in observations
    )
    return dict(sorted(counts.items()))


def _load_error_count(
    observations: list[HttpObservation],
    expected_statuses: tuple[int, ...],
) -> int:
    return sum(
        observation.error_type is not None
        or observation.status_code not in expected_statuses
        for observation in observations
    )


def _skipped_load(
    config: ValidationConfig,
    endpoints: tuple[str, ...],
    reason: str,
) -> dict[str, Any]:
    return {
        "status": "SKIPPED",
        "reason": reason,
        "scope": "distinct-configured-http-endpoints",
        "requestCountConfigured": config.load.request_count,
        "requestCountCompleted": 0,
        "concurrency": config.load.concurrency,
        "minimumEndpoints": config.load.minimum_endpoints,
        "baseUrlsConfigured": len(endpoints),
        "baseUrlsExercised": 0,
        "multipleHttpEndpointsExercised": False,
        "backingInstanceIdentityValidated": False,
        "statusCounts": {},
        "throughputRequestsPerSecond": 0,
        "errorRate": None,
        "latencyMs": _latency_summary([]),
        "thresholds": {
            "maxErrorRate": config.load.max_error_rate,
            "maxP95LatencyMs": config.load.max_p95_latency_ms,
        },
        "gates": [],
        "endpoints": [],
    }


def run_load(
    config: ValidationConfig,
    endpoints: tuple[str, ...],
    *,
    environ: Mapping[str, str],
    transport: Transport,
    monotonic: Callable[[], float] = time.perf_counter,
) -> dict[str, Any]:
    """Distribute a bounded, read-only load round-robin across endpoints."""
    try:
        headers = _credential_headers(config.load.request, environ)
    except CredentialUnavailable:
        report = _skipped_load(
            config,
            endpoints,
            "credential_unavailable",
        )
        report["status"] = "FAIL"
        report["gates"] = [
            {
                "name": "credential_available",
                "passed": False,
            }
        ]
        return report

    def invoke(index: int) -> tuple[str, HttpObservation]:
        endpoint = endpoints[index % len(endpoints)]
        observation = _send(
            transport,
            _request_for_endpoint(
                endpoint,
                config.load.request,
                headers,
            ),
            config.timeout_seconds,
        )
        return endpoint, observation

    started = monotonic()
    with ThreadPoolExecutor(
        max_workers=config.load.concurrency,
        thread_name_prefix="axon-validation",
    ) as executor:
        attempts = list(
            executor.map(invoke, range(config.load.request_count))
        )
    duration_seconds = max(monotonic() - started, 0.000001)

    observations = [observation for _, observation in attempts]
    latencies = [observation.latency_ms for observation in observations]
    error_count = _load_error_count(
        observations,
        config.load.request.expected_statuses,
    )
    error_rate = error_count / len(observations)
    latency = _latency_summary(latencies)
    exercised = {endpoint for endpoint, _ in attempts}

    endpoint_reports: list[dict[str, Any]] = []
    for endpoint in endpoints:
        endpoint_observations = [
            observation
            for attempted_endpoint, observation in attempts
            if attempted_endpoint == endpoint
        ]
        endpoint_errors = _load_error_count(
            endpoint_observations,
            config.load.request.expected_statuses,
        )
        endpoint_reports.append(
            {
                "baseUrl": endpoint,
                "requestCount": len(endpoint_observations),
                "statusCounts": _status_counts(endpoint_observations),
                "errorRate": round(
                    endpoint_errors / len(endpoint_observations),
                    6,
                )
                if endpoint_observations
                else None,
                "latencyMs": _latency_summary(
                    [
                        observation.latency_ms
                        for observation in endpoint_observations
                    ]
                ),
            }
        )

    gates = [
        {
            "name": "request_count_completed",
            "passed": len(attempts) == config.load.request_count,
        },
        {
            "name": "all_endpoints_exercised",
            "passed": exercised == set(endpoints),
        },
        {
            "name": "minimum_endpoints_exercised",
            "minimum": config.load.minimum_endpoints,
            "actual": len(exercised),
            "passed": len(exercised) >= config.load.minimum_endpoints,
        },
        {
            "name": "error_rate",
            "actual": round(error_rate, 6),
            "maximum": config.load.max_error_rate,
            "passed": error_rate <= config.load.max_error_rate,
        },
        {
            "name": "p95_latency_ms",
            "actual": latency["p95"],
            "maximum": config.load.max_p95_latency_ms,
            "passed": (
                latency["p95"] is not None
                and latency["p95"] <= config.load.max_p95_latency_ms
            ),
        },
    ]
    passed = all(gate["passed"] for gate in gates)
    return {
        "status": "PASS" if passed else "FAIL",
        "reason": None,
        "scope": "distinct-configured-http-endpoints",
        "method": config.load.request.method,
        "path": config.load.request.path,
        "expectedStatuses": list(
            config.load.request.expected_statuses
        ),
        "requestCountConfigured": config.load.request_count,
        "requestCountCompleted": len(attempts),
        "concurrency": config.load.concurrency,
        "minimumEndpoints": config.load.minimum_endpoints,
        "durationSeconds": round(duration_seconds, 6),
        "baseUrlsConfigured": len(endpoints),
        "baseUrlsExercised": len(exercised),
        "multipleHttpEndpointsExercised": len(exercised) >= 2,
        "backingInstanceIdentityValidated": False,
        "statusCounts": _status_counts(observations),
        "throughputRequestsPerSecond": round(
            len(attempts) / duration_seconds,
            3,
        ),
        "errorCount": error_count,
        "errorRate": round(error_rate, 6),
        "latencyMs": latency,
        "thresholds": {
            "maxErrorRate": config.load.max_error_rate,
            "maxP95LatencyMs": config.load.max_p95_latency_ms,
        },
        "gates": gates,
        "endpoints": endpoint_reports,
    }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def run_validation(
    config: ValidationConfig,
    endpoints: tuple[str, ...],
    *,
    environ: Mapping[str, str],
    transport: Transport = urllib_transport,
    monotonic: Callable[[], float] = time.perf_counter,
    now: Callable[[], datetime] = _utc_now,
) -> dict[str, Any]:
    """Run local policy, remote canaries, then load only after both pass."""
    started_at = now()
    authorization = evaluate_authorization_contract()
    canaries = run_canaries(
        config,
        endpoints,
        environ=environ,
        transport=transport,
    )
    prerequisites_passed = (
        authorization["status"] == "PASS"
        and canaries["status"] == "PASS"
        and canaries["allEndpointsCovered"]
    )
    if prerequisites_passed:
        load = run_load(
            config,
            endpoints,
            environ=environ,
            transport=transport,
            monotonic=monotonic,
        )
    else:
        load = _skipped_load(
            config,
            endpoints,
            "authorization_or_canary_prerequisite_failed",
        )
    passed = prerequisites_passed and load["status"] == "PASS"
    finished_at = now()
    return {
        "schemaVersion": REPORT_SCHEMA,
        "target": config.target,
        "validationScope": "source-policy-http-canary-and-load",
        "startedAt": started_at.isoformat(),
        "finishedAt": finished_at.isoformat(),
        "overallStatus": "PASS" if passed else "FAIL",
        "claims": {
            "agentcoreCutoverValidated": False,
            "queryBackendExercised": False,
            "backingInstanceIdentityValidated": False,
        },
        "httpEndpoints": list(endpoints),
        "authorizationContract": authorization,
        "canaries": canaries,
        "load": load,
    }


def _failure_report(code: str, message: str) -> dict[str, Any]:
    now = _utc_now().isoformat()
    return {
        "schemaVersion": REPORT_SCHEMA,
        "target": "unknown",
        "validationScope": "configuration",
        "startedAt": now,
        "finishedAt": now,
        "overallStatus": "FAIL",
        "claims": {
            "agentcoreCutoverValidated": False,
            "queryBackendExercised": False,
            "backingInstanceIdentityValidated": False,
        },
        "error": {
            "code": code,
            "message": message,
        },
    }


def _emit_report(
    report: dict[str, Any],
    output: str | None,
) -> tuple[int, str]:
    serialized = json.dumps(report, indent=2, sort_keys=True)
    if output:
        try:
            Path(output).write_text(f"{serialized}\n", encoding="utf-8")
        except OSError:
            failure = _failure_report(
                "output_unwritable",
                "the requested output file could not be written",
            )
            return 2, json.dumps(failure, indent=2, sort_keys=True)
    return (0 if report["overallStatus"] == "PASS" else 1), serialized


def main(
    argv: list[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    transport: Transport = urllib_transport,
) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run fail-closed AxonLLM RBAC canaries and read-only load "
            "against one or more HTTPS endpoints."
        )
    )
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--base-url",
        action="append",
        required=True,
        help="Repeat for each distinct HTTPS endpoint.",
    )
    parser.add_argument(
        "--output",
        help="Optional path for the same JSON report written to stdout.",
    )
    args = parser.parse_args(argv)

    try:
        try:
            raw_config = json.loads(
                Path(args.config).read_text(encoding="utf-8")
            )
        except json.JSONDecodeError as exc:
            raise ConfigurationError(
                "invalid_json",
                "the scenario file is not valid JSON",
            ) from exc
        except OSError as exc:
            raise ConfigurationError(
                "config_unreadable",
                "the scenario file could not be read",
            ) from exc
        config = parse_config(raw_config)
        endpoints = normalize_base_urls(args.base_url)
        validate_endpoint_count(config, endpoints)
        report = run_validation(
            config,
            endpoints,
            environ=os.environ if environ is None else environ,
            transport=transport,
        )
    except ConfigurationError as exc:
        report = _failure_report(exc.code, str(exc))
        _, serialized = _emit_report(report, args.output)
        print(serialized)
        return 2

    exit_code, serialized = _emit_report(report, args.output)
    print(serialized)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
