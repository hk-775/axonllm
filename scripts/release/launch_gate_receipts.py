#!/usr/bin/env python3
"""Run production launch gates and publish immutable signed receipts."""

from __future__ import annotations

import argparse
import base64
import binascii
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Mapping, Sequence

import kms_evidence
import launch_rehearsal_evidence as evidence


OPERATION_SCRIPT = "scripts/operations/rehearse_agentcore_launch.py"
TOOL = f"uv:python:{OPERATION_SCRIPT}"
COMMAND_PREFIX = (
    "uv",
    "run",
    "--frozen",
    "--no-sync",
    "python",
    OPERATION_SCRIPT,
)
CLEANUP_ACTION = "cleanup"
COORDINATOR_NOT_DRAINED_EXIT_CODE = 4
EXECUTION_BUNDLE_SCHEMA = "axonllm.agentcore-launch-gate-execution-bundle/v1"
EXECUTION_BUNDLE_FILE = "execution-bundle.json"
RETENTION_DAYS = 2555
COMMAND_TIMEOUT_SECONDS = 30 * 60
OPERATION_BUDGET_SECONDS = 130 * 60
MAX_OUTPUT_BYTES = evidence.MAX_COMMAND_OUTPUT_BYTES
MAX_CONFIG_BYTES = evidence.MAX_INPUT_BYTES
MAX_EXECUTION_BUNDLE_BYTES = 64 * 1024 * 1024
REQUIRED_CHILD_ENVIRONMENT = frozenset(
    {
        "PATH",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    }
)
OPTIONAL_CHILD_ENVIRONMENT = frozenset(
    {
        "HOME",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "AWS_CA_BUNDLE",
        "AWS_EC2_METADATA_DISABLED",
        "UV_CACHE_DIR",
        "XDG_CACHE_HOME",
    }
)
SENSITIVE_ENVIRONMENT_NAME = re.compile(
    r"(?i)(?:secret|token|password|credential|private|api[_-]?key|"
    r"access[_-]?key)"
)


class LaunchGatePublisherError(RuntimeError):
    """Raised when launch-gate evidence cannot be safely published."""


class LaunchGateExecutionError(LaunchGatePublisherError):
    """One gate run failed after cleanup was attempted."""

    def __init__(self, *, failure_stage: str, cleanup_status: str) -> None:
        self.failure_stage = failure_stage
        self.cleanup_status = cleanup_status
        super().__init__("launch-gate execution did not complete")


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True)
class PendingCommand:
    name: str
    argv: tuple[str, ...]
    stdout: bytes
    stderr: bytes
    started_at: str
    completed_at: str


@dataclass(frozen=True)
class FileSnapshot:
    device: int
    inode: int
    size: int
    modified_ns: int
    mode: int
    sha256: str


@dataclass(frozen=True)
class ValidatedExecutionBundle:
    status: str
    failure_stage: str | None
    cleanup_status: str
    started_at: datetime
    completed_at: datetime
    commands: dict[str, list[PendingCommand]]
    cleanup_output: dict[str, Any] | None


Runner = Callable[[Sequence[str], Path, Mapping[str, str]], CommandResult]
Clock = Callable[[], datetime]
Signer = Callable[[Path, Path, str], Any]
Verifier = Callable[[Path, Path, str], None]
CheckoutVerifier = Callable[[Path, str], None]


def _utc_now(clock: Clock) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise LaunchGatePublisherError("clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        process.kill()
    try:
        process.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        process.kill()


def _run_command(
    argv: Sequence[str],
    cwd: Path,
    environment: Mapping[str, str],
) -> CommandResult:
    """Run one fixed command while bounding both captured streams."""

    try:
        process = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        raise LaunchGatePublisherError("cannot start launch-gate operation") from exc
    if process.stdout is None or process.stderr is None:
        _terminate(process)
        raise LaunchGatePublisherError("cannot capture launch-gate operation output")

    streams = {
        process.stdout: bytearray(),
        process.stderr: bytearray(),
    }
    selector = selectors.DefaultSelector()
    deadline = time.monotonic() + COMMAND_TIMEOUT_SECONDS
    try:
        for stream in streams:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LaunchGatePublisherError("launch-gate operation timed out")
            events = selector.select(timeout=min(remaining, 1.0))
            if not events and process.poll() is not None:
                continue
            for key, _ in events:
                stream = key.fileobj
                try:
                    chunk = os.read(stream.fileno(), 64 * 1024)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(stream)
                    stream.close()
                    continue
                captured = streams[stream]
                captured.extend(chunk)
                if len(captured) > MAX_OUTPUT_BYTES:
                    raise LaunchGatePublisherError("launch-gate operation output exceeds the size limit")
        remaining = max(0.0, deadline - time.monotonic())
        exit_code = process.wait(timeout=remaining)
    except BaseException:
        _terminate(process)
        raise
    finally:
        selector.close()
        for stream in streams:
            if not stream.closed:
                stream.close()
    return CommandResult(
        exit_code=exit_code,
        stdout=bytes(streams[process.stdout]),
        stderr=bytes(streams[process.stderr]),
    )


def _read_stable_regular(path: Path, *, maximum: int) -> tuple[bytes, FileSnapshot]:
    try:
        before = path.lstat()
    except OSError as exc:
        raise LaunchGatePublisherError(f"cannot inspect required file: {path}") from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or stat.S_IMODE(before.st_mode) & 0o077
    ):
        raise LaunchGatePublisherError(f"required file must be owner-only, regular, and non-symlink: {path}")
    try:
        raw = evidence._read_regular(path, maximum=maximum)
        after = path.lstat()
    except (OSError, evidence.LaunchRehearsalError) as exc:
        raise LaunchGatePublisherError(f"cannot safely read required file: {path}") from exc
    snapshot = FileSnapshot(
        device=after.st_dev,
        inode=after.st_ino,
        size=after.st_size,
        modified_ns=after.st_mtime_ns,
        mode=stat.S_IMODE(after.st_mode),
        sha256=hashlib.sha256(raw).hexdigest(),
    )
    if (
        before.st_dev != snapshot.device
        or before.st_ino != snapshot.inode
        or before.st_size != snapshot.size
        or before.st_mtime_ns != snapshot.modified_ns
        or before.st_uid != after.st_uid
        or after.st_uid != os.getuid()
        or stat.S_IMODE(before.st_mode) != snapshot.mode
        or snapshot.mode & 0o077
    ):
        raise LaunchGatePublisherError(f"required file changed while reading: {path}")
    return raw, snapshot


def _require_same_file(
    path: Path,
    expected: FileSnapshot,
    *,
    maximum: int,
    location: str,
) -> None:
    _, current = _read_stable_regular(path, maximum=maximum)
    if current != expected:
        raise LaunchGatePublisherError(f"{location} changed during processing")


def _validate_local_inputs(
    reviewed_config: Path,
    state_dir: Path,
) -> FileSnapshot:
    raw, snapshot = _read_stable_regular(
        reviewed_config,
        maximum=MAX_CONFIG_BYTES,
    )
    try:
        config = evidence._strict_json_bytes(
            raw,
            location="reviewed launch-gate config",
        )
    except evidence.LaunchRehearsalError as exc:
        raise LaunchGatePublisherError("reviewed launch-gate config is not strict JSON") from exc
    if not isinstance(config, dict):
        raise LaunchGatePublisherError("reviewed launch-gate config must be a JSON object")
    try:
        directory = state_dir.lstat()
    except OSError as exc:
        raise LaunchGatePublisherError("cannot inspect launch-gate state directory") from exc
    if (
        stat.S_ISLNK(directory.st_mode)
        or not stat.S_ISDIR(directory.st_mode)
        or directory.st_uid != os.getuid()
        or stat.S_IMODE(directory.st_mode) != 0o700
    ):
        raise LaunchGatePublisherError("launch-gate state directory must be owner-owned mode 0700")
    return snapshot


def _verify_operation_checkout(
    operation_root: Path,
    workflow_commit: str,
) -> None:
    try:
        root = operation_root.lstat()
        script = (operation_root / OPERATION_SCRIPT).lstat()
    except OSError as exc:
        raise LaunchGatePublisherError("protected operation checkout is incomplete") from exc
    if (
        stat.S_ISLNK(root.st_mode)
        or not stat.S_ISDIR(root.st_mode)
        or stat.S_ISLNK(script.st_mode)
        or not stat.S_ISREG(script.st_mode)
    ):
        raise LaunchGatePublisherError("protected operation checkout is unsafe")
    try:
        completed = subprocess.run(
            ["git", "-C", str(operation_root), "rev-parse", "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="ascii",
            errors="strict",
            timeout=30,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise LaunchGatePublisherError("cannot verify protected operation checkout") from exc
    if completed.stdout.strip() != workflow_commit:
        raise LaunchGatePublisherError("operation checkout does not match the protected workflow commit")


def _sensitive_values(environment: Mapping[str, str]) -> tuple[bytes, ...]:
    values: set[bytes] = set()
    for name, value in environment.items():
        if SENSITIVE_ENVIRONMENT_NAME.search(name) is not None and isinstance(value, str) and len(value) >= 8:
            try:
                values.add(value.encode("utf-8"))
            except UnicodeError as exc:
                raise LaunchGatePublisherError("sensitive environment value is not valid UTF-8") from exc
    return tuple(sorted(values))


def _child_environment(environment: Mapping[str, str]) -> dict[str, str]:
    missing = sorted(
        name
        for name in REQUIRED_CHILD_ENVIRONMENT
        if not isinstance(environment.get(name), str) or not environment[name]
    )
    if missing:
        raise LaunchGatePublisherError("launch-gate child environment is missing required credentials")
    allowed = REQUIRED_CHILD_ENVIRONMENT | OPTIONAL_CHILD_ENVIRONMENT
    result = {name: value for name, value in environment.items() if name in allowed and isinstance(value, str)}
    result["PYTHONNOUSERSITE"] = "1"
    result["PYTHONUNBUFFERED"] = "1"
    return result


def _reject_secret_material(
    values: Sequence[bytes],
    *,
    argv: Sequence[str] = (),
    streams: Sequence[bytes] = (),
) -> None:
    encoded_argv = tuple(argument.encode("utf-8") for argument in argv)
    for secret in values:
        if any(secret in argument for argument in encoded_argv) or any(secret in stream for stream in streams):
            raise LaunchGatePublisherError("launch-gate command exposed sensitive environment material")


def _common_arguments(
    *,
    release: Mapping[str, str],
    execution: Mapping[str, str],
    reviewed_config: Path,
    state_dir: Path,
) -> tuple[str, ...]:
    return (
        "--release-commit",
        release["commit"],
        "--agentcore-image",
        release["agentcoreImage"],
        "--control-plane-image",
        release["controlPlaneImage"],
        "--region",
        release["region"],
        "--repository",
        execution["repository"],
        "--workflow-ref",
        execution["workflowRef"],
        "--workflow-commit",
        execution["workflowCommit"],
        "--run-id",
        execution["runId"],
        "--run-attempt",
        execution["runAttempt"],
        "--reviewed-config",
        str(reviewed_config),
        "--reviewed-config-s3-uri",
        execution["reviewedConfigS3Uri"],
        "--reviewed-config-version-id",
        execution["reviewedConfigVersionId"],
        "--reviewed-config-sha256",
        execution["reviewedConfigSha256"],
        "--state-dir",
        str(state_dir),
    )


def _command_argv(
    action: str,
    *,
    gate: str | None,
    common: Sequence[str],
) -> tuple[str, ...]:
    arguments = (*COMMAND_PREFIX, action)
    if gate is not None:
        arguments = (*arguments, "--gate", gate)
    arguments = (*arguments, *common)
    if len(arguments) > 64 or any(
        not argument
        or len(argument) > 512
        or any(ord(character) < 32 or ord(character) == 127 for character in argument)
        for argument in arguments
    ):
        raise LaunchGatePublisherError("launch-gate command arguments are malformed")
    return arguments


def _validate_result(result: CommandResult) -> None:
    if (
        not isinstance(result, CommandResult)
        or not isinstance(result.exit_code, int)
        or isinstance(result.exit_code, bool)
        or not isinstance(result.stdout, bytes)
        or not isinstance(result.stderr, bytes)
        or len(result.stdout) > MAX_OUTPUT_BYTES
        or len(result.stderr) > MAX_OUTPUT_BYTES
    ):
        raise LaunchGatePublisherError("launch-gate runner returned an invalid result")


def _validate_action_output(
    raw: bytes,
    *,
    gate: str,
    action: str,
    final: bool,
    release: Mapping[str, str],
    execution: Mapping[str, str],
) -> None:
    try:
        value = evidence._strict_json_bytes(
            raw,
            location=f"{gate} {action} stdout",
        )
        output = evidence._object(value, f"{gate} {action} stdout")
        evidence._exact_fields(
            output,
            {
                "schema",
                "gate",
                "action",
                "release",
                "execution",
                "observations",
            },
            f"{gate} {action} stdout",
        )
        if (
            output.get("schema") != evidence.COMMAND_OUTPUT_SCHEMA
            or output.get("gate") != gate
            or output.get("action") != action
            or output.get("release") != dict(release)
            or output.get("execution") != dict(execution)
        ):
            raise LaunchGatePublisherError("launch-gate stdout is not bound to its command")
        observations = output.get("observations")
        if not final:
            if observations is not None:
                raise LaunchGatePublisherError("non-final launch-gate command emitted observations")
        else:
            normalized = evidence._validate_observations(
                gate,
                observations,
                release=release,
            )
            if normalized != observations:
                raise LaunchGatePublisherError("final launch-gate observations are not normalized")
    except evidence.LaunchRehearsalError as exc:
        raise LaunchGatePublisherError("launch-gate stdout is invalid") from exc


def _validate_cleanup_output(
    raw: bytes,
    *,
    release: Mapping[str, str],
    execution: Mapping[str, str],
) -> dict[str, Any]:
    try:
        value = evidence._strict_json_bytes(
            raw,
            location="launch-gate cleanup stdout",
        )
        output = evidence._object(value, "launch-gate cleanup stdout")
        evidence._exact_fields(
            output,
            {
                "schema",
                "gate",
                "action",
                "release",
                "execution",
                "observations",
            },
            "launch-gate cleanup stdout",
        )
        if (
            output.get("schema") != evidence.COMMAND_OUTPUT_SCHEMA
            or output.get("gate") != "cleanup"
            or output.get("action") != CLEANUP_ACTION
            or output.get("release") != dict(release)
            or output.get("execution") != dict(execution)
        ):
            raise LaunchGatePublisherError("launch-gate cleanup stdout is not bound to this execution")
        observations = evidence._object(
            output.get("observations"),
            "launch-gate cleanup observations",
        )
        expected = {
            "restoredSnapshotRefs",
            "clearedFaultIds",
            "clearedFixtureIds",
            "redrivenDlqCorrelationIds",
            "removedDlqCorrelationIds",
            "primaryStateSelected",
            "productionEndpointStatus",
            "faultsRemaining",
            "fixturesRemaining",
            "correlatedDlqMessagesRemaining",
        }
        evidence._exact_fields(
            observations,
            expected,
            "launch-gate cleanup observations",
        )
        for name in (
            "restoredSnapshotRefs",
            "clearedFaultIds",
            "clearedFixtureIds",
            "redrivenDlqCorrelationIds",
            "removedDlqCorrelationIds",
        ):
            values = observations.get(name)
            if (
                type(values) is not list
                or any(not isinstance(item, str) for item in values)
                or values != sorted(values)
                or len(values) != len(set(values))
            ):
                raise LaunchGatePublisherError("launch-gate cleanup inventory is malformed")
        if (
            observations.get("primaryStateSelected") is not True
            or observations.get("productionEndpointStatus") != "READY"
            or observations.get("faultsRemaining") != 0
            or observations.get("fixturesRemaining") != 0
            or observations.get("correlatedDlqMessagesRemaining") != 0
        ):
            raise LaunchGatePublisherError("launch-gate cleanup left owned effects behind")
        return output
    except evidence.LaunchRehearsalError as exc:
        raise LaunchGatePublisherError("launch-gate cleanup stdout is invalid") from exc


def _execute_operations(
    *,
    release: Mapping[str, str],
    execution: Mapping[str, str],
    reviewed_config: Path,
    state_dir: Path,
    operation_root: Path,
    config_snapshot: FileSnapshot,
    runner: Runner,
    clock: Clock,
    environment: Mapping[str, str],
) -> tuple[dict[str, list[PendingCommand]], dict[str, Any]]:
    common = _common_arguments(
        release=release,
        execution=execution,
        reviewed_config=reviewed_config,
        state_dir=state_dir,
    )
    secrets = _sensitive_values(environment)
    commands: dict[str, list[PendingCommand]] = {gate: [] for gate in evidence.ALL_GATES}
    operation_failure: BaseException | None = None
    cleanup_failure: BaseException | None = None
    cleanup_output: dict[str, Any] | None = None
    cleanup_safe = True
    previous_completed: datetime | None = None
    operation_deadline = _utc_now(clock) + timedelta(seconds=OPERATION_BUDGET_SECONDS)
    try:
        for gate in evidence.ALL_GATES:
            actions = evidence.EXPECTED_COMMANDS[gate]
            for index, action in enumerate(actions):
                _require_same_file(
                    reviewed_config,
                    config_snapshot,
                    maximum=MAX_CONFIG_BYTES,
                    location="reviewed launch-gate config",
                )
                argv = _command_argv(action, gate=gate, common=common)
                _reject_secret_material(secrets, argv=argv)
                started = _utc_now(clock)
                if started >= operation_deadline:
                    raise LaunchGatePublisherError("launch-gate operation budget is exhausted")
                if previous_completed is not None and started < previous_completed:
                    raise LaunchGatePublisherError("launch-gate command clock moved backwards")
                result = runner(argv, operation_root, environment)
                completed = _utc_now(clock)
                if (
                    isinstance(result, CommandResult)
                    and type(result.exit_code) is int
                    and result.exit_code
                    == COORDINATOR_NOT_DRAINED_EXIT_CODE
                ):
                    cleanup_safe = False
                    raise LaunchGatePublisherError(
                        "launch-gate coordinator execution was not drained"
                    )
                _validate_result(result)
                _reject_secret_material(
                    secrets,
                    streams=(result.stdout, result.stderr),
                )
                if result.exit_code != 0:
                    raise LaunchGatePublisherError("launch-gate command failed")
                if result.stderr:
                    raise LaunchGatePublisherError("launch-gate command wrote to stderr")
                if completed < started:
                    raise LaunchGatePublisherError("launch-gate command completion precedes its start")
                if completed > operation_deadline:
                    raise LaunchGatePublisherError("launch-gate operation exceeded its global deadline")
                _validate_action_output(
                    result.stdout,
                    gate=gate,
                    action=action,
                    final=index == len(actions) - 1,
                    release=release,
                    execution=execution,
                )
                commands[gate].append(
                    PendingCommand(
                        name=action,
                        argv=argv,
                        stdout=result.stdout,
                        stderr=result.stderr,
                        started_at=_timestamp(started),
                        completed_at=_timestamp(completed),
                    )
                )
                previous_completed = completed
    except BaseException as exc:
        operation_failure = exc
    finally:
        cleanup_argv = _command_argv(CLEANUP_ACTION, gate=None, common=common)
        try:
            if not cleanup_safe:
                raise LaunchGatePublisherError(
                    "launch-gate cleanup was suppressed because the "
                    "coordinator execution was not drained"
                )
            _reject_secret_material(secrets, argv=cleanup_argv)
            cleanup = runner(cleanup_argv, operation_root, environment)
            _validate_result(cleanup)
            _reject_secret_material(
                secrets,
                streams=(cleanup.stdout, cleanup.stderr),
            )
            if cleanup.exit_code != 0 or cleanup.stderr:
                raise LaunchGatePublisherError("launch-gate cleanup operation failed")
            cleanup_output = _validate_cleanup_output(
                cleanup.stdout,
                release=release,
                execution=execution,
            )
            _require_same_file(
                reviewed_config,
                config_snapshot,
                maximum=MAX_CONFIG_BYTES,
                location="reviewed launch-gate config",
            )
            _validate_local_inputs(reviewed_config, state_dir)
        except BaseException as exc:
            cleanup_failure = exc
    if operation_failure is not None or cleanup_failure is not None:
        if operation_failure is not None and cleanup_failure is not None:
            stage = "operation-and-cleanup"
        elif cleanup_failure is not None:
            stage = "cleanup"
        else:
            stage = "operation"
        raise LaunchGateExecutionError(
            failure_stage=stage,
            cleanup_status=("FAILED" if cleanup_failure is not None else "SUCCEEDED"),
        ) from (cleanup_failure or operation_failure)
    if cleanup_output is None:
        raise LaunchGatePublisherError("launch-gate cleanup did not produce terminal evidence")
    return commands, cleanup_output


def _parse_retention(value: Any, *, location: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise LaunchGatePublisherError(f"{location} is malformed") from exc
    else:
        raise LaunchGatePublisherError(f"{location} is malformed")
    if parsed.tzinfo is None:
        raise LaunchGatePublisherError(f"{location} is missing a timezone")
    return parsed.astimezone(timezone.utc)


def _require_locked_bucket(
    s3_client: Any,
    bucket: str,
    *,
    expected_owner: str,
) -> None:
    try:
        versioning = s3_client.get_bucket_versioning(
            Bucket=bucket,
            ExpectedBucketOwner=expected_owner,
        )
        object_lock = s3_client.get_object_lock_configuration(
            Bucket=bucket,
            ExpectedBucketOwner=expected_owner,
        )
    except Exception as exc:
        raise LaunchGatePublisherError("cannot verify evidence bucket versioning and Object Lock") from exc
    configuration = object_lock.get("ObjectLockConfiguration") if isinstance(object_lock, dict) else None
    if (
        not isinstance(versioning, dict)
        or versioning.get("Status") != "Enabled"
        or not isinstance(configuration, dict)
        or configuration.get("ObjectLockEnabled") != "Enabled"
    ):
        raise LaunchGatePublisherError("evidence bucket must have versioning and Object Lock enabled")


def _destination(
    uri: str,
    *,
    bucket: str,
    prefix: str,
    location: str,
) -> tuple[str, str]:
    try:
        normalized, parsed_bucket, key = evidence._parse_s3_uri(uri, location)
    except evidence.LaunchRehearsalError as exc:
        raise LaunchGatePublisherError(f"{location} is malformed") from exc
    if normalized != uri or parsed_bucket != bucket or not key.startswith(f"{prefix}/"):
        raise LaunchGatePublisherError(f"{location} is outside the approved evidence prefix")
    return normalized, key


class _LockedUploader:
    def __init__(
        self,
        *,
        s3_client: Any,
        bucket: str,
        prefix: str,
        storage_key_arn: str,
        expected_owner: str,
        retain_until: datetime,
        clock: Clock,
    ) -> None:
        self._client = s3_client
        self._bucket = bucket
        self._prefix = prefix
        self._storage_key_arn = storage_key_arn
        self._expected_owner = expected_owner
        self._retain_until = retain_until
        self._clock = clock
        self._keys: set[str] = set()
        self._identities: set[tuple[str, str]] = set()

    def upload(
        self,
        payload: bytes,
        *,
        key: str,
        content_type: str,
        append_key: bool = False,
    ) -> evidence.S3Reference:
        uri = f"s3://{self._bucket}/{key}"
        try:
            normalized, parsed_bucket, parsed_key = evidence._parse_s3_uri(
                uri,
                "published launch-gate object",
            )
        except evidence.LaunchRehearsalError as exc:
            raise LaunchGatePublisherError("published object key is malformed") from exc
        if (
            normalized != uri
            or parsed_bucket != self._bucket
            or parsed_key != key
            or not key.startswith(f"{self._prefix}/")
            or key in self._keys
        ):
            raise LaunchGatePublisherError("published object key is duplicate or unsafe")
        self._keys.add(key)

        digest_bytes = hashlib.sha256(payload).digest()
        digest = digest_bytes.hex()
        checksum = base64.b64encode(digest_bytes).decode("ascii")
        request: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": key,
            "Body": payload,
            "ContentLength": len(payload),
            "ContentType": content_type,
            "ChecksumAlgorithm": "SHA256",
            "ChecksumSHA256": checksum,
            "ServerSideEncryption": "aws:kms",
            "SSEKMSKeyId": self._storage_key_arn,
            "ObjectLockMode": "COMPLIANCE",
            "ObjectLockRetainUntilDate": self._retain_until,
            "ExpectedBucketOwner": self._expected_owner,
        }
        if not append_key:
            request["IfNoneMatch"] = "*"
        try:
            response = self._client.put_object(**request)
        except Exception as exc:
            raise LaunchGatePublisherError("cannot publish immutable launch-gate object") from exc
        version_id = response.get("VersionId") if isinstance(response, dict) else None
        if (
            not isinstance(version_id, str)
            or evidence.VERSION_ID.fullmatch(version_id) is None
            or version_id == "null"
            or response.get("ChecksumSHA256") != checksum
        ):
            raise LaunchGatePublisherError("published launch-gate object lacks an immutable checksum version")
        identity = (uri, version_id)
        if identity in self._identities:
            raise LaunchGatePublisherError("published launch-gate object reuses an immutable version")
        self._identities.add(identity)

        try:
            downloaded = self._client.get_object(
                Bucket=self._bucket,
                Key=key,
                VersionId=version_id,
                ChecksumMode="ENABLED",
                ExpectedBucketOwner=self._expected_owner,
            )
            retention_response = self._client.get_object_retention(
                Bucket=self._bucket,
                Key=key,
                VersionId=version_id,
                ExpectedBucketOwner=self._expected_owner,
            )
        except Exception as exc:
            raise LaunchGatePublisherError("cannot read back immutable launch-gate object") from exc
        if not isinstance(downloaded, dict):
            raise LaunchGatePublisherError("immutable object readback is malformed")
        body = downloaded.get("Body")
        close = getattr(body, "close", None)
        if body is None or not callable(getattr(body, "read", None)) or not callable(close):
            raise LaunchGatePublisherError("immutable object readback body is malformed")
        close_error: Exception | None = None
        try:
            readback = body.read(len(payload) + 1)
        except Exception as exc:
            raise LaunchGatePublisherError("cannot read immutable object body") from exc
        finally:
            try:
                close()
            except Exception as exc:
                close_error = exc
        if close_error is not None:
            raise LaunchGatePublisherError("cannot close immutable object readback") from close_error

        retained_until = _parse_retention(
            downloaded.get("ObjectLockRetainUntilDate"),
            location="S3 object retention",
        )
        retention = retention_response.get("Retention") if isinstance(retention_response, dict) else None
        retained_again = _parse_retention(
            retention.get("RetainUntilDate") if isinstance(retention, dict) else None,
            location="S3 retention readback",
        )
        now = _utc_now(self._clock)
        if (
            readback != payload
            or downloaded.get("VersionId") != version_id
            or downloaded.get("ContentLength") != len(payload)
            or downloaded.get("ChecksumSHA256") != checksum
            or downloaded.get("ServerSideEncryption") != "aws:kms"
            or downloaded.get("SSEKMSKeyId") != self._storage_key_arn
            or downloaded.get("ObjectLockMode") != "COMPLIANCE"
            or retained_until < self._retain_until
            or retained_until <= now
            or not isinstance(retention, dict)
            or retention.get("Mode") != "COMPLIANCE"
            or retained_again < self._retain_until
            or retained_again <= now
            or hashlib.sha256(readback).hexdigest() != digest
        ):
            raise LaunchGatePublisherError("published launch-gate object failed immutable readback validation")
        return evidence.S3Reference(
            uri=uri,
            bucket=self._bucket,
            key=key,
            version_id=version_id,
            sha256=digest,
        )


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise LaunchGatePublisherError("launch-gate execution bundle is not canonical JSON") from exc


def _validate_bundle_location(
    execution_bundle: Path,
    state_dir: Path,
) -> None:
    if execution_bundle.name != EXECUTION_BUNDLE_FILE or execution_bundle.parent != state_dir:
        raise LaunchGatePublisherError("execution bundle must use the fixed file in the locked state directory")


def _pending_bundle_value(command: PendingCommand) -> dict[str, Any]:
    return {
        "name": command.name,
        "argv": list(command.argv),
        "stdoutBase64": base64.b64encode(command.stdout).decode("ascii"),
        "stderrBase64": base64.b64encode(command.stderr).decode("ascii"),
        "startedAt": command.started_at,
        "completedAt": command.completed_at,
        "exitCode": 0,
    }


def _write_execution_bundle(
    path: Path,
    value: Mapping[str, Any],
) -> FileSnapshot:
    raw = _canonical_json_bytes(value)
    if len(raw) > MAX_EXECUTION_BUNDLE_BYTES:
        raise LaunchGatePublisherError("launch-gate execution bundle exceeds its size limit")
    try:
        path.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise LaunchGatePublisherError("cannot inspect launch-gate execution bundle output") from exc
    else:
        raise LaunchGatePublisherError("launch-gate execution bundle output already exists")

    temporary: str | None = None
    directory_descriptor = -1
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.link(temporary, path, follow_symlinks=False)
        Path(temporary).unlink()
        temporary = None
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_CLOEXEC", 0)
        directory_descriptor = os.open(path.parent, directory_flags)
        os.fsync(directory_descriptor)
    except OSError as exc:
        raise LaunchGatePublisherError("cannot atomically write launch-gate execution bundle") from exc
    finally:
        if directory_descriptor >= 0:
            os.close(directory_descriptor)
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)

    written, snapshot = _read_stable_regular(
        path,
        maximum=MAX_EXECUTION_BUNDLE_BYTES,
    )
    if written != raw or snapshot.mode != 0o600:
        raise LaunchGatePublisherError("launch-gate execution bundle write was not stable and owner-only")
    return snapshot


def _decode_bundle_stream(value: Any, *, location: str) -> bytes:
    if not isinstance(value, str):
        raise LaunchGatePublisherError(f"{location} is malformed")
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise LaunchGatePublisherError(f"{location} is malformed") from exc
    if len(raw) > MAX_OUTPUT_BYTES or base64.b64encode(raw).decode("ascii") != value:
        raise LaunchGatePublisherError(f"{location} is malformed")
    return raw


def _validate_execution_bundle(
    value: Any,
    *,
    raw: bytes,
    release: Mapping[str, str],
    execution: Mapping[str, str],
    reviewed_config: Path,
    state_dir: Path,
) -> ValidatedExecutionBundle:
    try:
        bundle = evidence._object(value, "launch-gate execution bundle")
        evidence._exact_fields(
            bundle,
            {
                "schema",
                "release",
                "execution",
                "status",
                "failureStage",
                "cleanupStatus",
                "startedAt",
                "completedAt",
                "commands",
                "cleanup",
            },
            "launch-gate execution bundle",
        )
        if (
            raw != _canonical_json_bytes(bundle)
            or bundle.get("schema") != EXECUTION_BUNDLE_SCHEMA
            or bundle.get("release") != dict(release)
            or bundle.get("execution") != dict(execution)
        ):
            raise LaunchGatePublisherError("launch-gate execution bundle binding is invalid")
        started_text, started = evidence._timestamp(
            bundle.get("startedAt"),
            "launch-gate execution bundle startedAt",
        )
        completed_text, completed = evidence._timestamp(
            bundle.get("completedAt"),
            "launch-gate execution bundle completedAt",
        )
        if (
            started_text != bundle.get("startedAt")
            or completed_text != bundle.get("completedAt")
            or completed < started
            or completed - started > timedelta(seconds=OPERATION_BUDGET_SECONDS + COMMAND_TIMEOUT_SECONDS + 120)
        ):
            raise LaunchGatePublisherError("launch-gate execution bundle timing is invalid")

        command_groups = evidence._object(
            bundle.get("commands"),
            "launch-gate execution bundle commands",
        )
        evidence._exact_fields(
            command_groups,
            set(evidence.ALL_GATES),
            "launch-gate execution bundle commands",
        )
        common = _common_arguments(
            release=release,
            execution=execution,
            reviewed_config=reviewed_config,
            state_dir=state_dir,
        )
        commands: dict[str, list[PendingCommand]] = {}
        actual_sequence: list[tuple[str, str]] = []
        previous_completed = started
        for gate in evidence.ALL_GATES:
            raw_commands = command_groups.get(gate)
            if type(raw_commands) is not list:
                raise LaunchGatePublisherError("launch-gate execution bundle command group is malformed")
            commands[gate] = []
            for index, raw_command in enumerate(raw_commands):
                command = evidence._object(
                    raw_command,
                    "launch-gate execution bundle command",
                )
                evidence._exact_fields(
                    command,
                    {
                        "name",
                        "argv",
                        "stdoutBase64",
                        "stderrBase64",
                        "startedAt",
                        "completedAt",
                        "exitCode",
                    },
                    "launch-gate execution bundle command",
                )
                name = command.get("name")
                if not isinstance(name, str):
                    raise LaunchGatePublisherError("launch-gate execution bundle command name is malformed")
                argv = command.get("argv")
                expected_argv = list(_command_argv(name, gate=gate, common=common))
                if argv != expected_argv or command.get("exitCode") != 0:
                    raise LaunchGatePublisherError("launch-gate execution bundle command is not canonical")
                command_started_text, command_started = evidence._timestamp(
                    command.get("startedAt"),
                    "launch-gate execution command startedAt",
                )
                command_completed_text, command_completed = evidence._timestamp(
                    command.get("completedAt"),
                    "launch-gate execution command completedAt",
                )
                if (
                    command_started_text != command.get("startedAt")
                    or command_completed_text != command.get("completedAt")
                    or command_started < previous_completed
                    or command_completed < command_started
                    or command_completed > completed
                ):
                    raise LaunchGatePublisherError("launch-gate execution bundle command timing is invalid")
                stdout = _decode_bundle_stream(
                    command.get("stdoutBase64"),
                    location="launch-gate execution command stdout",
                )
                stderr = _decode_bundle_stream(
                    command.get("stderrBase64"),
                    location="launch-gate execution command stderr",
                )
                if stderr:
                    raise LaunchGatePublisherError("launch-gate execution bundle contains command stderr")
                _validate_action_output(
                    stdout,
                    gate=gate,
                    action=name,
                    final=index == len(evidence.EXPECTED_COMMANDS[gate]) - 1,
                    release=release,
                    execution=execution,
                )
                commands[gate].append(
                    PendingCommand(
                        name=name,
                        argv=tuple(argv),
                        stdout=stdout,
                        stderr=stderr,
                        started_at=command_started_text,
                        completed_at=command_completed_text,
                    )
                )
                actual_sequence.append((gate, name))
                previous_completed = command_completed

        expected_sequence = [
            (gate, action) for gate in evidence.ALL_GATES for action in evidence.EXPECTED_COMMANDS[gate]
        ]
        if actual_sequence != expected_sequence[: len(actual_sequence)]:
            raise LaunchGatePublisherError("launch-gate execution bundle commands are not an ordered prefix")

        status = bundle.get("status")
        failure_stage = bundle.get("failureStage")
        cleanup_status = bundle.get("cleanupStatus")
        cleanup_value = bundle.get("cleanup")
        cleanup_output: dict[str, Any] | None = None
        if status == "PASSED":
            if actual_sequence != expected_sequence or failure_stage is not None or cleanup_status != "SUCCEEDED":
                raise LaunchGatePublisherError("passed launch-gate execution bundle is incomplete")
            cleanup_output = _validate_cleanup_output(
                _canonical_json_bytes(
                    evidence._object(
                        cleanup_value,
                        "launch-gate execution cleanup",
                    )
                ),
                release=release,
                execution=execution,
            )
        elif status == "FAILED":
            if (
                actual_sequence
                or failure_stage not in {"operation", "cleanup", "operation-and-cleanup"}
                or cleanup_status not in {"SUCCEEDED", "FAILED"}
                or cleanup_value is not None
            ):
                raise LaunchGatePublisherError("failed launch-gate execution bundle is malformed")
        else:
            raise LaunchGatePublisherError("launch-gate execution bundle status is invalid")
        return ValidatedExecutionBundle(
            status=status,
            failure_stage=failure_stage,
            cleanup_status=cleanup_status,
            started_at=started,
            completed_at=completed,
            commands=commands,
            cleanup_output=cleanup_output,
        )
    except evidence.LaunchRehearsalError as exc:
        raise LaunchGatePublisherError("launch-gate execution bundle violates its schema") from exc


def _read_execution_bundle(
    path: Path,
    *,
    expected_sha256: str,
    release: Mapping[str, str],
    execution: Mapping[str, str],
    reviewed_config: Path,
    state_dir: Path,
) -> tuple[ValidatedExecutionBundle, FileSnapshot]:
    if evidence.SHA256.fullmatch(expected_sha256) is None:
        raise LaunchGatePublisherError("launch-gate execution bundle digest is malformed")
    raw, snapshot = _read_stable_regular(
        path,
        maximum=MAX_EXECUTION_BUNDLE_BYTES,
    )
    if snapshot.mode != 0o600 or snapshot.sha256 != expected_sha256:
        raise LaunchGatePublisherError("launch-gate execution bundle identity is invalid")
    try:
        value = evidence._strict_json_bytes(
            raw,
            location="launch-gate execution bundle",
        )
    except evidence.LaunchRehearsalError as exc:
        raise LaunchGatePublisherError("launch-gate execution bundle is not strict JSON") from exc
    return (
        _validate_execution_bundle(
            value,
            raw=raw,
            release=release,
            execution=execution,
            reviewed_config=reviewed_config,
            state_dir=state_dir,
        ),
        snapshot,
    )


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    try:
        evidence._atomic_write(path, value)
    except evidence.LaunchRehearsalError as exc:
        raise LaunchGatePublisherError("cannot write local launch-gate evidence") from exc


def _sign_and_verify(
    artifact: Path,
    signature: Path,
    *,
    signing_key_arn: str,
    signer: Signer,
    verifier: Verifier,
) -> None:
    try:
        signer(artifact, signature, signing_key_arn)
        verifier(artifact, signature, signing_key_arn)
        evidence._read_regular(signature, maximum=kms_evidence.MAX_BUNDLE_BYTES)
    except Exception as exc:
        raise LaunchGatePublisherError("KMS launch-gate signature failed") from exc


def _object_key(
    base: str,
    *parts: str,
) -> str:
    key = "/".join((base, *parts))
    try:
        evidence._parse_s3_uri(f"s3://placeholder-bucket/{key}", "evidence key")
    except evidence.LaunchRehearsalError as exc:
        raise LaunchGatePublisherError("generated evidence key is malformed") from exc
    return key


def _publish_failed_terminal(
    *,
    uploader: _LockedUploader,
    base: str,
    release: Mapping[str, str],
    execution: Mapping[str, str],
    failure: LaunchGateExecutionError,
    started_at: datetime,
    completed_at: datetime,
    state_dir: Path,
    signing_key_arn: str,
    signer: Signer,
    verifier: Verifier,
) -> None:
    terminal = {
        "schema": "axonllm.agentcore-launch-gate-terminal/v1",
        "release": dict(release),
        "execution": dict(execution),
        "status": "FAILED",
        "failureStage": failure.failure_stage,
        "cleanupStatus": failure.cleanup_status,
        "startedAt": _timestamp(started_at),
        "completedAt": _timestamp(completed_at),
    }
    try:
        with tempfile.TemporaryDirectory(
            prefix=".failed-terminal-",
            dir=state_dir,
        ) as temporary_name:
            temporary = Path(temporary_name)
            artifact = temporary / "attempt-terminal.json"
            signature = temporary / "attempt-terminal-kms-signature.json"
            _write_json(artifact, terminal)
            _sign_and_verify(
                artifact,
                signature,
                signing_key_arn=signing_key_arn,
                signer=signer,
                verifier=verifier,
            )
            uploader.upload(
                evidence._read_regular(artifact),
                key=_object_key(base, "attempt-terminal.json"),
                content_type="application/json",
            )
            uploader.upload(
                evidence._read_regular(
                    signature,
                    maximum=kms_evidence.MAX_BUNDLE_BYTES,
                ),
                key=_object_key(
                    base,
                    "attempt-terminal-kms-signature.json",
                ),
                content_type="application/json",
            )
    except LaunchGatePublisherError:
        raise
    except (OSError, evidence.LaunchRehearsalError) as exc:
        raise LaunchGatePublisherError("cannot publish failed launch-gate terminal record") from exc


def _validated_release_execution(
    *,
    release_commit: str,
    region: str,
    agentcore_image: str,
    control_plane_image: str,
    repository: str,
    workflow_ref: str,
    workflow_commit: str,
    parent_workflow_ref: str,
    parent_workflow_commit: str,
    run_id: str,
    run_attempt: str,
    reviewed_config_uri: str,
    reviewed_config_version_id: str,
    reviewed_config_sha256: str,
) -> tuple[dict[str, str], dict[str, str]]:
    try:
        release = evidence._expected_release(
            release_commit=release_commit,
            region=region,
            agentcore_image=agentcore_image,
            control_plane_image=control_plane_image,
        )
        execution = evidence._validate_execution(
            {
                "repository": repository,
                "workflowRef": workflow_ref,
                "workflowCommit": workflow_commit,
                "parentWorkflowRef": parent_workflow_ref,
                "parentWorkflowCommit": parent_workflow_commit,
                "checkedOutCommit": workflow_commit,
                "runId": run_id,
                "runAttempt": run_attempt,
                "reviewedConfigS3Uri": reviewed_config_uri,
                "reviewedConfigVersionId": reviewed_config_version_id,
                "reviewedConfigSha256": reviewed_config_sha256,
            },
            release_commit=release_commit,
        )
    except evidence.LaunchRehearsalError as exc:
        raise LaunchGatePublisherError("launch-gate execution binding is invalid") from exc
    return release, execution


def execute_gates(
    *,
    release_commit: str,
    region: str,
    agentcore_image: str,
    control_plane_image: str,
    repository: str,
    workflow_ref: str,
    workflow_commit: str,
    parent_workflow_ref: str,
    parent_workflow_commit: str,
    run_id: str,
    run_attempt: str,
    reviewed_config: Path,
    reviewed_config_uri: str,
    reviewed_config_version_id: str,
    reviewed_config_sha256: str,
    state_dir: Path,
    execution_bundle: Path,
    operation_root: Path,
    runner: Runner | None = None,
    clock: Clock | None = None,
    checkout_verifier: CheckoutVerifier | None = None,
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Run coordinator operations and atomically persist a credential-free bundle."""

    current_clock = clock or (lambda: datetime.now(timezone.utc))
    current_runner = runner or _run_command
    verify_checkout = checkout_verifier or _verify_operation_checkout
    child_environment = _child_environment(os.environ if environment is None else environment)
    release, execution = _validated_release_execution(
        release_commit=release_commit,
        region=region,
        agentcore_image=agentcore_image,
        control_plane_image=control_plane_image,
        repository=repository,
        workflow_ref=workflow_ref,
        workflow_commit=workflow_commit,
        parent_workflow_ref=parent_workflow_ref,
        parent_workflow_commit=parent_workflow_commit,
        run_id=run_id,
        run_attempt=run_attempt,
        reviewed_config_uri=reviewed_config_uri,
        reviewed_config_version_id=reviewed_config_version_id,
        reviewed_config_sha256=reviewed_config_sha256,
    )
    config_snapshot = _validate_local_inputs(reviewed_config, state_dir)
    _validate_bundle_location(execution_bundle, state_dir)
    if config_snapshot.sha256 != execution["reviewedConfigSha256"]:
        raise LaunchGatePublisherError("reviewed launch-gate config does not match its immutable digest")
    verify_checkout(operation_root, workflow_commit)

    attempt_started = _utc_now(current_clock)
    try:
        pending, cleanup_output = _execute_operations(
            release=release,
            execution=execution,
            reviewed_config=reviewed_config,
            state_dir=state_dir,
            operation_root=operation_root,
            config_snapshot=config_snapshot,
            runner=current_runner,
            clock=current_clock,
            environment=child_environment,
        )
        status = "PASSED"
        failure_stage: str | None = None
        cleanup_status = "SUCCEEDED"
        commands = {gate: [_pending_bundle_value(command) for command in pending[gate]] for gate in evidence.ALL_GATES}
        cleanup: dict[str, Any] | None = cleanup_output
    except LaunchGateExecutionError as failure:
        status = "FAILED"
        failure_stage = failure.failure_stage
        cleanup_status = failure.cleanup_status
        commands = {gate: [] for gate in evidence.ALL_GATES}
        cleanup = None
    attempt_completed = _utc_now(current_clock)
    bundle = {
        "schema": EXECUTION_BUNDLE_SCHEMA,
        "release": release,
        "execution": execution,
        "status": status,
        "failureStage": failure_stage,
        "cleanupStatus": cleanup_status,
        "startedAt": _timestamp(attempt_started),
        "completedAt": _timestamp(attempt_completed),
        "commands": commands,
        "cleanup": cleanup,
    }
    snapshot = _write_execution_bundle(execution_bundle, bundle)
    _require_same_file(
        reviewed_config,
        config_snapshot,
        maximum=MAX_CONFIG_BYTES,
        location="reviewed launch-gate config",
    )
    return {
        "execution_bundle_sha256": snapshot.sha256,
        "execution_status": status,
    }


def publish_receipts(
    *,
    release_commit: str,
    region: str,
    agentcore_image: str,
    control_plane_image: str,
    repository: str,
    workflow_ref: str,
    workflow_commit: str,
    parent_workflow_ref: str,
    parent_workflow_commit: str,
    run_id: str,
    run_attempt: str,
    reviewed_config: Path,
    reviewed_config_uri: str,
    reviewed_config_version_id: str,
    reviewed_config_sha256: str,
    state_dir: Path,
    execution_bundle: Path,
    execution_bundle_sha256: str,
    evidence_bucket: str,
    evidence_prefix: str,
    manifest_uri: str,
    manifest_signature_uri: str,
    storage_kms_key_arn: str,
    signing_key_arn: str,
    s3_client: Any | None = None,
    signer: Signer | None = None,
    verifier: Verifier | None = None,
    clock: Clock | None = None,
) -> dict[str, str]:
    """Publish immutable signed evidence from one validated execution bundle."""

    current_clock = clock or (lambda: datetime.now(timezone.utc))
    current_signer = signer or kms_evidence.sign_artifact
    current_verifier = verifier or kms_evidence.verify_artifact
    release, execution = _validated_release_execution(
        release_commit=release_commit,
        region=region,
        agentcore_image=agentcore_image,
        control_plane_image=control_plane_image,
        repository=repository,
        workflow_ref=workflow_ref,
        workflow_commit=workflow_commit,
        parent_workflow_ref=parent_workflow_ref,
        parent_workflow_commit=parent_workflow_commit,
        run_id=run_id,
        run_attempt=run_attempt,
        reviewed_config_uri=reviewed_config_uri,
        reviewed_config_version_id=reviewed_config_version_id,
        reviewed_config_sha256=reviewed_config_sha256,
    )

    try:
        prefix = evidence._validate_prefix(evidence_prefix)
        if evidence.BUCKET.fullmatch(evidence_bucket) is None:
            raise evidence.LaunchRehearsalError("evidence bucket is malformed")
        storage_key = evidence._validate_kms_key(
            storage_kms_key_arn,
            release=release,
            location="evidence storage KMS key",
        )
        signing_key = evidence._validate_kms_key(
            signing_key_arn,
            release=release,
            location="evidence signing KMS key",
        )
    except evidence.LaunchRehearsalError as exc:
        raise LaunchGatePublisherError("launch-gate publication binding is invalid") from exc
    if storage_key == signing_key:
        raise LaunchGatePublisherError("storage and asymmetric signing KMS keys must be distinct")
    image_match = evidence.ECR_IMAGE.fullmatch(release["agentcoreImage"])
    assert image_match is not None
    expected_bucket_owner = image_match.group("account")
    normalized_manifest_uri, manifest_key = _destination(
        manifest_uri,
        bucket=evidence_bucket,
        prefix=prefix,
        location="gate manifest URI",
    )
    normalized_signature_uri, manifest_signature_key = _destination(
        manifest_signature_uri,
        bucket=evidence_bucket,
        prefix=prefix,
        location="gate manifest signature URI",
    )
    if normalized_manifest_uri == normalized_signature_uri or manifest_key == manifest_signature_key:
        raise LaunchGatePublisherError("gate manifest artifact and signature destinations must be distinct")

    config_snapshot = _validate_local_inputs(reviewed_config, state_dir)
    if config_snapshot.sha256 != execution["reviewedConfigSha256"]:
        raise LaunchGatePublisherError("reviewed launch-gate config does not match its immutable digest")
    normalized_config_uri, config_key = _destination(
        execution["reviewedConfigS3Uri"],
        bucket=evidence_bucket,
        prefix=prefix,
        location="reviewed launch-gate config URI",
    )
    if normalized_config_uri != execution["reviewedConfigS3Uri"] or config_key in {
        manifest_key,
        manifest_signature_key,
    }:
        raise LaunchGatePublisherError("reviewed launch-gate config destination is unsafe")
    _validate_bundle_location(execution_bundle, state_dir)
    validated_bundle, bundle_snapshot = _read_execution_bundle(
        execution_bundle,
        expected_sha256=execution_bundle_sha256,
        release=release,
        execution=execution,
        reviewed_config=reviewed_config,
        state_dir=state_dir,
    )
    _require_same_file(
        reviewed_config,
        config_snapshot,
        maximum=MAX_CONFIG_BYTES,
        location="reviewed launch-gate config",
    )
    _require_same_file(
        execution_bundle,
        bundle_snapshot,
        maximum=MAX_EXECUTION_BUNDLE_BYTES,
        location="launch-gate execution bundle",
    )
    if s3_client is None:
        try:
            import boto3

            s3_client = boto3.client("s3", region_name=region)
        except Exception as exc:
            raise LaunchGatePublisherError("cannot create production S3 client") from exc
    _require_locked_bucket(
        s3_client,
        evidence_bucket,
        expected_owner=expected_bucket_owner,
    )
    publication_time = _utc_now(current_clock).replace(microsecond=0)
    uploader = _LockedUploader(
        s3_client=s3_client,
        bucket=evidence_bucket,
        prefix=prefix,
        storage_key_arn=storage_key,
        expected_owner=expected_bucket_owner,
        retain_until=publication_time + timedelta(days=RETENTION_DAYS),
        clock=current_clock,
    )
    base = f"{prefix}/launch-gates/{repository}/{release_commit}/{run_id}/{run_attempt}"
    if validated_bundle.status == "FAILED":
        assert validated_bundle.failure_stage is not None
        _require_same_file(
            execution_bundle,
            bundle_snapshot,
            maximum=MAX_EXECUTION_BUNDLE_BYTES,
            location="launch-gate execution bundle",
        )
        _require_same_file(
            reviewed_config,
            config_snapshot,
            maximum=MAX_CONFIG_BYTES,
            location="reviewed launch-gate config",
        )
        _publish_failed_terminal(
            uploader=uploader,
            base=base,
            release=release,
            execution=execution,
            failure=LaunchGateExecutionError(
                failure_stage=validated_bundle.failure_stage,
                cleanup_status=validated_bundle.cleanup_status,
            ),
            started_at=validated_bundle.started_at,
            completed_at=validated_bundle.completed_at,
            state_dir=state_dir,
            signing_key_arn=signing_key,
            signer=current_signer,
            verifier=current_verifier,
        )
        _require_same_file(
            execution_bundle,
            bundle_snapshot,
            maximum=MAX_EXECUTION_BUNDLE_BYTES,
            location="launch-gate execution bundle",
        )
        _require_same_file(
            reviewed_config,
            config_snapshot,
            maximum=MAX_CONFIG_BYTES,
            location="reviewed launch-gate config",
        )
        raise LaunchGatePublisherError("launch-gate execution failed; immutable terminal evidence was published")
    pending = validated_bundle.commands
    cleanup_output = validated_bundle.cleanup_output
    if cleanup_output is None:
        raise LaunchGatePublisherError("passed execution bundle is missing cleanup evidence")
    attempt_started = validated_bundle.started_at
    if manifest_key.startswith(f"{base}/") or manifest_signature_key.startswith(f"{base}/"):
        generated_reserved = {manifest_key, manifest_signature_key}
    else:
        generated_reserved = set()

    receipts: dict[str, dict[str, Any]] = {}
    gate_pairs: dict[str, evidence.ArtifactPair] = {}
    command_outputs: dict[tuple[str, int, str], bytes] = {}
    try:
        with tempfile.TemporaryDirectory(
            prefix=".receipt-publisher-",
            dir=state_dir,
        ) as temporary_name:
            temporary = Path(temporary_name)
            for gate in evidence.ALL_GATES:
                receipt_commands: list[dict[str, Any]] = []
                for index, command in enumerate(pending[gate]):
                    stem = f"{index + 1:02d}-{command.name}"
                    stdout_key = _object_key(
                        base,
                        "commands",
                        gate,
                        f"{stem}.stdout.json",
                    )
                    stderr_key = _object_key(
                        base,
                        "commands",
                        gate,
                        f"{stem}.stderr.log",
                    )
                    if stdout_key in generated_reserved or stderr_key in generated_reserved:
                        raise LaunchGatePublisherError("manifest destination collides with command evidence")
                    stdout_reference = uploader.upload(
                        command.stdout,
                        key=stdout_key,
                        content_type="application/json",
                    )
                    stderr_reference = uploader.upload(
                        command.stderr,
                        key=stderr_key,
                        content_type="text/plain",
                    )
                    command_outputs[(gate, index, "stdout")] = command.stdout
                    command_outputs[(gate, index, "stderr")] = command.stderr
                    receipt_commands.append(
                        {
                            "name": command.name,
                            "tool": TOOL,
                            "argv": list(command.argv),
                            "commandSha256": evidence._canonical_sha(list(command.argv)),
                            "stdout": stdout_reference.report_value(),
                            "stderr": stderr_reference.report_value(),
                            "startedAt": command.started_at,
                            "completedAt": command.completed_at,
                            "exitCode": 0,
                        }
                    )
                receipt = {
                    "schema": evidence.GATE_SCHEMA,
                    "gate": gate,
                    "release": release,
                    "execution": execution,
                    "environment": "production",
                    "commands": receipt_commands,
                }
                receipt_path = temporary / f"{gate}.json"
                signature_path = temporary / f"{gate}-kms-signature.json"
                _write_json(receipt_path, receipt)
                _sign_and_verify(
                    receipt_path,
                    signature_path,
                    signing_key_arn=signing_key,
                    signer=current_signer,
                    verifier=current_verifier,
                )
                artifact_key = _object_key(base, "receipts", f"{gate}.json")
                signature_key = _object_key(
                    base,
                    "receipts",
                    f"{gate}-kms-signature.json",
                )
                if artifact_key in generated_reserved or signature_key in generated_reserved:
                    raise LaunchGatePublisherError("manifest destination collides with gate receipt")
                artifact_reference = uploader.upload(
                    evidence._read_regular(receipt_path),
                    key=artifact_key,
                    content_type="application/json",
                )
                signature_reference = uploader.upload(
                    evidence._read_regular(
                        signature_path,
                        maximum=kms_evidence.MAX_BUNDLE_BYTES,
                    ),
                    key=signature_key,
                    content_type="application/json",
                )
                receipts[gate] = receipt
                gate_pairs[gate] = evidence.ArtifactPair(
                    artifact=artifact_reference,
                    signature=signature_reference,
                )

            terminal = {
                "schema": "axonllm.agentcore-launch-gate-terminal/v1",
                "release": release,
                "execution": execution,
                "status": "PASSED",
                "failureStage": None,
                "cleanupStatus": "SUCCEEDED",
                "cleanupObservations": cleanup_output["observations"],
                "startedAt": _timestamp(attempt_started),
                "completedAt": _timestamp(validated_bundle.completed_at),
            }
            terminal_path = temporary / "attempt-terminal.json"
            terminal_signature_path = temporary / "attempt-terminal-kms-signature.json"
            _write_json(terminal_path, terminal)
            _sign_and_verify(
                terminal_path,
                terminal_signature_path,
                signing_key_arn=signing_key,
                signer=current_signer,
                verifier=current_verifier,
            )
            terminal_reference = uploader.upload(
                evidence._read_regular(terminal_path),
                key=_object_key(
                    base,
                    "terminal",
                    "attempt-terminal.json",
                ),
                content_type="application/json",
            )
            terminal_signature_reference = uploader.upload(
                evidence._read_regular(
                    terminal_signature_path,
                    maximum=kms_evidence.MAX_BUNDLE_BYTES,
                ),
                key=_object_key(
                    base,
                    "terminal",
                    "attempt-terminal-kms-signature.json",
                ),
                content_type="application/json",
            )
            terminal_pair = evidence.ArtifactPair(
                artifact=terminal_reference,
                signature=terminal_signature_reference,
            )
            manifest = {
                "schema": evidence.SOURCE_SCHEMA,
                "release": release,
                "execution": execution,
                "terminal": terminal_pair.report_value(),
                "gates": {gate: gate_pairs[gate].report_value() for gate in evidence.ALL_GATES},
            }
            try:
                validated_source = evidence._validate_source_manifest(
                    manifest,
                    expected_release=release,
                    evidence_bucket=evidence_bucket,
                    evidence_prefix=prefix,
                )
                validation_time = _utc_now(current_clock)
                evidence._validate_terminal(
                    terminal,
                    source=validated_source,
                    now=validation_time,
                )
                for gate in evidence.ALL_GATES:
                    evidence._validate_gate_receipt(
                        receipts[gate],
                        gate_name=gate,
                        source=validated_source,
                        now=validation_time,
                        evidence_bucket=evidence_bucket,
                        evidence_prefix=prefix,
                        command_outputs=command_outputs,
                        script_checker=lambda _commit, _script: None,
                    )
            except evidence.LaunchRehearsalError as exc:
                raise LaunchGatePublisherError("generated launch-gate evidence violates the consumer schema") from exc

            _require_same_file(
                reviewed_config,
                config_snapshot,
                maximum=MAX_CONFIG_BYTES,
                location="reviewed launch-gate config",
            )
            _require_same_file(
                execution_bundle,
                bundle_snapshot,
                maximum=MAX_EXECUTION_BUNDLE_BYTES,
                location="launch-gate execution bundle",
            )
            manifest_path = temporary / "agentcore-launch-gate-set.json"
            manifest_signature_path = temporary / "agentcore-launch-gate-set-kms-signature.json"
            _write_json(manifest_path, manifest)
            _sign_and_verify(
                manifest_path,
                manifest_signature_path,
                signing_key_arn=signing_key,
                signer=current_signer,
                verifier=current_verifier,
            )
            signature_reference = uploader.upload(
                evidence._read_regular(
                    manifest_signature_path,
                    maximum=kms_evidence.MAX_BUNDLE_BYTES,
                ),
                key=manifest_signature_key,
                content_type="application/json",
                append_key=True,
            )
            manifest_reference = uploader.upload(
                evidence._read_regular(manifest_path),
                key=manifest_key,
                content_type="application/json",
                append_key=True,
            )
    except LaunchGatePublisherError:
        raise
    except (OSError, evidence.LaunchRehearsalError) as exc:
        raise LaunchGatePublisherError("launch-gate evidence publication failed") from exc

    _require_same_file(
        reviewed_config,
        config_snapshot,
        maximum=MAX_CONFIG_BYTES,
        location="reviewed launch-gate config",
    )
    _require_same_file(
        execution_bundle,
        bundle_snapshot,
        maximum=MAX_EXECUTION_BUNDLE_BYTES,
        location="launch-gate execution bundle",
    )
    return {
        "gate_manifest_uri": manifest_reference.uri,
        "gate_manifest_version_id": manifest_reference.version_id,
        "gate_manifest_sha256": manifest_reference.sha256,
        "gate_manifest_signature_uri": signature_reference.uri,
        "gate_manifest_signature_version_id": signature_reference.version_id,
        "gate_manifest_signature_sha256": signature_reference.sha256,
    }


def _append_github_output(
    path: Path,
    values: Mapping[str, str],
    *,
    expected: set[str],
) -> None:
    if set(values) != expected or any(
        not isinstance(value, str) or not value or "\n" in value or "\r" in value for value in values.values()
    ):
        raise LaunchGatePublisherError("GitHub launch-gate outputs are malformed")
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags, 0o600)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise LaunchGatePublisherError("GitHub output must be a regular file")
        with os.fdopen(descriptor, "a", encoding="utf-8") as output:
            descriptor = -1
            for name in sorted(values):
                output.write(f"{name}={values[name]}\n")
            output.flush()
            os.fsync(output.fileno())
    except LaunchGatePublisherError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise LaunchGatePublisherError("cannot write GitHub launch-gate outputs") from exc


def _write_github_output(path: Path, values: Mapping[str, str]) -> None:
    _append_github_output(
        path,
        values,
        expected={
            "gate_manifest_uri",
            "gate_manifest_version_id",
            "gate_manifest_sha256",
            "gate_manifest_signature_uri",
            "gate_manifest_signature_version_id",
            "gate_manifest_signature_sha256",
        },
    )


def _write_execution_github_output(
    path: Path,
    values: Mapping[str, str],
) -> None:
    _append_github_output(
        path,
        values,
        expected={"execution_bundle_sha256", "execution_status"},
    )


def _add_binding_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--release-commit", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--agentcore-image", required=True)
    parser.add_argument("--control-plane-image", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--workflow-ref", required=True)
    parser.add_argument("--workflow-commit", required=True)
    parser.add_argument("--parent-workflow-ref", required=True)
    parser.add_argument("--parent-workflow-commit", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--reviewed-config", required=True, type=Path)
    parser.add_argument("--reviewed-config-uri", required=True)
    parser.add_argument("--reviewed-config-version-id", required=True)
    parser.add_argument("--reviewed-config-sha256", required=True)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--execution-bundle", required=True, type=Path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Execute AgentCore launch gates or publish their receipts.",
    )
    subparsers = parser.add_subparsers(dest="phase", required=True)

    execute = subparsers.add_parser(
        "execute",
        help="Run coordinator actions and write a local execution bundle.",
    )
    _add_binding_arguments(execute)
    execute.add_argument("--operation-root", required=True, type=Path)
    execute.add_argument("--github-output", required=True, type=Path)

    publish = subparsers.add_parser(
        "publish",
        help="Validate, sign, and publish one local execution bundle.",
    )
    _add_binding_arguments(publish)
    publish.add_argument("--execution-bundle-sha256", required=True)
    publish.add_argument("--evidence-bucket", required=True)
    publish.add_argument("--evidence-prefix", required=True)
    publish.add_argument("--manifest-uri", required=True)
    publish.add_argument("--manifest-signature-uri", required=True)
    publish.add_argument("--storage-kms-key-arn", required=True)
    publish.add_argument("--signing-key-arn", required=True)
    publish.add_argument("--github-output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    previous_umask = os.umask(0o077)
    try:
        common = {
            "release_commit": args.release_commit,
            "region": args.region,
            "agentcore_image": args.agentcore_image,
            "control_plane_image": args.control_plane_image,
            "repository": args.repository,
            "workflow_ref": args.workflow_ref,
            "workflow_commit": args.workflow_commit,
            "parent_workflow_ref": args.parent_workflow_ref,
            "parent_workflow_commit": args.parent_workflow_commit,
            "run_id": args.run_id,
            "run_attempt": args.run_attempt,
            "reviewed_config": args.reviewed_config.resolve(),
            "reviewed_config_uri": args.reviewed_config_uri,
            "reviewed_config_version_id": args.reviewed_config_version_id,
            "reviewed_config_sha256": args.reviewed_config_sha256,
            "state_dir": args.state_dir.resolve(),
            "execution_bundle": args.execution_bundle.resolve(),
        }
        if args.phase == "execute":
            outputs = execute_gates(
                **common,
                operation_root=args.operation_root.resolve(),
            )
            _write_execution_github_output(args.github_output, outputs)
            print(json.dumps(outputs, allow_nan=False, sort_keys=True))
            return 0 if outputs["execution_status"] == "PASSED" else 1
        if args.phase == "publish":
            outputs = publish_receipts(
                **common,
                execution_bundle_sha256=args.execution_bundle_sha256,
                evidence_bucket=args.evidence_bucket,
                evidence_prefix=args.evidence_prefix,
                manifest_uri=args.manifest_uri,
                manifest_signature_uri=args.manifest_signature_uri,
                storage_kms_key_arn=args.storage_kms_key_arn,
                signing_key_arn=args.signing_key_arn,
            )
            _write_github_output(args.github_output, outputs)
            print(json.dumps(outputs, allow_nan=False, sort_keys=True))
            return 0
        raise LaunchGatePublisherError("unsupported launch-gate phase")
    except LaunchGatePublisherError as exc:
        print(f"launch-gate receipt publication failed: {exc}", file=sys.stderr)
        return 1
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    raise SystemExit(main())
