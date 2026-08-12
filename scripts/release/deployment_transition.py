#!/usr/bin/env python3
"""Create and verify append-only AgentCore transition terminal records."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Sequence


SCHEMA = "axonllm.agentcore-deployment-transition/v1"
RECOVERY_BINDING_SCHEMA = "axonllm.agentcore-deployment-transition-recovery/v1"
MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_S3_OBJECT_BYTES = 16 * 1024 * 1024
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
RUN_ID = re.compile(r"^[1-9][0-9]*$")
VERSION = re.compile(r"^[1-9][0-9]{0,31}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
CHANGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{2,127}$")
TRANSITION_ID = re.compile(r"^[0-9a-f]{64}$")
S3_PRESENT = "present"
S3_ABSENT = "absent"
S3_INDETERMINATE = "indeterminate"


class TransitionRecordError(RuntimeError):
    """Raised when a transition journal record is unsafe or inconsistent."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise TransitionRecordError(f"duplicate JSON field: {key}")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise TransitionRecordError(f"invalid JSON constant: {value}")


def _read_json(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise TransitionRecordError(f"input must be a regular non-symlink file: {path}")
        if before.st_size > MAX_INPUT_BYTES:
            raise TransitionRecordError(f"input is too large: {path}")
        raw = path.read_bytes()
        after = path.stat()
    except TransitionRecordError:
        raise
    except OSError as exc:
        raise TransitionRecordError(f"cannot read input: {path}") from exc
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or len(raw) != after.st_size
    ):
        raise TransitionRecordError(f"input changed while reading: {path}")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TransitionRecordError(f"input is not strict UTF-8 JSON: {path}") from exc
    if type(value) is not dict:
        raise TransitionRecordError(f"input must be a JSON object: {path}")
    return value, raw


def _intent(path: Path) -> tuple[dict[str, Any], str]:
    value, raw = _read_json(path)
    schema_version = value.get("schemaVersion")
    candidate = value.get("candidateRuntimeVersion")
    previous = value.get("previousProductionRuntimeVersion")
    if (
        schema_version not in {1, 2, 3}
        or not isinstance(candidate, str)
        or VERSION.fullmatch(candidate) is None
        or (
            previous is not None
            and (not isinstance(previous, str) or VERSION.fullmatch(previous) is None or previous == candidate)
        )
    ):
        raise TransitionRecordError("promotion intent is malformed")
    if schema_version == 3:
        transition = value.get("transition")
        expected = {
            "changeId",
            "deploymentCommit",
            "repository",
            "rollbackNotBefore",
            "runAttempt",
            "runId",
            "transitionId",
        }
        if (
            not isinstance(transition, dict)
            or set(transition) != expected
            or any(not isinstance(transition.get(name), str) for name in expected)
            or CHANGE_ID.fullmatch(transition["changeId"]) is None
            or COMMIT.fullmatch(transition["deploymentCommit"]) is None
            or REPOSITORY.fullmatch(transition["repository"]) is None
            or _timestamp(
                transition["rollbackNotBefore"],
                "promotion intent rollbackNotBefore",
            )
            is None
            or RUN_ID.fullmatch(transition["runAttempt"]) is None
            or RUN_ID.fullmatch(transition["runId"]) is None
            or TRANSITION_ID.fullmatch(transition["transitionId"]) is None
        ):
            raise TransitionRecordError("promotion intent transition identity is malformed")
    return value, hashlib.sha256(raw).hexdigest()


def _timestamp(value: str, location: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise TransitionRecordError(f"{location} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TransitionRecordError(f"{location} is invalid")
    return parsed.astimezone(timezone.utc)


def _identity(
    repository: str,
    run_id: str,
    run_attempt: str,
) -> None:
    if REPOSITORY.fullmatch(repository) is None:
        raise TransitionRecordError("repository must be owner/name")
    if RUN_ID.fullmatch(run_id) is None:
        raise TransitionRecordError("run ID must be a positive integer")
    if RUN_ID.fullmatch(run_attempt) is None:
        raise TransitionRecordError("run attempt must be a positive integer")


def _verify_intent_identity(
    intent: dict[str, Any],
    *,
    repository: str,
    run_id: str,
    run_attempt: str,
    require_bound: bool,
) -> None:
    transition = intent.get("transition")
    if intent.get("schemaVersion") != 3:
        if require_bound:
            raise TransitionRecordError("promotion intent is not bound to a journal identity")
        return
    if (
        not isinstance(transition, dict)
        or transition.get("repository") != repository
        or transition.get("runId") != run_id
        or transition.get("runAttempt") != run_attempt
    ):
        raise TransitionRecordError("promotion intent does not match its journal identity")


def create_record(args: argparse.Namespace) -> dict[str, Any]:
    _identity(args.repository, args.run_id, args.run_attempt)
    intent, digest = _intent(args.intent)
    _verify_intent_identity(
        intent,
        repository=args.repository,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        require_bound=False,
    )
    resulting_version = (
        intent["candidateRuntimeVersion"] if args.outcome == "committed" else intent["previousProductionRuntimeVersion"]
    )
    return {
        "schema": SCHEMA,
        "intentSha256": digest,
        "intentSchemaVersion": intent["schemaVersion"],
        "outcome": args.outcome,
        "repository": args.repository,
        "runId": args.run_id,
        "runAttempt": args.run_attempt,
        "candidateRuntimeVersion": intent["candidateRuntimeVersion"],
        "resultingProductionRuntimeVersion": resulting_version,
        "recordedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def verify_record(args: argparse.Namespace) -> dict[str, Any]:
    _identity(args.repository, args.run_id, args.run_attempt)
    intent, digest = _intent(args.intent)
    _verify_intent_identity(
        intent,
        repository=args.repository,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        require_bound=False,
    )
    value, _ = _read_json(args.record)
    expected_fields = {
        "schema",
        "intentSha256",
        "intentSchemaVersion",
        "outcome",
        "repository",
        "runId",
        "runAttempt",
        "candidateRuntimeVersion",
        "resultingProductionRuntimeVersion",
        "recordedAt",
    }
    outcome = value.get("outcome")
    resulting_version = (
        intent["candidateRuntimeVersion"] if outcome == "committed" else intent["previousProductionRuntimeVersion"]
    )
    if (
        set(value) != expected_fields
        or value.get("schema") != SCHEMA
        or value.get("intentSha256") != digest
        or value.get("intentSchemaVersion") != intent["schemaVersion"]
        or outcome not in {"committed", "rolled-back"}
        or (args.outcome is not None and outcome != args.outcome)
        or value.get("repository") != args.repository
        or value.get("runId") != args.run_id
        or value.get("runAttempt") != args.run_attempt
        or value.get("candidateRuntimeVersion") != intent["candidateRuntimeVersion"]
        or value.get("resultingProductionRuntimeVersion") != resulting_version
        or not isinstance(value.get("recordedAt"), str)
        or not value["recordedAt"].endswith("+00:00")
    ):
        raise TransitionRecordError("transition terminal record does not match its intent")
    return value


def verify_intent(args: argparse.Namespace) -> dict[str, Any]:
    _identity(args.repository, args.run_id, args.run_attempt)
    intent, _ = _intent(args.intent)
    _verify_intent_identity(
        intent,
        repository=args.repository,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        require_bound=True,
    )
    return intent


def create_recovery_binding(args: argparse.Namespace) -> dict[str, Any]:
    _identity(args.repository, args.run_id, args.run_attempt)
    intent, intent_digest = _intent(args.intent)
    _verify_intent_identity(
        intent,
        repository=args.repository,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        require_bound=True,
    )
    _, setup_raw = _read_json(args.setup_config)
    return {
        "schema": RECOVERY_BINDING_SCHEMA,
        "intentSha256": intent_digest,
        "setupConfigSha256": hashlib.sha256(setup_raw).hexdigest(),
        "repository": args.repository,
        "runId": args.run_id,
        "runAttempt": args.run_attempt,
        "recordedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def verify_recovery_binding(args: argparse.Namespace) -> dict[str, Any]:
    expected = create_recovery_binding(args)
    value, _ = _read_json(args.binding)
    expected_fields = set(expected)
    if (
        set(value) != expected_fields
        or any(value.get(name) != expected[name] for name in expected_fields - {"recordedAt"})
        or not isinstance(value.get("recordedAt"), str)
        or not value["recordedAt"].endswith("+00:00")
    ):
        raise TransitionRecordError("transition recovery binding does not match its intent and setup")
    return value


def _atomic_write_bytes(path: Path, encoded: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
        raise TransitionRecordError(f"cannot write transition record: {path}") from exc
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    encoded = (
        json.dumps(
            value,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    _atomic_write_bytes(path, encoded)


def _aws_error_code(exc: Exception) -> str | None:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    error = response.get("Error")
    if not isinstance(error, dict):
        return None
    code = error.get("Code")
    return code if isinstance(code, str) else None


def fetch_s3_object(
    args: argparse.Namespace,
    *,
    s3_client: Any | None = None,
) -> str:
    """Fetch one locked object without conflating absence with AWS failure."""
    try:
        args.output.unlink(missing_ok=True)
    except OSError:
        return S3_INDETERMINATE
    if s3_client is None:
        try:
            import boto3

            s3_client = boto3.client("s3")
        except Exception:
            return S3_INDETERMINATE
    try:
        response = s3_client.get_object(
            Bucket=args.bucket,
            Key=args.key,
            ChecksumMode="ENABLED",
        )
    except Exception as exc:
        return S3_ABSENT if _aws_error_code(exc) == "NoSuchKey" else S3_INDETERMINATE
    if (
        not isinstance(response, dict)
        or response.get("ObjectLockMode") != "COMPLIANCE"
        or response.get("ObjectLockRetainUntilDate") is None
        or not isinstance(response.get("ChecksumSHA256"), str)
        or not response["ChecksumSHA256"]
        or not isinstance(response.get("VersionId"), str)
        or not response["VersionId"]
    ):
        return S3_INDETERMINATE
    content_length = response.get("ContentLength")
    body = response.get("Body")
    if (
        not isinstance(content_length, int)
        or isinstance(content_length, bool)
        or content_length < 0
        or content_length > MAX_S3_OBJECT_BYTES
        or body is None
        or not callable(getattr(body, "read", None))
    ):
        return S3_INDETERMINATE
    try:
        raw = body.read(MAX_S3_OBJECT_BYTES + 1)
    except Exception:
        return S3_INDETERMINATE
    finally:
        close = getattr(body, "close", None)
        if callable(close):
            close()
    if not isinstance(raw, bytes) or len(raw) != content_length or len(raw) > MAX_S3_OBJECT_BYTES:
        return S3_INDETERMINATE
    try:
        _atomic_write_bytes(args.output, raw)
    except TransitionRecordError:
        return S3_INDETERMINATE
    return S3_PRESENT


def materialize_record(args: argparse.Namespace) -> bool:
    """Create one record, or verify and preserve its existing exact bytes."""
    if args.output.exists() or args.output.is_symlink():
        verify_record(
            argparse.Namespace(
                intent=args.intent,
                record=args.output,
                outcome=args.outcome,
                repository=args.repository,
                run_id=args.run_id,
                run_attempt=args.run_attempt,
            )
        )
        return False
    _atomic_write(args.output, create_record(args))
    return True


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create or verify an AgentCore transition terminal record",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    verify = commands.add_parser("verify")
    verify_intent_command = commands.add_parser("verify-intent")
    for command in (create, verify, verify_intent_command):
        command.add_argument("--intent", required=True, type=Path)
        command.add_argument("--repository", required=True)
        command.add_argument("--run-id", required=True)
        command.add_argument("--run-attempt", required=True)
    create.add_argument(
        "--outcome",
        choices=("committed", "rolled-back"),
        required=True,
    )
    verify.add_argument(
        "--outcome",
        choices=("committed", "rolled-back"),
    )
    create.add_argument("--output", required=True, type=Path)
    verify.add_argument("--record", required=True, type=Path)
    create_recovery = commands.add_parser("create-recovery-binding")
    verify_recovery = commands.add_parser("verify-recovery-binding")
    for command in (create_recovery, verify_recovery):
        command.add_argument("--intent", required=True, type=Path)
        command.add_argument("--setup-config", required=True, type=Path)
        command.add_argument("--repository", required=True)
        command.add_argument("--run-id", required=True)
        command.add_argument("--run-attempt", required=True)
    create_recovery.add_argument("--output", required=True, type=Path)
    verify_recovery.add_argument("--binding", required=True, type=Path)
    fetch = commands.add_parser("fetch-s3-object")
    fetch.add_argument("--bucket", required=True)
    fetch.add_argument("--key", required=True)
    fetch.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "create":
            created = materialize_record(args)
            action = "written" if created else "reused"
            print(f"Transition terminal record {action}: {args.output}")
        elif args.command == "verify":
            verify_record(args)
            print(f"Transition terminal record verified: {args.record}")
        elif args.command == "verify-intent":
            verify_intent(args)
            print(f"Promotion intent identity verified: {args.intent}")
        elif args.command == "create-recovery-binding":
            _atomic_write(args.output, create_recovery_binding(args))
            print(f"Transition recovery binding written: {args.output}")
        elif args.command == "verify-recovery-binding":
            verify_recovery_binding(args)
            print(f"Transition recovery binding verified: {args.binding}")
        else:
            print(fetch_s3_object(args))
    except TransitionRecordError as exc:
        print(f"Transition record failed: {exc}", file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
