#!/usr/bin/env python3
"""Validate AxonLLM state backups and optionally exercise a PITR restore."""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from aws_support import AwsCli, AwsError, parse_aws_time


class RecoveryValidationError(RuntimeError):
    """Raised when state protection or a restore exercise violates policy."""


_BACKUP_JOB_POLL_INTERVAL_SECONDS = 15
_WORKFLOW_CREDENTIAL_ENVELOPE_SECONDS = 3 * 60 * 60
_RECOVERY_WORKFLOW_MARGIN_SECONDS = 60 * 60
_RESTORE_TIMEOUT_SECONDS = 45 * 60
_BACKUP_JOB_TIMEOUT_SECONDS = (
    _WORKFLOW_CREDENTIAL_ENVELOPE_SECONDS - _RECOVERY_WORKFLOW_MARGIN_SECONDS - _RESTORE_TIMEOUT_SECONDS
)
_BACKUP_JOB_PENDING_STATES = {"CREATED", "PENDING", "RUNNING"}
_RESTORE_SAMPLE_LIMIT = 25
_BACKUP_LIFECYCLE = "MoveToColdStorageAfterDays=30,DeleteAfterDays=365"
_BACKUP_TAGS = "Application=AxonLLM,Runtime=AgentCore,Trigger=Deployment"


def _stack_resources(aws: AwsCli, stack_name: str) -> list[dict[str, Any]]:
    resources = aws.json(
        "cloudformation",
        "list-stack-resources",
        "--stack-name",
        stack_name,
    ).get("StackResourceSummaries")
    if not isinstance(resources, list):
        raise RecoveryValidationError("CloudFormation resource list is missing")
    return [item for item in resources if isinstance(item, dict)]


def _discover_table(
    resources: list[dict[str, Any]],
    requested_table: str | None,
) -> str:
    if requested_table:
        return requested_table
    tables = [
        item.get("PhysicalResourceId")
        for item in resources
        if item.get("ResourceType") == "AWS::DynamoDB::Table" and isinstance(item.get("PhysicalResourceId"), str)
    ]
    if len(tables) != 1:
        raise RecoveryValidationError(f"expected one DynamoDB table in the stack, found {len(tables)}")
    return tables[0]


def _discover_vault(
    aws: AwsCli,
    resources: list[dict[str, Any]],
    stack_name: str,
) -> str:
    stack = aws.json(
        "cloudformation",
        "describe-stacks",
        "--stack-name",
        stack_name,
    ).get("Stacks", [])
    if isinstance(stack, list) and len(stack) == 1 and isinstance(stack[0], dict):
        for output in stack[0].get("Outputs", []):
            if (
                isinstance(output, dict)
                and output.get("OutputKey") == "StateBackupVaultArn"
                and isinstance(output.get("OutputValue"), str)
            ):
                return output["OutputValue"].rsplit(":", 1)[-1]
    vaults = [
        item.get("PhysicalResourceId")
        for item in resources
        if item.get("ResourceType") == "AWS::Backup::BackupVault" and isinstance(item.get("PhysicalResourceId"), str)
    ]
    if len(vaults) != 1:
        raise RecoveryValidationError("could not uniquely discover the AWS Backup vault")
    return vaults[0]


def _discover_backup_role(aws: AwsCli, stack_name: str) -> str:
    stacks = aws.json(
        "cloudformation",
        "describe-stacks",
        "--stack-name",
        stack_name,
    ).get("Stacks")
    if not isinstance(stacks, list) or len(stacks) != 1 or not isinstance(stacks[0], dict):
        raise RecoveryValidationError("could not discover the AWS Backup service role")
    outputs = stacks[0].get("Outputs")
    if not isinstance(outputs, list):
        raise RecoveryValidationError("could not discover the AWS Backup service role")
    matches = [
        output.get("OutputValue")
        for output in outputs
        if (
            isinstance(output, dict)
            and output.get("OutputKey") == "StateBackupRoleArn"
            and isinstance(output.get("OutputValue"), str)
        )
    ]
    if len(matches) != 1 or not matches[0] or ":iam::" not in matches[0] or ":role/" not in matches[0]:
        raise RecoveryValidationError("could not discover the AWS Backup service role")
    return matches[0]


def _completed_backup_metadata(
    response: dict[str, Any],
    *,
    job_id: str,
    recovery_point_arn: str,
    table_arn: str,
    vault_name: str,
) -> dict[str, Any]:
    if (
        response.get("BackupJobId") != job_id
        or response.get("State") != "COMPLETED"
        or response.get("RecoveryPointArn") != recovery_point_arn
        or response.get("ResourceArn") != table_arn
        or response.get("BackupVaultName") != vault_name
    ):
        raise RecoveryValidationError("completed AWS Backup job metadata does not match the request")
    creation = parse_aws_time(
        response.get("CreationDate"),
        "backup job creation time",
    )
    completion = parse_aws_time(
        response.get("CompletionDate"),
        "backup job completion time",
    )
    if completion < creation:
        raise RecoveryValidationError("AWS Backup job completion precedes its creation")
    return {
        "backupJobId": job_id,
        "status": "COMPLETED",
        "backupVault": vault_name,
        "resourceArn": table_arn,
        "recoveryPointArn": recovery_point_arn,
        "creationDate": creation.isoformat(),
        "completionDate": completion.isoformat(),
    }


def _start_deployment_backup(
    aws: AwsCli,
    *,
    table_arn: str,
    vault_name: str,
    role_arn: str,
    poll_interval: float,
    timeout_seconds: int,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> dict[str, Any]:
    if poll_interval <= 0 or timeout_seconds <= 0:
        raise RecoveryValidationError("AWS Backup job polling bounds must be positive")
    started = aws.json(
        "backup",
        "start-backup-job",
        "--backup-vault-name",
        vault_name,
        "--resource-arn",
        table_arn,
        "--iam-role-arn",
        role_arn,
        "--lifecycle",
        _BACKUP_LIFECYCLE,
        "--recovery-point-tags",
        _BACKUP_TAGS,
    )
    job_id = started.get("BackupJobId")
    recovery_point_arn = started.get("RecoveryPointArn")
    if not isinstance(job_id, str) or not job_id or not isinstance(recovery_point_arn, str) or not recovery_point_arn:
        raise RecoveryValidationError("AWS Backup did not return deployment backup identifiers")

    deadline = monotonic() + timeout_seconds
    while monotonic() < deadline:
        response = aws.json(
            "backup",
            "describe-backup-job",
            "--backup-job-id",
            job_id,
        )
        if response.get("BackupJobId") != job_id:
            raise RecoveryValidationError("AWS Backup returned mismatched job metadata")
        state = response.get("State")
        if state == "COMPLETED":
            return _completed_backup_metadata(
                response,
                job_id=job_id,
                recovery_point_arn=recovery_point_arn,
                table_arn=table_arn,
                vault_name=vault_name,
            )
        if state not in _BACKUP_JOB_PENDING_STATES:
            if not isinstance(state, str) or not state:
                raise RecoveryValidationError("AWS Backup returned an invalid deployment backup state")
            raise RecoveryValidationError(f"AWS Backup deployment job ended in {state}")
        sleep(poll_interval)
    raise RecoveryValidationError("AWS Backup deployment job did not complete before timeout")


def _verify_table(table: dict[str, Any]) -> None:
    if table.get("TableStatus") != "ACTIVE":
        raise RecoveryValidationError("DynamoDB state table is not ACTIVE")
    if table.get("DeletionProtectionEnabled") is not True:
        raise RecoveryValidationError("DynamoDB deletion protection is disabled")
    if table.get("SSEDescription", {}).get("Status") != "ENABLED":
        raise RecoveryValidationError("DynamoDB server-side encryption is not enabled")
    key_schema = table.get("KeySchema")
    expected = [
        {"AttributeName": "PK", "KeyType": "HASH"},
        {"AttributeName": "SK", "KeyType": "RANGE"},
    ]
    if key_schema != expected:
        raise RecoveryValidationError("DynamoDB state table key schema is unexpected")


def _vault_lock_posture(
    vault: dict[str, Any],
    *,
    required: bool,
) -> dict[str, Any]:
    locked = vault.get("Locked") is True
    lock_date = vault.get("LockDate")
    minimum = vault.get("MinRetentionDays")
    maximum = vault.get("MaxRetentionDays")
    mode = "UNLOCKED" if not locked else "COMPLIANCE" if lock_date is not None else "GOVERNANCE"
    if required:
        if not locked:
            raise RecoveryValidationError("AWS Backup Vault Lock is not enabled")
        if minimum != 30 or maximum != 365:
            raise RecoveryValidationError("AWS Backup Vault Lock must enforce 30-365 day retention")
        if mode not in {"GOVERNANCE", "COMPLIANCE"}:
            raise RecoveryValidationError("AWS Backup Vault Lock must use governance or compliance mode")
    return {
        "locked": locked,
        "mode": mode,
        "minRetentionDays": minimum,
        "maxRetentionDays": maximum,
    }


def _wait_for_active_table(
    aws: AwsCli,
    table_name: str,
    *,
    poll_interval: float,
    timeout_seconds: int,
    sleep: Callable[[float], None],
    deletion_protection: bool | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        response = aws.json(
            "dynamodb",
            "describe-table",
            "--table-name",
            table_name,
        )
        table = response.get("Table")
        if (
            isinstance(table, dict)
            and table.get("TableStatus") == "ACTIVE"
            and (deletion_protection is None or table.get("DeletionProtectionEnabled") is deletion_protection)
        ):
            return table
        sleep(poll_interval)
    expected = "ACTIVE"
    if deletion_protection is not None:
        state = "enabled" if deletion_protection else "disabled"
        expected += f" with deletion protection {state}"
    raise RecoveryValidationError(f"table {table_name} did not reach {expected} in time")


def _wait_for_continuous_backups(
    aws: AwsCli,
    table_name: str,
    *,
    poll_interval: float,
    timeout_seconds: int,
    sleep: Callable[[float], None],
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        description = aws.json(
            "dynamodb",
            "describe-continuous-backups",
            "--table-name",
            table_name,
        ).get("ContinuousBackupsDescription", {})
        pitr = description.get("PointInTimeRecoveryDescription", {})
        if pitr.get("PointInTimeRecoveryStatus") == "ENABLED":
            return
        sleep(poll_interval)
    raise RecoveryValidationError(f"table {table_name} did not enable point-in-time recovery")


def _wait_for_ttl(
    aws: AwsCli,
    table_name: str,
    *,
    poll_interval: float,
    timeout_seconds: int,
    sleep: Callable[[float], None],
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        description = aws.json(
            "dynamodb",
            "describe-time-to-live",
            "--table-name",
            table_name,
        ).get("TimeToLiveDescription", {})
        if description.get("TimeToLiveStatus") == "ENABLED" and description.get("AttributeName") == "expires_at":
            return
        sleep(poll_interval)
    raise RecoveryValidationError(f"table {table_name} did not enable expires_at TTL")


def _protect_retained_restore(
    aws: AwsCli,
    table_name: str,
    *,
    poll_interval: float,
    timeout_seconds: int,
    sleep: Callable[[float], None],
) -> None:
    aws.json(
        "dynamodb",
        "update-continuous-backups",
        "--table-name",
        table_name,
        "--point-in-time-recovery-specification",
        "PointInTimeRecoveryEnabled=true",
    )
    _wait_for_continuous_backups(
        aws,
        table_name,
        poll_interval=poll_interval,
        timeout_seconds=timeout_seconds,
        sleep=sleep,
    )
    ttl = aws.json(
        "dynamodb",
        "describe-time-to-live",
        "--table-name",
        table_name,
    ).get("TimeToLiveDescription", {})
    ttl_status = ttl.get("TimeToLiveStatus")
    if ttl_status == "ENABLED" and ttl.get("AttributeName") != "expires_at":
        raise RecoveryValidationError(f"table {table_name} uses an unexpected TTL attribute")
    if ttl_status not in {"ENABLED", "ENABLING"}:
        aws.json(
            "dynamodb",
            "update-time-to-live",
            "--table-name",
            table_name,
            "--time-to-live-specification",
            "Enabled=true,AttributeName=expires_at",
        )
    _wait_for_ttl(
        aws,
        table_name,
        poll_interval=poll_interval,
        timeout_seconds=timeout_seconds,
        sleep=sleep,
    )
    aws.json(
        "dynamodb",
        "update-table",
        "--table-name",
        table_name,
        "--deletion-protection-enabled",
    )
    _wait_for_active_table(
        aws,
        table_name,
        poll_interval=poll_interval,
        timeout_seconds=min(timeout_seconds, 300),
        sleep=sleep,
        deletion_protection=True,
    )


def _canonical_json(value: Any, location: str) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise RecoveryValidationError(f"{location} is not valid DynamoDB JSON") from exc


def _validate_restored_sample(
    aws: AwsCli,
    *,
    source_name: str,
    items: Any,
) -> dict[str, Any]:
    if not isinstance(items, list) or not items:
        raise RecoveryValidationError("restored table contains no items to validate")
    if len(items) > _RESTORE_SAMPLE_LIMIT:
        raise RecoveryValidationError("restored table returned more items than requested")

    canonical_items: list[str] = []
    seen_keys: set[str] = set()
    for index, restored_item in enumerate(items):
        if type(restored_item) is not dict:
            raise RecoveryValidationError(f"restored sample item {index + 1} is malformed")
        key = {name: restored_item.get(name) for name in ("PK", "SK")}
        if any(type(value) is not dict or len(value) != 1 for value in key.values()):
            raise RecoveryValidationError(f"restored sample item {index + 1} has an invalid key")
        canonical_key = _canonical_json(
            key,
            f"restored sample item {index + 1} key",
        )
        if canonical_key in seen_keys:
            raise RecoveryValidationError("restored sample contains duplicate item keys")
        seen_keys.add(canonical_key)

        source_response = aws.json(
            "dynamodb",
            "get-item",
            "--table-name",
            source_name,
            "--key",
            canonical_key,
            "--consistent-read",
        )
        source_item = source_response.get("Item")
        if source_item is None:
            raise RecoveryValidationError(f"restored sample item {index + 1} is missing from the source")
        if type(source_item) is not dict:
            raise RecoveryValidationError(f"source sample item {index + 1} is malformed")
        if source_item != restored_item:
            raise RecoveryValidationError(f"restored sample item {index + 1} content differs from the source")
        canonical_items.append(
            _canonical_json(
                restored_item,
                f"restored sample item {index + 1}",
            )
        )

    digest = hashlib.sha256("\n".join(sorted(canonical_items)).encode("utf-8")).hexdigest()
    return {
        "sampledItemCount": len(canonical_items),
        "sampledItemsSha256": digest,
    }


def _exercise_restore(
    aws: AwsCli,
    source_table: dict[str, Any],
    *,
    keep: bool,
    poll_interval: float,
    timeout_seconds: int,
    sleep: Callable[[float], None],
) -> dict[str, Any]:
    source_name = source_table["TableName"]
    suffix = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    target_name = f"{source_name}-restore-validation-{suffix}-{secrets.token_hex(3)}"
    if len(target_name) > 255:
        raise RecoveryValidationError("source table name is too long for a scoped restore-validation target")
    created = False
    retained_ready = False
    try:
        aws.json(
            "dynamodb",
            "restore-table-to-point-in-time",
            "--source-table-name",
            source_name,
            "--target-table-name",
            target_name,
            "--use-latest-restorable-time",
        )
        created = True
        restored = _wait_for_active_table(
            aws,
            target_name,
            poll_interval=poll_interval,
            timeout_seconds=timeout_seconds,
            sleep=sleep,
        )
        if restored.get("KeySchema") != source_table.get("KeySchema"):
            raise RecoveryValidationError("restored table key schema differs from source")
        if restored.get("SSEDescription", {}).get("Status") != "ENABLED":
            raise RecoveryValidationError("restored table is not encrypted")

        restored_items = aws.json(
            "dynamodb",
            "scan",
            "--table-name",
            target_name,
            "--limit",
            str(_RESTORE_SAMPLE_LIMIT),
            "--consistent-read",
        ).get("Items")
        sample = _validate_restored_sample(
            aws,
            source_name=source_name,
            items=restored_items,
        )
        if keep:
            _protect_retained_restore(
                aws,
                target_name,
                poll_interval=poll_interval,
                timeout_seconds=timeout_seconds,
                sleep=sleep,
            )
            retained_ready = True
        return {
            "targetTable": target_name,
            "status": "validated",
            "retained": retained_ready,
            "pointInTimeRecovery": ("ENABLED" if retained_ready else None),
            "timeToLive": "ENABLED" if retained_ready else None,
            "deletionProtection": retained_ready,
            **sample,
        }
    finally:
        if created and not retained_ready:
            try:
                restored_table = aws.json(
                    "dynamodb",
                    "describe-table",
                    "--table-name",
                    target_name,
                ).get("Table", {})
                cleanup_timeout = min(timeout_seconds, 300)
                if restored_table.get("TableStatus") != "ACTIVE":
                    restored_table = _wait_for_active_table(
                        aws,
                        target_name,
                        poll_interval=poll_interval,
                        timeout_seconds=cleanup_timeout,
                        sleep=sleep,
                    )
                if restored_table.get("DeletionProtectionEnabled") is True:
                    aws.json(
                        "dynamodb",
                        "update-table",
                        "--table-name",
                        target_name,
                        "--no-deletion-protection-enabled",
                    )
                    _wait_for_active_table(
                        aws,
                        target_name,
                        poll_interval=poll_interval,
                        timeout_seconds=cleanup_timeout,
                        sleep=sleep,
                        deletion_protection=False,
                    )
                aws.json("dynamodb", "delete-table", "--table-name", target_name)
            except (AwsError, RecoveryValidationError) as exc:
                raise RecoveryValidationError(f"restore cleanup failed for {target_name}: {exc}") from exc


def validate_recovery(
    aws: AwsCli,
    *,
    stack_name: str,
    table_name: str | None,
    max_backup_age_hours: int,
    require_vault_lock: bool,
    exercise_restore: bool,
    keep_restored_table: bool,
    now: datetime,
    start_backup: bool = False,
    backup_job_poll_interval: float = (_BACKUP_JOB_POLL_INTERVAL_SECONDS),
    backup_job_timeout_seconds: int = _BACKUP_JOB_TIMEOUT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    resources = _stack_resources(aws, stack_name)
    resolved_table = _discover_table(resources, table_name)
    table_response = aws.json(
        "dynamodb",
        "describe-table",
        "--table-name",
        resolved_table,
    )
    table = table_response.get("Table")
    if not isinstance(table, dict):
        raise RecoveryValidationError("DynamoDB table description is missing")
    _verify_table(table)
    table_arn = table.get("TableArn")
    if not isinstance(table_arn, str):
        raise RecoveryValidationError("DynamoDB table ARN is missing")

    backups = aws.json(
        "dynamodb",
        "describe-continuous-backups",
        "--table-name",
        resolved_table,
    ).get("ContinuousBackupsDescription", {})
    pitr = backups.get("PointInTimeRecoveryDescription", {})
    if pitr.get("PointInTimeRecoveryStatus") != "ENABLED":
        raise RecoveryValidationError("DynamoDB point-in-time recovery is disabled")
    latest_restorable = parse_aws_time(
        pitr.get("LatestRestorableDateTime"),
        "latest restorable time",
    )
    if latest_restorable > now or now - latest_restorable > timedelta(hours=1):
        raise RecoveryValidationError("DynamoDB latest restorable time is stale")

    vault_name = _discover_vault(aws, resources, stack_name)
    vault = aws.json(
        "backup",
        "describe-backup-vault",
        "--backup-vault-name",
        vault_name,
    )
    if not vault.get("EncryptionKeyArn"):
        raise RecoveryValidationError("AWS Backup vault has no KMS encryption key")
    vault_lock = _vault_lock_posture(
        vault,
        required=require_vault_lock,
    )

    deployment_backup: dict[str, Any] | None = None
    freshness_now = now
    if start_backup:
        deployment_backup = _start_deployment_backup(
            aws,
            table_arn=table_arn,
            vault_name=vault_name,
            role_arn=_discover_backup_role(aws, stack_name),
            poll_interval=backup_job_poll_interval,
            timeout_seconds=backup_job_timeout_seconds,
            sleep=sleep,
            monotonic=monotonic,
        )
        completion = parse_aws_time(
            deployment_backup["completionDate"],
            "backup job completion time",
        )
        freshness_now = max(now, completion)

    recovery_points = aws.json(
        "backup",
        "list-recovery-points-by-backup-vault",
        "--backup-vault-name",
        vault_name,
        "--by-resource-arn",
        table_arn,
    ).get("RecoveryPoints", [])
    completed = [point for point in recovery_points if isinstance(point, dict) and point.get("Status") == "COMPLETED"]
    if not completed:
        raise RecoveryValidationError("no completed AWS Backup recovery point exists")
    newest = max(parse_aws_time(point.get("CreationDate"), "backup creation time") for point in completed)
    backup_age = freshness_now - newest
    if backup_age < timedelta(0) or backup_age > timedelta(hours=max_backup_age_hours):
        raise RecoveryValidationError("latest AWS Backup recovery point is outside the allowed age")

    result: dict[str, Any] = {
        "tableArn": table_arn,
        "pointInTimeRecovery": "ENABLED",
        "latestRestorableAgeMinutes": round(
            (now - latest_restorable).total_seconds() / 60,
            2,
        ),
        "backupVault": vault_name,
        "backupVaultLocked": vault_lock["locked"],
        "backupVaultLockMode": vault_lock["mode"],
        "backupVaultMinRetentionDays": vault_lock["minRetentionDays"],
        "backupVaultMaxRetentionDays": vault_lock["maxRetentionDays"],
        "latestBackupAgeHours": round(backup_age.total_seconds() / 3600, 2),
        "restoreExercise": None,
    }
    if deployment_backup is not None:
        result["deploymentBackup"] = deployment_backup
    if exercise_restore or start_backup:
        result["restoreExercise"] = _exercise_restore(
            aws,
            table,
            keep=keep_restored_table,
            poll_interval=10,
            timeout_seconds=_RESTORE_TIMEOUT_SECONDS,
            sleep=sleep,
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--stack-name", default="AxonLLMStack")
    parser.add_argument("--table-name")
    parser.add_argument("--max-backup-age-hours", type=int, default=30)
    parser.add_argument("--require-vault-lock", action="store_true")
    parser.add_argument(
        "--start-backup",
        action="store_true",
        help=("Start and await a retained on-demand backup before validating freshness, then exercise a restore"),
    )
    parser.add_argument("--exercise-restore", action="store_true")
    parser.add_argument("--keep-restored-table", action="store_true")
    args = parser.parse_args()
    if args.max_backup_age_hours < 1:
        parser.error("--max-backup-age-hours must be positive")
    if args.keep_restored_table and not args.exercise_restore and not args.start_backup:
        parser.error("--keep-restored-table requires --exercise-restore or --start-backup")
    try:
        result = validate_recovery(
            AwsCli(args.region),
            stack_name=args.stack_name,
            table_name=args.table_name,
            max_backup_age_hours=args.max_backup_age_hours,
            require_vault_lock=args.require_vault_lock,
            start_backup=args.start_backup,
            exercise_restore=args.exercise_restore,
            keep_restored_table=args.keep_restored_table,
            now=datetime.now(timezone.utc),
        )
    except (AwsError, RecoveryValidationError) as exc:
        print(f"state recovery validation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
