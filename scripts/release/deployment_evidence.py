#!/usr/bin/env python3
"""Create and verify redacted AgentCore deployment evidence."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Any, Sequence
from urllib.parse import quote, urlsplit

import launch_rehearsal_evidence


SCHEMA = "https://axonllm.dev/schemas/agentcore-deployment-evidence/v5"
COMMIT_SCHEMA = "axonllm.agentcore-deployment-evidence-commit/v1"
RELEASE_SCHEMA = "https://axonllm.dev/schemas/release-evidence/v3"
CERTIFICATION_SCHEMA = "axonllm.agentcore-certification/v1"
QUALIFICATION_TEARDOWN_SCHEMA = "axonllm.agentcore-qualification-teardown/v1"
EXTERNAL_OIDC_CERTIFICATION_SCHEMA = "https://axonllm.dev/schemas/external-oidc-agentcore-certification/v3"
PRODUCTION_VALIDATION_SCHEMA = "axonllm.production-validation/v1"
TARGET_HEALTH_SCHEMA = "axonllm.elb-target-health-observation/v1"
LAUNCH_REHEARSAL_SCHEMA = launch_rehearsal_evidence.REPORT_SCHEMA
IDENTITY_STACK = "AxonLLMIdentityStack"
AGENTCORE_STACK = "AxonLLMAgentCoreStack"
CONTROL_PLANE_STACK = "AxonLLMControlPlaneStack"
CUSTOM_DOMAIN = "custom-domain"
CLOUDFRONT = "cloudfront"
CONTROL_PLANE_ENDPOINT_CONTRACTS = {
    CUSTOM_DOMAIN: {
        "authMode": "alb-cognito",
        "credentialType": "alb-session-cookie",
    },
    CLOUDFRONT: {
        "authMode": "application-oidc",
        "credentialType": "browser-session-cookie",
    },
}
EXTERNAL_OIDC_STACK = "AxonLLMAgentCoreStack-external"
EXTERNAL_OIDC_WORKFLOW = ".github/workflows/certify-agentcore-external-oidc.yml"
LAUNCH_WORKFLOW = ".github/workflows/launch-agentcore-production.yml"
PRODUCTION_WORKFLOW = ".github/workflows/deploy-agentcore-production.yml"
MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_RESTORE_SAMPLE_ITEMS = 25

SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
RUN_ID = re.compile(r"^[1-9][0-9]*$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
ACTOR = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
CHANGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{2,127}$")
WORKFLOW_REF = re.compile(
    r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/"
    r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml@refs/heads/"
    r"[A-Za-z0-9._/-]+$"
)
IMAGE = re.compile(
    r"^(?P<account>[0-9]{12})\.dkr\.ecr\."
    r"(?P<region>[a-z0-9-]+)\.amazonaws\.com/"
    r"(?P<repository>[a-z0-9]+(?:[._/-][a-z0-9]+)*)@"
    r"(?P<digest>sha256:[0-9a-f]{64})$"
)
SAFE_OUTPUT_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,127}$")
SAFE_VALUE = re.compile(r"^[^\x00-\x1f\x7f]+$")
DNS_NAME = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}"
    r"[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
CANDIDATE_ENDPOINT = re.compile(r"^candidate_[0-9a-f]{32}$")
SECRET_NAME = re.compile(
    r"(password|secretstring|secretvalue|token|credential|api[_-]?key)",
    re.IGNORECASE,
)
PRODUCTION_LAUNCH_PROVIDERS = frozenset(
    {
        "anthropic",
        "azure_openai",
        "bedrock",
        "bedrock-mantle",
        "cohere",
        "fireworks",
        "google_ai",
        "groq",
        "openai",
        "together",
        "vertex_ai",
        "xai",
    }
)
PRODUCTION_OPTIONAL_PROVIDERS = frozenset({"ai21"})
PRODUCTION_ALLOWED_PROVIDERS = PRODUCTION_LAUNCH_PROVIDERS | PRODUCTION_OPTIONAL_PROVIDERS
PRODUCTION_PROVIDER_FEATURES = frozenset({"completion", "stream", "tool_calling"})
PRODUCTION_REQUIRED_PROVIDER_FEATURES = frozenset({"completion", "stream"})
PRODUCTION_PROVIDER_FEATURES_BY_PROVIDER = {
    provider: (PRODUCTION_REQUIRED_PROVIDER_FEATURES if provider == "fireworks" else PRODUCTION_PROVIDER_FEATURES)
    for provider in PRODUCTION_ALLOWED_PROVIDERS
}
PROVIDER_TOOL_CHECK_CATEGORIES = (
    "provider_tool_call",
    "provider_tool_required",
    "provider_tool_continuation",
    "provider_tool_none",
    "provider_tool_stream",
)
PROVIDER_CHECK_CATEGORIES = frozenset(
    {
        "provider_completion",
        "provider_stream",
        *PROVIDER_TOOL_CHECK_CATEGORIES,
    }
)
REQUIRED_CERTIFICATION_CATEGORIES = frozenset(
    {
        "cross_tenant_denied",
        "dependency_readiness",
        "inactive_membership_denied",
        "invalid_jwt_denied",
        "liveness",
        "missing_jwt_denied",
        "missing_project_grant_denied",
        "model_listing",
        "payload_identity_rejected",
        "query_mutation_denied",
        "query_select",
    }
)
REQUIRED_CONTROL_PLANE_CATEGORIES = frozenset(
    {
        "authenticated_read_allowed",
        "cross_tenant_denied",
        "tenant_admin_mutation_round_trip",
        "ungranted_project_denied",
        "viewer_mutation_denied",
    }
)
REQUIRED_REHEARSAL_GATES = frozenset(launch_rehearsal_evidence.ALL_GATES)
REQUIRED_EXTERNAL_OIDC_CHECKS = frozenset(
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
REDACTED_CERTIFICATION_CHECK_FIELDS = frozenset(
    {
        "name",
        "category",
        "passed",
        "validation",
        "statusCode",
        "latencyMs",
        "contentType",
        "responseBytes",
        "responseSha256",
        "transportError",
        "provider",
        "model",
    }
)


class DeploymentEvidenceError(RuntimeError):
    """Raised when deployment evidence is incomplete or inconsistent."""


def _production_provider_feature_matrix(
    providers: Sequence[str],
    *,
    location: str,
) -> dict[str, frozenset[str]]:
    provider_names = frozenset(providers)
    if (
        any(not isinstance(provider, str) or not provider or provider != provider.strip() for provider in providers)
        or len(provider_names) != len(providers)
        or not PRODUCTION_LAUNCH_PROVIDERS.issubset(provider_names)
        or not provider_names.issubset(PRODUCTION_ALLOWED_PROVIDERS)
    ):
        raise DeploymentEvidenceError(f"{location} does not match the production launch contract")
    return {provider: PRODUCTION_PROVIDER_FEATURES_BY_PROVIDER[provider] for provider in sorted(provider_names)}


def _expected_provider_checks(
    provider_features: dict[str, frozenset[str]],
) -> set[tuple[str, str]]:
    return {
        (provider, category)
        for provider, features in provider_features.items()
        for category in (
            "provider_completion",
            "provider_stream",
            *(PROVIDER_TOOL_CHECK_CATEGORIES if "tool_calling" in features else ()),
        )
    }


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DeploymentEvidenceError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise DeploymentEvidenceError(f"invalid JSON constant: {value}")


def _read_json(path: Path) -> Any:
    try:
        path_stat = path.lstat()
        if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
            raise DeploymentEvidenceError(f"input must be a regular non-symlink file: {path}")
        if path_stat.st_size > MAX_INPUT_BYTES:
            raise DeploymentEvidenceError(f"input is too large: {path}")
        raw = path.read_bytes()
        final_stat = path.stat()
    except OSError as exc:
        raise DeploymentEvidenceError(f"cannot read input: {path}") from exc
    if (
        path_stat.st_dev != final_stat.st_dev
        or path_stat.st_ino != final_stat.st_ino
        or path_stat.st_size != final_stat.st_size
        or len(raw) != final_stat.st_size
    ):
        raise DeploymentEvidenceError(f"input changed while reading: {path}")
    try:
        return json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeploymentEvidenceError(f"input is not strict UTF-8 JSON: {path}") from exc


def _hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise DeploymentEvidenceError(f"cannot hash input: {path}") from exc


def _object(value: Any, location: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise DeploymentEvidenceError(f"{location} must be a JSON object")
    return value


def _exact_fields(
    value: dict[str, Any],
    fields: set[str],
    location: str,
) -> None:
    if set(value) != fields:
        raise DeploymentEvidenceError(f"{location} fields do not match schema")


def _safe_string(value: Any, location: str, maximum: int = 4096) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or SAFE_VALUE.fullmatch(value) is None
    ):
        raise DeploymentEvidenceError(f"{location} is not a safe string")
    return value


def _image(value: str, *, region: str, location: str) -> tuple[str, str]:
    match = IMAGE.fullmatch(value)
    if match is None or match.group("region") != region:
        raise DeploymentEvidenceError(f"{location} must be an immutable private ECR digest URI in {region}")
    return match.group("digest"), match.group("account")


def _validate_stack_outputs(
    value: Any,
    stack_name: str,
) -> dict[str, str]:
    outputs = _object(value, f"{stack_name} outputs")
    result: dict[str, str] = {}
    for name, value in outputs.items():
        inactive_identity_output = (
            stack_name == IDENTITY_STACK
            and name in {"AlbClientId", "ControlPlaneDomainName"}
            and value == ""
        )
        if (
            SAFE_OUTPUT_NAME.fullmatch(name) is None
            or (SECRET_NAME.search(name) and not name.endswith(("Arn", "Version")))
            or not isinstance(value, str)
            or (not value and not inactive_identity_output)
            or value != value.strip()
            or len(value) > 16 * 1024
            or "\x00" in value
        ):
            raise DeploymentEvidenceError(f"{stack_name} contains an unsafe output")
        result[name] = value
    return result


def _stack_outputs(path: Path, stack_name: str) -> dict[str, str]:
    document = _object(_read_json(path), str(path))
    return _validate_stack_outputs(document.get(stack_name), stack_name)


def _required_output(
    outputs: dict[str, str],
    name: str,
    stack_name: str,
) -> str:
    value = outputs.get(name)
    if value is None:
        raise DeploymentEvidenceError(f"{stack_name} output {name} is missing")
    return value


def _control_plane_endpoint_binding(
    *,
    identity: dict[str, str],
    control: dict[str, str],
) -> dict[str, str]:
    endpoint_mode = _required_output(
        control,
        "EndpointMode",
        CONTROL_PLANE_STACK,
    )
    contract = CONTROL_PLANE_ENDPOINT_CONTRACTS.get(endpoint_mode)
    if contract is None:
        raise DeploymentEvidenceError(
            "control-plane endpoint mode is unsupported"
        )
    url = _required_output(
        control,
        "ControlPlaneUrl",
        CONTROL_PLANE_STACK,
    )
    domain = _required_output(
        control,
        "ControlPlaneDomainName",
        CONTROL_PLANE_STACK,
    )
    auth_mode = _required_output(
        control,
        "ControlPlaneAuthMode",
        CONTROL_PLANE_STACK,
    )
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise DeploymentEvidenceError(
            "control-plane endpoint outputs are inconsistent"
        ) from exc
    if (
        DNS_NAME.fullmatch(domain) is None
        or parsed.scheme != "https"
        or parsed.hostname != domain
        or parsed.netloc != domain
        or port not in {None, 443}
        or parsed.path
        or parsed.query
        or parsed.fragment
        or url != f"https://{domain}"
        or auth_mode != contract["authMode"]
    ):
        raise DeploymentEvidenceError(
            "control-plane endpoint outputs are inconsistent"
        )

    identity_mode = identity.get("EndpointMode", CUSTOM_DOMAIN)
    if identity_mode != endpoint_mode:
        raise DeploymentEvidenceError(
            "identity and control-plane endpoint modes do not match"
        )
    if endpoint_mode == CUSTOM_DOMAIN:
        if (
            _required_output(
                identity,
                "ControlPlaneDomainName",
                IDENTITY_STACK,
            )
            != domain
            or not _required_output(
                identity,
                "AlbClientId",
                IDENTITY_STACK,
            )
            or "BrowserClientId" in control
        ):
            raise DeploymentEvidenceError(
                "custom-domain endpoint is not bound to the ALB Cognito "
                "client"
            )
    elif (
        not _required_output(
            control,
            "BrowserClientId",
            CONTROL_PLANE_STACK,
        )
        or any(
            identity.get(name) not in {None, ""}
            for name in ("AlbClientId", "ControlPlaneDomainName")
        )
    ):
        raise DeploymentEvidenceError(
            "CloudFront endpoint is not bound to the application OIDC client"
        )
    return {
        "endpointMode": endpoint_mode,
        "url": url,
        "domainName": domain,
        "authMode": auth_mode,
        "credentialType": contract["credentialType"],
    }


def _runtime_provider_feature_matrix(
    runtime: dict[str, str],
) -> dict[str, frozenset[str]]:
    serialized = _required_output(
        runtime,
        "EnabledProviders",
        AGENTCORE_STACK,
    )
    provider_features = _production_provider_feature_matrix(
        serialized.split(","),
        location="AgentCore enabled-provider output",
    )
    if serialized != ",".join(provider_features):
        raise DeploymentEvidenceError("AgentCore enabled-provider output does not match the production launch contract")
    return provider_features


def _configuration_provider_feature_matrix(
    value: Any,
    *,
    location: str,
) -> dict[str, frozenset[str]]:
    if not isinstance(value, list) or not value:
        raise DeploymentEvidenceError(f"{location} does not match the production launch contract")
    provider_features: dict[str, frozenset[str]] = {}
    for index, item in enumerate(value):
        case = _object(item, f"{location}[{index}]")
        provider = case.get("provider")
        features = case.get("features")
        if (
            not isinstance(provider, str)
            or provider in provider_features
            or not isinstance(features, list)
            or any(not isinstance(feature, str) for feature in features)
            or len(features) != len(set(features))
        ):
            raise DeploymentEvidenceError(f"{location} does not match the production launch contract")
        provider_features[provider] = frozenset(features)
    expected = _production_provider_feature_matrix(
        list(provider_features),
        location=location,
    )
    if provider_features != expected:
        raise DeploymentEvidenceError(f"{location} does not match the production launch contract")
    return expected


def _release(
    path: Path,
    *,
    repository: str,
    release_commit: str,
    release_run_id: str,
    agentcore_digest: str,
    fargate_digest: str,
) -> dict[str, Any]:
    manifest = _object(_read_json(path), "release manifest")
    if manifest.get("schema") != RELEASE_SCHEMA:
        raise DeploymentEvidenceError("release manifest schema is unsupported")
    source = _object(manifest.get("source"), "release source")
    if (
        source.get("repository") != repository
        or source.get("commit") != release_commit
        or source.get("runId") != release_run_id
    ):
        raise DeploymentEvidenceError("release manifest source differs from the deployment request")
    ref = source.get("ref")
    if not isinstance(ref, str) or not ref.startswith("refs/tags/v"):
        raise DeploymentEvidenceError("release manifest is not from a v-prefixed tag")
    targets = _object(manifest.get("targets"), "release targets")
    if set(targets) != {"agentcore", "fargate"}:
        raise DeploymentEvidenceError("release target set is incomplete")
    actual = {name: _object(targets[name], f"release target {name}").get("digest") for name in targets}
    expected = {
        "agentcore": agentcore_digest,
        "fargate": fargate_digest,
    }
    if actual != expected:
        raise DeploymentEvidenceError("deployed image digests differ from release evidence")
    signing = _object(manifest.get("signing"), "release signing")
    return {
        "manifestSha256": _hash(path),
        "ref": ref,
        "runId": release_run_id,
        "signingKeyArn": _safe_string(
            signing.get("keyArn"),
            "release signing key ARN",
        ),
        "targets": {
            name: {
                "digest": expected[name],
                "platform": _safe_string(
                    _object(targets[name], f"release target {name}").get("platform"),
                    f"release target {name} platform",
                ),
            }
            for name in ("agentcore", "fargate")
        },
    }


def _validate_provider_secret(value: Any) -> dict[str, Any]:
    value = _object(value, "provider secret metadata")
    _exact_fields(
        value,
        {
            "secretArn",
            "versionId",
            "previousVersionId",
            "changed",
            "configuredFields",
            "fingerprint",
        },
        "provider secret metadata",
    )
    fields = value["configuredFields"]
    previous_version = value["previousVersionId"]
    if (
        not isinstance(fields, list)
        or not fields
        or fields != sorted(set(fields))
        or any(not isinstance(field, str) or re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", field) is None for field in fields)
        or not isinstance(value["changed"], bool)
        or (
            previous_version is not None
            and (
                not isinstance(previous_version, str)
                or not previous_version
                or previous_version != previous_version.strip()
                or len(previous_version) > 4096
                or SAFE_VALUE.fullmatch(previous_version) is None
            )
        )
        or not isinstance(value["fingerprint"], str)
        or SHA256.fullmatch(value["fingerprint"]) is None
    ):
        raise DeploymentEvidenceError("provider secret metadata is malformed")
    return {
        "secretArn": _safe_string(value["secretArn"], "provider secret ARN"),
        "versionId": _safe_string(value["versionId"], "provider secret version"),
        "previousVersionId": previous_version,
        "changed": value["changed"],
        "configuredFields": fields,
        "fingerprint": value["fingerprint"],
    }


def _provider_secret(path: Path) -> dict[str, Any]:
    return _validate_provider_secret(_read_json(path))


def _validate_recovery(
    recovery_value: Any,
    transition_value: Any,
    *,
    runtime: dict[str, str],
) -> dict[str, Any]:
    recovery = _object(recovery_value, "recovery validation")
    if "deploymentBackup" not in recovery:
        raise DeploymentEvidenceError("recovery validation is missing its deployment backup")
    _exact_fields(
        recovery,
        {
            "tableArn",
            "pointInTimeRecovery",
            "latestRestorableAgeMinutes",
            "backupVault",
            "backupVaultLocked",
            "backupVaultLockMode",
            "backupVaultMinRetentionDays",
            "backupVaultMaxRetentionDays",
            "latestBackupAgeHours",
            "deploymentBackup",
            "restoreExercise",
        },
        "recovery validation",
    )
    latest_restorable = recovery.get("latestRestorableAgeMinutes")
    latest_backup = recovery.get("latestBackupAgeHours")
    if (
        recovery.get("pointInTimeRecovery") != "ENABLED"
        or recovery.get("backupVaultLocked") is not True
        or recovery.get("backupVaultLockMode") not in {"GOVERNANCE", "COMPLIANCE"}
        or recovery.get("backupVaultMinRetentionDays") != 30
        or recovery.get("backupVaultMaxRetentionDays") != 365
        or isinstance(latest_restorable, bool)
        or not isinstance(latest_restorable, (int, float))
        or not math.isfinite(float(latest_restorable))
        or float(latest_restorable) < 0
        or isinstance(latest_backup, bool)
        or not isinstance(latest_backup, (int, float))
        or not math.isfinite(float(latest_backup))
        or float(latest_backup) < 0
    ):
        raise DeploymentEvidenceError("recovery validation does not prove required production controls")
    deployment_backup = _object(
        recovery.get("deploymentBackup"),
        "deployment backup",
    )
    _exact_fields(
        deployment_backup,
        {
            "backupJobId",
            "status",
            "backupVault",
            "resourceArn",
            "recoveryPointArn",
            "creationDate",
            "completionDate",
        },
        "deployment backup",
    )
    if (
        deployment_backup.get("status") != "COMPLETED"
        or deployment_backup.get("backupVault") != recovery.get("backupVault")
        or deployment_backup.get("resourceArn") != recovery.get("tableArn")
        or not isinstance(deployment_backup.get("backupJobId"), str)
        or not deployment_backup["backupJobId"]
        or not isinstance(
            deployment_backup.get("recoveryPointArn"),
            str,
        )
        or not deployment_backup["recoveryPointArn"].startswith("arn:")
        or not isinstance(deployment_backup.get("creationDate"), str)
        or not isinstance(deployment_backup.get("completionDate"), str)
    ):
        raise DeploymentEvidenceError("recovery validation does not prove a completed deployment backup")
    creation = _timestamp_value(
        deployment_backup["creationDate"],
        "deployment backup creation date",
    )
    completion = _timestamp_value(
        deployment_backup["completionDate"],
        "deployment backup completion date",
    )
    state_table_name = _required_output(
        runtime,
        "StateTableName",
        AGENTCORE_STACK,
    )
    if (
        creation > completion
        or recovery.get("tableArn") != deployment_backup["resourceArn"]
        or not isinstance(recovery.get("tableArn"), str)
        or not recovery["tableArn"].endswith(f":table/{state_table_name}")
    ):
        raise DeploymentEvidenceError("deployment backup is not bound to the production state table")
    restore_exercise = _object(
        recovery.get("restoreExercise"),
        "restore exercise",
    )
    _exact_fields(
        restore_exercise,
        {
            "targetTable",
            "status",
            "retained",
            "pointInTimeRecovery",
            "timeToLive",
            "deletionProtection",
            "sampledItemCount",
            "sampledItemsSha256",
        },
        "restore exercise",
    )
    sampled_item_count = restore_exercise.get("sampledItemCount")
    sampled_items_sha256 = restore_exercise.get("sampledItemsSha256")
    target_table = restore_exercise.get("targetTable")
    if (
        restore_exercise.get("status") != "validated"
        or restore_exercise.get("retained") is not False
        or restore_exercise.get("pointInTimeRecovery") is not None
        or restore_exercise.get("timeToLive") is not None
        or restore_exercise.get("deletionProtection") is not False
        or not isinstance(target_table, str)
        or not target_table.startswith(
            f"{_required_output(runtime, 'SelectedRuntimeStateTableName', AGENTCORE_STACK)}-restore-validation-"
        )
        or type(sampled_item_count) is not int
        or not 1 <= sampled_item_count <= MAX_RESTORE_SAMPLE_ITEMS
        or not isinstance(sampled_items_sha256, str)
        or SHA256.fullmatch(sampled_items_sha256) is None
    ):
        raise DeploymentEvidenceError("recovery validation does not prove a completed restore exercise")
    transition = _object(transition_value, "recovery transition")
    _exact_fields(
        transition,
        {
            "phase",
            "approvalId",
            "mode",
            "primaryTable",
            "selectedTable",
            "quiescedAt",
            "minimumQuiescenceSeconds",
            "endpoint",
            "controlPlane",
        },
        "recovery transition",
    )
    endpoint = _object(transition.get("endpoint"), "recovery transition endpoint")
    _exact_fields(
        endpoint,
        {"name", "arn", "status", "version"},
        "recovery transition endpoint",
    )
    control = _object(
        transition.get("controlPlane"),
        "recovery transition control plane",
    )
    _exact_fields(
        control,
        {
            "agentCoreStackName",
            "recoveryMode",
            "selectedTable",
            "pendingCount",
            "desiredCount",
            "runningCount",
        },
        "recovery transition control plane",
    )
    selected = _required_output(
        runtime,
        "SelectedRuntimeStateTableName",
        AGENTCORE_STACK,
    )
    if (
        transition.get("phase") != "status"
        or transition.get("mode") != "normal"
        or transition.get("approvalId") != _required_output(runtime, "RecoveryApprovalId", AGENTCORE_STACK)
        or transition.get("primaryTable") != state_table_name
        or transition.get("selectedTable") != selected
        or transition.get("quiescedAt") != "not-quiesced"
        or type(transition.get("minimumQuiescenceSeconds")) is not int
        or transition["minimumQuiescenceSeconds"] < 0
        or endpoint.get("name") != "production"
        or endpoint.get("status") != "READY"
        or endpoint.get("version") != _required_output(runtime, "RuntimeVersion", AGENTCORE_STACK)
        or endpoint.get("arn") != _required_output(runtime, "RuntimeEndpointArn", AGENTCORE_STACK)
        or control.get("agentCoreStackName") != AGENTCORE_STACK
        or control.get("recoveryMode") != "normal"
        or control.get("selectedTable") != selected
        or control.get("pendingCount") != 0
        or not isinstance(control.get("desiredCount"), int)
        or control["desiredCount"] < 1
        or control.get("runningCount") != control["desiredCount"]
    ):
        raise DeploymentEvidenceError("recovery transition is not stable in normal production mode")
    return {
        "validation": recovery,
        "transition": transition,
    }


def _recovery(
    recovery_path: Path,
    transition_path: Path,
    *,
    runtime: dict[str, str],
) -> dict[str, Any]:
    return _validate_recovery(
        _read_json(recovery_path),
        _read_json(transition_path),
        runtime=runtime,
    )


def _validate_certification_report(
    report: dict[str, Any],
    *,
    runtime: dict[str, str],
    endpoint_arn_output: str,
    endpoint_name_output: str,
    runtime_version_output: str,
    target: str,
) -> dict[str, Any]:
    endpoint = _object(
        report.get("endpoint"),
        f"{target} certification endpoint",
    )
    summary = _object(
        report.get("summary"),
        f"{target} certification summary",
    )
    checks = report.get("checks")
    runtime_arn = _required_output(runtime, "RuntimeArn", AGENTCORE_STACK)
    endpoint_name = endpoint.get("endpointName")
    endpoint_arn = endpoint.get("endpointArn")
    if (
        report.get("schema") != CERTIFICATION_SCHEMA
        or report.get("overallStatus") != "PASS"
        or not isinstance(checks, list)
        or not checks
        or any(not isinstance(check, dict) or check.get("passed") is not True for check in checks)
        or summary.get("failed") != 0
        or summary.get("checkCount") != len(checks)
        or summary.get("passed") != summary.get("checkCount")
        or summary.get("agentcoreHttpsInvoked") is not True
        or summary.get("queryBackendExercised") is not True
        or endpoint.get("runtimeArn") != runtime_arn
        or endpoint_arn
        != _required_output(
            runtime,
            endpoint_arn_output,
            AGENTCORE_STACK,
        )
        or endpoint_name
        != _required_output(
            runtime,
            endpoint_name_output,
            AGENTCORE_STACK,
        )
        or endpoint.get("runtimeVersion")
        != _required_output(
            runtime,
            runtime_version_output,
            AGENTCORE_STACK,
        )
        or endpoint.get("status") != "READY"
        or (
            target == "production"
            and (endpoint_name != "production" or endpoint_arn != f"{runtime_arn}/runtime-endpoint/production")
        )
    ):
        raise DeploymentEvidenceError(f"direct AgentCore {target} certification did not prove {target} readiness")
    enabled_features = _runtime_provider_feature_matrix(runtime)
    provider_features = summary.get("providerFeatures")
    serialized_provider_features = {provider: sorted(features) for provider, features in enabled_features.items()}
    if (
        summary.get("profile") != "production-launch"
        or not isinstance(provider_features, dict)
        or provider_features != serialized_provider_features
    ):
        raise DeploymentEvidenceError(
            f"direct AgentCore {target} certification did not use the production provider feature matrix"
        )
    categories = {check.get("category") for check in checks if isinstance(check, dict)}
    provider_checks = [
        (check.get("provider"), check.get("category"))
        for check in checks
        if (
            isinstance(check, dict)
            and check.get("provider") is not None
            and check.get("category") in PROVIDER_CHECK_CATEGORIES
        )
    ]
    expected_provider_checks = _expected_provider_checks(enabled_features)
    if (
        not REQUIRED_CERTIFICATION_CATEGORIES.issubset(categories)
        or len(provider_checks) != len(set(provider_checks))
        or set(provider_checks) != expected_provider_checks
        or summary.get("providerCount") != len(enabled_features)
    ):
        raise DeploymentEvidenceError(
            f"direct AgentCore {target} certification did not cover every launch contract and enabled provider"
        )
    return report


def _certification(
    path: Path,
    *,
    runtime: dict[str, str],
    endpoint_arn_output: str,
    endpoint_name_output: str,
    runtime_version_output: str,
    target: str,
) -> dict[str, Any]:
    report = _object(_read_json(path), f"{target} certification report")
    return _validate_certification_report(
        report,
        runtime=runtime,
        endpoint_arn_output=endpoint_arn_output,
        endpoint_name_output=endpoint_name_output,
        runtime_version_output=runtime_version_output,
        target=target,
    )


def _redacted_certification(report: dict[str, Any]) -> dict[str, Any]:
    endpoint = _object(report["endpoint"], "production certification endpoint")
    summary = _object(report["summary"], "production certification summary")
    checks = report["checks"]
    if not isinstance(checks, list):
        raise DeploymentEvidenceError("production certification checks must be a JSON array")
    return {
        "schema": report["schema"],
        "generatedAt": _safe_string(
            report.get("generatedAt"),
            "production certification generatedAt",
        ),
        "overallStatus": report["overallStatus"],
        "endpoint": {
            name: endpoint[name]
            for name in (
                "runtimeArn",
                "endpointArn",
                "endpointName",
                "status",
                "runtimeVersion",
            )
        },
        "summary": {
            name: summary[name]
            for name in (
                "checkCount",
                "passed",
                "failed",
                "providerCount",
                "profile",
                "providerFeatures",
                "queryBackendExercised",
                "agentcoreHttpsInvoked",
            )
        },
        "checks": [
            {name: value for name, value in check.items() if name in REDACTED_CERTIFICATION_CHECK_FIELDS}
            for check in checks
        ],
    }


def _timestamp(value: Any, location: str) -> str:
    text = _safe_string(value, location, maximum=64)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise DeploymentEvidenceError(f"{location} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DeploymentEvidenceError(f"{location} must include a timezone")
    return text


def _timestamp_value(value: Any, location: str) -> datetime:
    return datetime.fromisoformat(_timestamp(value, location)).astimezone(timezone.utc)


def _canonical_sha256(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _target_health_observation(
    value: Any,
    *,
    phase: str,
    expected_target_group_sha256: str,
) -> tuple[dict[str, Any], datetime]:
    observation = _object(
        value,
        f"production target health {phase}",
    )
    _exact_fields(
        observation,
        {
            "schemaVersion",
            "phase",
            "collectedAt",
            "sourceSha256",
            "healthyTargetCount",
            "targetIdSha256",
            "targetGroupArnSha256",
            "observationSha256",
        },
        f"production target health {phase}",
    )
    target_hashes = observation.get("targetIdSha256")
    healthy_count = observation.get("healthyTargetCount")
    valid_target_hashes = isinstance(target_hashes, list) and all(
        isinstance(target_hash, str) and SHA256.fullmatch(target_hash) is not None for target_hash in target_hashes
    )
    if (
        observation.get("schemaVersion") != TARGET_HEALTH_SCHEMA
        or observation.get("phase") != phase
        or SHA256.fullmatch(str(observation.get("sourceSha256"))) is None
        or observation.get("targetGroupArnSha256") != expected_target_group_sha256
        or not isinstance(healthy_count, int)
        or isinstance(healthy_count, bool)
        or healthy_count < 2
        or not valid_target_hashes
        or len(target_hashes) != healthy_count
        or target_hashes != sorted(set(target_hashes))
    ):
        raise DeploymentEvidenceError("production target-health observation is invalid")
    canonical = {
        name: observation[name]
        for name in (
            "schemaVersion",
            "phase",
            "collectedAt",
            "sourceSha256",
            "healthyTargetCount",
            "targetIdSha256",
            "targetGroupArnSha256",
        )
    }
    if observation.get("observationSha256") != _canonical_sha256(canonical):
        raise DeploymentEvidenceError("production target-health observation digest is invalid")
    collected_at = _timestamp_value(
        observation["collectedAt"],
        f"production target health {phase} collectedAt",
    )
    return observation, collected_at


def _target_health(
    value: Any,
    *,
    expected_target_group_arn: str,
    validation_started_at: datetime,
    validation_finished_at: datetime,
) -> dict[str, Any]:
    target_health = _object(value, "production target health")
    _exact_fields(
        target_health,
        {
            "status",
            "minimumHealthyTargets",
            "sameTargetSetAcrossLoad",
            "chronologyValidated",
            "backingInstanceIdentityValidated",
            "targetGroupArnSha256",
            "evidenceSha256",
            "loadInterval",
            "preLoad",
            "postLoad",
        },
        "production target health",
    )
    target_group_sha256 = hashlib.sha256(expected_target_group_arn.encode("utf-8")).hexdigest()
    pre_load, pre_collected_at = _target_health_observation(
        target_health.get("preLoad"),
        phase="pre-load",
        expected_target_group_sha256=target_group_sha256,
    )
    post_load, post_collected_at = _target_health_observation(
        target_health.get("postLoad"),
        phase="post-load",
        expected_target_group_sha256=target_group_sha256,
    )
    load_interval = _object(
        target_health.get("loadInterval"),
        "production target-health load interval",
    )
    _exact_fields(
        load_interval,
        {"startedAt", "finishedAt"},
        "production target-health load interval",
    )
    load_started_at = _timestamp_value(
        load_interval.get("startedAt"),
        "production target-health load startedAt",
    )
    load_finished_at = _timestamp_value(
        load_interval.get("finishedAt"),
        "production target-health load finishedAt",
    )
    evidence = {
        "schemaVersion": TARGET_HEALTH_SCHEMA,
        "preLoad": pre_load,
        "loadInterval": load_interval,
        "postLoad": post_load,
    }
    if (
        target_health.get("status") != "PASS"
        or target_health.get("minimumHealthyTargets") != 2
        or target_health.get("sameTargetSetAcrossLoad") is not True
        or target_health.get("chronologyValidated") is not True
        or target_health.get("backingInstanceIdentityValidated") is not True
        or target_health.get("targetGroupArnSha256") != target_group_sha256
        or pre_load["targetIdSha256"] != post_load["targetIdSha256"]
        or target_health.get("evidenceSha256") != _canonical_sha256(evidence)
        or not (
            validation_started_at
            <= pre_collected_at
            <= load_started_at
            <= load_finished_at
            <= post_collected_at
            <= validation_finished_at
        )
    ):
        raise DeploymentEvidenceError("production target health did not prove stable backing instances")
    return target_health


def _validate_production_validation(
    report: dict[str, Any],
    *,
    expected_base_url: str,
    expected_credential_type: str,
    expected_target_group_arn: str,
) -> dict[str, Any]:
    claims = _object(
        report.get("claims"),
        "production validation claims",
    )
    authorization = _object(
        report.get("authorizationContract"),
        "production authorization contract",
    )
    canaries = _object(
        report.get("canaries"),
        "production validation canaries",
    )
    load = _object(
        report.get("load"),
        "production validation load",
    )
    gates = _object(
        report.get("launchGates"),
        "production validation launch gates",
    )
    scenarios = _object(
        gates.get("scenarios"),
        "production validation scenarios",
    )
    concurrency = _object(
        gates.get("concurrencyLoad"),
        "production validation concurrency gate",
    )
    endpoints = report.get("httpEndpoints")
    results = canaries.get("results")
    configured_categories = canaries.get("configuredCategories")
    started_at = _timestamp_value(
        report.get("startedAt"),
        "production validation startedAt",
    )
    finished_at = _timestamp_value(
        report.get("finishedAt"),
        "production validation finishedAt",
    )
    target_health = _target_health(
        report.get("targetHealth"),
        expected_target_group_arn=expected_target_group_arn,
        validation_started_at=started_at,
        validation_finished_at=finished_at,
    )
    if (
        report.get("schemaVersion") != PRODUCTION_VALIDATION_SCHEMA
        or report.get("target") != "fargate"
        or report.get("overallStatus") != "PASS"
        or endpoints != [expected_base_url]
        or authorization.get("status") != "PASS"
        or authorization.get("sourcePolicyContractExercised") is not True
        or canaries.get("status") != "PASS"
        or canaries.get("allEndpointsCovered") is not True
        or canaries.get("allRequiredCanariesPassedOnAllEndpoints") is not True
        or not isinstance(configured_categories, list)
        or not REQUIRED_CONTROL_PLANE_CATEGORIES.issubset(set(configured_categories))
        or not isinstance(results, list)
        or not results
        or any(
            not isinstance(result, dict)
            or result.get("passed") is not True
            or result.get("baseUrl") != expected_base_url
            or result.get("credentialType")
            != expected_credential_type
            for result in results
        )
        or gates.get("status") != "PASS"
        or not REQUIRED_CONTROL_PLANE_CATEGORIES.issubset(set(scenarios))
        or any(
            not isinstance(scenarios.get(category), dict) or scenarios[category].get("passed") is not True
            for category in REQUIRED_CONTROL_PLANE_CATEGORIES
        )
        or load.get("status") != "PASS"
        or load.get("credentialType") != expected_credential_type
        or load.get("backingInstanceIdentityValidated") is not True
        or load.get("requestCountCompleted") != load.get("requestCountConfigured")
        or not isinstance(load.get("concurrency"), int)
        or load["concurrency"] < 2
        or concurrency.get("passed") is not True
        or concurrency.get("requestCountCompleted") != concurrency.get("requestCountConfigured")
        or not isinstance(concurrency.get("concurrency"), int)
        or concurrency["concurrency"] < 2
        or claims.get("agentcoreCutoverValidated") is not False
        or claims.get("backingInstanceIdentityValidated") is not True
    ):
        raise DeploymentEvidenceError("production validation did not prove control-plane RBAC and load")
    return {
        "schemaVersion": report["schemaVersion"],
        "target": report["target"],
        "startedAt": report["startedAt"],
        "finishedAt": report["finishedAt"],
        "overallStatus": report["overallStatus"],
        "claims": claims,
        "httpEndpoints": endpoints,
        "authorizationContract": authorization,
        "canaries": canaries,
        "load": load,
        "targetHealth": target_health,
        "launchGates": gates,
    }


def _production_validation(
    path: Path,
    *,
    expected_base_url: str,
    expected_credential_type: str,
    expected_target_group_arn: str,
) -> dict[str, Any]:
    return _validate_production_validation(
        _object(
            _read_json(path),
            "production validation report",
        ),
        expected_base_url=expected_base_url,
        expected_credential_type=expected_credential_type,
        expected_target_group_arn=expected_target_group_arn,
    )


def _validate_launch_rehearsal(
    report: dict[str, Any],
    *,
    release_commit: str,
    region: str,
    agentcore_image: str,
    fargate_image: str,
    repository: str,
    evidence_bucket: str,
    evidence_prefix: str,
) -> dict[str, Any]:
    try:
        return launch_rehearsal_evidence.validate_detailed_report(
            report,
            release_commit=release_commit,
            region=region,
            agentcore_image=agentcore_image,
            control_plane_image=fargate_image,
            repository=repository,
            evidence_bucket=evidence_bucket,
            evidence_prefix=evidence_prefix,
        )
    except launch_rehearsal_evidence.LaunchRehearsalError as exc:
        raise DeploymentEvidenceError(f"detailed launch rehearsal is invalid: {exc}") from exc


def _launch_rehearsal(
    path: Path,
    *,
    release_commit: str,
    region: str,
    agentcore_image: str,
    fargate_image: str,
    repository: str,
    evidence_bucket: str,
    evidence_prefix: str,
) -> dict[str, Any]:
    return _validate_launch_rehearsal(
        _object(
            _read_json(path),
            "launch rehearsal report",
        ),
        release_commit=release_commit,
        region=region,
        agentcore_image=agentcore_image,
        fargate_image=fargate_image,
        repository=repository,
        evidence_bucket=evidence_bucket,
        evidence_prefix=evidence_prefix,
    )


def _validate_launch_rehearsal_source(
    value: Any,
    *,
    report: dict[str, Any],
) -> dict[str, Any]:
    source = _object(value, "launch rehearsal source")
    _exact_fields(
        source,
        {
            "evidenceBucket",
            "evidencePrefix",
            "artifact",
            "signature",
        },
        "launch rehearsal source",
    )
    bucket = _safe_string(
        source.get("evidenceBucket"),
        "launch rehearsal evidence bucket",
        maximum=63,
    )
    prefix = _safe_string(
        source.get("evidencePrefix"),
        "launch rehearsal evidence prefix",
        maximum=256,
    )
    try:
        normalized_prefix = launch_rehearsal_evidence._validate_prefix(prefix)
        artifact = launch_rehearsal_evidence._validate_reference(
            source.get("artifact"),
            expected_bucket=bucket,
            expected_prefix=normalized_prefix,
            location="detailed launch rehearsal artifact",
        )
        signature = launch_rehearsal_evidence._validate_reference(
            source.get("signature"),
            expected_bucket=bucket,
            expected_prefix=normalized_prefix,
            location="detailed launch rehearsal signature",
        )
    except launch_rehearsal_evidence.LaunchRehearsalError as exc:
        raise DeploymentEvidenceError(f"launch rehearsal source is invalid: {exc}") from exc
    artifact_parent, artifact_name = artifact.key.rsplit("/", 1)
    signature_parent, signature_name = signature.key.rsplit("/", 1)
    external_identities = {
        (artifact.uri, artifact.version_id),
        (signature.uri, signature.version_id),
    }
    if (
        len(external_identities) != 2
        or artifact_parent != signature_parent
        or artifact_name != "agentcore-launch-rehearsal-evidence.json"
        or signature_name != "agentcore-launch-rehearsal-evidence-kms-signature.json"
    ):
        raise DeploymentEvidenceError(
            "launch rehearsal source does not identify the detailed report and its distinct signature"
        )
    internal_references = [
        *report["sourceManifest"].values(),
        *(reference for gate in report["gates"].values() for reference in (gate["artifact"], gate["signature"])),
    ]
    if any((reference["s3Uri"], reference["versionId"]) in external_identities for reference in internal_references):
        raise DeploymentEvidenceError("launch rehearsal report reuses its own immutable object version")
    return {
        "evidenceBucket": bucket,
        "evidencePrefix": normalized_prefix,
        "artifact": artifact.report_value(),
        "signature": signature.report_value(),
    }


def _launch_rehearsal_source(
    args: argparse.Namespace,
    *,
    report: dict[str, Any],
) -> dict[str, Any]:
    source = _validate_launch_rehearsal_source(
        {
            "evidenceBucket": args.evidence_bucket,
            "evidencePrefix": args.evidence_prefix,
            "artifact": {
                "s3Uri": args.launch_rehearsal_report_uri,
                "versionId": args.launch_rehearsal_report_version_id,
                "sha256": args.launch_rehearsal_report_sha256,
            },
            "signature": {
                "s3Uri": args.launch_rehearsal_signature_uri,
                "versionId": args.launch_rehearsal_signature_version_id,
                "sha256": args.launch_rehearsal_signature_sha256,
            },
        },
        report=report,
    )
    if source["artifact"]["sha256"] != _hash(args.launch_rehearsal_report) or source["signature"]["sha256"] != _hash(
        args.launch_rehearsal_signature
    ):
        raise DeploymentEvidenceError("launch rehearsal source hashes do not match the fetched files")
    return source


def _external_oidc_issuer(
    value: Any,
    *,
    location: str,
) -> dict[str, Any]:
    issuer = _object(value, location)
    _exact_fields(
        issuer,
        {
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
        location,
    )
    urls: dict[str, str] = {}
    origins: dict[str, tuple[str, str, int | None]] = {}
    for name in ("issuer", "discoveryUrl", "jwksUri"):
        text = _safe_string(issuer.get(name), f"{location} {name}")
        try:
            parsed = urlsplit(text)
            port = parsed.port
        except ValueError as exc:
            raise DeploymentEvidenceError(f"{location} contains an invalid HTTPS URL") from exc
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise DeploymentEvidenceError(f"{location} contains an invalid HTTPS URL")
        urls[name] = text.rstrip("/")
        origins[name] = (parsed.scheme, parsed.hostname, port)
    if (
        urls["discoveryUrl"] != f"{urls['issuer']}/.well-known/openid-configuration"
        or origins["issuer"] != origins["jwksUri"]
    ):
        raise DeploymentEvidenceError(f"{location} issuer metadata is inconsistent")
    for name in ("discoverySha256", "jwksSha256", "keySetSha256"):
        if SHA256.fullmatch(str(issuer.get(name))) is None:
            raise DeploymentEvidenceError(f"{location} contains an invalid digest")
    key_count = issuer.get("keyCount")
    if type(key_count) is not int or not 1 <= key_count <= 64:
        raise DeploymentEvidenceError(f"{location} contains an invalid key count")
    for name in ("discoveryFreshness", "jwksFreshness"):
        freshness = _object(
            issuer.get(name),
            f"{location} {name}",
        )
        _exact_fields(
            freshness,
            {"date", "maxAgeSeconds", "currentAgeSeconds"},
            f"{location} {name}",
        )
        max_age = freshness.get("maxAgeSeconds")
        current_age = freshness.get("currentAgeSeconds")
        if (
            type(max_age) is not int
            or not 1 <= max_age <= 3600
            or isinstance(current_age, bool)
            or not isinstance(current_age, (int, float))
            or not math.isfinite(float(current_age))
            or not 0 <= float(current_age) < max_age
        ):
            raise DeploymentEvidenceError(f"{location} freshness evidence is invalid")
        _timestamp(freshness.get("date"), f"{location} {name} date")
    return issuer


def _validate_external_oidc_certification(
    report: dict[str, Any],
    *,
    repository: str,
    release_commit: str,
    region: str,
    agentcore_image: str,
    run_id: str,
    run_attempt: str,
    expected_provider_features: dict[str, frozenset[str]],
) -> dict[str, Any]:
    _exact_fields(
        report,
        {
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
        "external OIDC certification report",
    )
    if report.get("schema") != EXTERNAL_OIDC_CERTIFICATION_SCHEMA or report.get("overallStatus") != "PASS":
        raise DeploymentEvidenceError("external OIDC certification did not pass")
    _timestamp(
        report.get("generatedAt"),
        "external OIDC certification generatedAt",
    )
    producer = _object(
        report.get("producer"),
        "external OIDC certification producer",
    )
    _exact_fields(
        producer,
        {"path", "sha256", "mode"},
        "external OIDC certification producer",
    )
    if (
        producer.get("path") != "scripts/operations/certify_external_oidc_agentcore.py"
        or producer.get("mode") != "live-probe-only"
        or SHA256.fullmatch(str(producer.get("sha256"))) is None
    ):
        raise DeploymentEvidenceError("external OIDC certification producer is invalid")

    source = _object(
        report.get("source"),
        "external OIDC certification source",
    )
    _exact_fields(
        source,
        {
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
        "external OIDC certification source",
    )
    if (
        source.get("repository") != repository
        or source.get("workflowRef") != f"{repository}/{EXTERNAL_OIDC_WORKFLOW}@refs/heads/main"
        or source.get("parentWorkflowRef") != f"{repository}/{LAUNCH_WORKFLOW}@refs/heads/main"
        or source.get("runId") != run_id
        or source.get("runAttempt") != run_attempt
        or source.get("releaseCommit") != release_commit
        or source.get("agentcoreImage") != agentcore_image
        or source.get("workflowCommit") != release_commit
        or source.get("parentWorkflowCommit") != release_commit
        or source.get("runtimeStackName") != EXTERNAL_OIDC_STACK
    ):
        raise DeploymentEvidenceError("external OIDC certification is not bound to this release")

    target = _object(
        report.get("target"),
        "external OIDC certification target",
    )
    _exact_fields(
        target,
        {
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
        "external OIDC certification target",
    )
    runtime_arn = _safe_string(
        target.get("runtimeArn"),
        "external OIDC runtime ARN",
    )
    endpoint_name = _safe_string(
        target.get("endpointName"),
        "external OIDC endpoint name",
    )
    runtime_version = _safe_string(
        target.get("runtimeVersion"),
        "external OIDC runtime version",
    )
    endpoint_arn = _safe_string(
        target.get("endpointArn"),
        "external OIDC endpoint ARN",
    )
    if (
        target.get("region") != region
        or target.get("stackName") != EXTERNAL_OIDC_STACK
        or target.get("image") != agentcore_image
        or target.get("stackStatus")
        not in {
            "CREATE_COMPLETE",
            "UPDATE_COMPLETE",
            "UPDATE_ROLLBACK_COMPLETE",
        }
        or f":stack/{EXTERNAL_OIDC_STACK}/" not in str(target.get("stackId", ""))
        or not runtime_arn.startswith(f"arn:aws:bedrock-agentcore:{region}:")
        or not runtime_version.isdigit()
        or CANDIDATE_ENDPOINT.fullmatch(endpoint_name) is None
        or endpoint_arn != f"{runtime_arn}/runtime-endpoint/{endpoint_name}"
        or target.get("endpointStatus") != "READY"
    ):
        raise DeploymentEvidenceError("external OIDC certification target is not an immutable READY candidate")

    oidc = _object(
        report.get("oidc"),
        "external OIDC certification metadata",
    )
    _exact_fields(
        oidc,
        {
            "identityMode",
            "clientId",
            "audience",
            "tenantClaim",
            "projectClaim",
            "expected",
            "mixup",
        },
        "external OIDC certification metadata",
    )
    for name in ("clientId", "audience", "tenantClaim", "projectClaim"):
        _safe_string(
            oidc.get(name),
            f"external OIDC certification {name}",
        )
    expected_issuer = _external_oidc_issuer(
        oidc.get("expected"),
        location="external OIDC expected issuer",
    )
    mixup_issuer = _external_oidc_issuer(
        oidc.get("mixup"),
        location="external OIDC mix-up issuer",
    )
    if (
        oidc.get("identityMode") != "external-oidc"
        or urlsplit(expected_issuer["issuer"]).netloc == urlsplit(mixup_issuer["issuer"]).netloc
    ):
        raise DeploymentEvidenceError("external OIDC issuer and mix-up evidence are incomplete")

    checks = report.get("checks")
    if not isinstance(checks, list) or len(checks) != len(REQUIRED_EXTERNAL_OIDC_CHECKS):
        raise DeploymentEvidenceError("external OIDC certification checks are incomplete")
    check_ids = {check.get("id") for check in checks if isinstance(check, dict) and check.get("passed") is True}
    if check_ids != REQUIRED_EXTERNAL_OIDC_CHECKS:
        raise DeploymentEvidenceError("external OIDC certification checks are incomplete")

    full = _object(
        report.get("fullLaunchCertification"),
        "external OIDC full launch certification",
    )
    _exact_fields(
        full,
        {
            "schema",
            "generatedAt",
            "overallStatus",
            "endpoint",
            "summary",
            "checks",
        },
        "external OIDC full launch certification",
    )
    full_endpoint = _object(
        full.get("endpoint"),
        "external OIDC full launch endpoint",
    )
    expected_invocation = (
        f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/"
        f"{quote(runtime_arn, safe='')}/invocations"
        f"?qualifier={endpoint_name}"
    )
    if (
        full.get("schema") != CERTIFICATION_SCHEMA
        or full.get("overallStatus") != "PASS"
        or full_endpoint
        != {
            "runtimeArn": runtime_arn,
            "endpointArn": endpoint_arn,
            "endpointName": endpoint_name,
            "status": "READY",
            "runtimeVersion": runtime_version,
            "invocationUrl": expected_invocation,
        }
    ):
        raise DeploymentEvidenceError("external OIDC full launch certification is invalid")
    full_summary = _object(
        full.get("summary"),
        "external OIDC full launch summary",
    )
    full_checks = full.get("checks")
    provider_features = full_summary.get("providerFeatures")
    serialized_provider_features = {
        provider: sorted(features) for provider, features in expected_provider_features.items()
    }
    if (
        full_summary.get("profile") != "production-launch"
        or full_summary.get("providerCount") != len(expected_provider_features)
        or not isinstance(provider_features, dict)
        or provider_features != serialized_provider_features
        or full_summary.get("agentcoreHttpsInvoked") is not True
        or full_summary.get("queryBackendExercised") is not True
        or not isinstance(full_checks, list)
        or not full_checks
        or full_summary.get("checkCount") != len(full_checks)
        or full_summary.get("passed") != len(full_checks)
        or full_summary.get("failed") != 0
    ):
        raise DeploymentEvidenceError("external OIDC provider feature matrix is incomplete")
    categories = {
        check.get("category") for check in full_checks if isinstance(check, dict) and check.get("passed") is True
    }
    provider_checks = [
        (check.get("provider"), check.get("category"))
        for check in full_checks
        if (
            isinstance(check, dict)
            and check.get("passed") is True
            and check.get("provider") is not None
            and check.get("category") in PROVIDER_CHECK_CATEGORIES
        )
    ]
    expected_provider_checks = _expected_provider_checks(expected_provider_features)
    if (
        not REQUIRED_CERTIFICATION_CATEGORIES.issubset(categories)
        or len(provider_checks) != len(set(provider_checks))
        or set(provider_checks) != expected_provider_checks
    ):
        raise DeploymentEvidenceError("external OIDC full launch checks are incomplete")

    fixtures = _object(
        report.get("fixtures"),
        "external OIDC certification fixtures",
    )
    _exact_fields(
        fixtures,
        {
            "fixtureIdSha256",
            "challengeSha256",
            "brokerResponseSha256",
            "expiresAt",
            "canonicalPrincipalCount",
            "datasourceId",
            "cleanup",
        },
        "external OIDC certification fixtures",
    )
    cleanup = _object(
        fixtures.get("cleanup"),
        "external OIDC fixture cleanup",
    )
    _exact_fields(
        cleanup,
        {"status", "complete", "localItemsRemoved", "broker"},
        "external OIDC fixture cleanup",
    )
    broker = _object(
        cleanup.get("broker"),
        "external OIDC broker cleanup",
    )
    _exact_fields(
        broker,
        {"status", "complete", "identitiesRevoked", "responseSha256"},
        "external OIDC broker cleanup",
    )
    if (
        any(
            SHA256.fullmatch(str(fixtures.get(name))) is None
            for name in (
                "fixtureIdSha256",
                "challengeSha256",
                "brokerResponseSha256",
            )
        )
        or fixtures.get("canonicalPrincipalCount") != 5
        or not isinstance(fixtures.get("datasourceId"), str)
        or not fixtures["datasourceId"]
        or cleanup.get("status") != "PASS"
        or cleanup.get("complete") is not True
        or cleanup.get("localItemsRemoved") != 6
        or broker.get("status") != "PASS"
        or broker.get("complete") is not True
        or broker.get("identitiesRevoked") is not True
        or SHA256.fullmatch(str(broker.get("responseSha256"))) is None
    ):
        raise DeploymentEvidenceError("external OIDC certification fixture cleanup is incomplete")
    _timestamp(
        fixtures.get("expiresAt"),
        "external OIDC fixture expiry",
    )

    expected_summary = {
        "checkCount": len(REQUIRED_EXTERNAL_OIDC_CHECKS),
        "passed": len(REQUIRED_EXTERNAL_OIDC_CHECKS),
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
    if report.get("summary") != expected_summary:
        raise DeploymentEvidenceError("external OIDC certification summary is incomplete")
    return report


def _external_oidc_certification(
    path: Path,
    *,
    repository: str,
    release_commit: str,
    region: str,
    agentcore_image: str,
    run_id: str,
    run_attempt: str,
    expected_provider_features: dict[str, frozenset[str]],
) -> dict[str, Any]:
    return _validate_external_oidc_certification(
        _object(
            _read_json(path),
            "external OIDC certification report",
        ),
        repository=repository,
        release_commit=release_commit,
        region=region,
        agentcore_image=agentcore_image,
        run_id=run_id,
        run_attempt=run_attempt,
        expected_provider_features=expected_provider_features,
    )


def _validate_external_oidc_source(value: Any) -> dict[str, Any]:
    source = _object(value, "external OIDC certification source")
    _exact_fields(
        source,
        {
            "evidenceBucket",
            "evidencePrefix",
            "artifact",
            "signature",
        },
        "external OIDC certification source",
    )
    bucket = _safe_string(
        source.get("evidenceBucket"),
        "external OIDC evidence bucket",
        maximum=63,
    )
    prefix = _safe_string(
        source.get("evidencePrefix"),
        "external OIDC evidence prefix",
        maximum=256,
    )
    try:
        normalized_prefix = launch_rehearsal_evidence._validate_prefix(prefix)
        artifact = launch_rehearsal_evidence._validate_reference(
            source.get("artifact"),
            expected_bucket=bucket,
            expected_prefix=normalized_prefix,
            location="external OIDC certification artifact",
        )
        signature = launch_rehearsal_evidence._validate_reference(
            source.get("signature"),
            expected_bucket=bucket,
            expected_prefix=normalized_prefix,
            location="external OIDC certification signature",
        )
    except launch_rehearsal_evidence.LaunchRehearsalError as exc:
        raise DeploymentEvidenceError(f"external OIDC certification source is invalid: {exc}") from exc
    artifact_parent, artifact_name = artifact.key.rsplit("/", 1)
    signature_parent, signature_name = signature.key.rsplit("/", 1)
    if (
        (artifact.uri, artifact.version_id) == (signature.uri, signature.version_id)
        or artifact_parent != signature_parent
        or artifact_name != "external-oidc-agentcore-report.json"
        or signature_name != "external-oidc-agentcore-kms-signature.json"
    ):
        raise DeploymentEvidenceError("external OIDC source does not identify a distinct report and signature")
    return {
        "evidenceBucket": bucket,
        "evidencePrefix": normalized_prefix,
        "artifact": artifact.report_value(),
        "signature": signature.report_value(),
    }


def _external_oidc_source(
    args: argparse.Namespace,
) -> dict[str, Any]:
    source = _validate_external_oidc_source(
        {
            "evidenceBucket": args.evidence_bucket,
            "evidencePrefix": args.external_oidc_evidence_prefix,
            "artifact": {
                "s3Uri": args.external_oidc_report_uri,
                "versionId": args.external_oidc_report_version_id,
                "sha256": args.external_oidc_report_sha256,
            },
            "signature": {
                "s3Uri": args.external_oidc_signature_uri,
                "versionId": args.external_oidc_signature_version_id,
                "sha256": args.external_oidc_signature_sha256,
            },
        }
    )
    if source["artifact"]["sha256"] != _hash(args.external_oidc_certification_report) or source["signature"][
        "sha256"
    ] != _hash(args.external_oidc_certification_signature):
        raise DeploymentEvidenceError("external OIDC source hashes do not match the fetched files")
    return source


def _validate_qualification_teardown(
    value: Any,
    *,
    repository: str,
    release_commit: str,
    run_id: str,
    run_attempt: str,
    account_id: str,
    region: str,
) -> dict[str, Any]:
    receipt = _object(value, "qualification teardown receipt")
    _exact_fields(
        receipt,
        {
            "schema",
            "generatedAt",
            "source",
            "accountId",
            "region",
            "namespace",
            "runtimeIdentity",
            "fixtures",
            "workers",
            "stacks",
        },
        "qualification teardown receipt",
    )
    generated_at = _timestamp(
        receipt.get("generatedAt"),
        "qualification teardown generatedAt",
    )
    source = _object(
        receipt.get("source"),
        "qualification teardown source identity",
    )
    _exact_fields(
        source,
        {
            "repository",
            "workflowRef",
            "workflowCommit",
            "releaseCommit",
            "runId",
            "runAttempt",
        },
        "qualification teardown source identity",
    )
    expected_source = {
        "repository": repository,
        "workflowRef": (f"{repository}/{LAUNCH_WORKFLOW}@refs/heads/main"),
        "workflowCommit": release_commit,
        "releaseCommit": release_commit,
        "runId": run_id,
        "runAttempt": run_attempt,
    }
    if (
        receipt.get("schema") != QUALIFICATION_TEARDOWN_SCHEMA
        or source != expected_source
        or receipt.get("accountId") != account_id
        or receipt.get("region") != region
        or receipt.get("namespace") != "managed"
    ):
        raise DeploymentEvidenceError("qualification teardown receipt is not bound to this deployment")

    runtime_identity = _object(
        receipt.get("runtimeIdentity"),
        "qualification teardown runtime identity",
    )
    _exact_fields(
        runtime_identity,
        {
            "secretArnSha256",
            "versionId",
            "currentStageVerified",
            "revokedPayloadSha256",
        },
        "qualification teardown runtime identity",
    )
    secret_digest = runtime_identity.get("secretArnSha256")
    version_id = runtime_identity.get("versionId")
    revoked_digest = runtime_identity.get("revokedPayloadSha256")
    expected_revoked_digest = hashlib.sha256(b'{"token":"revoked","expiresAtEpoch":0}\n').hexdigest()
    if (
        not isinstance(secret_digest, str)
        or SHA256.fullmatch(secret_digest) is None
        or not isinstance(version_id, str)
        or len(version_id) < 32
        or len(version_id) > 256
        or SAFE_VALUE.fullmatch(version_id) is None
        or runtime_identity.get("currentStageVerified") is not True
        or revoked_digest != expected_revoked_digest
    ):
        raise DeploymentEvidenceError("qualification runtime identity revocation is invalid")

    fixtures = _object(
        receipt.get("fixtures"),
        "qualification teardown fixtures",
    )
    _exact_fields(
        fixtures,
        {
            "controlPlaneStatePresent",
            "certificationStatePresent",
            "stateAbsentAfterCleanup",
        },
        "qualification teardown fixtures",
    )
    if fixtures != {
        "controlPlaneStatePresent": True,
        "certificationStatePresent": True,
        "stateAbsentAfterCleanup": True,
    }:
        raise DeploymentEvidenceError("qualification fixture cleanup is incomplete")

    workers = receipt.get("workers")
    expected_workers = {
        "axonllm-launch-action-worker-managed",
        "axonllm-launch-cleanup-worker-managed",
    }
    if (
        type(workers) is not list
        or len(workers) != len(expected_workers)
        or {item.get("serviceName") for item in workers if type(item) is dict} != expected_workers
        or any(
            type(item) is not dict
            or set(item)
            != {
                "serviceName",
                "desiredCount",
                "runningCount",
                "pendingCount",
            }
            or any(
                type(item[name]) is not int or item[name] != 0
                for name in (
                    "desiredCount",
                    "runningCount",
                    "pendingCount",
                )
            )
            for item in workers
        )
    ):
        raise DeploymentEvidenceError("qualification worker shutdown is incomplete")

    stacks = receipt.get("stacks")
    expected_stacks = {
        "AxonLLMLaunchWorkersStack-managed",
        "AxonLLMControlPlaneStack-managed",
        "AxonLLMAgentCoreStack-managed",
        "AxonLLMIdentityStack-managed",
    }
    if (
        type(stacks) is not list
        or len(stacks) != len(expected_stacks)
        or {item.get("name") for item in stacks if type(item) is dict and item.get("absent") is True} != expected_stacks
        or any(
            type(item) is not dict or set(item) != {"name", "absent"} or item.get("absent") is not True
            for item in stacks
        )
    ):
        raise DeploymentEvidenceError("qualification stack teardown is incomplete")

    return {
        "schema": QUALIFICATION_TEARDOWN_SCHEMA,
        "generatedAt": generated_at,
        "source": expected_source,
        "accountId": account_id,
        "region": region,
        "namespace": "managed",
        "runtimeIdentity": {
            "secretArnSha256": secret_digest,
            "versionId": version_id,
            "currentStageVerified": True,
            "revokedPayloadSha256": expected_revoked_digest,
        },
        "fixtures": {
            "controlPlaneStatePresent": True,
            "certificationStatePresent": True,
            "stateAbsentAfterCleanup": True,
        },
        "workers": sorted(workers, key=lambda item: item["serviceName"]),
        "stacks": sorted(stacks, key=lambda item: item["name"]),
    }


def _qualification_teardown(
    path: Path,
    *,
    repository: str,
    release_commit: str,
    run_id: str,
    run_attempt: str,
    account_id: str,
    region: str,
) -> dict[str, Any]:
    return _validate_qualification_teardown(
        _read_json(path),
        repository=repository,
        release_commit=release_commit,
        run_id=run_id,
        run_attempt=run_attempt,
        account_id=account_id,
        region=region,
    )


def _validate_qualification_teardown_source(
    value: Any,
    *,
    repository: str,
    release_commit: str,
    run_id: str,
    run_attempt: str,
) -> dict[str, Any]:
    source = _object(value, "qualification teardown evidence source")
    _exact_fields(
        source,
        {
            "evidenceBucket",
            "evidencePrefix",
            "artifact",
            "signature",
        },
        "qualification teardown evidence source",
    )
    bucket = _safe_string(
        source.get("evidenceBucket"),
        "qualification teardown evidence bucket",
        maximum=63,
    )
    prefix = _safe_string(
        source.get("evidencePrefix"),
        "qualification teardown evidence prefix",
        maximum=256,
    )
    try:
        normalized_prefix = launch_rehearsal_evidence._validate_prefix(prefix)
        artifact = launch_rehearsal_evidence._validate_reference(
            source.get("artifact"),
            expected_bucket=bucket,
            expected_prefix=normalized_prefix,
            location="qualification teardown receipt",
        )
        signature = launch_rehearsal_evidence._validate_reference(
            source.get("signature"),
            expected_bucket=bucket,
            expected_prefix=normalized_prefix,
            location="qualification teardown signature",
        )
    except launch_rehearsal_evidence.LaunchRehearsalError as exc:
        raise DeploymentEvidenceError(f"qualification teardown source is invalid: {exc}") from exc
    expected_parent = f"{normalized_prefix}/{repository}/{release_commit}/{run_id}/{run_attempt}"
    artifact_parent, artifact_name = artifact.key.rsplit("/", 1)
    signature_parent, signature_name = signature.key.rsplit("/", 1)
    if (
        (artifact.uri, artifact.version_id) == (signature.uri, signature.version_id)
        or artifact_parent != expected_parent
        or signature_parent != expected_parent
        or artifact_name != "qualification-teardown-receipt.json"
        or signature_name != "qualification-teardown-signature.json"
    ):
        raise DeploymentEvidenceError(
            "qualification teardown source does not identify the exact "
            "receipt and distinct signature for this deployment run"
        )
    return {
        "evidenceBucket": bucket,
        "evidencePrefix": normalized_prefix,
        "artifact": artifact.report_value(),
        "signature": signature.report_value(),
    }


def _qualification_teardown_source(
    args: argparse.Namespace,
) -> dict[str, Any]:
    source = _validate_qualification_teardown_source(
        {
            "evidenceBucket": args.evidence_bucket,
            "evidencePrefix": args.evidence_prefix,
            "artifact": {
                "s3Uri": args.qualification_teardown_receipt_uri,
                "versionId": (args.qualification_teardown_receipt_version_id),
                "sha256": args.qualification_teardown_receipt_sha256,
            },
            "signature": {
                "s3Uri": args.qualification_teardown_signature_uri,
                "versionId": (args.qualification_teardown_signature_version_id),
                "sha256": args.qualification_teardown_signature_sha256,
            },
        },
        repository=args.repository,
        release_commit=args.release_commit,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
    )
    if source["artifact"]["sha256"] != _hash(args.qualification_teardown_receipt) or source["signature"][
        "sha256"
    ] != _hash(args.qualification_teardown_signature):
        raise DeploymentEvidenceError("qualification teardown source hashes do not match the fetched files")
    return source


def _validate_configuration(
    value: Any,
    *,
    launch_source: dict[str, Any],
    external_oidc_source: dict[str, Any],
    qualification_teardown_source: dict[str, Any],
) -> dict[str, str]:
    configuration = _object(value, "deployment configuration")
    fields = {
        "setupSha256",
        "certificationSha256",
        "productionValidationConfigSha256",
        "productionValidationReportSha256",
        "launchRehearsalReportSha256",
        "launchRehearsalSignatureSha256",
        "externalOidcCertificationReportSha256",
        "externalOidcCertificationSignatureSha256",
        "qualificationTeardownReceiptSha256",
        "qualificationTeardownSignatureSha256",
    }
    _exact_fields(configuration, fields, "deployment configuration")
    if any(
        not isinstance(configuration.get(name), str) or SHA256.fullmatch(configuration[name]) is None for name in fields
    ):
        raise DeploymentEvidenceError("deployment configuration contains an invalid digest")
    if (
        configuration["launchRehearsalReportSha256"] != launch_source["artifact"]["sha256"]
        or configuration["launchRehearsalSignatureSha256"] != launch_source["signature"]["sha256"]
        or configuration["externalOidcCertificationReportSha256"] != external_oidc_source["artifact"]["sha256"]
        or configuration["externalOidcCertificationSignatureSha256"] != external_oidc_source["signature"]["sha256"]
        or configuration["qualificationTeardownReceiptSha256"] != qualification_teardown_source["artifact"]["sha256"]
        or configuration["qualificationTeardownSignatureSha256"] != qualification_teardown_source["signature"]["sha256"]
    ):
        raise DeploymentEvidenceError("deployment configuration is not bound to the immutable certification sources")
    return {name: configuration[name] for name in sorted(fields)}


def _validate_stack_bindings(
    *,
    identity: dict[str, str],
    runtime: dict[str, str],
    control: dict[str, str],
    provider: dict[str, Any],
    agentcore_image: str,
    fargate_image: str,
) -> dict[str, str]:
    for output in (
        "OidcIssuer",
        "OidcClientId",
        "OidcAudience",
        "UserPoolId",
    ):
        _required_output(identity, output, IDENTITY_STACK)
    candidate_endpoint_name = _required_output(
        runtime,
        "CandidateRuntimeEndpointName",
        AGENTCORE_STACK,
    )
    runtime_arn = _required_output(
        runtime,
        "RuntimeArn",
        AGENTCORE_STACK,
    )
    if (
        _required_output(runtime, "RuntimeImageUri", AGENTCORE_STACK) != agentcore_image
        or _required_output(
            runtime,
            "ProviderSecretArn",
            AGENTCORE_STACK,
        )
        != provider["secretArn"]
        or _required_output(runtime, "ProviderSecretVersion", AGENTCORE_STACK) != provider["versionId"]
        or _required_output(runtime, "RecoveryCutoverMode", AGENTCORE_STACK) != "normal"
        or _required_output(runtime, "RuntimeEndpointName", AGENTCORE_STACK) != "production"
        or _required_output(
            runtime,
            "RuntimeEndpointArn",
            AGENTCORE_STACK,
        )
        != f"{runtime_arn}/runtime-endpoint/production"
        or CANDIDATE_ENDPOINT.fullmatch(candidate_endpoint_name) is None
        or not (
            runtime_version := _required_output(
                runtime,
                "RuntimeVersion",
                AGENTCORE_STACK,
            )
        ).isdigit()
        or _required_output(
            runtime,
            "CandidateRuntimeVersion",
            AGENTCORE_STACK,
        )
        != runtime_version
        or _required_output(
            runtime,
            "ProductionRuntimeVersion",
            AGENTCORE_STACK,
        )
        != runtime_version
        or not _required_output(
            runtime,
            "CandidateRuntimeEndpointArn",
            AGENTCORE_STACK,
        )
        == (f"{runtime_arn}/runtime-endpoint/{candidate_endpoint_name}")
    ):
        raise DeploymentEvidenceError(
            "AgentCore outputs are not bound to the verified deployment inputs and production launch contract"
        )
    _runtime_provider_feature_matrix(runtime)
    expected_control = {
        "AgentCoreStackName": AGENTCORE_STACK,
        "PrimaryStateTableName": _required_output(
            runtime,
            "StateTableName",
            AGENTCORE_STACK,
        ),
        "SelectedRuntimeStateTableName": _required_output(
            runtime,
            "SelectedRuntimeStateTableName",
            AGENTCORE_STACK,
        ),
        "RecoveryCutoverMode": "normal",
        "RecoveryApprovalId": _required_output(
            runtime,
            "RecoveryApprovalId",
            AGENTCORE_STACK,
        ),
    }
    if any(control.get(name) != value for name, value in expected_control.items()):
        raise DeploymentEvidenceError("control-plane outputs do not match AgentCore recovery state")
    if (
        _required_output(
            control,
            "ControlPlaneImageUri",
            CONTROL_PLANE_STACK,
        )
        != fargate_image
        or _required_output(
            control,
            "QueryPlaneEnabled",
            CONTROL_PLANE_STACK,
        )
        != "true"
    ):
        raise DeploymentEvidenceError(
            "control-plane outputs are not bound to the verified image and query configuration"
        )
    _required_output(
        control,
        "TaskDefinitionArn",
        CONTROL_PLANE_STACK,
    )
    _required_output(
        control,
        "TargetGroupArn",
        CONTROL_PLANE_STACK,
    )
    _required_output(control, "ClusterName", CONTROL_PLANE_STACK)
    _required_output(control, "ServiceName", CONTROL_PLANE_STACK)
    return _control_plane_endpoint_binding(
        identity=identity,
        control=control,
    )


def _validate_deployment_projection(
    value: Any,
    *,
    repository: str,
    deployment_commit: str,
    release_commit: str,
    run_id: str,
    run_attempt: str,
) -> dict[str, Any]:
    deployment = _object(value, "deployment")
    _exact_fields(
        deployment,
        {
            "operation",
            "changeId",
            "environment",
            "repository",
            "commit",
            "workflowRef",
            "workflowCommit",
            "parentWorkflowRef",
            "parentWorkflowCommit",
            "runId",
            "runAttempt",
            "actor",
            "actorId",
            "triggeringActor",
            "generatedAt",
            "awsAccountId",
            "awsRegion",
        },
        "deployment",
    )
    expected = {
        "repository": repository,
        "commit": deployment_commit,
        "workflowRef": (f"{repository}/{PRODUCTION_WORKFLOW}@refs/heads/main"),
        "workflowCommit": release_commit,
        "parentWorkflowRef": (f"{repository}/{LAUNCH_WORKFLOW}@refs/heads/main"),
        "parentWorkflowCommit": release_commit,
        "runId": run_id,
        "runAttempt": run_attempt,
    }
    account_id = deployment.get("awsAccountId")
    region = deployment.get("awsRegion")
    if (
        any(deployment.get(name) != item for name, item in expected.items())
        or deployment.get("operation") not in {"deploy", "rollback"}
        or deployment.get("environment") != "production"
        or not isinstance(deployment.get("changeId"), str)
        or CHANGE_ID.fullmatch(deployment["changeId"]) is None
        or not isinstance(deployment.get("actor"), str)
        or ACTOR.fullmatch(deployment["actor"]) is None
        or not isinstance(deployment.get("triggeringActor"), str)
        or ACTOR.fullmatch(deployment["triggeringActor"]) is None
        or not isinstance(deployment.get("actorId"), str)
        or RUN_ID.fullmatch(deployment["actorId"]) is None
        or not isinstance(account_id, str)
        or len(account_id) != 12
        or not account_id.isdigit()
    ):
        raise DeploymentEvidenceError("deployment identity does not match expectation")
    _safe_string(region, "deployment AWS region", maximum=32)
    _timestamp(deployment.get("generatedAt"), "deployment generatedAt")
    return deployment


def _validate_images_projection(
    value: Any,
    *,
    region: str,
    account_id: str,
    agentcore_image: str,
    fargate_image: str,
) -> tuple[dict[str, Any], str, str]:
    images = _object(value, "images")
    _exact_fields(
        images,
        {"agentcore", "controlPlane"},
        "images",
    )
    expected = {
        "agentcore": agentcore_image,
        "controlPlane": fargate_image,
    }
    digests: dict[str, str] = {}
    for name, reference in expected.items():
        image = _object(images.get(name), f"{name} image")
        _exact_fields(
            image,
            {"reference", "digest"},
            f"{name} image",
        )
        digest, image_account = _image(
            reference,
            region=region,
            location=f"{name} image",
        )
        if image.get("reference") != reference or image.get("digest") != digest or image_account != account_id:
            raise DeploymentEvidenceError("deployment images do not match expectation")
        digests[name] = digest
    return images, digests["agentcore"], digests["controlPlane"]


def _validate_release_projection(
    value: Any,
    *,
    release_commit: str,
    agentcore_digest: str,
    fargate_digest: str,
) -> dict[str, Any]:
    release = _object(value, "release")
    _exact_fields(
        release,
        {
            "commit",
            "manifestSha256",
            "ref",
            "runId",
            "signingKeyArn",
            "targets",
        },
        "release",
    )
    targets = _object(release.get("targets"), "release targets")
    _exact_fields(
        targets,
        {"agentcore", "fargate"},
        "release targets",
    )
    expected_digests = {
        "agentcore": agentcore_digest,
        "fargate": fargate_digest,
    }
    for name, digest in expected_digests.items():
        target = _object(
            targets.get(name),
            f"release target {name}",
        )
        _exact_fields(
            target,
            {"digest", "platform"},
            f"release target {name}",
        )
        if (
            target.get("digest") != digest
            or _safe_string(
                target.get("platform"),
                f"release target {name} platform",
            )
            != target["platform"]
        ):
            raise DeploymentEvidenceError("release targets do not match deployed images")
    if (
        release.get("commit") != release_commit
        or not isinstance(release.get("manifestSha256"), str)
        or SHA256.fullmatch(release["manifestSha256"]) is None
        or not isinstance(release.get("ref"), str)
        or not release["ref"].startswith("refs/tags/v")
        or not isinstance(release.get("runId"), str)
        or RUN_ID.fullmatch(release["runId"]) is None
    ):
        raise DeploymentEvidenceError("release evidence does not match expectation")
    _safe_string(release.get("signingKeyArn"), "release signing key ARN")
    return release


def create_evidence(args: argparse.Namespace) -> dict[str, Any]:
    repository = _safe_string(args.repository, "repository")
    if REPOSITORY.fullmatch(repository) is None:
        raise DeploymentEvidenceError("repository must be owner/name")
    if SHA.fullmatch(args.deployment_commit) is None:
        raise DeploymentEvidenceError("deployment commit must be a full SHA")
    if SHA.fullmatch(args.release_commit) is None:
        raise DeploymentEvidenceError("release commit must be a full SHA")
    for name, value in (
        ("run ID", args.run_id),
        ("run attempt", args.run_attempt),
        ("release run ID", args.release_run_id),
        ("actor ID", args.actor_id),
    ):
        if RUN_ID.fullmatch(value) is None:
            raise DeploymentEvidenceError(f"{name} must be numeric")
    if ACTOR.fullmatch(args.actor) is None:
        raise DeploymentEvidenceError("actor is malformed")
    if ACTOR.fullmatch(args.triggering_actor) is None:
        raise DeploymentEvidenceError("triggering actor is malformed")
    if CHANGE_ID.fullmatch(args.change_id) is None:
        raise DeploymentEvidenceError("change ID is malformed")
    expected_workflow_ref = f"{repository}/{PRODUCTION_WORKFLOW}@refs/heads/main"
    expected_parent_workflow_ref = f"{repository}/{LAUNCH_WORKFLOW}@refs/heads/main"
    if (
        args.workflow_ref != expected_workflow_ref
        or args.parent_workflow_ref != expected_parent_workflow_ref
        or args.workflow_commit != args.release_commit
        or args.parent_workflow_commit != args.release_commit
        or args.deployment_commit != args.release_commit
    ):
        raise DeploymentEvidenceError(
            "deployment workflow, protected parent, and checkout must equal the release commit"
        )
    if args.operation not in {"deploy", "rollback"}:
        raise DeploymentEvidenceError("operation must be deploy or rollback")

    agentcore_digest, account_id = _image(
        args.agentcore_image,
        region=args.region,
        location="AgentCore image",
    )
    fargate_digest, fargate_account = _image(
        args.fargate_image,
        region=args.region,
        location="control-plane image",
    )
    if account_id != fargate_account:
        raise DeploymentEvidenceError("deployment images use different AWS accounts")

    identity = _stack_outputs(args.identity_outputs, IDENTITY_STACK)
    runtime = _stack_outputs(args.runtime_outputs, AGENTCORE_STACK)
    control = _stack_outputs(args.control_outputs, CONTROL_PLANE_STACK)
    provider = _provider_secret(args.provider_secret)
    control_endpoint = _validate_stack_bindings(
        identity=identity,
        runtime=runtime,
        control=control,
        provider=provider,
        agentcore_image=args.agentcore_image,
        fargate_image=args.fargate_image,
    )

    setup = _object(_read_json(args.setup_config), "setup configuration")
    setup_runtime = _object(setup.get("runtime"), "setup runtime")
    setup_control = _object(setup.get("control_plane"), "setup control plane")
    runtime_provider_features = _runtime_provider_feature_matrix(runtime)
    setup_provider_features = _production_provider_feature_matrix(
        setup_runtime.get("enabled_providers") if isinstance(setup_runtime.get("enabled_providers"), list) else [],
        location="setup enabled providers",
    )
    setup_endpoint_mode = setup_control.get(
        "endpoint_mode",
        CUSTOM_DOMAIN,
    )
    control_domain = setup_control.get("domain_name")
    if (
        setup.get("identity_mode") != "managed-cognito"
        or setup.get("aws_region") != args.region
        or setup_runtime.get("verified_image_uri") != args.agentcore_image
        or setup_control.get("verified_image_uri") != args.fargate_image
        or setup_provider_features != runtime_provider_features
        or setup_endpoint_mode != control_endpoint["endpointMode"]
        or (
            setup_endpoint_mode == CUSTOM_DOMAIN
            and (
                not isinstance(control_domain, str)
                or not control_domain
                or control_domain != control_endpoint["domainName"]
            )
        )
        or (
            setup_endpoint_mode == CLOUDFRONT
            and control_domain is not None
        )
    ):
        raise DeploymentEvidenceError(
            "setup configuration is not bound to the verified images and "
            "control-plane endpoint"
        )

    certification_config = _object(
        _read_json(args.certification_config),
        "certification configuration",
    )
    certification_provider_features = _configuration_provider_feature_matrix(
        certification_config.get("providers"),
        location="certification configuration providers",
    )
    if (
        certification_config.get("runtimeArn") != _required_output(runtime, "RuntimeArn", AGENTCORE_STACK)
        or certification_config.get("qualifier")
        != _required_output(
            runtime,
            "CandidateRuntimeEndpointName",
            AGENTCORE_STACK,
        )
        or certification_provider_features != runtime_provider_features
    ):
        raise DeploymentEvidenceError("certification configuration is not bound to the candidate runtime outputs")

    release = _release(
        args.release_manifest,
        repository=repository,
        release_commit=args.release_commit,
        release_run_id=args.release_run_id,
        agentcore_digest=agentcore_digest,
        fargate_digest=fargate_digest,
    )
    recovery = _recovery(
        args.recovery_report,
        args.transition_report,
        runtime=runtime,
    )
    certification = _certification(
        args.certification_report,
        runtime=runtime,
        endpoint_arn_output="CandidateRuntimeEndpointArn",
        endpoint_name_output="CandidateRuntimeEndpointName",
        runtime_version_output="CandidateRuntimeVersion",
        target="candidate",
    )
    production_certification = _certification(
        args.production_certification_report,
        runtime=runtime,
        endpoint_arn_output="RuntimeEndpointArn",
        endpoint_name_output="RuntimeEndpointName",
        runtime_version_output="ProductionRuntimeVersion",
        target="production",
    )
    production_validation = _production_validation(
        args.production_validation_report,
        expected_base_url=control_endpoint["url"],
        expected_credential_type=(
            control_endpoint["credentialType"]
        ),
        expected_target_group_arn=_required_output(
            control,
            "TargetGroupArn",
            CONTROL_PLANE_STACK,
        ),
    )
    launch_rehearsal = _launch_rehearsal(
        args.launch_rehearsal_report,
        release_commit=args.release_commit,
        region=args.region,
        agentcore_image=args.agentcore_image,
        fargate_image=args.fargate_image,
        repository=repository,
        evidence_bucket=args.evidence_bucket,
        evidence_prefix=args.evidence_prefix,
    )
    launch_rehearsal_source = _launch_rehearsal_source(
        args,
        report=launch_rehearsal,
    )
    external_oidc_certification = _external_oidc_certification(
        args.external_oidc_certification_report,
        repository=repository,
        release_commit=args.release_commit,
        region=args.region,
        agentcore_image=args.agentcore_image,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        expected_provider_features=runtime_provider_features,
    )
    external_oidc_source = _external_oidc_source(args)
    qualification_teardown = _qualification_teardown(
        args.qualification_teardown_receipt,
        repository=repository,
        release_commit=args.release_commit,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        account_id=account_id,
        region=args.region,
    )
    qualification_teardown_source = _qualification_teardown_source(args)
    configuration = _validate_configuration(
        {
            "setupSha256": _hash(args.setup_config),
            "certificationSha256": _hash(args.certification_config),
            "productionValidationConfigSha256": _hash(args.production_validation_config),
            "productionValidationReportSha256": _hash(args.production_validation_report),
            "launchRehearsalReportSha256": _hash(args.launch_rehearsal_report),
            "launchRehearsalSignatureSha256": _hash(args.launch_rehearsal_signature),
            "externalOidcCertificationReportSha256": _hash(args.external_oidc_certification_report),
            "externalOidcCertificationSignatureSha256": _hash(args.external_oidc_certification_signature),
            "qualificationTeardownReceiptSha256": _hash(args.qualification_teardown_receipt),
            "qualificationTeardownSignatureSha256": _hash(args.qualification_teardown_signature),
        },
        launch_source=launch_rehearsal_source,
        external_oidc_source=external_oidc_source,
        qualification_teardown_source=qualification_teardown_source,
    )
    return {
        "schema": SCHEMA,
        "deployment": {
            "operation": args.operation,
            "changeId": args.change_id,
            "environment": "production",
            "repository": repository,
            "commit": args.deployment_commit,
            "workflowRef": args.workflow_ref,
            "workflowCommit": args.workflow_commit,
            "parentWorkflowRef": args.parent_workflow_ref,
            "parentWorkflowCommit": args.parent_workflow_commit,
            "runId": args.run_id,
            "runAttempt": args.run_attempt,
            "actor": args.actor,
            "actorId": args.actor_id,
            "triggeringActor": args.triggering_actor,
            "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "awsAccountId": account_id,
            "awsRegion": args.region,
        },
        "release": {
            "commit": args.release_commit,
            **release,
        },
        "images": {
            "agentcore": {
                "reference": args.agentcore_image,
                "digest": agentcore_digest,
            },
            "controlPlane": {
                "reference": args.fargate_image,
                "digest": fargate_digest,
            },
        },
        "configuration": configuration,
        "stacks": {
            "identity": identity,
            "runtime": runtime,
            "controlPlane": control,
        },
        "providerSecret": provider,
        "recovery": recovery,
        "certification": certification,
        "productionCertification": _redacted_certification(production_certification),
        "productionValidation": production_validation,
        "launchRehearsalSource": launch_rehearsal_source,
        "launchRehearsal": launch_rehearsal,
        "externalOidcCertificationSource": external_oidc_source,
        "externalOidcCertification": external_oidc_certification,
        "qualificationTeardownSource": qualification_teardown_source,
        "qualificationTeardown": qualification_teardown,
    }


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(
            value,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        with os.fdopen(descriptor, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise DeploymentEvidenceError(f"cannot write deployment evidence: {path}") from exc
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def verify_evidence(args: argparse.Namespace) -> dict[str, Any]:
    value = _object(_read_json(args.evidence), "deployment evidence")
    _exact_fields(
        value,
        {
            "schema",
            "deployment",
            "release",
            "images",
            "configuration",
            "stacks",
            "providerSecret",
            "recovery",
            "certification",
            "productionCertification",
            "productionValidation",
            "launchRehearsalSource",
            "launchRehearsal",
            "externalOidcCertificationSource",
            "externalOidcCertification",
            "qualificationTeardownSource",
            "qualificationTeardown",
        },
        "deployment evidence",
    )
    if value.get("schema") != SCHEMA:
        raise DeploymentEvidenceError("deployment evidence schema is unsupported")
    repository = _safe_string(args.repository, "repository")
    if REPOSITORY.fullmatch(repository) is None:
        raise DeploymentEvidenceError("repository must be owner/name")
    if (
        SHA.fullmatch(args.deployment_commit) is None
        or SHA.fullmatch(args.release_commit) is None
        or args.deployment_commit != args.release_commit
        or RUN_ID.fullmatch(args.run_id) is None
        or RUN_ID.fullmatch(args.run_attempt) is None
    ):
        raise DeploymentEvidenceError("expected deployment identity is malformed")
    deployment = _validate_deployment_projection(
        value["deployment"],
        repository=repository,
        deployment_commit=args.deployment_commit,
        release_commit=args.release_commit,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
    )
    region = _safe_string(
        deployment["awsRegion"],
        "deployment AWS region",
        maximum=32,
    )
    account_id = _safe_string(
        deployment["awsAccountId"],
        "deployment AWS account ID",
        maximum=12,
    )
    _images, agentcore_digest, fargate_digest = _validate_images_projection(
        value["images"],
        region=region,
        account_id=account_id,
        agentcore_image=args.agentcore_image,
        fargate_image=args.fargate_image,
    )
    _validate_release_projection(
        value["release"],
        release_commit=args.release_commit,
        agentcore_digest=agentcore_digest,
        fargate_digest=fargate_digest,
    )
    stacks = _object(value["stacks"], "stacks")
    _exact_fields(
        stacks,
        {"identity", "runtime", "controlPlane"},
        "deployment stacks",
    )
    runtime = _validate_stack_outputs(
        stacks.get("runtime"),
        AGENTCORE_STACK,
    )
    identity = _validate_stack_outputs(
        stacks.get("identity"),
        IDENTITY_STACK,
    )
    control_outputs = _validate_stack_outputs(
        stacks.get("controlPlane"),
        CONTROL_PLANE_STACK,
    )
    provider = _validate_provider_secret(value["providerSecret"])
    control_endpoint = _validate_stack_bindings(
        identity=identity,
        runtime=runtime,
        control=control_outputs,
        provider=provider,
        agentcore_image=args.agentcore_image,
        fargate_image=args.fargate_image,
    )
    recovery_value = _object(value["recovery"], "recovery evidence")
    _exact_fields(
        recovery_value,
        {"validation", "transition"},
        "recovery evidence",
    )
    recovery = _validate_recovery(
        recovery_value["validation"],
        recovery_value["transition"],
        runtime=runtime,
    )
    if recovery != recovery_value:
        raise DeploymentEvidenceError("recovery evidence is not normalized")
    candidate_certification = _validate_certification_report(
        _object(value["certification"], "candidate certification"),
        runtime=runtime,
        endpoint_arn_output="CandidateRuntimeEndpointArn",
        endpoint_name_output="CandidateRuntimeEndpointName",
        runtime_version_output="CandidateRuntimeVersion",
        target="candidate",
    )
    if candidate_certification != value["certification"]:
        raise DeploymentEvidenceError("candidate certification evidence is not normalized")
    production_certification = _validate_certification_report(
        _object(
            value["productionCertification"],
            "production certification",
        ),
        runtime=runtime,
        endpoint_arn_output="RuntimeEndpointArn",
        endpoint_name_output="RuntimeEndpointName",
        runtime_version_output="ProductionRuntimeVersion",
        target="production",
    )
    if production_certification != _redacted_certification(production_certification):
        raise DeploymentEvidenceError("production certification evidence is not redacted")
    production_validation = _validate_production_validation(
        _object(
            value["productionValidation"],
            "production validation",
        ),
        expected_base_url=control_endpoint["url"],
        expected_credential_type=(
            control_endpoint["credentialType"]
        ),
        expected_target_group_arn=_required_output(
            control_outputs,
            "TargetGroupArn",
            CONTROL_PLANE_STACK,
        ),
    )
    if production_validation != value["productionValidation"]:
        raise DeploymentEvidenceError("production validation evidence is not normalized")
    launch_source_value = _object(
        value["launchRehearsalSource"],
        "launch rehearsal source",
    )
    evidence_bucket = _safe_string(
        launch_source_value.get("evidenceBucket"),
        "launch rehearsal evidence bucket",
        maximum=63,
    )
    evidence_prefix = _safe_string(
        launch_source_value.get("evidencePrefix"),
        "launch rehearsal evidence prefix",
        maximum=256,
    )
    launch_rehearsal = _validate_launch_rehearsal(
        _object(value["launchRehearsal"], "launch rehearsal"),
        release_commit=args.release_commit,
        region=region,
        agentcore_image=args.agentcore_image,
        fargate_image=args.fargate_image,
        repository=repository,
        evidence_bucket=evidence_bucket,
        evidence_prefix=evidence_prefix,
    )
    if launch_rehearsal != value["launchRehearsal"]:
        raise DeploymentEvidenceError("launch rehearsal evidence is not normalized")
    launch_source = _validate_launch_rehearsal_source(
        launch_source_value,
        report=launch_rehearsal,
    )
    if launch_source != launch_source_value:
        raise DeploymentEvidenceError("launch rehearsal source is not normalized")
    external_oidc_certification = _validate_external_oidc_certification(
        _object(
            value["externalOidcCertification"],
            "external OIDC certification report",
        ),
        repository=repository,
        release_commit=args.release_commit,
        region=region,
        agentcore_image=args.agentcore_image,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        expected_provider_features=_runtime_provider_feature_matrix(runtime),
    )
    if external_oidc_certification != value["externalOidcCertification"]:
        raise DeploymentEvidenceError("external OIDC certification is not normalized")
    external_oidc_source_value = _object(
        value["externalOidcCertificationSource"],
        "external OIDC certification source",
    )
    external_oidc_source = _validate_external_oidc_source(external_oidc_source_value)
    if external_oidc_source != external_oidc_source_value:
        raise DeploymentEvidenceError("external OIDC certification source is not normalized")
    qualification_teardown = _validate_qualification_teardown(
        value["qualificationTeardown"],
        repository=repository,
        release_commit=args.release_commit,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        account_id=account_id,
        region=region,
    )
    if qualification_teardown != value["qualificationTeardown"]:
        raise DeploymentEvidenceError("qualification teardown receipt is not normalized")
    qualification_teardown_source_value = _object(
        value["qualificationTeardownSource"],
        "qualification teardown evidence source",
    )
    qualification_teardown_source = _validate_qualification_teardown_source(
        qualification_teardown_source_value,
        repository=repository,
        release_commit=args.release_commit,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
    )
    if qualification_teardown_source != qualification_teardown_source_value:
        raise DeploymentEvidenceError("qualification teardown source is not normalized")
    configuration = _validate_configuration(
        value["configuration"],
        launch_source=launch_source,
        external_oidc_source=external_oidc_source,
        qualification_teardown_source=(qualification_teardown_source),
    )
    if configuration != value["configuration"]:
        raise DeploymentEvidenceError("deployment configuration is not normalized")
    return value


def verify_qualification_teardown(
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Verify a managed qualification teardown receipt for one launch run."""

    repository = _safe_string(args.repository, "repository")
    if REPOSITORY.fullmatch(repository) is None:
        raise DeploymentEvidenceError("repository must be owner/name")
    if SHA.fullmatch(args.release_commit) is None:
        raise DeploymentEvidenceError("release commit must be a full SHA")
    if RUN_ID.fullmatch(args.run_id) is None or RUN_ID.fullmatch(args.run_attempt) is None:
        raise DeploymentEvidenceError("qualification teardown run identity is malformed")
    if not isinstance(args.account_id, str) or not args.account_id.isdigit():
        raise DeploymentEvidenceError("qualification teardown account ID is malformed")
    if len(args.account_id) != 12:
        raise DeploymentEvidenceError("qualification teardown account ID is malformed")
    region = _safe_string(
        args.region,
        "qualification teardown region",
        maximum=32,
    )
    return _qualification_teardown(
        args.receipt,
        repository=repository,
        release_commit=args.release_commit,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        account_id=args.account_id,
        region=region,
    )


def _expected_commit_record(args: argparse.Namespace) -> dict[str, Any]:
    verify_evidence(args)
    _read_json(args.evidence_signature)
    return {
        "schema": COMMIT_SCHEMA,
        "deployment": {
            "repository": args.repository,
            "commit": args.deployment_commit,
            "runId": args.run_id,
            "runAttempt": args.run_attempt,
        },
        "release": {"commit": args.release_commit},
        "images": {
            "agentcore": args.agentcore_image,
            "controlPlane": args.fargate_image,
        },
        "artifacts": {
            "evidence": {
                "name": "agentcore-deployment.json",
                "sha256": _hash(args.evidence),
            },
            "signature": {
                "name": "agentcore-deployment-kms-signature.json",
                "sha256": _hash(args.evidence_signature),
            },
        },
    }


def create_commit_record(args: argparse.Namespace) -> dict[str, Any]:
    """Bind the exact preparatory evidence pair to its candidate identity."""

    return _expected_commit_record(args)


def verify_commit_record(args: argparse.Namespace) -> dict[str, Any]:
    """Verify a signed completion record against exact local artifacts."""

    value = _object(
        _read_json(args.commit_record),
        "deployment evidence commit record",
    )
    expected = _expected_commit_record(args)
    if value != expected:
        raise DeploymentEvidenceError(
            "deployment evidence commit record does not match exact artifacts and candidate identity"
        )
    return value


def _common_expected(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repository", required=True)
    parser.add_argument("--deployment-commit", required=True)
    parser.add_argument("--release-commit", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--agentcore-image", required=True)
    parser.add_argument("--fargate-image", required=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create or verify redacted AgentCore deployment evidence",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create")
    _common_expected(create)
    create.add_argument("--output", required=True, type=Path)
    create.add_argument("--release-run-id", required=True)
    create.add_argument("--release-manifest", required=True, type=Path)
    create.add_argument("--workflow-ref", required=True)
    create.add_argument("--workflow-commit", required=True)
    create.add_argument("--parent-workflow-ref", required=True)
    create.add_argument("--parent-workflow-commit", required=True)
    create.add_argument("--actor", required=True)
    create.add_argument("--actor-id", required=True)
    create.add_argument("--triggering-actor", required=True)
    create.add_argument("--change-id", required=True)
    create.add_argument("--operation", choices=("deploy", "rollback"), required=True)
    create.add_argument("--region", required=True)
    create.add_argument("--setup-config", required=True, type=Path)
    create.add_argument("--certification-config", required=True, type=Path)
    create.add_argument(
        "--production-validation-config",
        required=True,
        type=Path,
    )
    create.add_argument("--identity-outputs", required=True, type=Path)
    create.add_argument("--runtime-outputs", required=True, type=Path)
    create.add_argument("--control-outputs", required=True, type=Path)
    create.add_argument("--provider-secret", required=True, type=Path)
    create.add_argument("--recovery-report", required=True, type=Path)
    create.add_argument("--transition-report", required=True, type=Path)
    create.add_argument("--certification-report", required=True, type=Path)
    create.add_argument(
        "--production-certification-report",
        required=True,
        type=Path,
    )
    create.add_argument(
        "--production-validation-report",
        required=True,
        type=Path,
    )
    create.add_argument(
        "--launch-rehearsal-report",
        required=True,
        type=Path,
    )
    create.add_argument(
        "--launch-rehearsal-signature",
        required=True,
        type=Path,
    )
    create.add_argument("--evidence-bucket", required=True)
    create.add_argument("--evidence-prefix", required=True)
    create.add_argument("--launch-rehearsal-report-uri", required=True)
    create.add_argument(
        "--launch-rehearsal-report-version-id",
        required=True,
    )
    create.add_argument("--launch-rehearsal-report-sha256", required=True)
    create.add_argument("--launch-rehearsal-signature-uri", required=True)
    create.add_argument(
        "--launch-rehearsal-signature-version-id",
        required=True,
    )
    create.add_argument(
        "--launch-rehearsal-signature-sha256",
        required=True,
    )
    create.add_argument(
        "--external-oidc-certification-report",
        required=True,
        type=Path,
    )
    create.add_argument(
        "--external-oidc-certification-signature",
        required=True,
        type=Path,
    )
    create.add_argument("--external-oidc-evidence-prefix", required=True)
    create.add_argument("--external-oidc-report-uri", required=True)
    create.add_argument("--external-oidc-report-version-id", required=True)
    create.add_argument("--external-oidc-report-sha256", required=True)
    create.add_argument("--external-oidc-signature-uri", required=True)
    create.add_argument(
        "--external-oidc-signature-version-id",
        required=True,
    )
    create.add_argument("--external-oidc-signature-sha256", required=True)
    create.add_argument(
        "--qualification-teardown-receipt",
        required=True,
        type=Path,
    )
    create.add_argument(
        "--qualification-teardown-signature",
        required=True,
        type=Path,
    )
    create.add_argument(
        "--qualification-teardown-receipt-uri",
        required=True,
    )
    create.add_argument(
        "--qualification-teardown-receipt-version-id",
        required=True,
    )
    create.add_argument(
        "--qualification-teardown-receipt-sha256",
        required=True,
    )
    create.add_argument(
        "--qualification-teardown-signature-uri",
        required=True,
    )
    create.add_argument(
        "--qualification-teardown-signature-version-id",
        required=True,
    )
    create.add_argument(
        "--qualification-teardown-signature-sha256",
        required=True,
    )

    verify = commands.add_parser("verify")
    _common_expected(verify)
    verify.add_argument("--evidence", required=True, type=Path)

    verify_teardown = commands.add_parser("verify-qualification-teardown")
    verify_teardown.add_argument("--receipt", required=True, type=Path)
    verify_teardown.add_argument("--repository", required=True)
    verify_teardown.add_argument("--release-commit", required=True)
    verify_teardown.add_argument("--run-id", required=True)
    verify_teardown.add_argument("--run-attempt", required=True)
    verify_teardown.add_argument("--account-id", required=True)
    verify_teardown.add_argument("--region", required=True)

    create_commit = commands.add_parser("create-commit")
    _common_expected(create_commit)
    create_commit.add_argument("--evidence", required=True, type=Path)
    create_commit.add_argument(
        "--evidence-signature",
        required=True,
        type=Path,
    )
    create_commit.add_argument("--output", required=True, type=Path)

    verify_commit = commands.add_parser("verify-commit")
    _common_expected(verify_commit)
    verify_commit.add_argument("--evidence", required=True, type=Path)
    verify_commit.add_argument(
        "--evidence-signature",
        required=True,
        type=Path,
    )
    verify_commit.add_argument(
        "--commit-record",
        required=True,
        type=Path,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "create":
            value = create_evidence(args)
            _atomic_write(args.output, value)
            print(f"deployment evidence created: {args.output}")
        elif args.command == "verify":
            verify_evidence(args)
            print(f"deployment evidence verified: {args.evidence}")
        elif args.command == "verify-qualification-teardown":
            verify_qualification_teardown(args)
            print(f"qualification teardown verified: {args.receipt}")
        elif args.command == "create-commit":
            value = create_commit_record(args)
            _atomic_write(args.output, value)
            print(f"deployment evidence commit created: {args.output}")
        else:
            verify_commit_record(args)
            print(f"deployment evidence commit verified: {args.commit_record}")
    except DeploymentEvidenceError as exc:
        print(f"deployment evidence failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
