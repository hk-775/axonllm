#!/usr/bin/env python3
"""Prepare and remove managed-Cognito AgentCore certification fixtures."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import stat
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from certify_agentcore import CertificationConfig, load_config
from src.gateway.agentcore_setup import (
    DEFAULT_PROJECT_CLAIM,
    DEFAULT_TENANT_CLAIM,
    MANAGED_COGNITO,
    AgentCoreSetupConfig,
    load_agentcore_setup,
)
from src.gateway.auth.dynamo_principal_repository import (
    DynamoPrincipalRepository,
)
from src.gateway.models import (
    AuthMethod,
    MembershipStatus,
    Principal,
    TenantRole,
)

IDENTITY_STACK = "AxonLLMIdentityStack"
RUNTIME_STACK = "AxonLLMAgentCoreStack"
STATE_SCHEMA = "axonllm.agentcore-certification-fixtures/v3"
_FIXTURE_ID_FIELD = "certification_fixture_id"
_MAX_JSON_BYTES = 256 * 1024
_USERNAME_PATTERN = re.compile(
    r"^axon-cert-(?:active|inactive|ungranted|cross|admin|viewer)-"
    r"[0-9a-f]{24}@example\.invalid$"
)
_SAFE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$")
_CANDIDATE_ENDPOINT_PATTERN = re.compile(r"^candidate_[0-9a-f]{32}$")
_FIXTURE_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_IDENTITY_KEY_PATTERN = re.compile(r"^IDENTITY#[0-9a-f]{64}$")
_REGION_PATTERN = re.compile(r"^[a-z]{2}(?:-gov)?-[a-z]+-[0-9]+$")
_USER_POOL_PATTERN = re.compile(r"^[a-z]{2}(?:-gov)?-[a-z]+-[0-9]+_[A-Za-z0-9]+$")
_CASES = (
    "active",
    "inactive",
    "ungranted",
    "cross",
    "admin",
    "viewer",
)


class FixtureError(RuntimeError):
    """A credential-safe fixture lifecycle failure."""


class AwsFactory(Protocol):
    def client(self, service_name: str, *, region_name: str) -> Any: ...

    def table(self, table_name: str, *, region_name: str) -> Any: ...


class _BotoFactory:
    def client(self, service_name: str, *, region_name: str) -> Any:
        import boto3

        return boto3.client(service_name, region_name=region_name)

    def table(self, table_name: str, *, region_name: str) -> Any:
        import boto3

        return boto3.resource(
            "dynamodb",
            region_name=region_name,
        ).Table(table_name)


@dataclass(frozen=True)
class _IdentityOutputs:
    user_pool_id: str
    certification_client_id: str
    issuer: str


@dataclass(frozen=True)
class _FixtureCase:
    name: str
    env_name: str
    username: str
    claim_tenant_id: str
    claim_project_id: str
    principal_tenant_id: str
    role: TenantRole
    membership_status: MembershipStatus
    project_ids: frozenset[str]


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
        raise FixtureError(f"{location} is missing or invalid")
    return value


def _aws_error_code(exc: Exception) -> str | None:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    error = response.get("Error")
    if not isinstance(error, dict):
        return None
    code = error.get("Code")
    return code if isinstance(code, str) else None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise FixtureError("JSON input contains duplicate fields")
        value[key] = item
    return value


def _reject_constant(_value: str) -> None:
    raise FixtureError("JSON input contains a non-finite number")


def _read_json(path: Path) -> Any:
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise FixtureError(f"input must be a regular file: {path}")
        if before.st_size > _MAX_JSON_BYTES:
            raise FixtureError(f"input is too large: {path}")
        raw = path.read_bytes()
        after = path.stat()
    except FixtureError:
        raise
    except OSError as exc:
        raise FixtureError(f"cannot read input: {path}") from exc
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or len(raw) != after.st_size
    ):
        raise FixtureError(f"input changed while being read: {path}")
    try:
        return json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except FixtureError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FixtureError(f"input is not strict UTF-8 JSON: {path}") from exc


def _stack_outputs(path: Path, stack_name: str) -> dict[str, str]:
    payload = _read_json(path)
    outputs = payload.get(stack_name) if type(payload) is dict else None
    if type(outputs) is not dict:
        raise FixtureError(f"CDK outputs do not contain {stack_name}")
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in outputs.items()):
        raise FixtureError(f"{stack_name} outputs must be string values")
    return outputs


def _required_output(
    outputs: dict[str, str],
    name: str,
    stack_name: str,
) -> str:
    return _safe_string(
        outputs.get(name),
        f"{stack_name}.{name}",
        maximum=2048,
    )


def _identity_outputs(
    outputs: dict[str, str],
    *,
    region: str,
) -> _IdentityOutputs:
    if (
        _required_output(outputs, "TenantClaimName", IDENTITY_STACK) != DEFAULT_TENANT_CLAIM
        or _required_output(
            outputs,
            "ProjectClaimName",
            IDENTITY_STACK,
        )
        != DEFAULT_PROJECT_CLAIM
    ):
        raise FixtureError("managed Cognito emitted unexpected claim names")
    user_pool_id = _required_output(
        outputs,
        "UserPoolId",
        IDENTITY_STACK,
    )
    issuer = _required_output(outputs, "OidcIssuer", IDENTITY_STACK)
    discovery = _required_output(
        outputs,
        "OidcDiscoveryUrl",
        IDENTITY_STACK,
    )
    if (
        discovery != f"{issuer}/.well-known/openid-configuration"
        or not issuer.startswith(f"https://cognito-idp.{region}.")
        or not issuer.endswith(f"/{user_pool_id}")
    ):
        raise FixtureError("managed Cognito issuer outputs are inconsistent")
    public_client_id = _required_output(
        outputs,
        "OidcClientId",
        IDENTITY_STACK,
    )
    if _required_output(outputs, "OidcAudience", IDENTITY_STACK) != public_client_id:
        raise FixtureError("managed Cognito public audience is inconsistent")
    certification_client_id = _required_output(
        outputs,
        "CertificationClientId",
        IDENTITY_STACK,
    )
    if certification_client_id == public_client_id:
        raise FixtureError("certification client must be distinct from the public client")
    return _IdentityOutputs(
        user_pool_id=user_pool_id,
        certification_client_id=certification_client_id,
        issuer=issuer,
    )


def _runtime_table(
    outputs: dict[str, str],
    certification: CertificationConfig,
) -> str:
    if (
        _required_output(
            outputs,
            "RecoveryCutoverMode",
            RUNTIME_STACK,
        )
        != "normal"
    ):
        raise FixtureError("AgentCore recovery must be in normal mode")
    primary = _required_output(outputs, "StateTableName", RUNTIME_STACK)
    selected = _required_output(
        outputs,
        "SelectedRuntimeStateTableName",
        RUNTIME_STACK,
    )
    if primary != selected:
        raise FixtureError("AgentCore runtime is not using its primary table")
    if (
        _CANDIDATE_ENDPOINT_PATTERN.fullmatch(certification.qualifier) is None
        or _required_output(
            outputs,
            "RuntimeArn",
            RUNTIME_STACK,
        )
        != certification.runtime_arn
        or _required_output(
            outputs,
            "CandidateRuntimeEndpointName",
            RUNTIME_STACK,
        )
        != certification.qualifier
    ):
        raise FixtureError("certification configuration does not match the candidate runtime outputs")
    if _SAFE_NAME_PATTERN.fullmatch(selected) is None:
        raise FixtureError("runtime state table name is invalid")
    return selected


def _validate_configuration(
    setup: AgentCoreSetupConfig,
    certification: CertificationConfig,
) -> None:
    if setup.identity_mode != MANAGED_COGNITO:
        raise FixtureError("fixture preparation requires managed-cognito")
    if setup.aws_region != certification.region:
        raise FixtureError("setup and certification regions do not match")
    enabled_providers = set(setup.runtime.enabled_providers)
    certified_providers = {provider.provider for provider in certification.providers}
    if certified_providers != enabled_providers:
        raise FixtureError("certification providers must exactly match the enabled runtime providers")
    if (
        certification.identities.admin_env is None
        or certification.identities.viewer_env is None
        or certification.tenant_config is None
    ):
        raise FixtureError("managed certification requires fresh admin and viewer identity configuration")
    if (
        certification.tenant_config.tenant_id != setup.tenant.tenant_id
        or certification.tenant_config.project_id != setup.tenant.project_id
    ):
        raise FixtureError("certification tenant configuration binding does not match setup")


def _private_output_path(path: Path, location: str) -> Path:
    resolved = path.expanduser().resolve()
    if resolved.exists() or resolved.is_symlink():
        raise FixtureError(f"{location} already exists")
    try:
        resolved.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise FixtureError(f"cannot create parent for {location}") from exc
    return resolved


def _absolute_path(path: str | Path) -> Path:
    return Path(
        os.path.abspath(
            os.path.expanduser(os.fspath(path)),
        )
    )


def _write_private_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = (
        json.dumps(
            value,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    except OSError as exc:
        raise FixtureError("cannot write owner-only fixture output") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _random_material(
    random_bytes: Callable[[int], bytes],
    size: int,
) -> bytes:
    value = random_bytes(size)
    if not isinstance(value, bytes) or len(value) != size:
        raise FixtureError("secure random source returned invalid data")
    return value


def _strong_password(random_bytes: Callable[[int], bytes]) -> str:
    random_part = base64.urlsafe_b64encode(_random_material(random_bytes, 30)).decode("ascii")
    return f"Aa1!{random_part}"


def _secret_hash(
    client_secret: str,
    username: str,
    client_id: str,
) -> str:
    digest = hmac.new(
        client_secret.encode("utf-8"),
        f"{username}{client_id}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def _rfc6238(
    secret: str,
    *,
    timestamp: float | None = None,
    digits: int = 6,
    period: int = 30,
) -> str:
    """Generate an RFC6238 SHA-1 TOTP without an external MFA dependency."""
    if (
        not isinstance(secret, str)
        or not secret
        or isinstance(digits, bool)
        or digits not in {6, 7, 8}
        or isinstance(period, bool)
        or not isinstance(period, int)
        or period <= 0
    ):
        raise FixtureError("TOTP parameters are invalid")
    normalized = "".join(secret.split()).upper()
    padded = normalized + ("=" * ((8 - len(normalized) % 8) % 8))
    try:
        key = base64.b32decode(padded, casefold=True)
    except (ValueError, TypeError) as exc:
        raise FixtureError("Cognito returned an invalid TOTP seed") from exc
    instant = time.time() if timestamp is None else timestamp
    if isinstance(instant, bool) or not isinstance(instant, (int, float)) or not math.isfinite(instant) or instant < 0:
        raise FixtureError("TOTP timestamp is invalid")
    counter = int(instant // period).to_bytes(8, "big")
    digest = hmac.new(key, counter, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = int.from_bytes(digest[offset : offset + 4], "big")
    code = (binary & 0x7FFFFFFF) % (10**digits)
    return f"{code:0{digits}d}"


def _required_session(response: Any, location: str) -> str:
    if type(response) is not dict:
        raise FixtureError(f"{location} returned an invalid response")
    return _safe_string(
        response.get("Session"),
        f"{location} session",
        maximum=8192,
    )


def _authenticate_new_user(
    cognito: Any,
    *,
    user_pool_id: str,
    client_id: str,
    client_secret: str,
    username: str,
    temporary_password: str,
    permanent_password: str,
    clock: Callable[[], float],
) -> str:
    secret_hash = _secret_hash(client_secret, username, client_id)
    try:
        response = cognito.admin_initiate_auth(
            UserPoolId=user_pool_id,
            ClientId=client_id,
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={
                "USERNAME": username,
                "PASSWORD": temporary_password,
                "SECRET_HASH": secret_hash,
            },
        )
    except Exception as exc:
        raise FixtureError("Cognito password authentication failed") from exc
    if response.get("ChallengeName") != "NEW_PASSWORD_REQUIRED":
        raise FixtureError("new Cognito user did not require a permanent password")
    try:
        response = cognito.admin_respond_to_auth_challenge(
            UserPoolId=user_pool_id,
            ClientId=client_id,
            ChallengeName="NEW_PASSWORD_REQUIRED",
            Session=_required_session(response, "password challenge"),
            ChallengeResponses={
                "USERNAME": username,
                "NEW_PASSWORD": permanent_password,
                "SECRET_HASH": secret_hash,
            },
        )
    except FixtureError:
        raise
    except Exception as exc:
        raise FixtureError("Cognito password challenge failed") from exc
    if response.get("ChallengeName") != "MFA_SETUP":
        raise FixtureError("new Cognito user did not require TOTP enrollment")

    try:
        association = cognito.associate_software_token(
            Session=_required_session(response, "MFA setup challenge"),
        )
        seed = _safe_string(
            association.get("SecretCode") if type(association) is dict else None,
            "Cognito TOTP seed",
            maximum=512,
        )
        verification = cognito.verify_software_token(
            Session=_required_session(
                association,
                "TOTP association",
            ),
            UserCode=_rfc6238(seed, timestamp=clock()),
            FriendlyDeviceName="AxonLLM launch certification",
        )
    except FixtureError:
        raise
    except Exception as exc:
        raise FixtureError("Cognito TOTP enrollment failed") from exc
    if type(verification) is not dict or verification.get("Status") != "SUCCESS":
        raise FixtureError("Cognito rejected TOTP enrollment")
    try:
        response = cognito.admin_respond_to_auth_challenge(
            UserPoolId=user_pool_id,
            ClientId=client_id,
            ChallengeName="MFA_SETUP",
            Session=_required_session(
                verification,
                "TOTP verification",
            ),
            ChallengeResponses={
                "USERNAME": username,
                "SECRET_HASH": secret_hash,
            },
        )
    except FixtureError:
        raise
    except Exception as exc:
        raise FixtureError("Cognito MFA challenge failed") from exc
    result = response.get("AuthenticationResult") if type(response) is dict else None
    token = result.get("IdToken") if type(result) is dict else None
    return _safe_string(token, "Cognito ID token", maximum=32 * 1024)


def _subject_from_create(response: Any) -> str:
    user = response.get("User") if type(response) is dict else None
    attributes = user.get("Attributes") if type(user) is dict else None
    if not isinstance(attributes, list):
        raise FixtureError("Cognito create-user response has no subject")
    subjects = [item.get("Value") for item in attributes if type(item) is dict and item.get("Name") == "sub"]
    if len(subjects) != 1:
        raise FixtureError("Cognito create-user response has no unique subject")
    return _safe_string(subjects[0], "Cognito subject", maximum=256)


def _create_user(
    cognito: Any,
    *,
    user_pool_id: str,
    case: _FixtureCase,
    temporary_password: str,
) -> str:
    try:
        response = cognito.admin_create_user(
            UserPoolId=user_pool_id,
            Username=case.username,
            TemporaryPassword=temporary_password,
            MessageAction="SUPPRESS",
            ForceAliasCreation=False,
            UserAttributes=[
                {"Name": "email", "Value": case.username},
                {"Name": "email_verified", "Value": "true"},
                {
                    "Name": DEFAULT_TENANT_CLAIM,
                    "Value": case.claim_tenant_id,
                },
                {
                    "Name": DEFAULT_PROJECT_CLAIM,
                    "Value": case.claim_project_id,
                },
            ],
        )
    except Exception as exc:
        raise FixtureError("Cognito synthetic-user creation failed") from exc
    return _subject_from_create(response)


def _client_secret(
    cognito: Any,
    identity: _IdentityOutputs,
) -> str:
    try:
        response = cognito.describe_user_pool_client(
            UserPoolId=identity.user_pool_id,
            ClientId=identity.certification_client_id,
        )
    except Exception as exc:
        raise FixtureError("cannot describe the Cognito certification client") from exc
    client = response.get("UserPoolClient") if type(response) is dict else None
    if type(client) is not dict or client.get("ClientId") != identity.certification_client_id:
        raise FixtureError("Cognito returned a mismatched client")
    return _safe_string(
        client.get("ClientSecret"),
        "Cognito certification client secret",
        maximum=4096,
    )


def _fixture_cases(
    setup: AgentCoreSetupConfig,
    certification: CertificationConfig,
    *,
    random_bytes: Callable[[int], bytes],
) -> tuple[_FixtureCase, ...]:
    nonce_values = [_random_material(random_bytes, 12).hex() for _ in _CASES]
    if len(set(nonce_values)) != len(_CASES):
        raise FixtureError("secure random source produced duplicate usernames")
    cross_nonce = nonce_values[_CASES.index("cross")]
    cross_tenant_id = (
        "cert-cross-" + hashlib.sha256((f"{setup.tenant.tenant_id}\0{cross_nonce}").encode("utf-8")).hexdigest()[:20]
    )
    env_names = (
        certification.identities.active_env,
        certification.identities.inactive_env,
        certification.identities.ungranted_env,
        certification.identities.cross_tenant_env,
        certification.identities.admin_env,
        certification.identities.viewer_env,
    )
    if any(env_name is None for env_name in env_names):
        raise FixtureError("managed certification identity environment is missing")
    cases: list[_FixtureCase] = []
    for index, (name, env_name, nonce) in enumerate(zip(_CASES, env_names, nonce_values, strict=True)):
        claim_tenant_id = cross_tenant_id if name == "cross" else setup.tenant.tenant_id
        cases.append(
            _FixtureCase(
                name=name,
                env_name=env_name,
                username=f"axon-cert-{name}-{nonce}@example.invalid",
                claim_tenant_id=claim_tenant_id,
                claim_project_id=setup.tenant.project_id,
                principal_tenant_id=(claim_tenant_id if name == "cross" else setup.tenant.tenant_id),
                role=(
                    TenantRole.TENANT_ADMIN
                    if name == "admin"
                    else (TenantRole.PLATFORM_ADMIN if name == "ungranted" else TenantRole.TENANT_MEMBER)
                ),
                membership_status=(MembershipStatus.SUSPENDED if name == "inactive" else MembershipStatus.ACTIVE),
                project_ids=(frozenset() if name == "ungranted" else frozenset({setup.tenant.project_id})),
            )
        )
    return tuple(cases)


def _principal(
    *,
    case: _FixtureCase,
    subject: str,
    issuer: str,
) -> Principal:
    return Principal(
        principal_id=f"certification:{case.name}:{subject}",
        tenant_id=case.principal_tenant_id,
        subject=subject,
        issuer=issuer,
        roles=frozenset({case.role}),
        auth_method=AuthMethod.OIDC_JWT,
        membership_status=case.membership_status,
        project_ids=case.project_ids,
        scopes=frozenset(),
        authorization_version=1,
        email=case.username,
    )


def _put_owned_item(table: Any, item: dict[str, Any], kind: str) -> None:
    try:
        table.put_item(
            Item=item,
            ConditionExpression=("attribute_not_exists(PK) AND attribute_not_exists(SK)"),
        )
    except Exception as exc:
        raise FixtureError(f"cannot create certification {kind} without overwriting state") from exc


def _state_for(
    *,
    fixture_id: str,
    region: str,
    identity: _IdentityOutputs,
    table_name: str,
    credentials_path: Path,
) -> dict[str, Any]:
    return {
        "schema": STATE_SCHEMA,
        "fixtureId": fixture_id,
        "region": region,
        "userPoolId": identity.user_pool_id,
        "tableName": table_name,
        "credentialsPath": str(credentials_path),
        "users": [],
        "principals": [],
        # Backward-compatible cleanup slot for pre-add-on fixture state.
        "datasource": None,
    }


def _cleanup_principal(
    table: Any,
    value: dict[str, Any],
    *,
    fixture_id: str,
) -> None:
    key = {"PK": value["PK"], "SK": value["SK"]}
    response = table.get_item(Key=key, ConsistentRead=True)
    item = response.get("Item") if type(response) is dict else None
    if item is None:
        return
    if item.get(_FIXTURE_ID_FIELD) != fixture_id:
        return
    if item.get("entity_type") != "tenant_principal" or item.get("principal_id") != value["principalId"]:
        raise FixtureError("refusing to delete a principal not owned by this fixture")
    table.delete_item(
        Key=key,
        ConditionExpression=(
            "#fixture_id = :fixture_id AND #entity_type = :entity_type AND principal_id = :principal_id"
        ),
        ExpressionAttributeNames={
            "#entity_type": "entity_type",
            "#fixture_id": _FIXTURE_ID_FIELD,
        },
        ExpressionAttributeValues={
            ":entity_type": "tenant_principal",
            ":fixture_id": fixture_id,
            ":principal_id": value["principalId"],
        },
    )


def _cleanup_datasource(
    table: Any,
    value: dict[str, Any],
    *,
    fixture_id: str,
) -> None:
    key = {"PK": value["PK"], "SK": value["SK"]}
    response = table.get_item(Key=key, ConsistentRead=True)
    item = response.get("Item") if type(response) is dict else None
    if item is None:
        return
    if item.get(_FIXTURE_ID_FIELD) != fixture_id:
        return
    if (
        item.get("entity_type") != "athena_datasource"
        or item.get("document") != value["document"]
        or item.get("revision") != value["revision"]
    ):
        raise FixtureError("refusing to delete a datasource not owned by this fixture")
    table.delete_item(
        Key=key,
        ConditionExpression=(
            "#fixture_id = :fixture_id "
            "AND #entity_type = :entity_type "
            "AND #document = :document "
            "AND #revision = :revision"
        ),
        ExpressionAttributeNames={
            "#document": "document",
            "#entity_type": "entity_type",
            "#fixture_id": _FIXTURE_ID_FIELD,
            "#revision": "revision",
        },
        ExpressionAttributeValues={
            ":document": value["document"],
            ":entity_type": "athena_datasource",
            ":fixture_id": fixture_id,
            ":revision": value["revision"],
        },
    )


def _cleanup_user(
    cognito: Any,
    value: dict[str, str],
    *,
    user_pool_id: str,
) -> None:
    try:
        response = cognito.admin_get_user(
            UserPoolId=user_pool_id,
            Username=value["username"],
        )
    except Exception as exc:
        if _aws_error_code(exc) == "UserNotFoundException":
            return
        raise
    attributes = response.get("UserAttributes") if type(response) is dict else None
    subjects = (
        [
            attribute.get("Value")
            for attribute in attributes
            if type(attribute) is dict and attribute.get("Name") == "sub"
        ]
        if isinstance(attributes, list)
        else []
    )
    if subjects != [value["subject"]]:
        raise FixtureError("refusing to delete a Cognito user not owned by this fixture")
    cognito.admin_delete_user(
        UserPoolId=user_pool_id,
        Username=value["username"],
    )


def _validate_state(value: Any) -> dict[str, Any]:
    if type(value) is not dict or set(value) != {
        "schema",
        "fixtureId",
        "region",
        "userPoolId",
        "tableName",
        "credentialsPath",
        "users",
        "principals",
        "datasource",
    }:
        raise FixtureError("cleanup state is malformed")
    if value.get("schema") != STATE_SCHEMA:
        raise FixtureError("cleanup state schema is unsupported")
    if not isinstance(value.get("fixtureId"), str) or _FIXTURE_ID_PATTERN.fullmatch(value["fixtureId"]) is None:
        raise FixtureError("cleanup state fixtureId is malformed")
    for name in ("region", "userPoolId", "tableName", "credentialsPath"):
        _safe_string(value.get(name), f"cleanup state {name}")
    if (
        _REGION_PATTERN.fullmatch(value["region"]) is None
        or _USER_POOL_PATTERN.fullmatch(value["userPoolId"]) is None
        or _SAFE_NAME_PATTERN.fullmatch(value["tableName"]) is None
        or not Path(value["credentialsPath"]).is_absolute()
    ):
        raise FixtureError("cleanup state resource identifiers are malformed")
    users = value.get("users")
    if not isinstance(users, list) or len(users) > len(_CASES):
        raise FixtureError("cleanup state users are malformed")
    seen_usernames: set[str] = set()
    for user in users:
        if (
            type(user) is not dict
            or set(user) != {"username", "subject"}
            or not isinstance(user.get("username"), str)
            or _USERNAME_PATTERN.fullmatch(user["username"]) is None
            or user["username"] in seen_usernames
        ):
            raise FixtureError("cleanup state users are malformed")
        _safe_string(
            user.get("subject"),
            "cleanup state Cognito subject",
            maximum=256,
        )
        seen_usernames.add(user["username"])
    principals = value.get("principals")
    if not isinstance(principals, list) or len(principals) > len(_CASES):
        raise FixtureError("cleanup state principals are malformed")
    seen_cases: set[str] = set()
    for principal in principals:
        if type(principal) is not dict or set(principal) != {
            "case",
            "PK",
            "SK",
            "principalId",
        }:
            raise FixtureError("cleanup state principal is malformed")
        case = principal.get("case")
        if (
            case not in _CASES
            or case in seen_cases
            or _IDENTITY_KEY_PATTERN.fullmatch(principal.get("PK", "")) is None
            or not isinstance(principal.get("SK"), str)
            or not principal["SK"].startswith("TENANT#")
        ):
            raise FixtureError("cleanup state principal is malformed")
        _safe_string(
            principal.get("principalId"),
            "cleanup state principalId",
            maximum=512,
        )
        seen_cases.add(case)
    datasource = value.get("datasource")
    if datasource is not None:
        if type(datasource) is not dict or set(datasource) != {
            "PK",
            "SK",
            "document",
            "revision",
        }:
            raise FixtureError("cleanup state datasource is malformed")
        if (
            not isinstance(datasource.get("PK"), str)
            or not datasource["PK"].startswith("TENANT#")
            or not isinstance(datasource.get("SK"), str)
            or not datasource["SK"].startswith("DATASOURCE#")
            or not isinstance(datasource.get("document"), str)
            or datasource.get("revision") != 1
        ):
            raise FixtureError("cleanup state datasource is malformed")
        try:
            document = json.loads(datasource["document"])
        except json.JSONDecodeError as exc:
            raise FixtureError("cleanup state datasource is malformed") from exc
        if not isinstance(document, dict):
            raise FixtureError("cleanup state datasource is malformed")
    return value


def _load_state(path: Path) -> dict[str, Any] | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise FixtureError("cannot inspect cleanup state") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise FixtureError("cleanup state must be an owner-only regular file")
    return _validate_state(_read_json(path))


def _unlink_file(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise FixtureError("cannot inspect fixture output") from exc
    if stat.S_ISDIR(metadata.st_mode):
        raise FixtureError("refusing to remove a fixture output directory")
    try:
        path.unlink()
    except OSError as exc:
        raise FixtureError("cannot remove fixture output") from exc


def _cleanup_state(
    state: dict[str, Any],
    *,
    state_path: Path,
    aws_factory: AwsFactory,
) -> None:
    failures: list[Exception] = []
    table: Any | None = None
    cognito: Any | None = None
    try:
        table = aws_factory.table(
            state["tableName"],
            region_name=state["region"],
        )
    except Exception as exc:
        failures.append(exc)
    if table is not None:
        if state["datasource"] is not None:
            try:
                _cleanup_datasource(
                    table,
                    state["datasource"],
                    fixture_id=state["fixtureId"],
                )
            except Exception as exc:
                failures.append(exc)
        for principal in reversed(state["principals"]):
            try:
                _cleanup_principal(
                    table,
                    principal,
                    fixture_id=state["fixtureId"],
                )
            except Exception as exc:
                failures.append(exc)
    try:
        cognito = aws_factory.client(
            "cognito-idp",
            region_name=state["region"],
        )
    except Exception as exc:
        failures.append(exc)
    if cognito is not None:
        for user in reversed(state["users"]):
            try:
                _cleanup_user(
                    cognito,
                    user,
                    user_pool_id=state["userPoolId"],
                )
            except Exception as exc:
                failures.append(exc)
    try:
        _unlink_file(Path(state["credentialsPath"]))
    except Exception as exc:
        failures.append(exc)
    if failures:
        raise FixtureError("fixture cleanup was incomplete; cleanup state was retained")
    _unlink_file(state_path)


def prepare_fixtures(
    *,
    setup_config: str | Path,
    certification_config: str | Path,
    identity_outputs: str | Path,
    runtime_outputs: str | Path,
    credentials_output: str | Path,
    state_output: str | Path,
    aws_factory: AwsFactory | None = None,
    random_bytes: Callable[[int], bytes] = secrets.token_bytes,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Create one complete, short-lived launch-certification fixture set."""
    try:
        setup = load_agentcore_setup(setup_config)
        certification = load_config(certification_config)
    except Exception as exc:
        raise FixtureError("setup or certification configuration is invalid") from exc
    _validate_configuration(setup, certification)
    identity = _identity_outputs(
        _stack_outputs(Path(identity_outputs), IDENTITY_STACK),
        region=setup.aws_region,
    )
    table_name = _runtime_table(
        _stack_outputs(Path(runtime_outputs), RUNTIME_STACK),
        certification,
    )
    credential_path = _private_output_path(
        Path(credentials_output),
        "credentials output",
    )
    state_path = _private_output_path(
        Path(state_output),
        "cleanup state",
    )
    if credential_path == state_path:
        raise FixtureError("credentials and cleanup state paths must differ")

    factory = aws_factory or _BotoFactory()
    try:
        cognito = factory.client(
            "cognito-idp",
            region_name=setup.aws_region,
        )
        table = factory.table(
            table_name,
            region_name=setup.aws_region,
        )
    except Exception as exc:
        raise FixtureError("cannot initialize AWS fixture clients") from exc
    secret = _client_secret(cognito, identity)
    fixture_id = _random_material(random_bytes, 32).hex()
    cases = _fixture_cases(
        setup,
        certification,
        random_bytes=random_bytes,
    )
    state = _state_for(
        fixture_id=fixture_id,
        region=setup.aws_region,
        identity=identity,
        table_name=table_name,
        credentials_path=credential_path,
    )
    _write_private_json(state_path, state)

    credentials: dict[str, str] = {}
    try:
        for case in cases:
            temporary_password = _strong_password(random_bytes)
            permanent_password = _strong_password(random_bytes)
            if temporary_password == permanent_password:
                raise FixtureError("secure random source repeated password material")
            subject = _create_user(
                cognito,
                user_pool_id=identity.user_pool_id,
                case=case,
                temporary_password=temporary_password,
            )
            state["users"].append(
                {
                    "username": case.username,
                    "subject": subject,
                }
            )
            _write_private_json(state_path, state)
            credentials[case.env_name] = _authenticate_new_user(
                cognito,
                user_pool_id=identity.user_pool_id,
                client_id=identity.certification_client_id,
                client_secret=secret,
                username=case.username,
                temporary_password=temporary_password,
                permanent_password=permanent_password,
                clock=clock,
            )
            principal_item = DynamoPrincipalRepository.serialize(
                _principal(
                    case=case,
                    subject=subject,
                    issuer=identity.issuer,
                )
            )
            principal_item[_FIXTURE_ID_FIELD] = fixture_id
            state["principals"].append(
                {
                    "case": case.name,
                    "PK": principal_item["PK"],
                    "SK": principal_item["SK"],
                    "principalId": principal_item["principal_id"],
                }
            )
            _write_private_json(state_path, state)
            _put_owned_item(table, principal_item, "principal")

        if set(credentials) != {
            certification.identities.active_env,
            certification.identities.inactive_env,
            certification.identities.ungranted_env,
            certification.identities.cross_tenant_env,
            certification.identities.admin_env,
            certification.identities.viewer_env,
        }:
            raise FixtureError("credential map does not match certification")
        _write_private_json(credential_path, credentials)
    except Exception as exc:
        try:
            _cleanup_state(
                state,
                state_path=state_path,
                aws_factory=factory,
            )
        except Exception as cleanup_exc:
            raise FixtureError("fixture preparation failed and cleanup was incomplete") from cleanup_exc
        if isinstance(exc, FixtureError):
            raise
        raise FixtureError("fixture preparation failed") from exc
    finally:
        secret = ""

    return {
        "principalCount": len(state["principals"]),
        "userCount": len(state["users"]),
    }


def cleanup_fixtures(
    state_path: str | Path,
    *,
    credentials_output: str | Path | None = None,
    aws_factory: AwsFactory | None = None,
) -> dict[str, Any]:
    """Idempotently remove only resources proven to belong to the fixture."""
    path = _absolute_path(state_path)
    try:
        state = _load_state(path)
    except Exception:
        if credentials_output is not None:
            _unlink_file(_absolute_path(credentials_output))
        raise
    if state is None:
        if credentials_output is not None:
            _unlink_file(_absolute_path(credentials_output))
        return {"removed": False}
    if credentials_output is not None and (_absolute_path(credentials_output) != Path(state["credentialsPath"])):
        raise FixtureError("credentials output does not match cleanup state")
    factory = aws_factory or _BotoFactory()
    _cleanup_state(state, state_path=path, aws_factory=factory)
    return {"removed": True}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage AgentCore launch-certification fixtures",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--setup-config", required=True)
    prepare.add_argument("--certification-config", required=True)
    prepare.add_argument("--identity-outputs", required=True)
    prepare.add_argument("--runtime-outputs", required=True)
    prepare.add_argument("--credentials-output", required=True)
    prepare.add_argument("--state-output", required=True)

    cleanup = subparsers.add_parser("cleanup")
    cleanup.add_argument(
        "--state",
        "--state-output",
        dest="state",
        required=True,
    )
    cleanup.add_argument("--credentials-output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare_fixtures(
                setup_config=args.setup_config,
                certification_config=args.certification_config,
                identity_outputs=args.identity_outputs,
                runtime_outputs=args.runtime_outputs,
                credentials_output=args.credentials_output,
                state_output=args.state_output,
            )
            print(f"AgentCore certification fixtures prepared ({result['userCount']} users).")
        else:
            cleanup_fixtures(
                args.state,
                credentials_output=args.credentials_output,
            )
            print("AgentCore certification fixtures cleaned.")
    except FixtureError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
