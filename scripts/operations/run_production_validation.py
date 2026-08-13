#!/usr/bin/env python3
"""Run AxonLLM production RBAC canaries and read-only HTTP load.

The scenario file contains request shapes and credential or CSRF *environment
variable* names. Secret values are read only at request time and are never
included in the JSON report. The load request is restricted to GET or HEAD so a
validation run cannot intentionally generate mutation traffic.

This tool validates HTTP behavior and the checked-out authorization contract.
AgentCore HTTP and generic targets also validate a bounded read-only SQL query.
Fargate additionally requires hash-bound pre-load and post-load ELB target
health observations and a reversible tenant-admin project mutation. It does not
validate AgentCore cutover.

Example:
    python scripts/operations/run_production_validation.py \
      --config scripts/operations/production_validation.example.json \
      --base-url https://task-a.example.test \
      --base-url https://task-b.example.test \
      --output production-validation.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
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

import sqlglot
from sqlglot import exp
import production_validation_rollback as rollback_journal


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
TARGET_HEALTH_SCHEMA = "axonllm.elb-target-health-observation/v1"
CORE_REQUIRED_CANARY_CATEGORIES = frozenset(
    {
        "authenticated_read_allowed",
        "viewer_mutation_denied",
        "cross_tenant_denied",
        "ungranted_project_denied",
    }
)
TENANT_ADMIN_ROUND_TRIP_CATEGORY = "tenant_admin_mutation_round_trip"
SUPPORTED_TARGETS = frozenset({"fargate", "agentcore-http", "generic"})
REQUIRED_CANARY_CATEGORIES_BY_TARGET = {
    "fargate": CORE_REQUIRED_CANARY_CATEGORIES
    | {TENANT_ADMIN_ROUND_TRIP_CATEGORY},
    "agentcore-http": CORE_REQUIRED_CANARY_CATEGORIES
    | {"authenticated_query_allowed"},
    "generic": CORE_REQUIRED_CANARY_CATEGORIES
    | {"authenticated_query_allowed"},
}
SUPPORTED_CANARY_CATEGORIES = frozenset(
    category
    for categories in REQUIRED_CANARY_CATEGORIES_BY_TARGET.values()
    for category in categories
)
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
READ_METHODS = frozenset({"GET", "HEAD"})
SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "cookie",
        "host",
        "if-match",
        "proxy-authorization",
        "x-axon-csrf-token",
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
ERROR_CODE_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,127}")
PROJECT_PATH_PATTERN = re.compile(r"/admin/projects/[^/]{1,256}")
CSRF_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")
CSRF_COOKIE_NAME = "__Host-axon-csrf"
CSRF_HEADER_NAME = "X-Axon-CSRF-Token"
COOKIE_CREDENTIAL_TYPES = frozenset(
    {
        "alb-session-cookie",
        "browser-session-cookie",
    }
)
TARGET_GROUP_ARN_PATTERN = re.compile(
    r"arn:aws(?:-[a-z]+)*:elasticloadbalancing:[a-z0-9-]+:"
    r"[0-9]{12}:targetgroup/[A-Za-z0-9-]{1,32}/[0-9a-f]{16}"
)
REVERSIBLE_PROJECT_FIELDS = frozenset(
    {
        "cache_enabled",
        "cache_ttl_seconds",
        "log_level",
        "name",
        "prompt_caching_enabled",
        "semantic_cache_enabled",
        "semantic_cache_threshold",
    }
)
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_TARGET_HEALTH_OBSERVATION_BYTES = 1024 * 1024


class ConfigurationError(ValueError):
    """A safe-to-report configuration failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CredentialUnavailable(RuntimeError):
    """Raised without retaining a credential value."""

    def __init__(self, code: str = "credential_unavailable") -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class RequestSpec:
    method: str
    path: str
    expected_statuses: tuple[int, ...]
    credential_env: str
    credential_type: str
    headers: tuple[tuple[str, str], ...]
    body: bytes | None
    csrf_token_env: str | None


@dataclass(frozen=True)
class CanarySpec:
    name: str
    category: str
    request: RequestSpec
    expected_error_code: str | None


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
class TargetHealthSnapshot:
    target_group_arn: str
    healthy_target_hashes: tuple[str, ...]
    source_sha256: str


@dataclass(frozen=True)
class HttpRequest:
    url: str
    method: str
    headers: Mapping[str, str]
    body: bytes | None
    capture_response: bool = False


@dataclass(frozen=True)
class HttpObservation:
    status_code: int | None
    latency_ms: float
    error_type: str | None = None
    body: bytes = b""


class Transport(Protocol):
    def __call__(
        self,
        request: HttpRequest,
        timeout_seconds: float,
    ) -> HttpObservation: ...


class TargetHealthCollector(Protocol):
    def __call__(self, phase: str) -> TargetHealthSnapshot: ...


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
        *COOKIE_CREDENTIAL_TYPES,
        "bearer",
        "x-api-key",
    }:
        raise ConfigurationError(
            "invalid_configuration",
            f"{location}.credentialType must be alb-session-cookie, "
            "browser-session-cookie, bearer, or x-api-key",
        )

    csrf_token_env = request.get("csrfTokenEnv")
    if csrf_token_env is not None and (
        not isinstance(csrf_token_env, str)
        or ENV_PATTERN.fullmatch(csrf_token_env) is None
    ):
        raise ConfigurationError(
            "invalid_configuration",
            f"{location}.csrfTokenEnv must be an uppercase environment name",
        )
    cookie_backed_write = (
        credential_type in COOKIE_CREDENTIAL_TYPES
        and method in WRITE_METHODS
    )
    if cookie_backed_write and csrf_token_env is None:
        raise ConfigurationError(
            "invalid_canary_contract",
            f"{location} must load its CSRF token from csrfTokenEnv",
        )
    if not cookie_backed_write and csrf_token_env is not None:
        raise ConfigurationError(
            "invalid_canary_contract",
            f"{location}.csrfTokenEnv is valid only for cookie-backed writes",
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
        csrf_token_env=csrf_token_env,
    )


def _request_json(
    request: RequestSpec,
    location: str,
) -> Mapping[str, Any]:
    if request.body is None:
        raise ConfigurationError(
            "invalid_canary_contract",
            f"{location} must include a JSON request body",
        )
    try:
        value = json.loads(request.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError(
            "invalid_canary_contract",
            f"{location} must include a JSON object body",
        ) from exc
    return _mapping(value, f"{location} JSON body")


def _validate_query_canary(request: RequestSpec, location: str) -> None:
    body = _request_json(request, location)
    if (
        request.method != "POST"
        or not all(
            200 <= status < 300 for status in request.expected_statuses
        )
        or (
            request.path != "/v1/query"
            and body.get("action") != "query"
        )
        or (
            "action" in body
            and body.get("action") != "query"
        )
    ):
        raise ConfigurationError(
            "invalid_canary_contract",
            f"{location} must POST a query expecting only 2xx statuses",
        )
    datasource_id = body.get("datasource_id")
    max_rows = body.get("max_rows")
    request_id = body.get("request_id")
    if (
        not isinstance(datasource_id, str)
        or not datasource_id
        or datasource_id != datasource_id.strip()
        or len(datasource_id) > 128
        or isinstance(max_rows, bool)
        or not isinstance(max_rows, int)
        or not 1 <= max_rows <= 10_000
        or not isinstance(request_id, str)
        or not request_id
        or request_id != request_id.strip()
        or len(request_id) > 128
        or any(ord(character) < 32 for character in request_id)
    ):
        raise ConfigurationError(
            "invalid_canary_contract",
            f"{location} must identify a datasource, request_id, and "
            "bounded max_rows",
        )
    sql = body.get("sql")
    if not isinstance(sql, str) or not sql or len(sql) > 64 * 1024:
        raise ConfigurationError(
            "invalid_canary_contract",
            f"{location} must include a read-only SQL query",
        )
    try:
        statements = sqlglot.parse(
            sql,
            read="athena",
            error_level=sqlglot.ErrorLevel.RAISE,
            error_message_context=0,
        )
    except Exception as exc:
        raise ConfigurationError(
            "invalid_canary_contract",
            f"{location} must include a valid read-only SQL query",
        ) from exc
    if (
        len(statements) != 1
        or not isinstance(statements[0], exp.Query)
        or any(
            isinstance(node, (exp.DDL, exp.DML, exp.Command, exp.Into))
            for node in statements[0].walk()
        )
    ):
        raise ConfigurationError(
            "invalid_canary_contract",
            f"{location} must include exactly one read-only SQL query",
        )


def _validate_project_mutation(
    request: RequestSpec,
    location: str,
    *,
    expected_statuses: tuple[int, ...],
) -> None:
    body = _request_json(request, location)
    if (
        request.method != "PUT"
        or PROJECT_PATH_PATTERN.fullmatch(request.path) is None
        or request.expected_statuses != expected_statuses
        or len(body) != 1
    ):
        raise ConfigurationError(
            "invalid_canary_contract",
            f"{location} must PUT one reversible project field and expect "
            f"exactly {expected_statuses[0]}",
        )
    field, value = next(iter(body.items()))
    if field not in REVERSIBLE_PROJECT_FIELDS:
        raise ConfigurationError(
            "invalid_canary_contract",
            f"{location} contains an unsupported reversible project field",
        )
    if field in {
        "cache_enabled",
        "prompt_caching_enabled",
        "semantic_cache_enabled",
    }:
        valid = type(value) is bool
    elif field == "cache_ttl_seconds":
        valid = (
            not isinstance(value, bool)
            and isinstance(value, int)
            and 1 <= value <= 86_400
        )
    elif field == "semantic_cache_threshold":
        valid = (
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(float(value))
            and 0 <= float(value) <= 1
        )
    elif field == "log_level":
        valid = value in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    else:
        valid = (
            isinstance(value, str)
            and value == value.strip()
            and 1 <= len(value) <= 128
            and not any(ord(character) < 32 for character in value)
        )
    if not valid:
        raise ConfigurationError(
            "invalid_canary_contract",
            f"{location} contains an invalid reversible project value",
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
    elif canary.category == "authenticated_query_allowed":
        _validate_query_canary(request, location)
    elif canary.category == "viewer_mutation_denied":
        _validate_project_mutation(
            request,
            location,
            expected_statuses=(403,),
        )
        if canary.expected_error_code is None:
            raise ConfigurationError(
                "invalid_canary_contract",
                f"{location} must configure its expected RBAC error code",
            )
    elif canary.category == TENANT_ADMIN_ROUND_TRIP_CATEGORY:
        _validate_project_mutation(
            request,
            location,
            expected_statuses=(200,),
        )
    elif request.method not in READ_METHODS or not set(
        request.expected_statuses
    ).issubset({403, 404}):
        raise ConfigurationError(
            "invalid_canary_contract",
            f"{location} must be a read expecting only 403 or 404",
        )
    if (
        canary.category != "viewer_mutation_denied"
        and canary.expected_error_code is not None
    ):
        raise ConfigurationError(
            "invalid_canary_contract",
            f"{location} must not configure an RBAC error code",
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
    required_categories = REQUIRED_CANARY_CATEGORIES_BY_TARGET[target]
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
        if category not in SUPPORTED_CANARY_CATEGORIES:
            raise ConfigurationError(
                "invalid_configuration",
                f"{location}.category is unsupported",
            )
        expected_error_code = raw_canary.get("expectedErrorCode")
        if expected_error_code is not None and (
            not isinstance(expected_error_code, str)
            or ERROR_CODE_PATTERN.fullmatch(expected_error_code) is None
        ):
            raise ConfigurationError(
                "invalid_configuration",
                f"{location}.expectedErrorCode is invalid",
            )
        if expected_error_code == "csrf_validation_failed":
            raise ConfigurationError(
                "invalid_canary_contract",
                f"{location}.expectedErrorCode must identify an RBAC denial",
            )
        canary = CanarySpec(
            name=name,
            category=category,
            request=_parse_request(raw_canary, location),
            expected_error_code=expected_error_code,
        )
        _validate_canary_semantics(canary)
        canaries.append(canary)

    categories = {canary.category for canary in canaries}
    missing_categories = required_categories.difference(categories)
    if missing_categories:
        raise ConfigurationError(
            "missing_required_canaries",
            f"configuration does not cover every required {target} "
            "canary category",
        )
    if target == "fargate" and any(
        canary.request.credential_type not in COOKIE_CREDENTIAL_TYPES
        for canary in canaries
    ):
        raise ConfigurationError(
            "invalid_canary_contract",
            "Fargate canaries must use short-lived browser session cookies",
        )
    round_trip_canaries = [
        canary
        for canary in canaries
        if canary.category == TENANT_ADMIN_ROUND_TRIP_CATEGORY
    ]
    if target != "fargate" and round_trip_canaries:
        raise ConfigurationError(
            "invalid_canary_contract",
            "tenant-admin project mutation round trips are Fargate-only",
        )
    if target == "fargate":
        viewer_canaries = [
            canary
            for canary in canaries
            if canary.category == "viewer_mutation_denied"
        ]
        if len(viewer_canaries) != 1 or len(round_trip_canaries) != 1:
            raise ConfigurationError(
                "invalid_canary_contract",
                "Fargate requires exactly one paired viewer denial and "
                "tenant-admin mutation round trip",
            )
        viewer = viewer_canaries[0].request
        admin = round_trip_canaries[0].request
        if viewer.path != admin.path or viewer.body != admin.body:
            raise ConfigurationError(
                "invalid_canary_contract",
                "Fargate viewer and tenant-admin mutations must use the same "
                "project path and JSON body",
            )
        if (
            viewer.credential_env == admin.credential_env
            or viewer.csrf_token_env == admin.csrf_token_env
        ):
            raise ConfigurationError(
                "invalid_canary_contract",
                "Fargate viewer and tenant-admin mutations must use distinct "
                "cookie and CSRF identities",
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
    if (
        target == "fargate"
        and load_request.credential_type not in COOKIE_CREDENTIAL_TYPES
    ):
        raise ConfigurationError(
            "invalid_canary_contract",
            "Fargate load must use a short-lived browser session cookie",
        )
    if (
        target == "fargate"
        and {
            load_request.credential_type,
            *(canary.request.credential_type for canary in canaries),
        }
        not in (
            {"alb-session-cookie"},
            {"browser-session-cookie"},
        )
    ):
        raise ConfigurationError(
            "invalid_canary_contract",
            "Fargate canaries and load must use one credential type",
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
        minimum=2,
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
    credential_envs = {
        category: {
            canary.request.credential_env
            for canary in canaries
            if canary.category == category
        }
        for category in categories
    }
    if any(len(values) != 1 for values in credential_envs.values()):
        raise ConfigurationError(
            "invalid_canary_contract",
            "each configured canary category must use one credential identity",
        )
    denial_envs = {
        next(iter(credential_envs[category]))
        for category in (
            "viewer_mutation_denied",
            "cross_tenant_denied",
            "ungranted_project_denied",
        )
    }
    authenticated_envs = set().union(
        *(
            credential_envs[category]
            for category in (
                "authenticated_read_allowed",
                "authenticated_query_allowed",
            )
            if category in credential_envs
        )
    )
    if len(denial_envs) != 3 or denial_envs & authenticated_envs:
        raise ConfigurationError(
            "invalid_canary_contract",
            "viewer, cross-tenant, ungranted-project, and authenticated "
            "canaries must use distinct identity classes",
        )
    if target == "fargate":
        admin_env = next(
            iter(credential_envs[TENANT_ADMIN_ROUND_TRIP_CATEGORY])
        )
        if admin_env in denial_envs or admin_env in authenticated_envs:
            raise ConfigurationError(
                "invalid_canary_contract",
                "the tenant-admin round trip must use a distinct identity",
            )
    if load.request.credential_env not in authenticated_envs:
        raise ConfigurationError(
            "invalid_canary_contract",
            "configuration.load.request must use an authenticated canary "
            "credential",
        )
    return ValidationConfig(
        target=target,
        timeout_seconds=timeout_seconds,
        canaries=tuple(canaries),
        load=load,
    )


def parse_target_health_snapshot(
    value: Any,
    *,
    target_group_arn: str,
    source_sha256: str,
) -> TargetHealthSnapshot:
    if (
        not isinstance(target_group_arn, str)
        or TARGET_GROUP_ARN_PATTERN.fullmatch(target_group_arn) is None
    ):
        raise ConfigurationError(
            "invalid_target_health_evidence",
            "target-health observation targetGroupArn is invalid",
        )
    raw = _mapping(value, "target-health observation")
    descriptions = raw.get("TargetHealthDescriptions")
    if not isinstance(descriptions, list):
        raise ConfigurationError(
            "invalid_target_health_evidence",
            "target-health observation descriptions must be an array",
        )
    healthy_ids: list[str] = []
    for index, item in enumerate(descriptions):
        description = _mapping(
            item,
            f"target-health observation description {index}",
        )
        target = _mapping(
            description.get("Target"),
            f"target-health observation target {index}",
        )
        health = _mapping(
            description.get("TargetHealth"),
            f"target-health observation health {index}",
        )
        target_id = target.get("Id")
        state = health.get("State")
        if (
            not isinstance(target_id, str)
            or not target_id
            or target_id != target_id.strip()
            or len(target_id) > 256
            or any(ord(character) < 33 for character in target_id)
            or not isinstance(state, str)
        ):
            raise ConfigurationError(
                "invalid_target_health_evidence",
                "target-health observation contains malformed target data",
            )
        if state == "healthy":
            healthy_ids.append(target_id)
    if len(set(healthy_ids)) < 2:
        raise ConfigurationError(
            "insufficient_healthy_targets",
            "target-health observation must contain at least two distinct "
            "healthy target IDs",
        )
    target_hashes = tuple(
        sorted(
            hashlib.sha256(
                f"{target_group_arn}\0{target_id}".encode("utf-8")
            ).hexdigest()
            for target_id in set(healthy_ids)
        )
    )
    if not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
        raise ConfigurationError(
            "invalid_target_health_evidence",
            "target-health observation source digest is invalid",
        )
    return TargetHealthSnapshot(
        target_group_arn=target_group_arn,
        healthy_target_hashes=target_hashes,
        source_sha256=source_sha256,
    )


class AwsCliTargetHealthCollector:
    """Collect exact ELB target health from AWS at each requested boundary."""

    def __init__(
        self,
        target_group_arn: str,
        *,
        runner: Callable[..., subprocess.CompletedProcess] | None = None,
    ) -> None:
        if TARGET_GROUP_ARN_PATTERN.fullmatch(target_group_arn) is None:
            raise ConfigurationError(
                "invalid_target_health_evidence",
                "target-group ARN is invalid",
        )
        self._target_group_arn = target_group_arn
        self._runner = subprocess.run if runner is None else runner

    def __call__(self, phase: str) -> TargetHealthSnapshot:
        if phase not in {"pre-load", "post-load"}:
            raise ConfigurationError(
                "invalid_target_health_evidence",
                "target-health collection phase is invalid",
            )
        try:
            completed = self._runner(
                [
                    "aws",
                    "elbv2",
                    "describe-target-health",
                    "--target-group-arn",
                    self._target_group_arn,
                    "--output",
                    "json",
                    "--no-cli-pager",
                ],
                capture_output=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ConfigurationError(
                "target_health_collection_failed",
                "ELB target health could not be collected",
            ) from exc
        if completed.returncode != 0:
            raise ConfigurationError(
                "target_health_collection_failed",
                "ELB target health collection returned a failure",
            )
        payload = completed.stdout
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if (
            not isinstance(payload, bytes)
            or not payload
            or len(payload) > MAX_TARGET_HEALTH_OBSERVATION_BYTES
        ):
            raise ConfigurationError(
                "invalid_target_health_evidence",
                "ELB target-health response size is invalid",
            )
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConfigurationError(
                "invalid_target_health_evidence",
                "ELB target-health response is not valid JSON",
            ) from exc
        return parse_target_health_snapshot(
            value,
            target_group_arn=self._target_group_arn,
            source_sha256=hashlib.sha256(payload).hexdigest(),
        )


def _collect_target_health(
    collector: TargetHealthCollector,
    phase: str,
) -> TargetHealthSnapshot:
    try:
        snapshot = collector(phase)
    except ConfigurationError:
        raise
    except Exception as exc:
        raise ConfigurationError(
            "target_health_collection_failed",
            f"ELB target health could not be collected at {phase}",
        ) from exc
    if not isinstance(snapshot, TargetHealthSnapshot):
        raise ConfigurationError(
            "invalid_target_health_evidence",
            f"ELB target-health collector returned invalid data at {phase}",
        )
    return snapshot


def _trusted_time(
    now: Callable[[], datetime],
    location: str,
) -> datetime:
    try:
        value = now()
    except Exception as exc:
        raise ConfigurationError(
            "invalid_validation_clock",
            f"the validation clock failed at {location}",
        ) from exc
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ConfigurationError(
            "invalid_validation_clock",
            f"the validation clock must be timezone-aware at {location}",
        )
    return value.astimezone(timezone.utc)


def _bound_target_health_observation(
    observation: TargetHealthSnapshot,
    *,
    phase: str,
    collected_at: datetime,
) -> dict[str, Any]:
    redacted = {
        "schemaVersion": TARGET_HEALTH_SCHEMA,
        "phase": phase,
        "collectedAt": collected_at.isoformat(),
        "sourceSha256": observation.source_sha256,
        "healthyTargetCount": len(observation.healthy_target_hashes),
        "targetIdSha256": list(observation.healthy_target_hashes),
        "targetGroupArnSha256": hashlib.sha256(
            observation.target_group_arn.encode("utf-8")
        ).hexdigest(),
    }
    canonical = json.dumps(
        redacted,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return {
        **redacted,
        "observationSha256": hashlib.sha256(canonical).hexdigest(),
    }


def _target_health_report(
    pre_load: TargetHealthSnapshot,
    *,
    pre_collected_at: datetime,
    load_started_at: datetime,
    load_finished_at: datetime,
    post_load: TargetHealthSnapshot,
    post_collected_at: datetime,
) -> dict[str, Any]:
    if (
        pre_load.target_group_arn != post_load.target_group_arn
        or pre_load.healthy_target_hashes != post_load.healthy_target_hashes
    ):
        raise ConfigurationError(
            "target_health_identity_changed",
            "Fargate target-health collections must contain the same target "
            "group and healthy target set",
        )
    if not (
        pre_collected_at <= load_started_at
        <= load_finished_at
        <= post_collected_at
    ):
        raise ConfigurationError(
            "invalid_target_health_chronology",
            "target-health collections do not bracket the HTTP load interval",
        )

    pre_observation = _bound_target_health_observation(
        pre_load,
        phase="pre-load",
        collected_at=pre_collected_at,
    )
    post_observation = _bound_target_health_observation(
        post_load,
        phase="post-load",
        collected_at=post_collected_at,
    )
    load_interval = {
        "startedAt": load_started_at.isoformat(),
        "finishedAt": load_finished_at.isoformat(),
    }
    evidence = {
        "schemaVersion": TARGET_HEALTH_SCHEMA,
        "preLoad": pre_observation,
        "loadInterval": load_interval,
        "postLoad": post_observation,
    }
    canonical_evidence = json.dumps(
        evidence,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    return {
        "status": "PASS",
        "minimumHealthyTargets": 2,
        "sameTargetSetAcrossLoad": True,
        "chronologyValidated": True,
        "backingInstanceIdentityValidated": True,
        "targetGroupArnSha256": hashlib.sha256(
            pre_load.target_group_arn.encode("utf-8")
        ).hexdigest(),
        "evidenceSha256": hashlib.sha256(canonical_evidence).hexdigest(),
        "loadInterval": load_interval,
        "preLoad": pre_observation,
        "postLoad": post_observation,
    }


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
    elif request.credential_type in COOKIE_CREDENTIAL_TYPES:
        cookie_parts = [
            part.strip()
            for part in credential.split(";")
            if part.strip().partition("=")[0] != CSRF_COOKIE_NAME
        ]
        if request.csrf_token_env is not None:
            csrf_token = environ.get(request.csrf_token_env)
            if (
                not isinstance(csrf_token, str)
                or CSRF_TOKEN_PATTERN.fullmatch(csrf_token) is None
            ):
                raise CredentialUnavailable("csrf_token_unavailable")
            cookie_parts.append(f"{CSRF_COOKIE_NAME}={csrf_token}")
            headers[CSRF_HEADER_NAME] = csrf_token
        headers["Cookie"] = "; ".join(cookie_parts)
    else:  # pragma: no cover - configuration parsing owns the closed set
        raise CredentialUnavailable("credential_type_unsupported")
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
            body = response.read(
                MAX_RESPONSE_BYTES + 1
                if request.capture_response
                else 65536
            )
            status_code = response.getcode()
        if request.capture_response and len(body) > MAX_RESPONSE_BYTES:
            return HttpObservation(
                status_code=None,
                latency_ms=(time.perf_counter() - started) * 1000,
                error_type="response_too_large",
            )
        return HttpObservation(
            status_code=status_code,
            latency_ms=(time.perf_counter() - started) * 1000,
            body=body if request.capture_response else b"",
        )
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(
                MAX_RESPONSE_BYTES + 1
                if request.capture_response
                else 65536
            )
        finally:
            exc.close()
        if request.capture_response and len(body) > MAX_RESPONSE_BYTES:
            return HttpObservation(
                status_code=None,
                latency_ms=(time.perf_counter() - started) * 1000,
                error_type="response_too_large",
            )
        return HttpObservation(
            status_code=exc.code,
            latency_ms=(time.perf_counter() - started) * 1000,
            body=body if request.capture_response else b"",
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
        or not isinstance(observation.body, bytes)
        or len(observation.body) > MAX_RESPONSE_BYTES
    ):
        return HttpObservation(None, 0.0, "invalid_transport_result")
    return observation


def _request_for_endpoint(
    endpoint: str,
    spec: RequestSpec,
    headers: Mapping[str, str],
    *,
    capture_response: bool = False,
) -> HttpRequest:
    return HttpRequest(
        url=f"{endpoint}{spec.path}",
        method=spec.method,
        headers=headers,
        body=spec.body,
        capture_response=capture_response,
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


def _query_response_matches(
    request: RequestSpec,
    observation: HttpObservation,
) -> bool:
    try:
        request_body = json.loads(request.body or b"")
        response_body = json.loads(observation.body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if type(request_body) is not dict or type(response_body) is not dict:
        return False
    return (
        response_body.get("datasource_id")
        == request_body.get("datasource_id")
        and isinstance(response_body.get("rows"), list)
        and isinstance(response_body.get("statistics"), dict)
        and response_body.get("request_id")
        == request_body.get("request_id")
    )


def _response_object(observation: HttpObservation) -> dict[str, Any] | None:
    try:
        value = json.loads(observation.body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if type(value) is dict else None


def _response_error_code(observation: HttpObservation) -> str | None:
    value = _response_object(observation)
    if value is None or type(value.get("error")) is not dict:
        return None
    code = value["error"].get("code")
    return code if isinstance(code, str) else None


def _response_revision(observation: HttpObservation) -> int | None:
    value = _response_object(observation)
    if value is None:
        return None
    revision = value.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int):
        return None
    return revision if revision >= 0 else None


def _project_snapshot(
    observation: HttpObservation,
    fields: frozenset[str],
) -> tuple[int, dict[str, Any]] | None:
    if observation.error_type is not None or observation.status_code != 200:
        return None
    value = _response_object(observation)
    if value is None:
        return None
    revision = value.get("revision")
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 0
        or not fields.issubset(value)
    ):
        return None
    return revision, {field: value[field] for field in fields}


def _captured_request(
    endpoint: str,
    request: RequestSpec,
    headers: Mapping[str, str],
    *,
    method: str,
    body: bytes | None,
) -> HttpRequest:
    return HttpRequest(
        url=f"{endpoint}{request.path}",
        method=method,
        headers=headers,
        body=body,
        capture_response=True,
    )


def _rollback_request(entry: Mapping[str, Any]) -> RequestSpec:
    return RequestSpec(
        method="PUT",
        path=entry["path"],
        expected_statuses=(200,),
        credential_env=entry["credentialEnv"],
        credential_type=entry["credentialType"],
        headers=(),
        body=json.dumps(
            entry["mutationValues"],
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8"),
        csrf_token_env=entry["csrfTokenEnv"],
    )


def _reconcile_rollback_entry(
    journal: rollback_journal.RollbackJournal,
    entry: Mapping[str, Any],
    *,
    environ: Mapping[str, str],
    transport: Transport,
) -> dict[str, Any]:
    observations: list[HttpObservation] = []
    entry_id = entry["id"]
    request = _rollback_request(entry)
    fields = frozenset(entry["priorValues"])
    try:
        write_headers = _credential_headers(request, environ)
    except CredentialUnavailable as exc:
        return {
            "entryId": entry_id,
            "status": "PENDING",
            "reason": exc.code,
            "rollbackAttempted": False,
            "rollbackSucceeded": False,
            "restorationVerified": False,
            "observations": observations,
        }
    read_headers = {
        key: value
        for key, value in write_headers.items()
        if key.lower()
        not in {"content-type", "if-match", CSRF_HEADER_NAME.lower()}
    }

    current_observation = _send(
        transport,
        _captured_request(
            entry["endpoint"],
            request,
            read_headers,
            method="GET",
            body=None,
        ),
        entry["timeoutSeconds"],
    )
    observations.append(current_observation)
    current = _project_snapshot(current_observation, fields)
    if current is None:
        return {
            "entryId": entry_id,
            "status": "PENDING",
            "reason": (
                current_observation.error_type
                or "rollback_state_validation_failed"
            ),
            "rollbackAttempted": False,
            "rollbackSucceeded": False,
            "restorationVerified": False,
            "observations": observations,
        }
    if current[1] == entry["priorValues"]:
        try:
            journal.mark_complete(entry_id, current[0])
        except rollback_journal.RollbackJournalError:
            return {
                "entryId": entry_id,
                "status": "PENDING",
                "reason": "rollback_journal_update_failed",
                "rollbackAttempted": False,
                "rollbackSucceeded": False,
                "restorationVerified": True,
                "observations": observations,
            }
        return {
            "entryId": entry_id,
            "status": "COMPLETE",
            "reason": None,
            "rollbackAttempted": False,
            "rollbackSucceeded": False,
            "restorationVerified": True,
            "observations": observations,
        }

    mutation_revision = entry["mutationRevision"]
    expected_revision = (
        mutation_revision
        if mutation_revision is not None
        else entry["priorRevision"] + 1
    )
    if (
        current[1] != entry["mutationValues"]
        or current[0] != expected_revision
    ):
        return {
            "entryId": entry_id,
            "status": "PENDING",
            "reason": "rollback_state_conflict",
            "rollbackAttempted": False,
            "rollbackSucceeded": False,
            "restorationVerified": False,
            "observations": observations,
        }

    rollback_headers = dict(write_headers)
    rollback_headers["If-Match"] = f'"{current[0]}"'
    rollback_body = json.dumps(
        entry["priorValues"],
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    rollback_observation = _send(
        transport,
        _captured_request(
            entry["endpoint"],
            request,
            rollback_headers,
            method="PUT",
            body=rollback_body,
        ),
        entry["timeoutSeconds"],
    )
    observations.append(rollback_observation)
    restored_revision = _response_revision(rollback_observation)
    rollback_succeeded = (
        rollback_observation.error_type is None
        and rollback_observation.status_code == 200
        and restored_revision == current[0] + 1
    )
    if not rollback_succeeded:
        confirmation_observation = _send(
            transport,
            _captured_request(
                entry["endpoint"],
                request,
                read_headers,
                method="GET",
                body=None,
            ),
            entry["timeoutSeconds"],
        )
        observations.append(confirmation_observation)
        confirmed = _project_snapshot(confirmation_observation, fields)
        if confirmed is not None and confirmed[1] == entry["priorValues"]:
            try:
                journal.mark_complete(entry_id, confirmed[0])
            except rollback_journal.RollbackJournalError:
                return {
                    "entryId": entry_id,
                    "status": "PENDING",
                    "reason": "rollback_journal_update_failed",
                    "rollbackAttempted": True,
                    "rollbackSucceeded": True,
                    "restorationVerified": True,
                    "observations": observations,
                }
            return {
                "entryId": entry_id,
                "status": "COMPLETE",
                "reason": None,
                "rollbackAttempted": True,
                "rollbackSucceeded": True,
                "restorationVerified": True,
                "observations": observations,
            }
        return {
            "entryId": entry_id,
            "status": "PENDING",
            "reason": "rollback_failed",
            "rollbackAttempted": True,
            "rollbackSucceeded": False,
            "restorationVerified": False,
            "observations": observations,
        }

    verification_observation = _send(
        transport,
        _captured_request(
            entry["endpoint"],
            request,
            read_headers,
            method="GET",
            body=None,
        ),
        entry["timeoutSeconds"],
    )
    observations.append(verification_observation)
    restored = _project_snapshot(verification_observation, fields)
    if restored != (restored_revision, entry["priorValues"]):
        return {
            "entryId": entry_id,
            "status": "PENDING",
            "reason": "restoration_verification_failed",
            "rollbackAttempted": True,
            "rollbackSucceeded": True,
            "restorationVerified": False,
            "observations": observations,
        }
    try:
        journal.mark_complete(entry_id, restored_revision)
    except rollback_journal.RollbackJournalError:
        return {
            "entryId": entry_id,
            "status": "PENDING",
            "reason": "rollback_journal_update_failed",
            "rollbackAttempted": True,
            "rollbackSucceeded": True,
            "restorationVerified": True,
            "observations": observations,
        }
    return {
        "entryId": entry_id,
        "status": "COMPLETE",
        "reason": None,
        "rollbackAttempted": True,
        "rollbackSucceeded": True,
        "restorationVerified": True,
        "observations": observations,
    }


def reconcile_production_validation_rollbacks(
    journal: rollback_journal.RollbackJournal,
    *,
    environ: Mapping[str, str],
    transport: Transport = urllib_transport,
) -> dict[str, Any]:
    """Independently restore every pending configuration mutation."""

    results = [
        _reconcile_rollback_entry(
            journal,
            entry,
            environ=environ,
            transport=transport,
        )
        for entry in journal.entries(pending_only=True)
    ]
    summary = journal.summary()
    return {
        **summary,
        "results": [
            {
                key: result[key]
                for key in (
                    "entryId",
                    "status",
                    "reason",
                    "rollbackAttempted",
                    "rollbackSucceeded",
                    "restorationVerified",
                )
            }
            for result in results
        ],
    }


def _tenant_admin_round_trip(
    canary: CanarySpec,
    endpoint: str,
    *,
    environ: Mapping[str, str],
    journal: rollback_journal.RollbackJournal,
    transport: Transport,
    timeout_seconds: float,
) -> dict[str, Any]:
    observations: list[HttpObservation] = []
    phases = {
        "priorStateLoaded": False,
        "mutationApplied": False,
        "changedStateVerified": False,
        "rollbackAttempted": False,
        "rollbackSucceeded": False,
        "restorationVerified": False,
    }
    failure_reason: str | None = None
    mutation_status: int | None = None

    def record(observation: HttpObservation) -> HttpObservation:
        observations.append(observation)
        return observation

    def fail(reason: str, *, cleanup: bool = False) -> None:
        nonlocal failure_reason
        if cleanup or failure_reason is None:
            failure_reason = reason

    try:
        write_headers = _credential_headers(canary.request, environ)
    except CredentialUnavailable as exc:
        failure_reason = exc.code
        write_headers = None

    if write_headers is not None:
        mutation_body = dict(
            _request_json(canary.request, f"canary {canary.name}")
        )
        fields = frozenset(mutation_body)
        read_headers = {
            key: value
            for key, value in write_headers.items()
            if key.lower()
            not in {"content-type", "if-match", CSRF_HEADER_NAME.lower()}
        }
        prior_observation = record(
            _send(
                transport,
                _captured_request(
                    endpoint,
                    canary.request,
                    read_headers,
                    method="GET",
                    body=None,
                ),
                timeout_seconds,
            )
        )
        prior = _project_snapshot(prior_observation, fields)
        if prior is None:
            fail(
                prior_observation.error_type or "prior_state_validation_failed"
            )
        elif prior[1] == mutation_body:
            fail("mutation_would_not_change_state")
        else:
            phases["priorStateLoaded"] = True
            try:
                entry_id = journal.prepare(
                    endpoint=endpoint,
                    path=canary.request.path,
                    credential_env=canary.request.credential_env,
                    credential_type=canary.request.credential_type,
                    csrf_token_env=canary.request.csrf_token_env,
                    timeout_seconds=timeout_seconds,
                    prior_revision=prior[0],
                    prior_values=prior[1],
                    mutation_values=mutation_body,
                )
            except rollback_journal.RollbackJournalError:
                fail("rollback_journal_prepare_failed", cleanup=True)
            else:
                mutation_headers = dict(write_headers)
                mutation_headers["If-Match"] = f'"{prior[0]}"'
                mutation_revision: int | None = None
                try:
                    mutation_observation = record(
                        _send(
                            transport,
                            _captured_request(
                                endpoint,
                                canary.request,
                                mutation_headers,
                                method="PUT",
                                body=canary.request.body,
                            ),
                            timeout_seconds,
                        )
                    )
                    mutation_status = mutation_observation.status_code
                    mutation_revision = _response_revision(
                        mutation_observation
                    )
                    if mutation_observation.error_type is not None:
                        fail(mutation_observation.error_type)
                    elif mutation_status not in canary.request.expected_statuses:
                        fail("unexpected_status")
                    elif mutation_revision != prior[0] + 1:
                        fail("invalid_mutation_response")
                    else:
                        try:
                            journal.mark_mutation_revision(
                                entry_id,
                                mutation_revision,
                            )
                        except rollback_journal.RollbackJournalError:
                            fail(
                                "rollback_journal_update_failed",
                                cleanup=True,
                            )
                        phases["mutationApplied"] = True
                        changed_observation = record(
                            _send(
                                transport,
                                _captured_request(
                                    endpoint,
                                    canary.request,
                                    read_headers,
                                    method="GET",
                                    body=None,
                                ),
                                timeout_seconds,
                            )
                        )
                        changed = _project_snapshot(
                            changed_observation,
                            fields,
                        )
                        if changed != (
                            mutation_revision,
                            mutation_body,
                        ):
                            fail(
                                changed_observation.error_type
                                or "changed_state_verification_failed"
                            )
                        else:
                            phases["changedStateVerified"] = True
                finally:
                    pending = next(
                        (
                            entry
                            for entry in journal.entries(pending_only=True)
                            if entry["id"] == entry_id
                        )
                        ,
                        None,
                    )
                    if pending is None:
                        fail("rollback_journal_state_invalid", cleanup=True)
                    else:
                        reconciliation = _reconcile_rollback_entry(
                            journal,
                            pending,
                            environ=environ,
                            transport=transport,
                        )
                        observations.extend(
                            reconciliation["observations"]
                        )
                        phases["rollbackAttempted"] = reconciliation[
                            "rollbackAttempted"
                        ]
                        phases["rollbackSucceeded"] = reconciliation[
                            "rollbackSucceeded"
                        ]
                        phases["restorationVerified"] = reconciliation[
                            "restorationVerified"
                        ]
                        if reconciliation["status"] != "COMPLETE":
                            fail(reconciliation["reason"], cleanup=True)

    passed = (
        failure_reason is None
        and all(phases.values())
    )
    response_bytes = sum(len(observation.body) for observation in observations)
    response_digest = hashlib.sha256()
    for observation in observations:
        response_digest.update(len(observation.body).to_bytes(8, "big"))
        response_digest.update(observation.body)
    return {
        "name": canary.name,
        "category": canary.category,
        "baseUrl": endpoint,
        "method": canary.request.method,
        "path": canary.request.path,
        "credentialEnv": canary.request.credential_env,
        "credentialType": canary.request.credential_type,
        "expectedStatuses": list(canary.request.expected_statuses),
        "statusCode": mutation_status,
        "latencyMs": round(
            sum(observation.latency_ms for observation in observations),
            3,
        ),
        "responseBytes": response_bytes,
        "responseSha256": response_digest.hexdigest(),
        "queryResponseValidated": None,
        "errorCodeValidated": None,
        "roundTrip": phases,
        "passed": passed,
        "failureReason": failure_reason,
    }


def run_canaries(
    config: ValidationConfig,
    endpoints: tuple[str, ...],
    *,
    environ: Mapping[str, str],
    rollback: rollback_journal.RollbackJournal | None = None,
    transport: Transport,
) -> dict[str, Any]:
    """Run every required canary against every configured endpoint."""
    results: list[dict[str, Any]] = []
    for canary in config.canaries:
        if canary.category == TENANT_ADMIN_ROUND_TRIP_CATEGORY:
            if rollback is None:
                raise ConfigurationError(
                    "missing_rollback_journal",
                    "tenant-admin mutation requires a durable rollback journal",
                )
            results.extend(
                _tenant_admin_round_trip(
                    canary,
                    endpoint,
                    environ=environ,
                    journal=rollback,
                    transport=transport,
                    timeout_seconds=config.timeout_seconds,
                )
                for endpoint in endpoints
            )
            continue
        try:
            headers = _credential_headers(canary.request, environ)
            credential_error = None
        except CredentialUnavailable as exc:
            headers = None
            credential_error = exc.code
        for endpoint in endpoints:
            if headers is None:
                observation = HttpObservation(
                    status_code=None,
                    latency_ms=0,
                    error_type=credential_error,
                )
            else:
                observation = _send(
                    transport,
                    _request_for_endpoint(
                        endpoint,
                        canary.request,
                        headers,
                        capture_response=(
                            canary.category
                            in {
                                "authenticated_query_allowed",
                                "viewer_mutation_denied",
                            }
                        ),
                    ),
                    config.timeout_seconds,
                )
            query_response_valid = (
                canary.category != "authenticated_query_allowed"
                or _query_response_matches(canary.request, observation)
            )
            observed_error_code = (
                _response_error_code(observation)
                if canary.category == "viewer_mutation_denied"
                else None
            )
            error_code_valid = (
                canary.category != "viewer_mutation_denied"
                or (
                    observed_error_code != "csrf_validation_failed"
                    and observed_error_code == canary.expected_error_code
                )
            )
            passed = (
                observation.error_type is None
                and observation.status_code
                in canary.request.expected_statuses
                and query_response_valid
                and error_code_valid
            )
            failure_reason = None
            if not passed:
                if observation.error_type is not None:
                    failure_reason = observation.error_type
                elif (
                    observation.status_code
                    not in canary.request.expected_statuses
                ):
                    failure_reason = "unexpected_status"
                elif not query_response_valid:
                    failure_reason = "invalid_query_response"
                elif observed_error_code == "csrf_validation_failed":
                    failure_reason = "csrf_validation_failed"
                else:
                    failure_reason = "invalid_rbac_error_response"
            results.append(
                {
                    "name": canary.name,
                    "category": canary.category,
                    "baseUrl": endpoint,
                    "method": canary.request.method,
                    "path": canary.request.path,
                    "credentialEnv": canary.request.credential_env,
                    "credentialType": canary.request.credential_type,
                    "expectedStatuses": list(
                        canary.request.expected_statuses
                    ),
                    "statusCode": observation.status_code,
                    "latencyMs": round(observation.latency_ms, 3),
                    "responseBytes": len(observation.body),
                    "responseSha256": hashlib.sha256(
                        observation.body
                    ).hexdigest(),
                    "queryResponseValidated": (
                        query_response_valid
                        if (
                            canary.category
                            == "authenticated_query_allowed"
                        )
                        else None
                    ),
                    "errorCodeValidated": (
                        error_code_valid
                        if canary.category == "viewer_mutation_denied"
                        else None
                    ),
                    "roundTrip": None,
                    "passed": passed,
                    "failureReason": failure_reason,
                }
            )
    passed = all(result["passed"] for result in results)
    expected_result_count = len(config.canaries) * len(endpoints)
    return {
        "status": "PASS" if passed else "FAIL",
        "requiredCategories": sorted(
            REQUIRED_CANARY_CATEGORIES_BY_TARGET[config.target]
        ),
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
        "credentialType": config.load.request.credential_type,
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
            "name": "parallel_concurrency_configured",
            "minimum": 2,
            "actual": config.load.concurrency,
            "passed": config.load.concurrency >= 2,
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
        "credentialType": config.load.request.credential_type,
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


def _launch_gate_report(
    config: ValidationConfig,
    canaries: dict[str, Any],
    load: dict[str, Any],
    endpoints: tuple[str, ...],
) -> dict[str, Any]:
    scenario_gates: dict[str, dict[str, Any]] = {}
    results = canaries["results"]
    required_categories = REQUIRED_CANARY_CATEGORIES_BY_TARGET[config.target]
    for category in sorted(required_categories):
        category_results = [
            result
            for result in results
            if result["category"] == category
        ]
        covered_endpoints = {
            result["baseUrl"] for result in category_results
        }
        passed = (
            len(category_results) == len(endpoints)
            and covered_endpoints == set(endpoints)
            and all(result["passed"] for result in category_results)
        )
        scenario_gates[category] = {
            "passed": passed,
            "requestCount": len(category_results),
            "allEndpointsCovered": covered_endpoints == set(endpoints),
        }
    load_gate = {
        "passed": load["status"] == "PASS",
        "requestCountConfigured": load["requestCountConfigured"],
        "requestCountCompleted": load["requestCountCompleted"],
        "concurrency": load["concurrency"],
        "maxErrorRate": load["thresholds"]["maxErrorRate"],
        "maxP95LatencyMs": load["thresholds"]["maxP95LatencyMs"],
    }
    passed = (
        all(gate["passed"] for gate in scenario_gates.values())
        and load_gate["passed"]
    )
    return {
        "status": "PASS" if passed else "FAIL",
        "requiredScenarios": sorted(required_categories),
        "scenarios": scenario_gates,
        "concurrencyLoad": load_gate,
    }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def run_validation(
    config: ValidationConfig,
    endpoints: tuple[str, ...],
    *,
    environ: Mapping[str, str],
    rollback: rollback_journal.RollbackJournal | None = None,
    target_health_collector: TargetHealthCollector | None = None,
    transport: Transport = urllib_transport,
    monotonic: Callable[[], float] = time.perf_counter,
    now: Callable[[], datetime] = _utc_now,
) -> dict[str, Any]:
    """Run local policy, remote canaries, then load only after both pass."""
    if config.target == "fargate" and target_health_collector is None:
        raise ConfigurationError(
            "missing_target_health_collector",
            "Fargate validation requires an in-process ELB target-health "
            "collector",
        )
    if config.target == "fargate" and rollback is None:
        raise ConfigurationError(
            "missing_rollback_journal",
            "Fargate validation requires a durable rollback journal",
        )
    if config.target != "fargate" and target_health_collector is not None:
        raise ConfigurationError(
            "unexpected_target_health_collector",
            "ELB target-health collection is valid only for Fargate",
        )
    if config.target != "fargate" and rollback is not None:
        raise ConfigurationError(
            "unexpected_rollback_journal",
            "rollback journaling is valid only for Fargate",
        )
    started_at = _trusted_time(now, "validation start")
    authorization = evaluate_authorization_contract()
    canaries = run_canaries(
        config,
        endpoints,
        environ=environ,
        rollback=rollback,
        transport=transport,
    )
    prerequisites_passed = (
        authorization["status"] == "PASS"
        and canaries["status"] == "PASS"
        and canaries["allEndpointsCovered"]
    )
    target_health: dict[str, Any] | None = None
    if prerequisites_passed:
        if target_health_collector is not None:
            pre_load = _collect_target_health(
                target_health_collector,
                "pre-load",
            )
            pre_collected_at = _trusted_time(
                now,
                "pre-load target-health collection",
            )
            load_started_at = _trusted_time(now, "HTTP load start")
        load = run_load(
            config,
            endpoints,
            environ=environ,
            transport=transport,
            monotonic=monotonic,
        )
        if target_health_collector is not None:
            load_finished_at = _trusted_time(now, "HTTP load finish")
            post_load = _collect_target_health(
                target_health_collector,
                "post-load",
            )
            post_collected_at = _trusted_time(
                now,
                "post-load target-health collection",
            )
            target_health = _target_health_report(
                pre_load,
                pre_collected_at=pre_collected_at,
                load_started_at=load_started_at,
                load_finished_at=load_finished_at,
                post_load=post_load,
                post_collected_at=post_collected_at,
            )
    else:
        load = _skipped_load(
            config,
            endpoints,
            "authorization_or_canary_prerequisite_failed",
        )
    backing_identity_validated = (
        target_health is not None
        and target_health["status"] == "PASS"
        and load["status"] == "PASS"
    )
    load["backingInstanceIdentityValidated"] = (
        backing_identity_validated
    )
    launch_gates = _launch_gate_report(config, canaries, load, endpoints)
    passed = (
        authorization["status"] == "PASS"
        and launch_gates["status"] == "PASS"
    )
    query_results = [
        result
        for result in canaries["results"]
        if result["category"] == "authenticated_query_allowed"
    ]
    query_backend_exercised = (
        bool(query_results)
        and {result["baseUrl"] for result in query_results} == set(endpoints)
        and all(
            result["passed"] and result["queryResponseValidated"]
            for result in query_results
        )
    )
    query_canary_configured = any(
        canary.category == "authenticated_query_allowed"
        for canary in config.canaries
    )
    finished_at = _trusted_time(now, "validation finish")
    return {
        "schemaVersion": REPORT_SCHEMA,
        "target": config.target,
        "validationScope": (
            "source-policy-http-query-canary-and-load"
            if query_canary_configured
            else "source-policy-http-canary-and-load"
        ),
        "startedAt": started_at.isoformat(),
        "finishedAt": finished_at.isoformat(),
        "overallStatus": "PASS" if passed else "FAIL",
        "claims": {
            "agentcoreCutoverValidated": False,
            "queryBackendExercised": query_backend_exercised,
            "backingInstanceIdentityValidated": (
                backing_identity_validated
            ),
        },
        "httpEndpoints": list(endpoints),
        "authorizationContract": authorization,
        "canaries": canaries,
        "load": load,
        "targetHealth": target_health,
        "launchGates": launch_gates,
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
    parser.add_argument("--config")
    parser.add_argument(
        "--base-url",
        action="append",
        help="Repeat for each distinct HTTPS endpoint.",
    )
    parser.add_argument(
        "--output",
        help="Optional path for the same JSON report written to stdout.",
    )
    parser.add_argument(
        "--target-group-arn",
        help=(
            "Fargate target group collected from ELB immediately before and "
            "after this validation's HTTP load."
        ),
    )
    parser.add_argument(
        "--rollback-journal",
        type=Path,
        help=(
            "New durable journal for Fargate tenant-admin configuration "
            "rollback."
        ),
    )
    parser.add_argument(
        "--reconcile-rollback-journal",
        type=Path,
        help=(
            "Independently reconcile an existing rollback journal and exit."
        ),
    )
    args = parser.parse_args(argv)

    resolved_environ = os.environ if environ is None else environ
    if args.reconcile_rollback_journal is not None:
        if any(
            value is not None
            for value in (
                args.config,
                args.base_url,
                args.output,
                args.target_group_arn,
                args.rollback_journal,
            )
        ):
            parser.error(
                "--reconcile-rollback-journal cannot be combined with a "
                "validation run"
            )
        try:
            journal = rollback_journal.RollbackJournal.open(
                args.reconcile_rollback_journal,
                clock=lambda: _utc_now().isoformat(),
            )
            reconciliation = reconcile_production_validation_rollbacks(
                journal,
                environ=resolved_environ,
                transport=transport,
            )
        except rollback_journal.RollbackJournalError:
            reconciliation = {
                "schema": rollback_journal.RECONCILIATION_SCHEMA,
                "status": "FAIL",
                "error": {"code": "rollback_journal_invalid"},
                "results": [],
            }
            print(json.dumps(reconciliation, indent=2, sort_keys=True))
            return 2
        print(json.dumps(reconciliation, indent=2, sort_keys=True))
        return 0 if reconciliation["status"] == "COMPLETE" else 1

    try:
        if args.config is None or not args.base_url:
            raise ConfigurationError(
                "missing_arguments",
                "validation requires --config and at least one --base-url",
            )
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
        target_health_collector = (
            AwsCliTargetHealthCollector(args.target_group_arn)
            if args.target_group_arn is not None
            else None
        )
        if config.target == "fargate":
            if args.rollback_journal is None:
                raise ConfigurationError(
                    "missing_rollback_journal",
                    "Fargate validation requires --rollback-journal",
                )
            try:
                rollback = rollback_journal.RollbackJournal.create(
                    args.rollback_journal,
                    clock=lambda: _utc_now().isoformat(),
                )
            except rollback_journal.RollbackJournalError as exc:
                raise ConfigurationError(
                    "rollback_journal_unavailable",
                    "the rollback journal could not be created safely",
                ) from exc
        else:
            if args.rollback_journal is not None:
                raise ConfigurationError(
                    "unexpected_rollback_journal",
                    "--rollback-journal is valid only for Fargate",
                )
            rollback = None
        report = run_validation(
            config,
            endpoints,
            environ=resolved_environ,
            rollback=rollback,
            target_health_collector=target_health_collector,
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
