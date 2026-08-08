#!/usr/bin/env python3
"""Validate AxonLLM state backups and optionally exercise a PITR restore."""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from aws_support import AwsCli, AwsError, parse_aws_time


class RecoveryValidationError(RuntimeError):
    """Raised when state protection or a restore exercise violates policy."""


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
    target_name = target_name[:255].rstrip("-")
    created = False
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

        source_sample = aws.json(
            "dynamodb",
            "scan",
            "--table-name",
            source_name,
            "--select",
            "COUNT",
            "--limit",
            "1",
            "--consistent-read",
        ).get("Count")
        restored_sample = aws.json(
            "dynamodb",
            "scan",
            "--table-name",
            target_name,
            "--select",
            "COUNT",
            "--limit",
            "1",
            "--consistent-read",
        ).get("Count")
        if source_sample and not restored_sample:
            raise RecoveryValidationError("source contains data but restored table sample is empty")
        return {
            "targetTable": target_name,
            "status": "validated",
            "retained": keep,
        }
    finally:
        if created and not keep:
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
    sleep: Callable[[float], None] = time.sleep,
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
    locked = vault.get("Locked") is True
    if require_vault_lock and not locked:
        raise RecoveryValidationError("AWS Backup Vault Lock is not enabled")

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
    backup_age = now - newest
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
        "backupVaultLocked": locked,
        "latestBackupAgeHours": round(backup_age.total_seconds() / 3600, 2),
        "restoreExercise": None,
    }
    if exercise_restore:
        result["restoreExercise"] = _exercise_restore(
            aws,
            table,
            keep=keep_restored_table,
            poll_interval=10,
            timeout_seconds=2700,
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
    parser.add_argument("--exercise-restore", action="store_true")
    parser.add_argument("--keep-restored-table", action="store_true")
    args = parser.parse_args()
    if args.max_backup_age_hours < 1:
        parser.error("--max-backup-age-hours must be positive")
    if args.keep_restored_table and not args.exercise_restore:
        parser.error("--keep-restored-table requires --exercise-restore")
    try:
        result = validate_recovery(
            AwsCli(args.region),
            stack_name=args.stack_name,
            table_name=args.table_name,
            max_backup_age_hours=args.max_backup_age_hours,
            require_vault_lock=args.require_vault_lock,
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
