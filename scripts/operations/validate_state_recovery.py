#!/usr/bin/env python3
"""Validate AxonLLM DynamoDB protection and optionally exercise a PITR restore."""

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


_RESTORE_TIMEOUT_SECONDS = 45 * 60
_RESTORE_SAMPLE_LIMIT = 25


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

    result: dict[str, Any] = {
        "tableArn": table_arn,
        "deletionProtection": True,
        "serverSideEncryption": "ENABLED",
        "pointInTimeRecovery": "ENABLED",
        "latestRestorableAgeMinutes": round(
            (now - latest_restorable).total_seconds() / 60,
            2,
        ),
        "restoreExercise": None,
    }
    if exercise_restore:
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
    parser.add_argument("--exercise-restore", action="store_true")
    parser.add_argument("--keep-restored-table", action="store_true")
    args = parser.parse_args()
    if args.keep_restored_table and not args.exercise_restore:
        parser.error("--keep-restored-table requires --exercise-restore")
    try:
        result = validate_recovery(
            AwsCli(args.region),
            stack_name=args.stack_name,
            table_name=args.table_name,
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
