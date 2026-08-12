#!/usr/bin/env python3
"""Create and verify redacted AgentCore deployment evidence."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Any, Sequence


SCHEMA = "https://axonllm.dev/schemas/agentcore-deployment-evidence/v1"
RELEASE_SCHEMA = "https://axonllm.dev/schemas/release-evidence/v3"
CERTIFICATION_SCHEMA = "axonllm.agentcore-certification/v1"
IDENTITY_STACK = "AxonLLMIdentityStack"
AGENTCORE_STACK = "AxonLLMAgentCoreStack"
CONTROL_PLANE_STACK = "AxonLLMControlPlaneStack"
MAX_INPUT_BYTES = 2 * 1024 * 1024

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
SECRET_NAME = re.compile(
    r"(password|secretstring|secretvalue|token|credential|api[_-]?key)",
    re.IGNORECASE,
)


class DeploymentEvidenceError(RuntimeError):
    """Raised when deployment evidence is incomplete or inconsistent."""


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
            raise DeploymentEvidenceError(
                f"input must be a regular non-symlink file: {path}"
            )
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
        raise DeploymentEvidenceError(
            f"{location} must be an immutable private ECR digest URI in {region}"
        )
    return match.group("digest"), match.group("account")


def _stack_outputs(path: Path, stack_name: str) -> dict[str, str]:
    document = _object(_read_json(path), str(path))
    outputs = _object(document.get(stack_name), f"{path}:{stack_name}")
    result: dict[str, str] = {}
    for name, value in outputs.items():
        if (
            SAFE_OUTPUT_NAME.fullmatch(name) is None
            or (
                SECRET_NAME.search(name)
                and not name.endswith(("Arn", "Version"))
            )
            or not isinstance(value, str)
            or not value
            or value != value.strip()
            or len(value) > 16 * 1024
            or "\x00" in value
        ):
            raise DeploymentEvidenceError(
                f"{stack_name} contains an unsafe output"
            )
        result[name] = value
    return result


def _required_output(
    outputs: dict[str, str],
    name: str,
    stack_name: str,
) -> str:
    value = outputs.get(name)
    if value is None:
        raise DeploymentEvidenceError(
            f"{stack_name} output {name} is missing"
        )
    return value


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
        raise DeploymentEvidenceError(
            "release manifest source differs from the deployment request"
        )
    ref = source.get("ref")
    if not isinstance(ref, str) or not ref.startswith("refs/tags/v"):
        raise DeploymentEvidenceError("release manifest is not from a v-prefixed tag")
    targets = _object(manifest.get("targets"), "release targets")
    if set(targets) != {"agentcore", "fargate"}:
        raise DeploymentEvidenceError("release target set is incomplete")
    actual = {
        name: _object(targets[name], f"release target {name}").get("digest")
        for name in targets
    }
    expected = {
        "agentcore": agentcore_digest,
        "fargate": fargate_digest,
    }
    if actual != expected:
        raise DeploymentEvidenceError(
            "deployed image digests differ from release evidence"
        )
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
                    _object(targets[name], f"release target {name}").get(
                        "platform"
                    ),
                    f"release target {name} platform",
                ),
            }
            for name in ("agentcore", "fargate")
        },
    }


def _provider_secret(path: Path) -> dict[str, Any]:
    value = _object(_read_json(path), "provider secret metadata")
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
    if (
        not isinstance(fields, list)
        or not fields
        or fields != sorted(set(fields))
        or any(
            not isinstance(field, str)
            or re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", field) is None
            for field in fields
        )
        or not isinstance(value["changed"], bool)
        or (
            value["previousVersionId"] is not None
            and not isinstance(value["previousVersionId"], str)
        )
        or not isinstance(value["fingerprint"], str)
        or SHA256.fullmatch(value["fingerprint"]) is None
    ):
        raise DeploymentEvidenceError("provider secret metadata is malformed")
    return {
        "secretArn": _safe_string(value["secretArn"], "provider secret ARN"),
        "versionId": _safe_string(value["versionId"], "provider secret version"),
        "previousVersionId": value["previousVersionId"],
        "changed": value["changed"],
        "configuredFields": fields,
        "fingerprint": value["fingerprint"],
    }


def _recovery(
    recovery_path: Path,
    transition_path: Path,
    *,
    runtime: dict[str, str],
) -> dict[str, Any]:
    recovery = _object(_read_json(recovery_path), "recovery validation")
    if (
        recovery.get("pointInTimeRecovery") != "ENABLED"
        or recovery.get("backupVaultLocked") is not True
        or recovery.get("backupVaultLockMode") != "GOVERNANCE"
        or recovery.get("backupVaultMinRetentionDays") != 30
        or recovery.get("backupVaultMaxRetentionDays") != 365
    ):
        raise DeploymentEvidenceError(
            "recovery validation does not prove required production controls"
        )
    transition = _object(_read_json(transition_path), "recovery transition")
    endpoint = _object(transition.get("endpoint"), "recovery transition endpoint")
    control = _object(
        transition.get("controlPlane"),
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
        or transition.get("selectedTable") != selected
        or endpoint.get("name") != "production"
        or endpoint.get("status") != "READY"
        or endpoint.get("version")
        != _required_output(runtime, "RuntimeVersion", AGENTCORE_STACK)
        or endpoint.get("arn")
        != _required_output(runtime, "RuntimeEndpointArn", AGENTCORE_STACK)
        or control.get("agentCoreStackName") != AGENTCORE_STACK
        or control.get("recoveryMode") != "normal"
        or control.get("selectedTable") != selected
        or control.get("pendingCount") != 0
        or not isinstance(control.get("desiredCount"), int)
        or control["desiredCount"] < 1
        or control.get("runningCount") != control["desiredCount"]
    ):
        raise DeploymentEvidenceError(
            "recovery transition is not stable in normal production mode"
        )
    return {
        "validation": recovery,
        "transition": transition,
    }


def _certification(
    path: Path,
    *,
    runtime: dict[str, str],
) -> dict[str, Any]:
    report = _object(_read_json(path), "certification report")
    endpoint = _object(report.get("endpoint"), "certification endpoint")
    summary = _object(report.get("summary"), "certification summary")
    checks = report.get("checks")
    if (
        report.get("schema") != CERTIFICATION_SCHEMA
        or report.get("overallStatus") != "PASS"
        or not isinstance(checks, list)
        or not checks
        or any(
            not isinstance(check, dict) or check.get("passed") is not True
            for check in checks
        )
        or summary.get("failed") != 0
        or summary.get("passed") != summary.get("checkCount")
        or summary.get("agentcoreHttpsInvoked") is not True
        or summary.get("queryBackendExercised") is not True
        or endpoint.get("runtimeArn")
        != _required_output(runtime, "RuntimeArn", AGENTCORE_STACK)
        or endpoint.get("endpointArn")
        != _required_output(runtime, "RuntimeEndpointArn", AGENTCORE_STACK)
        or endpoint.get("endpointName")
        != _required_output(runtime, "RuntimeEndpointName", AGENTCORE_STACK)
        or endpoint.get("runtimeVersion")
        != _required_output(runtime, "RuntimeVersion", AGENTCORE_STACK)
        or endpoint.get("status") != "READY"
    ):
        raise DeploymentEvidenceError(
            "direct AgentCore certification did not prove promotion readiness"
        )
    return report


def _validate_stack_bindings(
    *,
    identity: dict[str, str],
    runtime: dict[str, str],
    control: dict[str, str],
    provider: dict[str, Any],
    agentcore_image: str,
) -> None:
    for output in (
        "OidcIssuer",
        "OidcClientId",
        "OidcAudience",
        "UserPoolId",
    ):
        _required_output(identity, output, IDENTITY_STACK)
    if (
        _required_output(runtime, "RuntimeImageUri", AGENTCORE_STACK)
        != agentcore_image
        or _required_output(runtime, "ProviderSecretVersion", AGENTCORE_STACK)
        != provider["versionId"]
        or _required_output(runtime, "RecoveryCutoverMode", AGENTCORE_STACK)
        != "normal"
        or _required_output(runtime, "RuntimeEndpointName", AGENTCORE_STACK)
        != "production"
        or not _required_output(
            runtime,
            "RuntimeVersion",
            AGENTCORE_STACK,
        ).isdigit()
    ):
        raise DeploymentEvidenceError(
            "AgentCore outputs are not bound to the verified deployment inputs"
        )
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
        raise DeploymentEvidenceError(
            "control-plane outputs do not match AgentCore recovery state"
        )


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
    if WORKFLOW_REF.fullmatch(args.workflow_ref) is None:
        raise DeploymentEvidenceError("workflow ref must identify a branch workflow")
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
    _validate_stack_bindings(
        identity=identity,
        runtime=runtime,
        control=control,
        provider=provider,
        agentcore_image=args.agentcore_image,
    )

    setup = _object(_read_json(args.setup_config), "setup configuration")
    setup_runtime = _object(setup.get("runtime"), "setup runtime")
    setup_control = _object(setup.get("control_plane"), "setup control plane")
    if (
        setup.get("identity_mode") != "managed-cognito"
        or setup.get("aws_region") != args.region
        or setup_runtime.get("verified_image_uri") != args.agentcore_image
        or setup_control.get("verified_image_uri") != args.fargate_image
    ):
        raise DeploymentEvidenceError(
            "setup configuration is not bound to the verified images"
        )

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
            "runId": args.run_id,
            "runAttempt": args.run_attempt,
            "actor": args.actor,
            "actorId": args.actor_id,
            "triggeringActor": args.triggering_actor,
            "generatedAt": datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            ),
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
        "configuration": {
            "setupSha256": _hash(args.setup_config),
            "certificationSha256": _hash(args.certification_config),
        },
        "stacks": {
            "identity": identity,
            "runtime": runtime,
            "controlPlane": control,
        },
        "providerSecret": provider,
        "recovery": recovery,
        "certification": certification,
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
        raise DeploymentEvidenceError(
            f"cannot write deployment evidence: {path}"
        ) from exc
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
        },
        "deployment evidence",
    )
    if value.get("schema") != SCHEMA:
        raise DeploymentEvidenceError("deployment evidence schema is unsupported")
    deployment = _object(value["deployment"], "deployment")
    release = _object(value["release"], "release")
    images = _object(value["images"], "images")
    agentcore = _object(images.get("agentcore"), "AgentCore image")
    control = _object(images.get("controlPlane"), "control-plane image")
    expected = {
        "repository": args.repository,
        "commit": args.deployment_commit,
        "runId": args.run_id,
        "runAttempt": args.run_attempt,
    }
    if any(deployment.get(name) != item for name, item in expected.items()):
        raise DeploymentEvidenceError("deployment identity does not match expectation")
    if release.get("commit") != args.release_commit:
        raise DeploymentEvidenceError("release commit does not match expectation")
    if (
        agentcore.get("reference") != args.agentcore_image
        or control.get("reference") != args.fargate_image
    ):
        raise DeploymentEvidenceError("deployment images do not match expectation")
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
    create.add_argument("--actor", required=True)
    create.add_argument("--actor-id", required=True)
    create.add_argument("--triggering-actor", required=True)
    create.add_argument("--change-id", required=True)
    create.add_argument("--operation", choices=("deploy", "rollback"), required=True)
    create.add_argument("--region", required=True)
    create.add_argument("--setup-config", required=True, type=Path)
    create.add_argument("--certification-config", required=True, type=Path)
    create.add_argument("--identity-outputs", required=True, type=Path)
    create.add_argument("--runtime-outputs", required=True, type=Path)
    create.add_argument("--control-outputs", required=True, type=Path)
    create.add_argument("--provider-secret", required=True, type=Path)
    create.add_argument("--recovery-report", required=True, type=Path)
    create.add_argument("--transition-report", required=True, type=Path)
    create.add_argument("--certification-report", required=True, type=Path)

    verify = commands.add_parser("verify")
    _common_expected(verify)
    verify.add_argument("--evidence", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "create":
            value = create_evidence(args)
            _atomic_write(args.output, value)
            print(f"deployment evidence created: {args.output}")
        else:
            verify_evidence(args)
            print(f"deployment evidence verified: {args.evidence}")
    except DeploymentEvidenceError as exc:
        print(f"deployment evidence failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
