#!/usr/bin/env python3
"""Certify external OIDC against an immutable AxonLLM AgentCore candidate."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import os
import re
import secrets
import stat
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

import httpx
import jwt
from jwt import PyJWTError

from certify_agentcore import (
    PRODUCTION_LAUNCH_PROFILE,
    PRODUCTION_LAUNCH_PROVIDERS,
    PRODUCTION_PROVIDER_FEATURES_BY_PROVIDER,
    REPORT_SCHEMA as LAUNCH_CERTIFICATION_SCHEMA,
    CertificationConfig,
    InvocationObservation,
    InvocationRequest,
    invocation_url,
    load_config as load_certification_config,
    run_certification,
    urllib_transport,
)
from src.gateway.agentcore_setup import (
    EXTERNAL_OIDC,
    AgentCoreSetupConfig,
    load_agentcore_setup,
)
from src.gateway.auth.authorization import Action, ResourceRef, authorize
from src.gateway.auth.dynamo_principal_repository import (
    DynamoPrincipalRepository,
)
from src.gateway.models import (
    AuthMethod,
    MembershipStatus,
    Principal,
    TenantRole,
)
from src.gateway.persistence import DynamoPersistence
from src.gateway.query.models import AthenaDatasource


REPORT_SCHEMA = "https://axonllm.dev/schemas/external-oidc-agentcore-certification/v3"
BROKER_REQUEST_SCHEMA = "axonllm.external-oidc-fixture-request/v1"
BROKER_RESPONSE_SCHEMA = "axonllm.external-oidc-fixture-response/v1"
BROKER_CLEANUP_SCHEMA = "axonllm.external-oidc-fixture-cleanup/v1"
CLEANUP_STATE_SCHEMA = "axonllm.external-oidc-cleanup-state/v1"
RUNTIME_STACK = "AxonLLMAgentCoreStack"
EXTERNAL_OIDC_WORKFLOW = (
    ".github/workflows/certify-agentcore-external-oidc.yml"
)
LAUNCH_WORKFLOW = ".github/workflows/launch-agentcore-production.yml"
PRODUCER_PATH = "scripts/operations/certify_external_oidc_agentcore.py"
BROKER_CREDENTIAL_ENV = "AXON_EXTERNAL_OIDC_FIXTURE_BROKER_TOKEN"
FIXTURE_MARKER = "external_oidc_certification_fixture_id"
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_HTTP_BYTES = 512 * 1024
MAX_JWT_BYTES = 64 * 1024
MAX_JWKS_KEYS = 64
MAX_TOKEN_LIFETIME_SECONDS = 15 * 60
MAX_JWKS_FRESHNESS_SECONDS = 60 * 60
MAX_PUBLISHED_REPORT_AGE_SECONDS = 30 * 24 * 60 * 60
HTTP_TIMEOUT_SECONDS = 15.0
ALLOWED_JWT_ALGORITHMS = frozenset({"RS256", "ES256"})
ALLOWED_STACK_STATUSES = frozenset(
    {
        "CREATE_COMPLETE",
        "UPDATE_COMPLETE",
        "UPDATE_ROLLBACK_COMPLETE",
    }
)
TOKEN_CASES = (
    "admin",
    "viewer",
    "inactive",
    "ungranted",
    "crossTenant",
    "wrongAudience",
    "missingTenant",
    "missingProject",
    "expired",
    "issuerMixup",
)
CANONICAL_CASES = (
    "admin",
    "viewer",
    "inactive",
    "ungranted",
    "crossTenant",
)
REQUIRED_CHECKS = frozenset(
    {
        "admin_model_list",
        "viewer_model_list",
        "admin_tenant_config_read",
        "viewer_tenant_config_read",
        "viewer_tenant_config_write_denied",
        "admin_tenant_config_mutation",
        "admin_tenant_config_mutation_confirmed",
        "admin_tenant_config_rollback",
        "admin_tenant_config_rollback_confirmed",
        "admin_query_select",
        "viewer_query_select",
        "viewer_query_mutation_denied",
        "viewer_payload_role_escalation_denied",
        "wrong_audience_denied",
        "missing_tenant_claim_denied",
        "missing_project_claim_denied",
        "expired_identity_denied",
        "issuer_mixup_denied",
        "tampered_signature_denied",
        "canonical_admin_config_read_allowed",
        "canonical_admin_config_write_allowed",
        "canonical_viewer_config_read_allowed",
        "canonical_viewer_config_write_denied",
        "canonical_admin_query_select_allowed",
        "canonical_viewer_query_select_allowed",
        "canonical_cross_tenant_query_concealed",
    }
)
TENANT_CONFIG_FIELDS = frozenset(
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
_CANDIDATE_PATTERN = re.compile(r"^candidate_[0-9a-f]{32}$")
_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_RUN_PATTERN = re.compile(r"^[1-9][0-9]*$")
_REPOSITORY_PATTERN = re.compile(
    r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
)
_FIXTURE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,255}$")
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_RUNTIME_STACK_PATTERN = re.compile(
    r"^AxonLLMAgentCoreStack"
    r"(?:-[a-z](?:[a-z0-9-]{0,14}[a-z0-9])?)?$"
)
_SAFE_KID = re.compile(r"^[\x21-\x7e]{1,256}$")
_IMAGE_PATTERN = re.compile(
    r"^(?P<account>[0-9]{12})\.dkr\.ecr\."
    r"(?P<region>[a-z0-9-]+)\.amazonaws\.com/"
    r"[a-z0-9]+(?:[._/-][a-z0-9]+)*@sha256:(?P<digest>[0-9a-f]{64})$"
)
_JWT_SHAPE = re.compile(
    r"^[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}$"
)
_SENSITIVE_KEY = re.compile(
    r"(?:^|[_-])(?:access[_-]?tokens?|api[_-]?keys?|authorization|"
    r"client[_-]?secrets?|credentials?|passwords?|private[_-]?keys?|"
    r"refresh[_-]?tokens?|secrets?|tokens?)(?:$|[_-])",
    re.IGNORECASE,
)


class ExternalOidcCertificationError(RuntimeError):
    """A failure safe to print without disclosing identity material."""


def _production_provider_feature_matrix(
    providers: set[str] | frozenset[str],
    *,
    location: str,
) -> dict[str, frozenset[str]]:
    if any(not isinstance(provider, str) for provider in providers):
        raise ExternalOidcCertificationError(
            f"{location} contains an invalid provider name"
        )
    provider_names = frozenset(providers)
    missing = PRODUCTION_LAUNCH_PROVIDERS - provider_names
    unsupported = (
        provider_names - PRODUCTION_PROVIDER_FEATURES_BY_PROVIDER.keys()
    )
    if missing or unsupported:
        raise ExternalOidcCertificationError(
            f"{location} does not match the production provider contract"
        )
    return {
        provider: PRODUCTION_PROVIDER_FEATURES_BY_PROVIDER[provider]
        for provider in sorted(provider_names)
    }


def _certification_provider_feature_matrix(
    certification: CertificationConfig,
) -> dict[str, frozenset[str]]:
    return {
        case.provider: case.features
        for case in certification.providers
    }


@dataclass(frozen=True)
class JsonResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes
    value: Any


class JsonTransport(Protocol):
    def __call__(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
        maximum_bytes: int,
    ) -> JsonResponse: ...


@dataclass(frozen=True)
class Freshness:
    date: datetime
    max_age_seconds: int
    current_age_seconds: float

    def to_report(self) -> dict[str, Any]:
        return {
            "date": self.date.isoformat(),
            "maxAgeSeconds": self.max_age_seconds,
            "currentAgeSeconds": round(self.current_age_seconds, 3),
        }


@dataclass(frozen=True)
class IssuerMaterial:
    issuer: str
    discovery_url: str
    jwks_uri: str
    discovery_sha256: str
    jwks_sha256: str
    discovery_freshness: Freshness
    jwks_freshness: Freshness
    jwks: dict[str, Any]


@dataclass(frozen=True)
class VerifiedIdentity:
    case: str
    token: str
    issuer: str
    subject: str
    audience: tuple[str, ...]
    expires_at: int
    issued_at: int
    jwt_id: str
    tenant_id: str | None
    project_id: str | None


@dataclass(frozen=True)
class BrokerFixture:
    fixture_id: str
    challenge: str
    expires_at: int
    tokens: Mapping[str, str]
    response_sha256: str


@dataclass(frozen=True)
class RuntimeBinding:
    stack_name: str
    stack_id: str
    stack_status: str
    runtime_arn: str
    runtime_version: str
    endpoint_name: str
    endpoint_arn: str
    image: str
    table_name: str
    endpoint_status: str

    def to_report(self, region: str) -> dict[str, str]:
        return {
            "region": region,
            "stackName": self.stack_name,
            "stackId": self.stack_id,
            "stackStatus": self.stack_status,
            "runtimeArn": self.runtime_arn,
            "runtimeVersion": self.runtime_version,
            "endpointName": self.endpoint_name,
            "endpointArn": self.endpoint_arn,
            "endpointStatus": self.endpoint_status,
            "image": self.image,
        }


@dataclass(frozen=True)
class SourceBinding:
    repository: str
    workflow_ref: str
    parent_workflow_ref: str
    run_id: str
    run_attempt: str
    workflow_commit: str
    parent_workflow_commit: str
    release_commit: str
    agentcore_image: str
    runtime_stack_name: str

    def to_report(self) -> dict[str, str]:
        return {
            "repository": self.repository,
            "workflowRef": self.workflow_ref,
            "parentWorkflowRef": self.parent_workflow_ref,
            "runId": self.run_id,
            "runAttempt": self.run_attempt,
            "workflowCommit": self.workflow_commit,
            "parentWorkflowCommit": self.parent_workflow_commit,
            "releaseCommit": self.release_commit,
            "agentcoreImage": self.agentcore_image,
            "runtimeStackName": self.runtime_stack_name,
        }


class AwsSession(Protocol):
    def client(self, service_name: str, *, region_name: str) -> Any: ...

    def resource(self, service_name: str, *, region_name: str) -> Any: ...


def _strict_object(
    value: Any,
    location: str,
    *,
    fields: set[str],
) -> dict[str, Any]:
    if type(value) is not dict or set(value) != fields:
        raise ExternalOidcCertificationError(
            f"{location} fields do not match the required schema"
        )
    return value


def _safe_string(
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
        raise ExternalOidcCertificationError(
            f"{location} must be a non-empty safe string"
        )
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ExternalOidcCertificationError(
                f"duplicate JSON field: {key}"
            )
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ExternalOidcCertificationError(
        f"non-finite JSON value is not allowed: {value}"
    )


def _strict_json(raw: bytes, location: str) -> Any:
    try:
        return json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except ExternalOidcCertificationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExternalOidcCertificationError(
            f"{location} is not strict UTF-8 JSON"
        ) from exc


def _read_json(path: Path, *, maximum: int = MAX_JSON_BYTES) -> Any:
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise ExternalOidcCertificationError(
                f"input must be a regular file: {path}"
            )
        if before.st_size > maximum:
            raise ExternalOidcCertificationError(
                f"input is too large: {path}"
            )
        raw = path.read_bytes()
        after = path.stat()
    except ExternalOidcCertificationError:
        raise
    except OSError as exc:
        raise ExternalOidcCertificationError(
            f"cannot read input: {path}"
        ) from exc
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or len(raw) != after.st_size
    ):
        raise ExternalOidcCertificationError(
            f"input changed while being read: {path}"
        )
    return _strict_json(raw, str(path))


def _atomic_private_json(
    path: Path,
    value: Any,
    *,
    replace: bool,
) -> None:
    resolved = path.expanduser().resolve()
    try:
        resolved.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not replace:
            descriptor = os.open(
                resolved,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    json.dump(
                        value,
                        handle,
                        allow_nan=False,
                        indent=2,
                        sort_keys=True,
                    )
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                resolved.chmod(0o600)
            except Exception:
                try:
                    resolved.unlink()
                except OSError:
                    pass
                raise
            return
        temporary = resolved.with_name(
            f".{resolved.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        )
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(
                    value,
                    handle,
                    allow_nan=False,
                    indent=2,
                    sort_keys=True,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, resolved)
            resolved.chmod(0o600)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    except ExternalOidcCertificationError:
        raise
    except OSError as exc:
        raise ExternalOidcCertificationError(
            f"cannot write owner-only output: {resolved}"
        ) from exc


def _safe_https_url(
    value: Any,
    location: str,
    *,
    issuer: bool = False,
) -> str:
    value = _safe_string(value, location)
    if "\\" in value or any(character.isspace() for character in value):
        raise ExternalOidcCertificationError(
            f"{location} must be a canonical HTTPS URL"
        )
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except (UnicodeError, ValueError) as exc:
        raise ExternalOidcCertificationError(
            f"{location} must be a canonical HTTPS URL"
        ) from exc
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or hostname.endswith(".")
        or hostname.casefold() == "localhost"
        or hostname.casefold().endswith(".localhost")
        or port == 0
    ):
        raise ExternalOidcCertificationError(
            f"{location} must be a canonical HTTPS URL"
        )
    try:
        hostname.encode("ascii")
        ipaddress.ip_address(hostname)
    except UnicodeEncodeError as exc:
        raise ExternalOidcCertificationError(
            f"{location} hostname must be ASCII"
        ) from exc
    except ValueError:
        pass
    else:
        raise ExternalOidcCertificationError(
            f"{location} must not use an IP-literal host"
        )
    if issuer and (
        parsed.query
        or value.endswith("/")
        or parsed.path.endswith(
            "/.well-known/openid-configuration"
        )
    ):
        raise ExternalOidcCertificationError(
            f"{location} must be an issuer URL without query or trailing slash"
        )
    return value


def _origin(value: str) -> tuple[str, int]:
    parsed = urlsplit(value)
    assert parsed.hostname is not None
    return parsed.hostname.casefold(), parsed.port or 443


def httpx_json_transport(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str],
    body: bytes | None,
    timeout_seconds: float,
    maximum_bytes: int,
) -> JsonResponse:
    """Perform one bounded HTTPS JSON exchange without proxies or redirects."""
    try:
        timeout = httpx.Timeout(timeout_seconds, connect=min(5.0, timeout_seconds))
        limits = httpx.Limits(max_connections=2, max_keepalive_connections=0)
        with httpx.Client(
            timeout=timeout,
            limits=limits,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            with client.stream(
                method,
                url,
                headers=dict(headers),
                content=body,
            ) as response:
                payload = bytearray()
                for chunk in response.iter_bytes():
                    payload.extend(chunk)
                    if len(payload) > maximum_bytes:
                        raise ExternalOidcCertificationError(
                            "HTTPS JSON response exceeds the configured limit"
                        )
                normalized: dict[str, str] = {}
                for name, value in response.headers.multi_items():
                    lowered = name.casefold()
                    if lowered in normalized:
                        raise ExternalOidcCertificationError(
                            f"HTTPS response repeats security-relevant header: {lowered}"
                        )
                    normalized[lowered] = value
                encoded = bytes(payload)
                parsed = (
                    _strict_json(encoded, "HTTPS response")
                    if encoded
                    else None
                )
                return JsonResponse(
                    status_code=response.status_code,
                    headers=normalized,
                    body=encoded,
                    value=parsed,
                )
    except ExternalOidcCertificationError:
        raise
    except Exception as exc:
        raise ExternalOidcCertificationError(
            "HTTPS JSON exchange failed"
        ) from exc


def _json_exchange(
    transport: JsonTransport,
    method: str,
    url: str,
    *,
    headers: Mapping[str, str],
    body: bytes | None,
    expected_status: int,
    maximum_bytes: int = MAX_HTTP_BYTES,
) -> JsonResponse:
    _safe_https_url(url, "HTTPS endpoint")
    response = transport(
        method,
        url,
        headers=headers,
        body=body,
        timeout_seconds=HTTP_TIMEOUT_SECONDS,
        maximum_bytes=maximum_bytes,
    )
    if (
        not isinstance(response, JsonResponse)
        or response.status_code != expected_status
        or type(response.value) is not dict
    ):
        raise ExternalOidcCertificationError(
            "HTTPS JSON endpoint returned an unexpected response"
        )
    content_type = response.headers.get("content-type", "")
    if content_type.partition(";")[0].strip().casefold() not in {
        "application/json",
        "application/jwk-set+json",
    }:
        raise ExternalOidcCertificationError(
            "HTTPS JSON endpoint returned an unsupported content type"
        )
    return response


def _http_freshness(
    headers: Mapping[str, str],
    *,
    now: float,
    location: str,
) -> Freshness:
    raw_date = headers.get("date")
    raw_cache = headers.get("cache-control")
    if not isinstance(raw_date, str) or not isinstance(raw_cache, str):
        raise ExternalOidcCertificationError(
            f"{location} lacks verifiable HTTP freshness metadata"
        )
    try:
        date = parsedate_to_datetime(raw_date)
    except (TypeError, ValueError) as exc:
        raise ExternalOidcCertificationError(
            f"{location} Date header is invalid"
        ) from exc
    if date.tzinfo is None:
        raise ExternalOidcCertificationError(
            f"{location} Date header lacks a timezone"
        )
    date = date.astimezone(timezone.utc)
    directives: dict[str, str | None] = {}
    for raw_directive in raw_cache.split(","):
        name, separator, raw_value = raw_directive.strip().partition("=")
        name = name.casefold()
        if not name or name in directives:
            raise ExternalOidcCertificationError(
                f"{location} Cache-Control is ambiguous"
            )
        directives[name] = raw_value.strip().strip('"') if separator else None
    max_age_text = directives.get("max-age")
    if (
        "no-store" in directives
        or "no-cache" in directives
        or not isinstance(max_age_text, str)
        or not max_age_text.isascii()
        or not max_age_text.isdigit()
    ):
        raise ExternalOidcCertificationError(
            f"{location} must advertise a bounded reusable max-age"
        )
    max_age = int(max_age_text)
    if not 1 <= max_age <= MAX_JWKS_FRESHNESS_SECONDS:
        raise ExternalOidcCertificationError(
            f"{location} max-age is outside the accepted range"
        )
    raw_age = headers.get("age", "0")
    if (
        not raw_age.isascii()
        or not raw_age.isdigit()
        or int(raw_age) > MAX_JWKS_FRESHNESS_SECONDS
    ):
        raise ExternalOidcCertificationError(
            f"{location} Age header is invalid"
        )
    apparent_age = max(
        float(int(raw_age)),
        now - date.timestamp(),
    )
    if date.timestamp() > now + 300 or apparent_age < 0 or apparent_age >= max_age:
        raise ExternalOidcCertificationError(
            f"{location} is stale or has unverifiable clock metadata"
        )
    return Freshness(
        date=date,
        max_age_seconds=max_age,
        current_age_seconds=apparent_age,
    )


def _valid_jwks(value: Any) -> dict[str, Any]:
    jwks = value if type(value) is dict else None
    keys = jwks.get("keys") if jwks is not None else None
    if (
        not isinstance(keys, list)
        or not keys
        or len(keys) > MAX_JWKS_KEYS
        or any(type(key) is not dict for key in keys)
    ):
        raise ExternalOidcCertificationError(
            "OIDC JWKS has no bounded signing-key set"
        )
    seen: set[str] = set()
    for key in keys:
        kid = key.get("kid")
        kty = key.get("kty")
        algorithm = key.get("alg")
        if (
            not isinstance(kid, str)
            or _SAFE_KID.fullmatch(kid) is None
            or kid in seen
            or kty not in {"RSA", "EC"}
            or algorithm not in ALLOWED_JWT_ALGORITHMS
            or (algorithm == "RS256" and kty != "RSA")
            or (algorithm == "ES256" and kty != "EC")
            or key.get("use") not in (None, "sig")
        ):
            raise ExternalOidcCertificationError(
                "OIDC JWKS contains an ambiguous or unsupported signing key"
            )
        key_ops = key.get("key_ops")
        if key_ops is not None and (
            not isinstance(key_ops, list)
            or "verify" not in key_ops
            or any(not isinstance(operation, str) for operation in key_ops)
        ):
            raise ExternalOidcCertificationError(
                "OIDC JWKS key is not authorized for signature verification"
            )
        seen.add(kid)
    return jwks


def fetch_issuer_material(
    issuer: str,
    *,
    transport: JsonTransport = httpx_json_transport,
    clock: Callable[[], float] = time.time,
) -> IssuerMaterial:
    """Fetch one fresh, same-origin discovery document and JWKS."""
    issuer = _safe_https_url(issuer, "OIDC issuer", issuer=True)
    discovery_url = f"{issuer}/.well-known/openid-configuration"
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "User-Agent": "axonllm-external-oidc-certification/1",
    }
    now = clock()
    discovery_response = _json_exchange(
        transport,
        "GET",
        discovery_url,
        headers=headers,
        body=None,
        expected_status=200,
    )
    discovery = discovery_response.value
    if discovery.get("issuer") != issuer:
        raise ExternalOidcCertificationError(
            "OIDC discovery issuer does not exactly match the configured issuer"
        )
    jwks_uri = _safe_https_url(
        discovery.get("jwks_uri"),
        "OIDC JWKS URI",
    )
    if _origin(jwks_uri) != _origin(issuer):
        raise ExternalOidcCertificationError(
            "OIDC JWKS URI must have the configured issuer origin"
        )
    discovery_freshness = _http_freshness(
        discovery_response.headers,
        now=now,
        location="OIDC discovery document",
    )
    jwks_response = _json_exchange(
        transport,
        "GET",
        jwks_uri,
        headers={
            **headers,
            "Accept": "application/jwk-set+json, application/json",
        },
        body=None,
        expected_status=200,
    )
    jwks_freshness = _http_freshness(
        jwks_response.headers,
        now=clock(),
        location="OIDC JWKS",
    )
    return IssuerMaterial(
        issuer=issuer,
        discovery_url=discovery_url,
        jwks_uri=jwks_uri,
        discovery_sha256=hashlib.sha256(
            discovery_response.body
        ).hexdigest(),
        jwks_sha256=hashlib.sha256(jwks_response.body).hexdigest(),
        discovery_freshness=discovery_freshness,
        jwks_freshness=jwks_freshness,
        jwks=_valid_jwks(jwks_response.value),
    )


def _token_audiences(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        audiences = (value,)
    elif (
        isinstance(value, list)
        and value
        and all(isinstance(item, str) and item for item in value)
    ):
        audiences = tuple(value)
    else:
        raise ExternalOidcCertificationError(
            "OIDC identity has an invalid audience claim"
        )
    if len(audiences) > 8 or len(set(audiences)) != len(audiences):
        raise ExternalOidcCertificationError(
            "OIDC identity has an ambiguous audience claim"
        )
    return audiences


def _jwk_for_token(
    token: str,
    material: IssuerMaterial,
) -> tuple[dict[str, Any], str]:
    if (
        not isinstance(token, str)
        or not token
        or len(token.encode("utf-8")) > MAX_JWT_BYTES
        or any(character.isspace() for character in token)
    ):
        raise ExternalOidcCertificationError(
            "fixture broker returned an invalid identity"
        )
    try:
        header = jwt.get_unverified_header(token)
    except PyJWTError as exc:
        raise ExternalOidcCertificationError(
            "fixture broker returned an undecodable identity"
        ) from exc
    if type(header) is not dict:
        raise ExternalOidcCertificationError(
            "fixture identity header is malformed"
        )
    algorithm = header.get("alg")
    kid = header.get("kid")
    if (
        algorithm not in ALLOWED_JWT_ALGORITHMS
        or not isinstance(kid, str)
        or _SAFE_KID.fullmatch(kid) is None
        or any(name in header for name in ("crit", "jku", "x5u"))
    ):
        raise ExternalOidcCertificationError(
            "fixture identity uses unsupported JWT trust metadata"
        )
    matches = [
        key
        for key in material.jwks["keys"]
        if key.get("kid") == kid
        and key.get("alg") == algorithm
    ]
    if len(matches) != 1:
        raise ExternalOidcCertificationError(
            "fixture identity signing key is absent or ambiguous"
        )
    return matches[0], algorithm


def _integer_claim(
    claims: Mapping[str, Any],
    name: str,
) -> int:
    value = claims.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExternalOidcCertificationError(
            f"OIDC identity claim {name} must be an integer"
        )
    return value


def verify_fixture_identity(
    case: str,
    token: str,
    *,
    material: IssuerMaterial,
    audience: str,
    tenant_claim: str,
    project_claim: str,
    expected_tenant: str | None,
    expected_project: str | None,
    expect_expired: bool = False,
    expect_wrong_audience: bool = False,
    clock: Callable[[], float] = time.time,
) -> VerifiedIdentity:
    """Verify one broker identity independently of AgentCore."""
    key, algorithm = _jwk_for_token(token, material)
    options = {
        "require": ["aud", "exp", "iat", "iss", "sub"],
        "verify_aud": not expect_wrong_audience,
        "verify_exp": not expect_expired,
        # The certification clock is injected and the bounded iat/nbf policy is
        # enforced below, so do not compare these claims to wall-clock time here.
        "verify_iat": False,
        "verify_nbf": False,
    }
    try:
        claims = jwt.decode(
            token,
            jwt.PyJWK.from_dict(key, algorithm=algorithm),
            algorithms=[algorithm],
            audience=None if expect_wrong_audience else audience,
            issuer=material.issuer,
            options=options,
        )
    except PyJWTError as exc:
        raise ExternalOidcCertificationError(
            f"{case} identity signature or registered claims are invalid"
        ) from exc
    if type(claims) is not dict:
        raise ExternalOidcCertificationError(
            f"{case} identity claims are malformed"
        )
    observed_audiences = _token_audiences(claims.get("aud"))
    if expect_wrong_audience:
        if audience in observed_audiences:
            raise ExternalOidcCertificationError(
                "wrong-audience identity includes the accepted audience"
            )
    elif observed_audiences != (audience,):
        raise ExternalOidcCertificationError(
            f"{case} identity audience does not exactly match configuration"
        )
    now = int(clock())
    issued_at = _integer_claim(claims, "iat")
    expires_at = _integer_claim(claims, "exp")
    if expect_expired:
        if (
            not now - MAX_TOKEN_LIFETIME_SECONDS <= expires_at <= now - 5
            or issued_at > expires_at
            or expires_at - issued_at > MAX_TOKEN_LIFETIME_SECONDS
            or issued_at < now - (2 * MAX_TOKEN_LIFETIME_SECONDS)
        ):
            raise ExternalOidcCertificationError(
                "expired identity is not a bounded stale credential"
            )
    elif (
        issued_at > now + 60
        or issued_at < now - 300
        or expires_at <= now + 30
        or expires_at - issued_at > MAX_TOKEN_LIFETIME_SECONDS
        or expires_at <= issued_at
    ):
        raise ExternalOidcCertificationError(
            f"{case} identity is not short lived"
        )
    not_before = claims.get("nbf")
    if not_before is not None and (
        isinstance(not_before, bool)
        or not isinstance(not_before, int)
        or not_before > now + 60
    ):
        raise ExternalOidcCertificationError(
            f"{case} identity has an invalid not-before claim"
        )
    subject = _safe_string(
        claims.get("sub"),
        f"{case} identity subject",
        maximum=2048,
    )
    jwt_id = _safe_string(
        claims.get("jti"),
        f"{case} identity JWT ID",
        maximum=512,
    )
    observed_tenant = claims.get(tenant_claim)
    observed_project = claims.get(project_claim)
    for name, observed, expected in (
        (tenant_claim, observed_tenant, expected_tenant),
        (project_claim, observed_project, expected_project),
    ):
        if expected is None:
            if observed is not None:
                raise ExternalOidcCertificationError(
                    f"{case} identity unexpectedly contains claim {name}"
                )
        elif observed != expected:
            raise ExternalOidcCertificationError(
                f"{case} identity claim {name} does not match the fixture"
            )
    return VerifiedIdentity(
        case=case,
        token=token,
        issuer=material.issuer,
        subject=subject,
        audience=observed_audiences,
        expires_at=expires_at,
        issued_at=issued_at,
        jwt_id=jwt_id,
        tenant_id=observed_tenant,
        project_id=observed_project,
    )


def validate_token_bundle(
    tokens: Mapping[str, str],
    *,
    expected_material: IssuerMaterial,
    mixup_material: IssuerMaterial,
    setup: AgentCoreSetupConfig,
    cross_tenant_id: str,
    clock: Callable[[], float] = time.time,
) -> dict[str, VerifiedIdentity]:
    """Verify all positive and negative broker identities."""
    if set(tokens) != set(TOKEN_CASES):
        raise ExternalOidcCertificationError(
            "fixture broker identity cases do not match the certification contract"
        )
    oidc = setup.external_oidc
    if setup.identity_mode != EXTERNAL_OIDC or oidc is None:
        raise ExternalOidcCertificationError(
            "external OIDC setup is required"
        )
    common = {
        "audience": oidc.audience,
        "tenant_claim": oidc.tenant_claim,
        "project_claim": oidc.project_claim,
        "clock": clock,
    }
    verified: dict[str, VerifiedIdentity] = {}
    for case in ("admin", "viewer", "inactive", "ungranted"):
        verified[case] = verify_fixture_identity(
            case,
            tokens[case],
            material=expected_material,
            expected_tenant=setup.tenant.tenant_id,
            expected_project=setup.tenant.project_id,
            **common,
        )
    verified["crossTenant"] = verify_fixture_identity(
        "crossTenant",
        tokens["crossTenant"],
        material=expected_material,
        expected_tenant=cross_tenant_id,
        expected_project=setup.tenant.project_id,
        **common,
    )
    verified["wrongAudience"] = verify_fixture_identity(
        "wrongAudience",
        tokens["wrongAudience"],
        material=expected_material,
        expected_tenant=setup.tenant.tenant_id,
        expected_project=setup.tenant.project_id,
        expect_wrong_audience=True,
        **common,
    )
    verified["missingTenant"] = verify_fixture_identity(
        "missingTenant",
        tokens["missingTenant"],
        material=expected_material,
        expected_tenant=None,
        expected_project=setup.tenant.project_id,
        **common,
    )
    verified["missingProject"] = verify_fixture_identity(
        "missingProject",
        tokens["missingProject"],
        material=expected_material,
        expected_tenant=setup.tenant.tenant_id,
        expected_project=None,
        **common,
    )
    verified["expired"] = verify_fixture_identity(
        "expired",
        tokens["expired"],
        material=expected_material,
        expected_tenant=setup.tenant.tenant_id,
        expected_project=setup.tenant.project_id,
        expect_expired=True,
        **common,
    )
    verified["issuerMixup"] = verify_fixture_identity(
        "issuerMixup",
        tokens["issuerMixup"],
        material=mixup_material,
        expected_tenant=setup.tenant.tenant_id,
        expected_project=setup.tenant.project_id,
        **common,
    )
    if len({verified[case].subject for case in CANONICAL_CASES}) != len(
        CANONICAL_CASES
    ):
        raise ExternalOidcCertificationError(
            "canonical fixture identities must have distinct subjects"
        )
    if len({identity.jwt_id for identity in verified.values()}) != len(
        verified
    ):
        raise ExternalOidcCertificationError(
            "fixture identities must have unique JWT IDs"
        )
    viewer_subject = verified["viewer"].subject
    for case in (
        "wrongAudience",
        "missingTenant",
        "missingProject",
        "expired",
    ):
        if verified[case].subject != viewer_subject:
            raise ExternalOidcCertificationError(
                f"{case} must vary claims for the viewer subject"
            )
    return verified


def _credential() -> str:
    value = os.environ.get(BROKER_CREDENTIAL_ENV)
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(character.isspace() for character in value)
    ):
        raise ExternalOidcCertificationError(
            "external OIDC fixture broker credential is unavailable"
        )
    return value


def create_broker_fixture(
    *,
    broker_url: str,
    mixup_issuer: str,
    setup: AgentCoreSetupConfig,
    challenge: str,
    cross_tenant_id: str,
    transport: JsonTransport = httpx_json_transport,
    clock: Callable[[], float] = time.time,
) -> BrokerFixture:
    """Create one short-lived identity fixture without persisting credentials."""
    broker_url = _safe_https_url(
        broker_url,
        "fixture broker URL",
    )
    mixup_issuer = _safe_https_url(
        mixup_issuer,
        "mix-up issuer",
        issuer=True,
    )
    oidc = setup.external_oidc
    if oidc is None:
        raise ExternalOidcCertificationError(
            "external OIDC setup is missing"
        )
    request = {
        "schema": BROKER_REQUEST_SCHEMA,
        "challenge": challenge,
        "ttlSeconds": 600,
        "issuer": oidc.issuer,
        "mixupIssuer": mixup_issuer,
        "clientId": oidc.client_id,
        "audience": oidc.audience,
        "claims": {
            "tenant": oidc.tenant_claim,
            "project": oidc.project_claim,
        },
        "resources": {
            "tenantId": setup.tenant.tenant_id,
            "projectId": setup.tenant.project_id,
            "crossTenantId": cross_tenant_id,
        },
        "identityCases": list(TOKEN_CASES),
    }
    encoded = json.dumps(
        request,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    response = _json_exchange(
        transport,
        "POST",
        broker_url,
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Authorization": f"Bearer {_credential()}",
            "Cache-Control": "no-store",
            "Content-Type": "application/json",
            "User-Agent": "axonllm-external-oidc-certification/1",
        },
        body=encoded,
        expected_status=201,
    )
    if "no-store" not in response.headers.get("cache-control", "").casefold():
        raise ExternalOidcCertificationError(
            "fixture broker response must prohibit caching"
        )
    value = _strict_object(
        response.value,
        "fixture broker response",
        fields={
            "schema",
            "challenge",
            "fixtureId",
            "expiresAt",
            "issuer",
            "mixupIssuer",
            "audience",
            "claims",
            "identities",
        },
    )
    claims = _strict_object(
        value["claims"],
        "fixture broker claims",
        fields={"tenant", "project"},
    )
    identities = value["identities"]
    now = int(clock())
    if (
        value["schema"] != BROKER_RESPONSE_SCHEMA
        or value["challenge"] != challenge
        or value["issuer"] != oidc.issuer
        or value["mixupIssuer"] != mixup_issuer
        or value["audience"] != oidc.audience
        or claims
        != {
            "tenant": oidc.tenant_claim,
            "project": oidc.project_claim,
        }
        or type(identities) is not dict
        or set(identities) != set(TOKEN_CASES)
        or any(
            not isinstance(identity, str) or not identity
            for identity in identities.values()
        )
        or not isinstance(value["expiresAt"], int)
        or isinstance(value["expiresAt"], bool)
        or not now + 120 <= value["expiresAt"] <= now + MAX_TOKEN_LIFETIME_SECONDS
    ):
        raise ExternalOidcCertificationError(
            "fixture broker response is not bound to this request"
        )
    fixture_id = _safe_string(
        value["fixtureId"],
        "fixture broker fixture ID",
        maximum=256,
    )
    if _FIXTURE_ID_PATTERN.fullmatch(fixture_id) is None:
        raise ExternalOidcCertificationError(
            "fixture broker fixture ID is invalid"
        )
    return BrokerFixture(
        fixture_id=fixture_id,
        challenge=challenge,
        expires_at=value["expiresAt"],
        tokens=dict(identities),
        response_sha256=hashlib.sha256(response.body).hexdigest(),
    )


def delete_broker_fixture(
    *,
    broker_url: str,
    fixture_id: str,
    challenge: str,
    transport: JsonTransport = httpx_json_transport,
) -> dict[str, Any]:
    endpoint = (
        f"{_safe_https_url(broker_url, 'fixture broker URL').rstrip('/')}/"
        f"{quote(fixture_id, safe='')}"
    )
    encoded = json.dumps(
        {
            "schema": BROKER_CLEANUP_SCHEMA,
            "challenge": challenge,
        },
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    response = _json_exchange(
        transport,
        "DELETE",
        endpoint,
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Authorization": f"Bearer {_credential()}",
            "Cache-Control": "no-store",
            "Content-Type": "application/json",
            "User-Agent": "axonllm-external-oidc-certification/1",
        },
        body=encoded,
        expected_status=200,
    )
    value = _strict_object(
        response.value,
        "fixture broker cleanup response",
        fields={
            "schema",
            "challenge",
            "fixtureId",
            "complete",
            "identitiesRevoked",
        },
    )
    if (
        value["schema"] != BROKER_CLEANUP_SCHEMA
        or value["challenge"] != challenge
        or value["fixtureId"] != fixture_id
        or value["complete"] is not True
        or value["identitiesRevoked"] is not True
    ):
        raise ExternalOidcCertificationError(
            "fixture broker did not prove fixture cleanup"
        )
    return {
        "status": "PASS",
        "complete": True,
        "identitiesRevoked": value["identitiesRevoked"],
        "responseSha256": hashlib.sha256(response.body).hexdigest(),
    }


def _source_binding(
    *,
    repository: str,
    workflow_ref: str,
    parent_workflow_ref: str,
    run_id: str,
    run_attempt: str,
    workflow_commit: str,
    parent_workflow_commit: str,
    release_commit: str,
    agentcore_image: str,
    runtime_stack_name: str,
    region: str,
) -> SourceBinding:
    if _REPOSITORY_PATTERN.fullmatch(repository) is None:
        raise ExternalOidcCertificationError(
            "repository must be owner/name"
        )
    expected_workflow_ref = (
        f"{repository}/{EXTERNAL_OIDC_WORKFLOW}@refs/heads/main"
    )
    expected_parent_workflow_ref = (
        f"{repository}/{LAUNCH_WORKFLOW}@refs/heads/main"
    )
    if (
        workflow_ref != expected_workflow_ref
        or parent_workflow_ref != expected_parent_workflow_ref
    ):
        raise ExternalOidcCertificationError(
            "external OIDC certification must be called by the protected "
            "production launch workflow"
        )
    if (
        _RUN_PATTERN.fullmatch(run_id) is None
        or _RUN_PATTERN.fullmatch(run_attempt) is None
    ):
        raise ExternalOidcCertificationError(
            "workflow run identity is invalid"
        )
    if (
        _SHA_PATTERN.fullmatch(workflow_commit) is None
        or _SHA_PATTERN.fullmatch(parent_workflow_commit) is None
        or _SHA_PATTERN.fullmatch(release_commit) is None
    ):
        raise ExternalOidcCertificationError(
            "workflow and release commits must be full lowercase SHAs"
        )
    if (
        workflow_commit != release_commit
        or parent_workflow_commit != release_commit
    ):
        raise ExternalOidcCertificationError(
            "workflow, parent, and release commits must match exactly"
        )
    image_match = _IMAGE_PATTERN.fullmatch(agentcore_image)
    if image_match is None or image_match.group("region") != region:
        raise ExternalOidcCertificationError(
            "AgentCore image must be an immutable ECR digest in the target region"
        )
    if _RUNTIME_STACK_PATTERN.fullmatch(runtime_stack_name) is None:
        raise ExternalOidcCertificationError(
            "runtime stack name must be AxonLLMAgentCoreStack or an exact "
            "validated deployment namespace"
        )
    return SourceBinding(
        repository=repository,
        workflow_ref=workflow_ref,
        parent_workflow_ref=parent_workflow_ref,
        run_id=run_id,
        run_attempt=run_attempt,
        workflow_commit=workflow_commit,
        parent_workflow_commit=parent_workflow_commit,
        release_commit=release_commit,
        agentcore_image=agentcore_image,
        runtime_stack_name=runtime_stack_name,
    )


def _stack_map(
    values: Any,
    *,
    key_name: str,
    value_name: str,
    location: str,
) -> dict[str, str]:
    if not isinstance(values, list):
        raise ExternalOidcCertificationError(
            f"{location} is malformed"
        )
    result: dict[str, str] = {}
    for item in values:
        if (
            type(item) is not dict
            or not isinstance(item.get(key_name), str)
            or not isinstance(item.get(value_name), str)
            or item[key_name] in result
        ):
            raise ExternalOidcCertificationError(
                f"{location} is malformed or ambiguous"
            )
        result[item[key_name]] = item[value_name]
    return result


def resolve_runtime_binding(
    session: AwsSession,
    *,
    setup: AgentCoreSetupConfig,
    certification: CertificationConfig,
    expected_image: str,
    runtime_stack_name: str,
) -> RuntimeBinding:
    """Resolve the exact CloudFormation and AgentCore candidate binding."""
    cloudformation = session.client(
        "cloudformation",
        region_name=setup.aws_region,
    )
    if _RUNTIME_STACK_PATTERN.fullmatch(runtime_stack_name) is None:
        raise ExternalOidcCertificationError(
            "runtime stack name is invalid"
        )
    try:
        response = cloudformation.describe_stacks(
            StackName=runtime_stack_name,
        )
    except Exception as exc:
        raise ExternalOidcCertificationError(
            "cannot resolve the AgentCore CloudFormation stack"
        ) from exc
    stacks = response.get("Stacks") if type(response) is dict else None
    if not isinstance(stacks, list) or len(stacks) != 1 or type(stacks[0]) is not dict:
        raise ExternalOidcCertificationError(
            "AgentCore CloudFormation stack response is ambiguous"
        )
    stack = stacks[0]
    status = stack.get("StackStatus")
    stack_id = stack.get("StackId")
    if (
        status not in ALLOWED_STACK_STATUSES
        or not isinstance(stack_id, str)
        or not stack_id.startswith("arn:")
        or f":stack/{runtime_stack_name}/" not in stack_id
    ):
        raise ExternalOidcCertificationError(
            "AgentCore CloudFormation stack is not in a stable state"
        )
    parameters = _stack_map(
        stack.get("Parameters"),
        key_name="ParameterKey",
        value_name="ParameterValue",
        location="AgentCore stack parameters",
    )
    outputs = _stack_map(
        stack.get("Outputs"),
        key_name="OutputKey",
        value_name="OutputValue",
        location="AgentCore stack outputs",
    )
    oidc = setup.external_oidc
    if oidc is None:
        raise ExternalOidcCertificationError(
            "external OIDC setup is missing"
        )
    expected_parameters = {
        "OidcIssuer": oidc.issuer,
        "OidcDiscoveryUrl": oidc.discovery_url,
        "OidcClientIds": oidc.client_id,
        "OidcAudiences": oidc.audience,
        "OidcTenantClaim": oidc.tenant_claim,
        "OidcProjectClaim": oidc.project_claim,
        "VerifiedImageUri": expected_image,
    }
    if any(
        parameters.get(name) != value
        for name, value in expected_parameters.items()
    ):
        raise ExternalOidcCertificationError(
            "deployed AgentCore OIDC or image parameters differ from reviewed setup"
        )
    required_outputs = {
        "RuntimeArn",
        "RuntimeVersion",
        "CandidateRuntimeVersion",
        "CandidateRuntimeEndpointName",
        "CandidateRuntimeEndpointArn",
        "RuntimeImageUri",
        "RecoveryCutoverMode",
        "StateTableName",
        "SelectedRuntimeStateTableName",
    }
    if any(
        not isinstance(outputs.get(name), str) or not outputs[name]
        for name in required_outputs
    ):
        raise ExternalOidcCertificationError(
            "AgentCore stack lacks required candidate outputs"
        )
    runtime_arn = outputs["RuntimeArn"]
    runtime_version = outputs["RuntimeVersion"]
    endpoint_name = outputs["CandidateRuntimeEndpointName"]
    endpoint_arn = outputs["CandidateRuntimeEndpointArn"]
    if (
        runtime_arn != certification.runtime_arn
        or endpoint_name != certification.qualifier
        or _CANDIDATE_PATTERN.fullmatch(endpoint_name) is None
        or not runtime_version.isdigit()
        or outputs["CandidateRuntimeVersion"] != runtime_version
        or endpoint_arn
        != f"{runtime_arn}/runtime-endpoint/{endpoint_name}"
        or outputs["RuntimeImageUri"] != expected_image
        or outputs["RecoveryCutoverMode"] != "normal"
        or outputs["StateTableName"]
        != outputs["SelectedRuntimeStateTableName"]
        or _SAFE_IDENTIFIER.fullmatch(
            outputs["SelectedRuntimeStateTableName"]
        )
        is None
    ):
        raise ExternalOidcCertificationError(
            "AgentCore candidate outputs are not immutable or internally consistent"
        )
    control = session.client(
        "bedrock-agentcore-control",
        region_name=setup.aws_region,
    )
    try:
        endpoint = control.get_agent_runtime_endpoint(
            agentRuntimeId=runtime_arn.rsplit("/", 1)[-1],
            endpointName=endpoint_name,
        )
    except Exception as exc:
        raise ExternalOidcCertificationError(
            "cannot resolve AgentCore candidate endpoint"
        ) from exc
    if (
        type(endpoint) is not dict
        or endpoint.get("agentRuntimeArn") != runtime_arn
        or endpoint.get("agentRuntimeEndpointArn") != endpoint_arn
        or endpoint.get("name") != endpoint_name
        or endpoint.get("status") != "READY"
        or endpoint.get("liveVersion") != runtime_version
        or endpoint.get("targetVersion") != runtime_version
    ):
        raise ExternalOidcCertificationError(
            "AgentCore candidate endpoint is not READY on one immutable runtime version"
        )
    return RuntimeBinding(
        stack_name=runtime_stack_name,
        stack_id=stack_id,
        stack_status=status,
        runtime_arn=runtime_arn,
        runtime_version=runtime_version,
        endpoint_name=endpoint_name,
        endpoint_arn=endpoint_arn,
        image=expected_image,
        table_name=outputs["SelectedRuntimeStateTableName"],
        endpoint_status="READY",
    )


def _validate_configs(
    setup: AgentCoreSetupConfig,
    certification: CertificationConfig,
    *,
    expected_image: str,
) -> None:
    oidc = setup.external_oidc
    if (
        setup.identity_mode != EXTERNAL_OIDC
        or oidc is None
        or setup.control_plane is not None
        or setup.managed_cognito is not None
    ):
        raise ExternalOidcCertificationError(
            "certification requires a disjoint external-oidc setup"
        )
    if (
        setup.aws_region != certification.region
        or setup.runtime.verified_image_uri != expected_image
        or certification.profile != PRODUCTION_LAUNCH_PROFILE
    ):
        raise ExternalOidcCertificationError(
            "setup and certification do not match the production launch contract"
        )
    setup_providers = set(setup.runtime.enabled_providers)
    expected_features = _production_provider_feature_matrix(
        setup_providers,
        location="reviewed external-OIDC setup",
    )
    if (
        _certification_provider_feature_matrix(certification)
        != expected_features
    ):
        raise ExternalOidcCertificationError(
            "reviewed setup and certification providers must exactly match"
        )
    athena = setup.runtime.athena_query
    if (
        athena is None
        or certification.query.role_arn not in athena.role_arns
    ):
        raise ExternalOidcCertificationError(
            "certification query role is outside the reviewed AgentCore setup"
        )
    if oidc.discovery_url != (
        f"{oidc.issuer}/.well-known/openid-configuration"
    ):
        raise ExternalOidcCertificationError(
            "configured external OIDC discovery URL does not match its issuer"
        )
    tenant_config = certification.tenant_config
    if tenant_config is not None and (
        tenant_config.tenant_id != setup.tenant.tenant_id
        or tenant_config.project_id != setup.tenant.project_id
    ):
        raise ExternalOidcCertificationError(
            "certification tenantConfig does not match the external setup "
            "tenant and project"
        )


def _certification_credentials(
    certification: CertificationConfig,
    verified: Mapping[str, VerifiedIdentity],
) -> dict[str, str]:
    credentials = {
        certification.identities.active_env: verified["admin"].token,
        certification.identities.inactive_env: verified["inactive"].token,
        certification.identities.ungranted_env: verified["ungranted"].token,
        certification.identities.cross_tenant_env: (
            verified["crossTenant"].token
        ),
    }
    if certification.identities.admin_env is not None:
        credentials[certification.identities.admin_env] = (
            verified["admin"].token
        )
    if certification.identities.viewer_env is not None:
        credentials[certification.identities.viewer_env] = (
            verified["viewer"].token
        )
    return credentials


def _fixture_principal(
    identity: VerifiedIdentity,
    *,
    tenant_id: str,
    project_ids: frozenset[str],
    role: TenantRole,
    status: MembershipStatus,
) -> Principal:
    subject_digest = hashlib.sha256(
        f"{identity.issuer}\0{identity.subject}".encode("utf-8")
    ).hexdigest()[:32]
    return Principal(
        principal_id=f"external-oidc-cert:{identity.case}:{subject_digest}",
        tenant_id=tenant_id,
        subject=identity.subject,
        issuer=identity.issuer,
        roles=frozenset({role}),
        auth_method=AuthMethod.OIDC_JWT,
        membership_status=status,
        project_ids=project_ids,
        scopes=frozenset(),
        authorization_version=1,
    )


def _datasource_item(
    setup: AgentCoreSetupConfig,
    certification: CertificationConfig,
    *,
    fixture_id: str,
    expires_at: int,
    clock: Callable[[], float],
) -> dict[str, Any]:
    instant = datetime.fromtimestamp(
        clock(),
        tz=timezone.utc,
    ).isoformat()
    datasource = AthenaDatasource(
        datasource_id=certification.query.datasource_id,
        tenant_id=setup.tenant.tenant_id,
        project_id=setup.tenant.project_id,
        name=certification.query.datasource_id,
        role_arn=certification.query.role_arn,
        region=certification.query.region,
        catalog=certification.query.catalog,
        database=certification.query.database,
        workgroup=certification.query.workgroup,
        enabled=True,
        revision=1,
        created_at=instant,
        updated_at=instant,
    )
    document = datasource.to_dict()
    for field in (
        "tenant_id",
        "project_id",
        "datasource_id",
        "revision",
    ):
        document.pop(field)
    item = DynamoPersistence.serialize_tenant_datasource(
        setup.tenant.tenant_id,
        setup.tenant.project_id,
        certification.query.datasource_id,
        document,
        revision=1,
    )
    item[FIXTURE_MARKER] = fixture_id
    item["expires_at"] = expires_at
    return item


def _new_cleanup_state(
    *,
    region: str,
    table_name: str,
    broker_url: str,
    fixture: BrokerFixture,
) -> dict[str, Any]:
    return {
        "schema": CLEANUP_STATE_SCHEMA,
        "region": region,
        "tableName": table_name,
        "brokerUrl": broker_url,
        "fixtureId": fixture.fixture_id,
        "challenge": fixture.challenge,
        "expiresAt": fixture.expires_at,
        "principals": [],
        "datasource": None,
    }


def _put_owned_item(
    table: Any,
    item: dict[str, Any],
    *,
    location: str,
) -> None:
    try:
        table.put_item(
            Item=item,
            ConditionExpression=(
                "attribute_not_exists(PK) AND attribute_not_exists(SK)"
            ),
        )
    except Exception as exc:
        raise ExternalOidcCertificationError(
            f"cannot create {location} without overwriting existing state"
        ) from exc


def install_canonical_fixtures(
    table: Any,
    *,
    setup: AgentCoreSetupConfig,
    certification: CertificationConfig,
    verified: Mapping[str, VerifiedIdentity],
    fixture: BrokerFixture,
    state: dict[str, Any],
    state_path: Path,
    clock: Callable[[], float] = time.time,
) -> dict[str, Principal]:
    """Install canonical authority with collision checks and TTL fallback."""
    expiry = fixture.expires_at + 900
    principal_specs = {
        "admin": (
            setup.tenant.tenant_id,
            frozenset({setup.tenant.project_id}),
            TenantRole.TENANT_ADMIN,
            MembershipStatus.ACTIVE,
        ),
        "viewer": (
            setup.tenant.tenant_id,
            frozenset({setup.tenant.project_id}),
            TenantRole.TENANT_MEMBER,
            MembershipStatus.ACTIVE,
        ),
        "inactive": (
            setup.tenant.tenant_id,
            frozenset({setup.tenant.project_id}),
            TenantRole.TENANT_MEMBER,
            MembershipStatus.SUSPENDED,
        ),
        "ungranted": (
            setup.tenant.tenant_id,
            frozenset(),
            TenantRole.TENANT_MEMBER,
            MembershipStatus.ACTIVE,
        ),
        "crossTenant": (
            verified["crossTenant"].tenant_id,
            frozenset({setup.tenant.project_id}),
            TenantRole.TENANT_MEMBER,
            MembershipStatus.ACTIVE,
        ),
    }
    principals: dict[str, Principal] = {}
    for case in CANONICAL_CASES:
        tenant_id, project_ids, role, membership = principal_specs[case]
        if not isinstance(tenant_id, str):
            raise ExternalOidcCertificationError(
                "cross-tenant fixture lacks a tenant"
            )
        principal = _fixture_principal(
            verified[case],
            tenant_id=tenant_id,
            project_ids=project_ids,
            role=role,
            status=membership,
        )
        item = DynamoPrincipalRepository.serialize(principal)
        item[FIXTURE_MARKER] = fixture.fixture_id
        item["expires_at"] = expiry
        state["principals"].append(
            {
                "case": case,
                "PK": item["PK"],
                "SK": item["SK"],
                "principalId": item["principal_id"],
                "installed": False,
            }
        )
        _atomic_private_json(state_path, state, replace=True)
        _put_owned_item(
            table,
            item,
            location=f"{case} canonical principal",
        )
        state["principals"][-1]["installed"] = True
        _atomic_private_json(state_path, state, replace=True)
        principals[case] = principal
    datasource = _datasource_item(
        setup,
        certification,
        fixture_id=fixture.fixture_id,
        expires_at=expiry,
        clock=clock,
    )
    state["datasource"] = {
        "PK": datasource["PK"],
        "SK": datasource["SK"],
        "document": datasource["document"],
        "revision": datasource["revision"],
        "installed": False,
    }
    _atomic_private_json(state_path, state, replace=True)
    _put_owned_item(
        table,
        datasource,
        location="external OIDC certification datasource",
    )
    state["datasource"]["installed"] = True
    _atomic_private_json(state_path, state, replace=True)
    return principals


def _validate_cleanup_state(value: Any) -> dict[str, Any]:
    state = _strict_object(
        value,
        "cleanup state",
        fields={
            "schema",
            "region",
            "tableName",
            "brokerUrl",
            "fixtureId",
            "challenge",
            "expiresAt",
            "principals",
            "datasource",
        },
    )
    if (
        state["schema"] != CLEANUP_STATE_SCHEMA
        or _SAFE_IDENTIFIER.fullmatch(
            _safe_string(state["region"], "cleanup region", maximum=64)
        )
        is None
        or _SAFE_IDENTIFIER.fullmatch(
            _safe_string(
                state["tableName"],
                "cleanup table",
                maximum=255,
            )
        )
        is None
        or _FIXTURE_ID_PATTERN.fullmatch(
            _safe_string(
                state["fixtureId"],
                "cleanup fixture ID",
                maximum=256,
            )
        )
        is None
        or _SHA256_PATTERN.fullmatch(
            _safe_string(
                state["challenge"],
                "cleanup challenge",
                maximum=64,
            )
        )
        is None
        or not isinstance(state["expiresAt"], int)
        or isinstance(state["expiresAt"], bool)
    ):
        raise ExternalOidcCertificationError(
            "cleanup state metadata is malformed"
        )
    _safe_https_url(state["brokerUrl"], "cleanup broker URL")
    principals = state["principals"]
    if (
        not isinstance(principals, list)
        or len(principals) > len(CANONICAL_CASES)
    ):
        raise ExternalOidcCertificationError(
            "cleanup principal state is malformed"
        )
    seen: set[str] = set()
    for principal in principals:
        item = _strict_object(
            principal,
            "cleanup principal",
            fields={"case", "PK", "SK", "principalId", "installed"},
        )
        case = item["case"]
        if (
            case not in CANONICAL_CASES
            or case in seen
            or not _safe_string(item["PK"], "cleanup principal PK").startswith(
                "IDENTITY#"
            )
            or not _safe_string(item["SK"], "cleanup principal SK").startswith(
                "TENANT#"
            )
            or not isinstance(item["installed"], bool)
        ):
            raise ExternalOidcCertificationError(
                "cleanup principal state is malformed"
            )
        _safe_string(item["principalId"], "cleanup principal ID")
        seen.add(case)
    datasource = state["datasource"]
    if datasource is not None:
        item = _strict_object(
            datasource,
            "cleanup datasource",
            fields={"PK", "SK", "document", "revision", "installed"},
        )
        if (
            not _safe_string(
                item["PK"],
                "cleanup datasource PK",
            ).startswith("TENANT#")
            or not _safe_string(
                item["SK"],
                "cleanup datasource SK",
            ).startswith("DATASOURCE#")
            or not isinstance(item["document"], str)
            or item["revision"] != 1
            or not isinstance(item["installed"], bool)
        ):
            raise ExternalOidcCertificationError(
                "cleanup datasource state is malformed"
            )
    return state


def _owned_item(
    table: Any,
    *,
    key: Mapping[str, str],
    fixture_id: str,
    allow_unowned: bool,
) -> dict[str, Any] | None:
    try:
        response = table.get_item(
            Key=dict(key),
            ConsistentRead=True,
        )
    except Exception as exc:
        raise ExternalOidcCertificationError(
            "cannot inspect external OIDC fixture during cleanup"
        ) from exc
    item = response.get("Item") if type(response) is dict else None
    if item is None:
        return None
    if type(item) is not dict:
        raise ExternalOidcCertificationError(
            "fixture cleanup encountered malformed state"
        )
    if item.get(FIXTURE_MARKER) != fixture_id:
        if allow_unowned:
            return None
        raise ExternalOidcCertificationError(
            "refusing to delete state not owned by this fixture"
        )
    return item


def _delete_principal(
    table: Any,
    value: Mapping[str, Any],
    *,
    fixture_id: str,
) -> bool:
    key = {"PK": value["PK"], "SK": value["SK"]}
    item = _owned_item(
        table,
        key=key,
        fixture_id=fixture_id,
        allow_unowned=value["installed"] is False,
    )
    if item is None:
        return False
    if (
        item.get("entity_type") != "tenant_principal"
        or item.get("principal_id") != value["principalId"]
    ):
        raise ExternalOidcCertificationError(
            "refusing to delete a changed canonical principal"
        )
    table.delete_item(
        Key=key,
        ConditionExpression=(
            "#marker = :marker AND #entity = :entity "
            "AND principal_id = :principal_id"
        ),
        ExpressionAttributeNames={
            "#marker": FIXTURE_MARKER,
            "#entity": "entity_type",
        },
        ExpressionAttributeValues={
            ":marker": fixture_id,
            ":entity": "tenant_principal",
            ":principal_id": value["principalId"],
        },
    )
    return True


def _delete_datasource(
    table: Any,
    value: Mapping[str, Any],
    *,
    fixture_id: str,
) -> bool:
    key = {"PK": value["PK"], "SK": value["SK"]}
    item = _owned_item(
        table,
        key=key,
        fixture_id=fixture_id,
        allow_unowned=value["installed"] is False,
    )
    if item is None:
        return False
    if (
        item.get("entity_type") != "athena_datasource"
        or item.get("document") != value["document"]
        or item.get("revision") != value["revision"]
    ):
        raise ExternalOidcCertificationError(
            "refusing to delete a changed certification datasource"
        )
    table.delete_item(
        Key=key,
        ConditionExpression=(
            "#marker = :marker AND #entity = :entity "
            "AND #document = :document AND #revision = :revision"
        ),
        ExpressionAttributeNames={
            "#marker": FIXTURE_MARKER,
            "#entity": "entity_type",
            "#document": "document",
            "#revision": "revision",
        },
        ExpressionAttributeValues={
            ":marker": fixture_id,
            ":entity": "athena_datasource",
            ":document": value["document"],
            ":revision": value["revision"],
        },
    )
    return True


def cleanup_fixtures(
    state_path: Path,
    *,
    session: AwsSession,
    transport: JsonTransport = httpx_json_transport,
) -> dict[str, Any]:
    """Idempotently remove only state carrying this fixture's ownership marker."""
    path = state_path.expanduser().resolve()
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {
            "status": "PASS",
            "complete": True,
            "localItemsRemoved": 0,
            "broker": None,
        }
    except OSError as exc:
        raise ExternalOidcCertificationError(
            "cannot inspect external OIDC cleanup state"
        ) from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ExternalOidcCertificationError(
            "cleanup state must be an owner-only regular file"
        )
    state = _validate_cleanup_state(_read_json(path))
    table = session.resource(
        "dynamodb",
        region_name=state["region"],
    ).Table(state["tableName"])
    removed = 0
    datasource = state["datasource"]
    if datasource is not None and _delete_datasource(
        table,
        datasource,
        fixture_id=state["fixtureId"],
    ):
        removed += 1
    for principal in reversed(state["principals"]):
        if _delete_principal(
            table,
            principal,
            fixture_id=state["fixtureId"],
        ):
            removed += 1
    broker = delete_broker_fixture(
        broker_url=state["brokerUrl"],
        fixture_id=state["fixtureId"],
        challenge=state["challenge"],
        transport=transport,
    )
    try:
        path.unlink()
    except OSError as exc:
        raise ExternalOidcCertificationError(
            "fixtures were removed but cleanup state could not be deleted"
        ) from exc
    return {
        "status": "PASS",
        "complete": True,
        "localItemsRemoved": removed,
        "broker": broker,
    }


def _invocation(
    certification: CertificationConfig,
    *,
    payload: Mapping[str, Any],
    identity: str | None,
) -> InvocationRequest:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "axonllm-external-oidc-certification/1",
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": (
            f"axonllm-external-oidc-{secrets.token_hex(16)}"
        ),
    }
    if identity is not None:
        headers["Authorization"] = f"Bearer {identity}"
    return InvocationRequest(
        url=invocation_url(certification),
        payload=json.dumps(
            dict(payload),
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8"),
        headers=headers,
    )


def _send(
    certification: CertificationConfig,
    *,
    payload: Mapping[str, Any],
    identity: str | None,
    transport: Callable[
        [InvocationRequest, float, int],
        InvocationObservation,
    ],
) -> InvocationObservation:
    try:
        observation = transport(
            _invocation(
                certification,
                payload=payload,
                identity=identity,
            ),
            certification.timeout_seconds,
            certification.max_response_bytes,
        )
    except Exception:
        return InvocationObservation(
            status_code=None,
            latency_ms=0,
            content_type="",
            body=b"",
            error_type="transport_error",
        )
    if (
        not isinstance(observation, InvocationObservation)
        or (
            observation.status_code is not None
            and (
                isinstance(observation.status_code, bool)
                or not isinstance(observation.status_code, int)
                or not 100 <= observation.status_code <= 599
            )
        )
        or not isinstance(observation.body, bytes)
        or len(observation.body) > certification.max_response_bytes
        or isinstance(observation.latency_ms, bool)
        or not isinstance(observation.latency_ms, (int, float))
        or not math.isfinite(observation.latency_ms)
        or observation.latency_ms < 0
    ):
        return InvocationObservation(
            status_code=None,
            latency_ms=0,
            content_type="",
            body=b"",
            error_type="invalid_transport_result",
        )
    return observation


def _json_body(observation: InvocationObservation) -> dict[str, Any] | None:
    try:
        value = _strict_json(observation.body, "AgentCore response")
    except ExternalOidcCertificationError:
        return None
    return value if type(value) is dict else None


def _response_error_code(
    observation: InvocationObservation,
) -> str | None:
    body = _json_body(observation)
    if body is None:
        return None
    error = body.get("error")
    if type(error) is dict and isinstance(error.get("code"), str):
        return error["code"]
    if isinstance(body.get("code"), str):
        return body["code"]
    return None


def _http_check(
    check_id: str,
    observation: InvocationObservation,
    *,
    expected_statuses: set[int],
    semantic: bool,
    validation: str,
) -> dict[str, Any]:
    passed = (
        observation.error_type is None
        and observation.status_code in expected_statuses
        and semantic
    )
    return {
        "id": check_id,
        "kind": "agentcore_http",
        "passed": passed,
        "expectedStatuses": sorted(expected_statuses),
        "statusCode": observation.status_code,
        "latencyMs": round(float(observation.latency_ms), 3),
        "contentType": observation.content_type.partition(";")[0]
        .strip()
        .casefold(),
        "responseBytes": len(observation.body),
        "responseSha256": hashlib.sha256(observation.body).hexdigest(),
        "transportError": observation.error_type,
        "observedErrorCode": _response_error_code(observation),
        "validation": validation,
    }


def _query_semantic(
    body: dict[str, Any] | None,
    *,
    request_id: str,
    datasource_id: str,
    project_id: str,
) -> bool:
    return (
        body is not None
        and body.get("request_id") == request_id
        and body.get("datasource_id") == datasource_id
        and body.get("project_id") == project_id
        and isinstance(body.get("rows"), list)
        and isinstance(body.get("statistics"), dict)
        and isinstance(body.get("query_execution_id"), str)
    )


def _tenant_config_snapshot(
    body: dict[str, Any] | None,
    *,
    tenant_id: str,
    project_id: str,
) -> tuple[int, str] | None:
    if (
        body is None
        or set(body)
        != {"tenant_id", "project_id", "revision", "config"}
        or body.get("tenant_id") != tenant_id
        or body.get("project_id") != project_id
    ):
        return None
    revision = body.get("revision")
    config = body.get("config")
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 0
        or type(config) is not dict
        or set(config) != TENANT_CONFIG_FIELDS
    ):
        return None
    name = config.get("name")
    if (
        not isinstance(name, str)
        or not name
        or name != name.strip()
        or len(name) > 256
    ):
        return None
    return revision, name


def _restore_tenant_config_name(
    certification: CertificationConfig,
    *,
    setup: AgentCoreSetupConfig,
    admin_identity: str,
    original_name: str,
    canary_name: str,
    transport: Callable[
        [InvocationRequest, float, int],
        InvocationObservation,
    ],
) -> bool:
    """Best-effort, ownership-fenced rollback after an ambiguous HTTP result."""
    for _attempt in range(3):
        read = _send(
            certification,
            payload={"action": "get_tenant_config"},
            identity=admin_identity,
            transport=transport,
        )
        snapshot = _tenant_config_snapshot(
            _json_body(read),
            tenant_id=setup.tenant.tenant_id,
            project_id=setup.tenant.project_id,
        )
        if snapshot is None:
            continue
        revision, name = snapshot
        if name == original_name:
            return True
        if name != canary_name:
            return False
        rollback = _send(
            certification,
            payload={
                "action": "update_tenant_config",
                "expected_revision": revision,
                "config": {"name": original_name},
            },
            identity=admin_identity,
            transport=transport,
        )
        restored = _tenant_config_snapshot(
            _json_body(rollback),
            tenant_id=setup.tenant.tenant_id,
            project_id=setup.tenant.project_id,
        )
        if (
            rollback.error_type is None
            and rollback.status_code == 200
            and restored is not None
            and restored[1] == original_name
        ):
            return True
    return False


def _tenant_config_checks(
    certification: CertificationConfig,
    *,
    setup: AgentCoreSetupConfig,
    verified: Mapping[str, VerifiedIdentity],
    challenge: str,
    transport: Callable[
        [InvocationRequest, float, int],
        InvocationObservation,
    ],
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    admin_read = _send(
        certification,
        payload={"action": "get_tenant_config"},
        identity=verified["admin"].token,
        transport=transport,
    )
    admin_body = _json_body(admin_read)
    original = _tenant_config_snapshot(
        admin_body,
        tenant_id=setup.tenant.tenant_id,
        project_id=setup.tenant.project_id,
    )
    admin_check = _http_check(
        "admin_tenant_config_read",
        admin_read,
        expected_statuses={200},
        semantic=original is not None,
        validation="canonical_admin_config_read_over_agentcore_https",
    )
    checks.append(admin_check)
    if admin_check["passed"] is not True or original is None:
        raise ExternalOidcCertificationError(
            "admin tenant configuration read failed"
        )
    original_revision, original_name = original

    viewer_read = _send(
        certification,
        payload={"action": "get_tenant_config"},
        identity=verified["viewer"].token,
        transport=transport,
    )
    viewer_body = _json_body(viewer_read)
    viewer_check = _http_check(
        "viewer_tenant_config_read",
        viewer_read,
        expected_statuses={200},
        semantic=(
            _tenant_config_snapshot(
                viewer_body,
                tenant_id=setup.tenant.tenant_id,
                project_id=setup.tenant.project_id,
            )
            == original
            and viewer_body == admin_body
        ),
        validation="canonical_viewer_config_read_matches_admin_snapshot",
    )
    checks.append(viewer_check)
    if viewer_check["passed"] is not True:
        raise ExternalOidcCertificationError(
            "viewer tenant configuration read failed"
        )

    canary_name = f"OIDC certification {challenge[:24]}"
    viewer_write = _send(
        certification,
        payload={
            "action": "update_tenant_config",
            "expected_revision": original_revision,
            "config": {"name": canary_name},
        },
        identity=verified["viewer"].token,
        transport=transport,
    )
    viewer_write_check = _http_check(
        "viewer_tenant_config_write_denied",
        viewer_write,
        expected_statuses={403},
        semantic=(
            _response_error_code(viewer_write)
            == "authorization_denied"
        ),
        validation="viewer_config_mutation_denied_by_runtime_rbac",
    )
    checks.append(viewer_write_check)
    if viewer_write_check["passed"] is not True:
        raise ExternalOidcCertificationError(
            "viewer tenant configuration mutation was not denied"
        )

    mutation = _send(
        certification,
        payload={
            "action": "update_tenant_config",
            "expected_revision": original_revision,
            "config": {"name": canary_name},
        },
        identity=verified["admin"].token,
        transport=transport,
    )
    mutated = _tenant_config_snapshot(
        _json_body(mutation),
        tenant_id=setup.tenant.tenant_id,
        project_id=setup.tenant.project_id,
    )
    mutation_check = _http_check(
        "admin_tenant_config_mutation",
        mutation,
        expected_statuses={200},
        semantic=mutated == (original_revision + 1, canary_name),
        validation="admin_config_cas_mutation_committed",
    )
    checks.append(mutation_check)
    if mutation_check["passed"] is not True or mutated is None:
        if not _restore_tenant_config_name(
            certification,
            setup=setup,
            admin_identity=verified["admin"].token,
            original_name=original_name,
            canary_name=canary_name,
            transport=transport,
        ):
            raise ExternalOidcCertificationError(
                "tenant configuration mutation failed and rollback is incomplete"
            )
        raise ExternalOidcCertificationError(
            "admin tenant configuration mutation failed"
        )
    mutated_revision = mutated[0]

    mutation_read = _send(
        certification,
        payload={"action": "get_tenant_config"},
        identity=verified["admin"].token,
        transport=transport,
    )
    mutation_confirmed = _tenant_config_snapshot(
        _json_body(mutation_read),
        tenant_id=setup.tenant.tenant_id,
        project_id=setup.tenant.project_id,
    )
    mutation_confirmed_check = _http_check(
        "admin_tenant_config_mutation_confirmed",
        mutation_read,
        expected_statuses={200},
        semantic=mutation_confirmed == mutated,
        validation="admin_config_mutation_visible_on_strong_read",
    )
    checks.append(mutation_confirmed_check)
    if mutation_confirmed_check["passed"] is not True:
        if not _restore_tenant_config_name(
            certification,
            setup=setup,
            admin_identity=verified["admin"].token,
            original_name=original_name,
            canary_name=canary_name,
            transport=transport,
        ):
            raise ExternalOidcCertificationError(
                "tenant configuration confirmation failed and rollback is incomplete"
            )
        raise ExternalOidcCertificationError(
            "tenant configuration mutation was not confirmed"
        )

    rollback = _send(
        certification,
        payload={
            "action": "update_tenant_config",
            "expected_revision": mutated_revision,
            "config": {"name": original_name},
        },
        identity=verified["admin"].token,
        transport=transport,
    )
    rolled_back = _tenant_config_snapshot(
        _json_body(rollback),
        tenant_id=setup.tenant.tenant_id,
        project_id=setup.tenant.project_id,
    )
    rollback_check = _http_check(
        "admin_tenant_config_rollback",
        rollback,
        expected_statuses={200},
        semantic=rolled_back == (mutated_revision + 1, original_name),
        validation="admin_config_cas_rollback_committed",
    )
    checks.append(rollback_check)
    if rollback_check["passed"] is not True or rolled_back is None:
        if not _restore_tenant_config_name(
            certification,
            setup=setup,
            admin_identity=verified["admin"].token,
            original_name=original_name,
            canary_name=canary_name,
            transport=transport,
        ):
            raise ExternalOidcCertificationError(
                "tenant configuration rollback is incomplete"
            )
        raise ExternalOidcCertificationError(
            "tenant configuration rollback evidence is invalid"
        )

    rollback_read = _send(
        certification,
        payload={"action": "get_tenant_config"},
        identity=verified["admin"].token,
        transport=transport,
    )
    rollback_confirmed = _tenant_config_snapshot(
        _json_body(rollback_read),
        tenant_id=setup.tenant.tenant_id,
        project_id=setup.tenant.project_id,
    )
    rollback_confirmed_check = _http_check(
        "admin_tenant_config_rollback_confirmed",
        rollback_read,
        expected_statuses={200},
        semantic=rollback_confirmed == rolled_back,
        validation="admin_config_rollback_visible_on_strong_read",
    )
    checks.append(rollback_confirmed_check)
    if rollback_confirmed_check["passed"] is not True:
        if not _restore_tenant_config_name(
            certification,
            setup=setup,
            admin_identity=verified["admin"].token,
            original_name=original_name,
            canary_name=canary_name,
            transport=transport,
        ):
            raise ExternalOidcCertificationError(
                "tenant configuration rollback confirmation is incomplete"
            )
        raise ExternalOidcCertificationError(
            "tenant configuration rollback was not confirmed"
        )
    return checks


def _tamper_signature(identity: str) -> str:
    segments = identity.split(".")
    if len(segments) != 3 or not segments[2]:
        raise ExternalOidcCertificationError(
            "cannot construct signature-tamper canary"
        )
    first = "A" if segments[2][0] != "A" else "B"
    segments[2] = f"{first}{segments[2][1:]}"
    tampered = ".".join(segments)
    if tampered == identity:
        raise ExternalOidcCertificationError(
            "signature-tamper canary did not change the identity"
        )
    return tampered


def _policy_check(
    check_id: str,
    principal: Principal,
    action: Action,
    resource: ResourceRef,
    *,
    expected_allowed: bool,
    expected_status: int,
    expected_reason: str | None = None,
) -> dict[str, Any]:
    decision = authorize(principal, action, resource)
    passed = (
        decision.allowed is expected_allowed
        and decision.status_code == expected_status
        and (
            expected_reason is None
            or decision.reason == expected_reason
        )
    )
    return {
        "id": check_id,
        "kind": "canonical_policy",
        "passed": passed,
        "role": next(iter(principal.roles)).value,
        "action": action.value,
        "expectedAllowed": expected_allowed,
        "allowed": decision.allowed,
        "expectedStatus": expected_status,
        "statusCode": decision.status_code,
        "reason": decision.reason,
        "validation": "server_held_role_policy_decision",
    }


def run_external_checks(
    certification: CertificationConfig,
    *,
    setup: AgentCoreSetupConfig,
    verified: Mapping[str, VerifiedIdentity],
    principals: Mapping[str, Principal],
    challenge: str,
    transport: Callable[
        [InvocationRequest, float, int],
        InvocationObservation,
    ] = urllib_transport,
) -> list[dict[str, Any]]:
    """Exercise external identity, tenant isolation, and canonical RBAC."""
    checks: list[dict[str, Any]] = []
    for case in ("admin", "viewer"):
        observation = _send(
            certification,
            payload={"action": "list_models"},
            identity=verified[case].token,
            transport=transport,
        )
        body = _json_body(observation)
        checks.append(
            _http_check(
                f"{case}_model_list",
                observation,
                expected_statuses={200},
                semantic=(
                    body is not None
                    and isinstance(body.get("models"), list)
                    and bool(body["models"])
                ),
                validation="canonical_membership_resolved_and_models_returned",
            )
        )
    checks.extend(
        _tenant_config_checks(
            certification,
            setup=setup,
            verified=verified,
            challenge=challenge,
            transport=transport,
        )
    )
    for case in ("admin", "viewer"):
        request_id = (
            f"external-oidc-{case}-{challenge[:24]}"
        )
        observation = _send(
            certification,
            payload={
                "action": "query",
                "datasource_id": certification.query.datasource_id,
                "sql": certification.query.sql,
                "max_rows": certification.query.max_rows,
                "request_id": request_id,
            },
            identity=verified[case].token,
            transport=transport,
        )
        checks.append(
            _http_check(
                f"{case}_query_select",
                observation,
                expected_statuses={200},
                semantic=_query_semantic(
                    _json_body(observation),
                    request_id=request_id,
                    datasource_id=certification.query.datasource_id,
                    project_id=setup.tenant.project_id,
                ),
                validation="signed_claims_canonical_role_and_query_backend",
            )
        )
    observation = _send(
        certification,
        payload={
            "action": "query",
            "datasource_id": certification.query.datasource_id,
            "sql": "DELETE FROM external_oidc_launch_canary",
            "max_rows": 1,
        },
        identity=verified["viewer"].token,
        transport=transport,
    )
    checks.append(
        _http_check(
            "viewer_query_mutation_denied",
            observation,
            expected_statuses={400, 403},
            semantic=True,
            validation="read_only_query_boundary_rejected_mutation",
        )
    )
    observation = _send(
        certification,
        payload={
            "action": "list_models",
            "roles": ["tenant_admin"],
        },
        identity=verified["viewer"].token,
        transport=transport,
    )
    checks.append(
        _http_check(
            "viewer_payload_role_escalation_denied",
            observation,
            expected_statuses={400},
            semantic=True,
            validation="payload_authority_fields_rejected",
        )
    )
    negative_cases = (
        (
            "wrong_audience_denied",
            "wrongAudience",
            "agentcore_authorizer_rejected_wrong_audience",
        ),
        (
            "missing_tenant_claim_denied",
            "missingTenant",
            "runtime_rejected_missing_tenant_claim",
        ),
        (
            "missing_project_claim_denied",
            "missingProject",
            "runtime_rejected_missing_project_claim",
        ),
        (
            "expired_identity_denied",
            "expired",
            "agentcore_authorizer_rejected_expired_identity",
        ),
        (
            "issuer_mixup_denied",
            "issuerMixup",
            "agentcore_authorizer_rejected_other_issuer",
        ),
    )
    for check_id, case, validation in negative_cases:
        observation = _send(
            certification,
            payload={"action": "list_models"},
            identity=verified[case].token,
            transport=transport,
        )
        checks.append(
            _http_check(
                check_id,
                observation,
                expected_statuses={401, 403},
                semantic=True,
                validation=validation,
            )
        )
    observation = _send(
        certification,
        payload={"action": "list_models"},
        identity=_tamper_signature(verified["viewer"].token),
        transport=transport,
    )
    checks.append(
        _http_check(
            "tampered_signature_denied",
            observation,
            expected_statuses={401, 403},
            semantic=True,
            validation="agentcore_authorizer_rejected_tampered_signature",
        )
    )
    resource = ResourceRef(
        resource_type="project",
        resource_id=setup.tenant.project_id,
        tenant_id=setup.tenant.tenant_id,
        project_id=setup.tenant.project_id,
    )
    checks.extend(
        [
            _policy_check(
                "canonical_admin_config_read_allowed",
                principals["admin"],
                Action.TENANT_CONFIG_READ,
                resource,
                expected_allowed=True,
                expected_status=200,
            ),
            _policy_check(
                "canonical_admin_config_write_allowed",
                principals["admin"],
                Action.TENANT_CONFIG_WRITE,
                resource,
                expected_allowed=True,
                expected_status=200,
            ),
            _policy_check(
                "canonical_viewer_config_read_allowed",
                principals["viewer"],
                Action.TENANT_CONFIG_READ,
                resource,
                expected_allowed=True,
                expected_status=200,
            ),
            _policy_check(
                "canonical_viewer_config_write_denied",
                principals["viewer"],
                Action.TENANT_CONFIG_WRITE,
                resource,
                expected_allowed=False,
                expected_status=403,
                expected_reason="role_not_allowed",
            ),
            _policy_check(
                "canonical_admin_query_select_allowed",
                principals["admin"],
                Action.QUERY_SELECT,
                resource,
                expected_allowed=True,
                expected_status=200,
            ),
            _policy_check(
                "canonical_viewer_query_select_allowed",
                principals["viewer"],
                Action.QUERY_SELECT,
                resource,
                expected_allowed=True,
                expected_status=200,
            ),
            _policy_check(
                "canonical_cross_tenant_query_concealed",
                principals["crossTenant"],
                Action.QUERY_SELECT,
                resource,
                expected_allowed=False,
                expected_status=404,
                expected_reason="resource_not_found",
            ),
        ]
    )
    return checks


def _issuer_report(material: IssuerMaterial) -> dict[str, Any]:
    key_ids = sorted(key["kid"] for key in material.jwks["keys"])
    return {
        "issuer": material.issuer,
        "discoveryUrl": material.discovery_url,
        "jwksUri": material.jwks_uri,
        "discoverySha256": material.discovery_sha256,
        "jwksSha256": material.jwks_sha256,
        "keySetSha256": hashlib.sha256(
            "\0".join(key_ids).encode("utf-8")
        ).hexdigest(),
        "keyCount": len(key_ids),
        "discoveryFreshness": material.discovery_freshness.to_report(),
        "jwksFreshness": material.jwks_freshness.to_report(),
    }


def _producer_sha256() -> str:
    try:
        return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    except OSError as exc:
        raise ExternalOidcCertificationError(
            "cannot hash the certification producer"
        ) from exc


def _assert_no_sensitive_material(value: Any, location: str = "report") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or _SENSITIVE_KEY.search(key):
                raise ExternalOidcCertificationError(
                    f"{location} contains a sensitive field name"
                )
            _assert_no_sensitive_material(item, f"{location}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_no_sensitive_material(
                item,
                f"{location}[{index}]",
            )
    elif isinstance(value, str) and _JWT_SHAPE.fullmatch(value):
        raise ExternalOidcCertificationError(
            f"{location} contains JWT-shaped material"
        )


def _endpoint_metadata(binding: RuntimeBinding, region: str) -> dict[str, str]:
    encoded_arn = quote(binding.runtime_arn, safe="")
    return {
        "runtimeArn": binding.runtime_arn,
        "endpointArn": binding.endpoint_arn,
        "endpointName": binding.endpoint_name,
        "status": binding.endpoint_status,
        "runtimeVersion": binding.runtime_version,
        "invocationUrl": (
            f"https://bedrock-agentcore.{region}.amazonaws.com/"
            f"runtimes/{encoded_arn}/invocations"
            f"?qualifier={binding.endpoint_name}"
        ),
    }


def _boto_session(region: str) -> AwsSession:
    try:
        import boto3

        return boto3.Session(region_name=region)
    except Exception as exc:
        raise ExternalOidcCertificationError(
            "cannot initialize AWS clients"
        ) from exc


def run_live_certification(
    *,
    setup_path: Path,
    certification_path: Path,
    output_path: Path,
    cleanup_state_path: Path,
    broker_url: str,
    mixup_issuer: str,
    source: SourceBinding,
    session: AwsSession | None = None,
    json_transport: JsonTransport = httpx_json_transport,
    invocation_transport: Callable[
        [InvocationRequest, float, int],
        InvocationObservation,
    ] = urllib_transport,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Run the complete live path and emit evidence only after cleanup."""
    output = output_path.expanduser().resolve()
    state_path = cleanup_state_path.expanduser().resolve()
    if output == state_path:
        raise ExternalOidcCertificationError(
            "report and cleanup state paths must differ"
        )
    if output.exists() or output.is_symlink():
        raise ExternalOidcCertificationError(
            "refusing to replace an existing certification report"
        )
    setup = load_agentcore_setup(setup_path)
    certification = load_certification_config(certification_path)
    _validate_configs(
        setup,
        certification,
        expected_image=source.agentcore_image,
    )
    oidc = setup.external_oidc
    assert oidc is not None
    expected_material = fetch_issuer_material(
        oidc.issuer,
        transport=json_transport,
        clock=clock,
    )
    if expected_material.discovery_url != oidc.discovery_url:
        raise ExternalOidcCertificationError(
            "live discovery endpoint differs from reviewed setup"
        )
    mixup_issuer = _safe_https_url(
        mixup_issuer,
        "mix-up issuer",
        issuer=True,
    )
    if (
        mixup_issuer == oidc.issuer
        or _origin(mixup_issuer) == _origin(oidc.issuer)
    ):
        raise ExternalOidcCertificationError(
            "mix-up issuer must be a distinct HTTPS origin"
        )
    mixup_material = fetch_issuer_material(
        mixup_issuer,
        transport=json_transport,
        clock=clock,
    )
    aws = session or _boto_session(setup.aws_region)
    binding = resolve_runtime_binding(
        aws,
        setup=setup,
        certification=certification,
        expected_image=source.agentcore_image,
        runtime_stack_name=source.runtime_stack_name,
    )
    challenge = secrets.token_hex(32)
    cross_tenant_id = f"oidc-cert-cross-{challenge[:24]}"
    fixture = create_broker_fixture(
        broker_url=broker_url,
        mixup_issuer=mixup_issuer,
        setup=setup,
        challenge=challenge,
        cross_tenant_id=cross_tenant_id,
        transport=json_transport,
        clock=clock,
    )
    state = _new_cleanup_state(
        region=setup.aws_region,
        table_name=binding.table_name,
        broker_url=broker_url,
        fixture=fixture,
    )
    try:
        _atomic_private_json(state_path, state, replace=False)
    except Exception as state_exc:
        try:
            delete_broker_fixture(
                broker_url=broker_url,
                fixture_id=fixture.fixture_id,
                challenge=fixture.challenge,
                transport=json_transport,
            )
        except Exception as cleanup_exc:
            raise ExternalOidcCertificationError(
                "cannot persist cleanup state and broker rollback failed"
            ) from cleanup_exc
        raise ExternalOidcCertificationError(
            "cannot persist external OIDC cleanup state"
        ) from state_exc
    cleanup_result: dict[str, Any] | None = None
    full_certification: dict[str, Any] | None = None
    external_checks: list[dict[str, Any]] = []
    try:
        verified = validate_token_bundle(
            fixture.tokens,
            expected_material=expected_material,
            mixup_material=mixup_material,
            setup=setup,
            cross_tenant_id=cross_tenant_id,
            clock=clock,
        )
        if any(
            identity.expires_at > fixture.expires_at
            for case, identity in verified.items()
            if case not in {"expired"}
        ):
            raise ExternalOidcCertificationError(
                "fixture identity outlives the broker fixture"
            )
        table = aws.resource(
            "dynamodb",
            region_name=setup.aws_region,
        ).Table(binding.table_name)
        principals = install_canonical_fixtures(
            table,
            setup=setup,
            certification=certification,
            verified=verified,
            fixture=fixture,
            state=state,
            state_path=state_path,
            clock=clock,
        )
        credential_environment = _certification_credentials(
            certification,
            verified,
        )
        full_certification = run_certification(
            certification,
            environ=credential_environment,
            transport=invocation_transport,
            endpoint_metadata=_endpoint_metadata(
                binding,
                setup.aws_region,
            ),
        )
        _verify_full_launch_certification(
            full_certification,
            binding=binding,
            region=setup.aws_region,
            expected_provider_features=(
                _certification_provider_feature_matrix(certification)
            ),
        )
        external_checks = run_external_checks(
            certification,
            setup=setup,
            verified=verified,
            principals=principals,
            challenge=challenge,
            transport=invocation_transport,
        )
        if (
            full_certification.get("overallStatus") != "PASS"
            or {check.get("id") for check in external_checks}
            != REQUIRED_CHECKS
            or not all(check.get("passed") is True for check in external_checks)
        ):
            raise ExternalOidcCertificationError(
                "external OIDC launch checks did not all pass"
            )
    finally:
        try:
            cleanup_result = cleanup_fixtures(
                state_path,
                session=aws,
                transport=json_transport,
            )
        except Exception as cleanup_exc:
            raise ExternalOidcCertificationError(
                "external OIDC fixture cleanup was incomplete"
            ) from cleanup_exc
    if (
        cleanup_result is None
        or cleanup_result.get("status") != "PASS"
        or cleanup_result.get("complete") is not True
        or full_certification is None
    ):
        raise ExternalOidcCertificationError(
            "external OIDC cleanup proof is incomplete"
        )
    report = {
        "schema": REPORT_SCHEMA,
        "generatedAt": datetime.fromtimestamp(
            clock(),
            tz=timezone.utc,
        ).isoformat(),
        "overallStatus": "PASS",
        "producer": {
            "path": PRODUCER_PATH,
            "sha256": _producer_sha256(),
            "mode": "live-probe-only",
        },
        "source": source.to_report(),
        "target": binding.to_report(setup.aws_region),
        "oidc": {
            "identityMode": EXTERNAL_OIDC,
            "clientId": oidc.client_id,
            "audience": oidc.audience,
            "tenantClaim": oidc.tenant_claim,
            "projectClaim": oidc.project_claim,
            "expected": _issuer_report(expected_material),
            "mixup": _issuer_report(mixup_material),
        },
        "fixtures": {
            "fixtureIdSha256": hashlib.sha256(
                fixture.fixture_id.encode("utf-8")
            ).hexdigest(),
            "challengeSha256": hashlib.sha256(
                challenge.encode("ascii")
            ).hexdigest(),
            "brokerResponseSha256": fixture.response_sha256,
            "expiresAt": datetime.fromtimestamp(
                fixture.expires_at,
                tz=timezone.utc,
            ).isoformat(),
            "canonicalPrincipalCount": len(CANONICAL_CASES),
            "datasourceId": certification.query.datasource_id,
            "cleanup": cleanup_result,
        },
        "fullLaunchCertification": full_certification,
        "checks": external_checks,
        "summary": {
            "checkCount": len(external_checks),
            "passed": len(external_checks),
            "failed": 0,
            "expectedIssuerVerified": True,
            "mixupIssuerVerifiedAndRejected": True,
            "freshJwksVerified": True,
            "shortLivedIdentitiesVerified": True,
            "canonicalTenantRbacVerified": True,
            "agentcoreHttpsInvoked": True,
            "queryBackendExercised": True,
            "allLaunchProvidersExercised": True,
            "agentcoreTenantConfigMutationExercised": True,
            "fixturesCleaned": True,
        },
    }
    _assert_no_sensitive_material(report)
    _atomic_private_json(output, report, replace=False)
    return report


def _required_report_shape(value: Any) -> dict[str, Any]:
    report = _strict_object(
        value,
        "external OIDC report",
        fields={
            "schema",
            "generatedAt",
            "overallStatus",
            "producer",
            "source",
            "target",
            "oidc",
            "fixtures",
            "fullLaunchCertification",
            "checks",
            "summary",
        },
    )
    if (
        report["schema"] != REPORT_SCHEMA
        or report["overallStatus"] != "PASS"
    ):
        raise ExternalOidcCertificationError(
            "external OIDC report is not a PASS report from the supported schema"
        )
    return report


def _verify_issuer_evidence(
    value: Any,
    *,
    expected_issuer: str | None,
    expected_discovery: str | None,
    location: str,
) -> dict[str, Any]:
    material = _strict_object(
        value,
        location,
        fields={
            "issuer",
            "discoveryUrl",
            "jwksUri",
            "discoverySha256",
            "jwksSha256",
            "keySetSha256",
            "keyCount",
            "discoveryFreshness",
            "jwksFreshness",
        },
    )
    issuer = _safe_https_url(
        material["issuer"],
        f"{location} issuer",
        issuer=True,
    )
    discovery = _safe_https_url(
        material["discoveryUrl"],
        f"{location} discovery URL",
    )
    jwks_uri = _safe_https_url(
        material["jwksUri"],
        f"{location} JWKS URI",
    )
    if (
        discovery != f"{issuer}/.well-known/openid-configuration"
        or _origin(jwks_uri) != _origin(issuer)
        or (expected_issuer is not None and issuer != expected_issuer)
        or (
            expected_discovery is not None
            and discovery != expected_discovery
        )
        or any(
            _SHA256_PATTERN.fullmatch(material[name]) is None
            for name in (
                "discoverySha256",
                "jwksSha256",
                "keySetSha256",
            )
            if isinstance(material[name], str)
        )
        or any(
            not isinstance(material[name], str)
            for name in (
                "discoverySha256",
                "jwksSha256",
                "keySetSha256",
            )
        )
        or isinstance(material["keyCount"], bool)
        or not isinstance(material["keyCount"], int)
        or not 1 <= material["keyCount"] <= MAX_JWKS_KEYS
    ):
        raise ExternalOidcCertificationError(
            f"{location} is incomplete or inconsistent"
        )
    for freshness_name in (
        "discoveryFreshness",
        "jwksFreshness",
    ):
        freshness = _strict_object(
            material[freshness_name],
            f"{location} {freshness_name}",
            fields={"date", "maxAgeSeconds", "currentAgeSeconds"},
        )
        try:
            observed_date = datetime.fromisoformat(freshness["date"])
        except (TypeError, ValueError) as exc:
            raise ExternalOidcCertificationError(
                f"{location} freshness date is invalid"
            ) from exc
        if (
            observed_date.tzinfo is None
            or isinstance(freshness["maxAgeSeconds"], bool)
            or not isinstance(freshness["maxAgeSeconds"], int)
            or not 1
            <= freshness["maxAgeSeconds"]
            <= MAX_JWKS_FRESHNESS_SECONDS
            or isinstance(freshness["currentAgeSeconds"], bool)
            or not isinstance(
                freshness["currentAgeSeconds"],
                (int, float),
            )
            or not math.isfinite(
                float(freshness["currentAgeSeconds"])
            )
            or not 0
            <= float(freshness["currentAgeSeconds"])
            < freshness["maxAgeSeconds"]
        ):
            raise ExternalOidcCertificationError(
                f"{location} contains stale or unverifiable freshness evidence"
            )
    return material


def _verify_external_checks(value: Any) -> None:
    if not isinstance(value, list) or len(value) != len(REQUIRED_CHECKS):
        raise ExternalOidcCertificationError(
            "report checks are incomplete"
        )
    checks = {
        check.get("id"): check
        for check in value
        if type(check) is dict
    }
    if set(checks) != REQUIRED_CHECKS or len(checks) != len(value):
        raise ExternalOidcCertificationError(
            "report checks are missing or ambiguous"
        )
    http_expectations: dict[str, tuple[set[int], str]] = {
        "admin_model_list": (
            {200},
            "canonical_membership_resolved_and_models_returned",
        ),
        "viewer_model_list": (
            {200},
            "canonical_membership_resolved_and_models_returned",
        ),
        "admin_tenant_config_read": (
            {200},
            "canonical_admin_config_read_over_agentcore_https",
        ),
        "viewer_tenant_config_read": (
            {200},
            "canonical_viewer_config_read_matches_admin_snapshot",
        ),
        "viewer_tenant_config_write_denied": (
            {403},
            "viewer_config_mutation_denied_by_runtime_rbac",
        ),
        "admin_tenant_config_mutation": (
            {200},
            "admin_config_cas_mutation_committed",
        ),
        "admin_tenant_config_mutation_confirmed": (
            {200},
            "admin_config_mutation_visible_on_strong_read",
        ),
        "admin_tenant_config_rollback": (
            {200},
            "admin_config_cas_rollback_committed",
        ),
        "admin_tenant_config_rollback_confirmed": (
            {200},
            "admin_config_rollback_visible_on_strong_read",
        ),
        "admin_query_select": (
            {200},
            "signed_claims_canonical_role_and_query_backend",
        ),
        "viewer_query_select": (
            {200},
            "signed_claims_canonical_role_and_query_backend",
        ),
        "viewer_query_mutation_denied": (
            {400, 403},
            "read_only_query_boundary_rejected_mutation",
        ),
        "viewer_payload_role_escalation_denied": (
            {400},
            "payload_authority_fields_rejected",
        ),
        "wrong_audience_denied": (
            {401, 403},
            "agentcore_authorizer_rejected_wrong_audience",
        ),
        "missing_tenant_claim_denied": (
            {401, 403},
            "runtime_rejected_missing_tenant_claim",
        ),
        "missing_project_claim_denied": (
            {401, 403},
            "runtime_rejected_missing_project_claim",
        ),
        "expired_identity_denied": (
            {401, 403},
            "agentcore_authorizer_rejected_expired_identity",
        ),
        "issuer_mixup_denied": (
            {401, 403},
            "agentcore_authorizer_rejected_other_issuer",
        ),
        "tampered_signature_denied": (
            {401, 403},
            "agentcore_authorizer_rejected_tampered_signature",
        ),
    }
    http_fields = {
        "id",
        "kind",
        "passed",
        "expectedStatuses",
        "statusCode",
        "latencyMs",
        "contentType",
        "responseBytes",
        "responseSha256",
        "transportError",
        "observedErrorCode",
        "validation",
    }
    for check_id, (statuses, validation) in http_expectations.items():
        check = _strict_object(
            checks[check_id],
            f"check {check_id}",
            fields=http_fields,
        )
        if (
            check["kind"] != "agentcore_http"
            or check["passed"] is not True
            or check["expectedStatuses"] != sorted(statuses)
            or check["statusCode"] not in statuses
            or check["transportError"] is not None
            or check["validation"] != validation
            or isinstance(check["latencyMs"], bool)
            or not isinstance(check["latencyMs"], (int, float))
            or not math.isfinite(float(check["latencyMs"]))
            or check["latencyMs"] < 0
            or isinstance(check["responseBytes"], bool)
            or not isinstance(check["responseBytes"], int)
            or check["responseBytes"] < 0
            or _SHA256_PATTERN.fullmatch(
                check["responseSha256"]
                if isinstance(check["responseSha256"], str)
                else ""
            )
            is None
            or (
                check["observedErrorCode"] is not None
                and not isinstance(check["observedErrorCode"], str)
            )
        ):
            raise ExternalOidcCertificationError(
                f"check {check_id} is not valid live HTTP evidence"
            )
    policy_expectations = {
        "canonical_admin_config_read_allowed": (
            "tenant_admin",
            "tenant.config.read",
            True,
            200,
            "role_allowed",
        ),
        "canonical_admin_config_write_allowed": (
            "tenant_admin",
            "tenant.config.write",
            True,
            200,
            "role_allowed",
        ),
        "canonical_viewer_config_read_allowed": (
            "tenant_member",
            "tenant.config.read",
            True,
            200,
            "role_allowed",
        ),
        "canonical_viewer_config_write_denied": (
            "tenant_member",
            "tenant.config.write",
            False,
            403,
            "role_not_allowed",
        ),
        "canonical_admin_query_select_allowed": (
            "tenant_admin",
            "query.select",
            True,
            200,
            "role_allowed",
        ),
        "canonical_viewer_query_select_allowed": (
            "tenant_member",
            "query.select",
            True,
            200,
            "role_allowed",
        ),
        "canonical_cross_tenant_query_concealed": (
            "tenant_member",
            "query.select",
            False,
            404,
            "resource_not_found",
        ),
    }
    policy_fields = {
        "id",
        "kind",
        "passed",
        "role",
        "action",
        "expectedAllowed",
        "allowed",
        "expectedStatus",
        "statusCode",
        "reason",
        "validation",
    }
    for check_id, expected in policy_expectations.items():
        check = _strict_object(
            checks[check_id],
            f"check {check_id}",
            fields=policy_fields,
        )
        observed = (
            check["role"],
            check["action"],
            check["allowed"],
            check["statusCode"],
            check["reason"],
        )
        if (
            check["kind"] != "canonical_policy"
            or check["passed"] is not True
            or observed != expected
            or check["expectedAllowed"] is not expected[2]
            or check["expectedStatus"] != expected[3]
            or check["validation"]
            != "server_held_role_policy_decision"
        ):
            raise ExternalOidcCertificationError(
                f"check {check_id} is not the required RBAC decision"
            )


def _verify_full_launch_certification(
    value: Any,
    *,
    binding: RuntimeBinding,
    region: str,
    expected_provider_features: Mapping[
        str,
        frozenset[str],
    ]
    | None = None,
) -> None:
    full = _strict_object(
        value,
        "embedded production-launch certification",
        fields={
            "schema",
            "generatedAt",
            "overallStatus",
            "endpoint",
            "summary",
            "checks",
        },
    )
    summary = full["summary"]
    checks = full["checks"]
    if type(summary) is not dict:
        raise ExternalOidcCertificationError(
            "embedded production-launch certification is incomplete"
        )
    raw_provider_features = summary.get("providerFeatures")
    if type(raw_provider_features) is not dict:
        raise ExternalOidcCertificationError(
            "embedded production-launch provider features are incomplete"
        )
    provider_features = _production_provider_feature_matrix(
        set(raw_provider_features),
        location="embedded production-launch certification",
    )
    serialized_provider_features = {
        provider: sorted(features)
        for provider, features in provider_features.items()
    }
    if raw_provider_features != serialized_provider_features:
        raise ExternalOidcCertificationError(
            "embedded production-launch provider features are incomplete"
        )
    if expected_provider_features is not None:
        reviewed_features = {
            provider: frozenset(features)
            for provider, features in expected_provider_features.items()
        }
        reviewed_contract = _production_provider_feature_matrix(
            set(reviewed_features),
            location="reviewed production-launch certification",
        )
        if (
            reviewed_features != reviewed_contract
            or provider_features != reviewed_features
        ):
            raise ExternalOidcCertificationError(
                "embedded production-launch providers differ from the "
                "reviewed certification"
            )
    if (
        full["schema"] != LAUNCH_CERTIFICATION_SCHEMA
        or full["overallStatus"] != "PASS"
        or full["endpoint"] != _endpoint_metadata(binding, region)
        or summary.get("profile") != PRODUCTION_LAUNCH_PROFILE
        or summary.get("providerCount")
        != len(provider_features)
        or summary.get("agentcoreHttpsInvoked") is not True
        or summary.get("queryBackendExercised") is not True
        or not isinstance(checks, list)
        or not checks
        or summary.get("checkCount") != len(checks)
        or summary.get("passed") != len(checks)
        or summary.get("failed") != 0
    ):
        raise ExternalOidcCertificationError(
            "embedded production-launch certification is incomplete"
        )
    required_categories = {
        "missing_jwt_denied",
        "invalid_jwt_denied",
        "inactive_membership_denied",
        "missing_project_grant_denied",
        "cross_tenant_denied",
        "payload_identity_rejected",
        "liveness",
        "dependency_readiness",
        "model_listing",
        "query_select",
        "query_mutation_denied",
    }
    provider_matrix: dict[str, set[str]] = {
        provider: set() for provider in provider_features
    }
    provider_checks: list[tuple[str, str]] = []
    observed_categories: set[str] = set()
    tool_categories = (
        "provider_tool_call",
        "provider_tool_required",
        "provider_tool_continuation",
        "provider_tool_none",
        "provider_tool_stream",
    )
    provider_categories = {
        "provider_completion",
        "provider_stream",
        *tool_categories,
    }
    for check in checks:
        if (
            type(check) is not dict
            or check.get("passed") is not True
            or _SHA256_PATTERN.fullmatch(
                check.get("responseSha256", "")
            )
            is None
            or check.get("transportError") is not None
        ):
            raise ExternalOidcCertificationError(
                "embedded production-launch check is not valid evidence"
            )
        category = check.get("category")
        if isinstance(category, str):
            observed_categories.add(category)
        provider = check.get("provider")
        if provider is None:
            if category in provider_categories:
                raise ExternalOidcCertificationError(
                    "embedded production-launch provider check is unbound"
                )
        else:
            if not isinstance(provider, str) or not isinstance(
                category,
                str,
            ):
                raise ExternalOidcCertificationError(
                    "embedded production-launch provider check is invalid"
                )
            provider_checks.append((provider, category))
            if provider in provider_matrix:
                feature = {
                    "provider_completion": "completion",
                    "provider_stream": "stream",
                    "provider_tool_call": "tool_calling",
                }.get(category)
                if feature is not None:
                    provider_matrix[provider].add(feature)
    required_provider_checks = {
        (provider, category)
        for provider, features in provider_features.items()
        for category in (
            "provider_completion",
            "provider_stream",
            *(
                tool_categories
                if "tool_calling" in features
                else ()
            ),
        )
    }
    if (
        not required_categories.issubset(observed_categories)
        or len(provider_checks) != len(set(provider_checks))
        or set(provider_checks) != required_provider_checks
        or any(
            provider_matrix[provider] != features
            for provider, features in provider_features.items()
        )
    ):
        raise ExternalOidcCertificationError(
            "embedded production-launch feature matrix is incomplete"
        )


def validate_published_report(
    value: Any,
    *,
    repository: str,
    release_commit: str,
    agentcore_image: str,
    runtime_stack_name: str,
    region: str,
    clock: Callable[[], float] = time.time,
    maximum_age_seconds: int = MAX_PUBLISHED_REPORT_AGE_SECONDS,
) -> dict[str, Any]:
    """Validate the portable proof in an immutable published report."""

    report = _required_report_shape(value)
    _assert_no_sensitive_material(report)
    try:
        generated = datetime.fromisoformat(report["generatedAt"])
    except (TypeError, ValueError) as exc:
        raise ExternalOidcCertificationError(
            "report generation time is invalid"
        ) from exc
    age = (
        clock() - generated.timestamp()
        if generated.tzinfo is not None
        else -1
    )
    if (
        generated.tzinfo is None
        or isinstance(maximum_age_seconds, bool)
        or not isinstance(maximum_age_seconds, int)
        or maximum_age_seconds <= 0
        or age < -60
        or age > maximum_age_seconds
    ):
        raise ExternalOidcCertificationError(
            "published report is stale, future-dated, or lacks a timezone"
        )

    producer = _strict_object(
        report["producer"],
        "report producer",
        fields={"path", "sha256", "mode"},
    )
    if (
        producer["path"] != PRODUCER_PATH
        or producer["mode"] != "live-probe-only"
        or _SHA256_PATTERN.fullmatch(
            producer["sha256"]
            if isinstance(producer["sha256"], str)
            else ""
        )
        is None
    ):
        raise ExternalOidcCertificationError(
            "published report does not identify the live producer"
        )

    source_value = _strict_object(
        report["source"],
        "report source",
        fields={
            "repository",
            "workflowRef",
            "parentWorkflowRef",
            "runId",
            "runAttempt",
            "workflowCommit",
            "parentWorkflowCommit",
            "releaseCommit",
            "agentcoreImage",
            "runtimeStackName",
        },
    )
    source = _source_binding(
        repository=source_value["repository"],
        workflow_ref=source_value["workflowRef"],
        parent_workflow_ref=source_value["parentWorkflowRef"],
        run_id=source_value["runId"],
        run_attempt=source_value["runAttempt"],
        workflow_commit=source_value["workflowCommit"],
        parent_workflow_commit=source_value["parentWorkflowCommit"],
        release_commit=source_value["releaseCommit"],
        agentcore_image=source_value["agentcoreImage"],
        runtime_stack_name=source_value["runtimeStackName"],
        region=region,
    )
    if (
        source.repository != repository
        or source.release_commit != release_commit
        or source.agentcore_image != agentcore_image
        or source.runtime_stack_name != runtime_stack_name
    ):
        raise ExternalOidcCertificationError(
            "published report is not bound to the requested release"
        )

    target = _strict_object(
        report["target"],
        "report target",
        fields={
            "region",
            "stackName",
            "stackId",
            "stackStatus",
            "runtimeArn",
            "runtimeVersion",
            "endpointName",
            "endpointArn",
            "endpointStatus",
            "image",
        },
    )
    runtime_arn = _safe_string(target["runtimeArn"], "target runtime ARN")
    endpoint_name = _safe_string(
        target["endpointName"],
        "target endpoint name",
    )
    runtime_version = _safe_string(
        target["runtimeVersion"],
        "target runtime version",
    )
    binding = RuntimeBinding(
        stack_name=_safe_string(
            target["stackName"],
            "target stack name",
        ),
        stack_id=_safe_string(target["stackId"], "target stack ID"),
        stack_status=_safe_string(
            target["stackStatus"],
            "target stack status",
        ),
        runtime_arn=runtime_arn,
        runtime_version=runtime_version,
        endpoint_name=endpoint_name,
        endpoint_arn=_safe_string(
            target["endpointArn"],
            "target endpoint ARN",
        ),
        image=_safe_string(target["image"], "target image"),
        table_name="published-evidence",
        endpoint_status=_safe_string(
            target["endpointStatus"],
            "target endpoint status",
        ),
    )
    if (
        target["region"] != region
        or binding.stack_name != runtime_stack_name
        or binding.stack_status not in ALLOWED_STACK_STATUSES
        or not binding.stack_id.startswith("arn:")
        or f":stack/{binding.stack_name}/" not in binding.stack_id
        or not binding.runtime_arn.startswith(
            f"arn:aws:bedrock-agentcore:{region}:"
        )
        or not binding.runtime_version.isdigit()
        or _CANDIDATE_PATTERN.fullmatch(binding.endpoint_name) is None
        or binding.endpoint_arn
        != (
            f"{binding.runtime_arn}/runtime-endpoint/"
            f"{binding.endpoint_name}"
        )
        or binding.endpoint_status != "READY"
        or binding.image != agentcore_image
        or target != binding.to_report(region)
    ):
        raise ExternalOidcCertificationError(
            "published report target is not an immutable READY candidate"
        )

    oidc = _strict_object(
        report["oidc"],
        "report OIDC metadata",
        fields={
            "identityMode",
            "clientId",
            "audience",
            "tenantClaim",
            "projectClaim",
            "expected",
            "mixup",
        },
    )
    for name in ("clientId", "audience", "tenantClaim", "projectClaim"):
        _safe_string(oidc[name], f"report OIDC {name}")
    expected_issuer = _verify_issuer_evidence(
        oidc["expected"],
        expected_issuer=None,
        expected_discovery=None,
        location="expected issuer evidence",
    )
    mixup_issuer = _verify_issuer_evidence(
        oidc["mixup"],
        expected_issuer=None,
        expected_discovery=None,
        location="mix-up issuer evidence",
    )
    if (
        oidc["identityMode"] != EXTERNAL_OIDC
        or expected_issuer["issuer"] == mixup_issuer["issuer"]
        or _origin(expected_issuer["issuer"])
        == _origin(mixup_issuer["issuer"])
    ):
        raise ExternalOidcCertificationError(
            "published report OIDC issuer evidence is incomplete"
        )

    _verify_external_checks(report["checks"])
    _verify_full_launch_certification(
        report["fullLaunchCertification"],
        binding=binding,
        region=region,
    )

    fixtures = _strict_object(
        report["fixtures"],
        "report fixtures",
        fields={
            "fixtureIdSha256",
            "challengeSha256",
            "brokerResponseSha256",
            "expiresAt",
            "canonicalPrincipalCount",
            "datasourceId",
            "cleanup",
        },
    )
    cleanup = _strict_object(
        fixtures["cleanup"],
        "report fixture cleanup",
        fields={
            "status",
            "complete",
            "localItemsRemoved",
            "broker",
        },
    )
    broker_cleanup = _strict_object(
        cleanup["broker"],
        "report broker cleanup",
        fields={
            "status",
            "complete",
            "identitiesRevoked",
            "responseSha256",
        },
    )
    try:
        fixture_expiry = datetime.fromisoformat(fixtures["expiresAt"])
    except (TypeError, ValueError) as exc:
        raise ExternalOidcCertificationError(
            "report fixture expiry is invalid"
        ) from exc
    if (
        fixture_expiry.tzinfo is None
        or fixtures["canonicalPrincipalCount"] != len(CANONICAL_CASES)
        or _SAFE_IDENTIFIER.fullmatch(
            fixtures["datasourceId"]
            if isinstance(fixtures["datasourceId"], str)
            else ""
        )
        is None
        or any(
            _SHA256_PATTERN.fullmatch(
                fixtures[name]
                if isinstance(fixtures[name], str)
                else ""
            )
            is None
            for name in (
                "fixtureIdSha256",
                "challengeSha256",
                "brokerResponseSha256",
            )
        )
        or cleanup["status"] != "PASS"
        or cleanup["complete"] is not True
        or cleanup["localItemsRemoved"] != len(CANONICAL_CASES) + 1
        or broker_cleanup["status"] != "PASS"
        or broker_cleanup["complete"] is not True
        or broker_cleanup["identitiesRevoked"] is not True
        or _SHA256_PATTERN.fullmatch(
            broker_cleanup["responseSha256"]
            if isinstance(broker_cleanup["responseSha256"], str)
            else ""
        )
        is None
    ):
        raise ExternalOidcCertificationError(
            "published report does not prove complete fixture cleanup"
        )

    expected_summary = {
        "checkCount": len(REQUIRED_CHECKS),
        "passed": len(REQUIRED_CHECKS),
        "failed": 0,
        "expectedIssuerVerified": True,
        "mixupIssuerVerifiedAndRejected": True,
        "freshJwksVerified": True,
        "shortLivedIdentitiesVerified": True,
        "canonicalTenantRbacVerified": True,
        "agentcoreHttpsInvoked": True,
        "queryBackendExercised": True,
        "allLaunchProvidersExercised": True,
        "agentcoreTenantConfigMutationExercised": True,
        "fixturesCleaned": True,
    }
    if report["summary"] != expected_summary:
        raise ExternalOidcCertificationError(
            "published report summary is not derived from required checks"
        )
    return report


def verify_report(
    path: Path,
    *,
    setup: AgentCoreSetupConfig,
    certification: CertificationConfig,
    source: SourceBinding,
    session: AwsSession,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Revalidate a live-produced report before KMS signing."""
    report = validate_published_report(
        _read_json(path),
        repository=source.repository,
        release_commit=source.release_commit,
        agentcore_image=source.agentcore_image,
        runtime_stack_name=source.runtime_stack_name,
        region=setup.aws_region,
        clock=clock,
        maximum_age_seconds=30 * 60,
    )
    try:
        generated = datetime.fromisoformat(report["generatedAt"])
    except (TypeError, ValueError) as exc:
        raise ExternalOidcCertificationError(
            "report generation time is invalid"
        ) from exc
    if generated.tzinfo is None:
        raise ExternalOidcCertificationError(
            "report generation time lacks a timezone"
        )
    age = clock() - generated.timestamp()
    if age < -60 or age > 30 * 60:
        raise ExternalOidcCertificationError(
            "report is stale or future-dated"
        )
    producer = _strict_object(
        report["producer"],
        "report producer",
        fields={"path", "sha256", "mode"},
    )
    if producer != {
        "path": PRODUCER_PATH,
        "sha256": _producer_sha256(),
        "mode": "live-probe-only",
    }:
        raise ExternalOidcCertificationError(
            "report was not emitted by the checked-out live producer"
        )
    if report["source"] != source.to_report():
        raise ExternalOidcCertificationError(
            "report source metadata does not match this workflow run"
        )
    binding = resolve_runtime_binding(
        session,
        setup=setup,
        certification=certification,
        expected_image=source.agentcore_image,
        runtime_stack_name=source.runtime_stack_name,
    )
    if report["target"] != binding.to_report(setup.aws_region):
        raise ExternalOidcCertificationError(
            "report target no longer matches the immutable candidate"
        )
    oidc = setup.external_oidc
    if oidc is None:
        raise ExternalOidcCertificationError(
            "reviewed setup no longer contains external OIDC"
        )
    oidc_report = _strict_object(
        report["oidc"],
        "report OIDC metadata",
        fields={
            "identityMode",
            "clientId",
            "audience",
            "tenantClaim",
            "projectClaim",
            "expected",
            "mixup",
        },
    )
    if (
        oidc_report["identityMode"] != EXTERNAL_OIDC
        or oidc_report["clientId"] != oidc.client_id
        or oidc_report["audience"] != oidc.audience
        or oidc_report["tenantClaim"] != oidc.tenant_claim
        or oidc_report["projectClaim"] != oidc.project_claim
    ):
        raise ExternalOidcCertificationError(
            "report OIDC metadata differs from reviewed setup"
        )
    _verify_issuer_evidence(
        oidc_report["expected"],
        expected_issuer=oidc.issuer,
        expected_discovery=oidc.discovery_url,
        location="expected issuer evidence",
    )
    mixup = _verify_issuer_evidence(
        oidc_report["mixup"],
        expected_issuer=None,
        expected_discovery=None,
        location="mix-up issuer evidence",
    )
    if (
        mixup["issuer"] == oidc.issuer
        or _origin(mixup["issuer"]) == _origin(oidc.issuer)
    ):
        raise ExternalOidcCertificationError(
            "report issuer evidence is missing or mixed up"
        )
    _verify_external_checks(report["checks"])
    _verify_full_launch_certification(
        report["fullLaunchCertification"],
        binding=binding,
        region=setup.aws_region,
        expected_provider_features=(
            _certification_provider_feature_matrix(certification)
        ),
    )
    fixtures = _strict_object(
        report["fixtures"],
        "report fixtures",
        fields={
            "fixtureIdSha256",
            "challengeSha256",
            "brokerResponseSha256",
            "expiresAt",
            "canonicalPrincipalCount",
            "datasourceId",
            "cleanup",
        },
    )
    cleanup = _strict_object(
        fixtures["cleanup"],
        "report fixture cleanup",
        fields={
            "status",
            "complete",
            "localItemsRemoved",
            "broker",
        },
    )
    broker_cleanup = _strict_object(
        cleanup["broker"],
        "report broker cleanup",
        fields={
            "status",
            "complete",
            "identitiesRevoked",
            "responseSha256",
        },
    )
    try:
        fixture_expiry = datetime.fromisoformat(fixtures["expiresAt"])
    except (TypeError, ValueError) as exc:
        raise ExternalOidcCertificationError(
            "report fixture expiry is invalid"
        ) from exc
    if (
        fixture_expiry.tzinfo is None
        or fixtures["canonicalPrincipalCount"]
        != len(CANONICAL_CASES)
        or fixtures["datasourceId"]
        != certification.query.datasource_id
        or any(
            _SHA256_PATTERN.fullmatch(
                fixtures[name]
                if isinstance(fixtures[name], str)
                else ""
            )
            is None
            for name in (
                "fixtureIdSha256",
                "challengeSha256",
                "brokerResponseSha256",
            )
        )
        or cleanup["status"] != "PASS"
        or cleanup["complete"] is not True
        or cleanup["localItemsRemoved"] != len(CANONICAL_CASES) + 1
        or broker_cleanup["status"] != "PASS"
        or broker_cleanup["complete"] is not True
        or broker_cleanup["identitiesRevoked"] is not True
        or _SHA256_PATTERN.fullmatch(
            broker_cleanup["responseSha256"]
            if isinstance(
                broker_cleanup["responseSha256"],
                str,
            )
            else ""
        )
        is None
    ):
        raise ExternalOidcCertificationError(
            "report does not prove complete fixture cleanup"
        )
    expected_summary = {
        "checkCount": len(REQUIRED_CHECKS),
        "passed": len(REQUIRED_CHECKS),
        "failed": 0,
        "expectedIssuerVerified": True,
        "mixupIssuerVerifiedAndRejected": True,
        "freshJwksVerified": True,
        "shortLivedIdentitiesVerified": True,
        "canonicalTenantRbacVerified": True,
        "agentcoreHttpsInvoked": True,
        "queryBackendExercised": True,
        "allLaunchProvidersExercised": True,
        "agentcoreTenantConfigMutationExercised": True,
        "fixturesCleaned": True,
    }
    if report["summary"] != expected_summary:
        raise ExternalOidcCertificationError(
            "report summary is not derived from the required live checks"
        )
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Certify an immutable AgentCore candidate with external OIDC"
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser(
        "run",
        help="Create fixtures, run live probes, clean up, and emit a report",
    )
    verify = subparsers.add_parser(
        "verify-report",
        help="Revalidate a live report before signing",
    )
    for action in (run, verify):
        action.add_argument("--setup-config", required=True, type=Path)
        action.add_argument(
            "--certification-config",
            required=True,
            type=Path,
        )
        action.add_argument("--repository", required=True)
        action.add_argument("--workflow-ref", required=True)
        action.add_argument("--parent-workflow-ref", required=True)
        action.add_argument("--run-id", required=True)
        action.add_argument("--run-attempt", required=True)
        action.add_argument("--workflow-commit", required=True)
        action.add_argument("--parent-workflow-commit", required=True)
        action.add_argument("--release-commit", required=True)
        action.add_argument("--agentcore-image", required=True)
        action.add_argument("--runtime-stack-name", required=True)
    run.add_argument("--output", required=True, type=Path)
    run.add_argument("--cleanup-state", required=True, type=Path)
    run.add_argument("--broker-url", required=True)
    run.add_argument("--mixup-issuer", required=True)
    verify.add_argument("--report", required=True, type=Path)

    published = subparsers.add_parser(
        "verify-published-report",
        help="Verify portable evidence from the protected certification workflow",
    )
    published.add_argument("--repository", required=True)
    published.add_argument("--release-commit", required=True)
    published.add_argument("--agentcore-image", required=True)
    published.add_argument("--runtime-stack-name", required=True)
    published.add_argument("--region", required=True)
    published.add_argument("--report", required=True, type=Path)

    cleanup = subparsers.add_parser(
        "cleanup",
        help="Retry idempotent cleanup after an interrupted certification",
    )
    cleanup.add_argument("--state", required=True, type=Path)
    return parser


def _source_from_args(
    args: argparse.Namespace,
    *,
    region: str,
) -> SourceBinding:
    return _source_binding(
        repository=args.repository,
        workflow_ref=args.workflow_ref,
        parent_workflow_ref=args.parent_workflow_ref,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        workflow_commit=args.workflow_commit,
        parent_workflow_commit=args.parent_workflow_commit,
        release_commit=args.release_commit,
        agentcore_image=args.agentcore_image,
        runtime_stack_name=args.runtime_stack_name,
        region=region,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "cleanup":
            state = _validate_cleanup_state(_read_json(args.state))
            session = _boto_session(state["region"])
            result = cleanup_fixtures(
                args.state,
                session=session,
            )
            print(
                "External OIDC certification cleanup: "
                f"{result['status']}"
            )
            return 0

        if args.command == "verify-published-report":
            validate_published_report(
                _read_json(args.report),
                repository=args.repository,
                release_commit=args.release_commit,
                agentcore_image=args.agentcore_image,
                runtime_stack_name=args.runtime_stack_name,
                region=args.region,
            )
            print(
                "Published external OIDC AgentCore certification report: "
                "VERIFIED"
            )
            return 0

        setup = load_agentcore_setup(args.setup_config)
        certification = load_certification_config(
            args.certification_config
        )
        source = _source_from_args(
            args,
            region=setup.aws_region,
        )
        if args.command == "run":
            report = run_live_certification(
                setup_path=args.setup_config,
                certification_path=args.certification_config,
                output_path=args.output,
                cleanup_state_path=args.cleanup_state,
                broker_url=args.broker_url,
                mixup_issuer=args.mixup_issuer,
                source=source,
            )
            print(
                "External OIDC AgentCore certification: "
                f"{report['overallStatus']} "
                f"({report['summary']['passed']}/"
                f"{report['summary']['checkCount']})"
            )
            return 0
        _validate_configs(
            setup,
            certification,
            expected_image=source.agentcore_image,
        )
        verify_report(
            args.report,
            setup=setup,
            certification=certification,
            source=source,
            session=_boto_session(setup.aws_region),
        )
        print("External OIDC AgentCore certification report: VERIFIED")
        return 0
    except ExternalOidcCertificationError as exc:
        print(
            f"External OIDC AgentCore certification failed: {exc}",
            file=sys.stderr,
        )
        return 2
    except Exception:
        print(
            "External OIDC AgentCore certification failed safely",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
