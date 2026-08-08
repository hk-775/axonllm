#!/usr/bin/env python3
"""Validate AxonLLM provider-secret rotation using metadata only."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from aws_support import AwsCli, AwsError, parse_aws_time


DEFAULT_SECRET_NAME = "axonllm/api-keys"


class SecretValidationError(RuntimeError):
    """Raised when secret or KMS metadata violates policy."""


def _discover_secret(aws: AwsCli, stack_name: str) -> str:
    resources = aws.json(
        "cloudformation",
        "list-stack-resources",
        "--stack-name",
        stack_name,
    ).get("StackResourceSummaries", [])
    secrets = [
        resource.get("PhysicalResourceId")
        for resource in resources
        if isinstance(resource, dict)
        and resource.get("ResourceType") == "AWS::SecretsManager::Secret"
        and isinstance(resource.get("PhysicalResourceId"), str)
    ]
    if len(secrets) == 1:
        return secrets[0]
    named = [secret for secret in secrets if DEFAULT_SECRET_NAME in secret]
    if len(named) == 1:
        return named[0]
    raise SecretValidationError("could not uniquely discover the AxonLLM secret from CloudFormation")


def _current_version(versions: list[Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    current: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for version in versions:
        if not isinstance(version, dict):
            continue
        stages = version.get("VersionStages") or []
        if "AWSCURRENT" in stages:
            current.append(version)
        if "AWSPENDING" in stages and "AWSCURRENT" not in stages:
            pending.append(version)
    if len(current) != 1:
        raise SecretValidationError(f"expected exactly one AWSCURRENT version, found {len(current)}")
    return current[0], pending


def _validate_kms_key(aws: AwsCli, kms_key_id: str) -> str:
    response = aws.json("kms", "describe-key", "--key-id", kms_key_id)
    metadata = response.get("KeyMetadata")
    if not isinstance(metadata, dict):
        raise SecretValidationError("secret KMS key metadata is missing")
    key_arn = metadata.get("Arn")
    if not isinstance(key_arn, str) or ":kms:" not in key_arn or ":key/" not in key_arn:
        raise SecretValidationError("secret KMS key ARN is missing or unsupported")
    if metadata.get("KeyManager") != "CUSTOMER":
        raise SecretValidationError("secret is not encrypted with a customer-managed KMS key")
    if metadata.get("KeyState") != "Enabled":
        raise SecretValidationError("secret KMS key is not enabled")

    rotation = aws.json(
        "kms",
        "get-key-rotation-status",
        "--key-id",
        key_arn,
    )
    if rotation.get("KeyRotationEnabled") is not True:
        raise SecretValidationError("secret KMS key rotation is disabled")
    return key_arn


def _has_rotation_schedule(metadata: dict[str, Any]) -> bool:
    rules = metadata.get("RotationRules")
    if isinstance(rules, dict):
        days = rules.get("AutomaticallyAfterDays")
        expression = rules.get("ScheduleExpression")
        if isinstance(days, int) and days > 0:
            return True
        if isinstance(expression, str) and expression.strip():
            return True
    return metadata.get("NextRotationDate") is not None


def validate_secret(
    aws: AwsCli,
    *,
    stack_name: str,
    secret_id: str | None,
    max_age_days: int,
    pending_max_hours: int,
    require_automatic_rotation: bool,
    now: datetime,
) -> dict[str, Any]:
    resolved_secret = secret_id or _discover_secret(aws, stack_name)
    metadata = aws.json(
        "secretsmanager",
        "describe-secret",
        "--secret-id",
        resolved_secret,
    )
    arn = metadata.get("ARN")
    if not isinstance(arn, str) or not arn.startswith("arn:aws:secretsmanager:"):
        raise SecretValidationError("secret ARN is missing or unsupported")
    if metadata.get("DeletedDate") is not None:
        raise SecretValidationError("secret is scheduled for deletion")
    kms_key = metadata.get("KmsKeyId")
    if not isinstance(kms_key, str) or not kms_key:
        raise SecretValidationError("secret is not encrypted with a customer-managed KMS key")
    kms_key_arn = _validate_kms_key(aws, kms_key)

    versions_response = aws.json(
        "secretsmanager",
        "list-secret-version-ids",
        "--secret-id",
        resolved_secret,
        "--include-deprecated",
    )
    versions = versions_response.get("Versions")
    if not isinstance(versions, list):
        raise SecretValidationError("secret version metadata is missing")
    current, pending = _current_version(versions)
    current_version_id = current.get("VersionId")
    if not isinstance(current_version_id, str) or not current_version_id:
        raise SecretValidationError("AWSCURRENT version ID is missing")
    described_stages = metadata.get("VersionIdsToStages")
    if not isinstance(described_stages, dict):
        raise SecretValidationError("described secret version stages are missing")
    if "AWSCURRENT" not in (described_stages.get(current_version_id) or []):
        raise SecretValidationError("AWSCURRENT version metadata is inconsistent between APIs")
    changed = parse_aws_time(
        current.get("CreatedDate"),
        "AWSCURRENT creation time",
    )
    age = now - changed
    if age < timedelta(0):
        raise SecretValidationError("AWSCURRENT creation time is in the future")
    if age > timedelta(days=max_age_days):
        raise SecretValidationError(f"AWSCURRENT is {age.days} days old; limit is {max_age_days}")

    stale_pending: list[str] = []
    for version in pending:
        created = parse_aws_time(
            version.get("CreatedDate"),
            "AWSPENDING creation time",
        )
        pending_age = now - created
        if pending_age < timedelta(0):
            raise SecretValidationError("AWSPENDING creation time is in the future")
        if pending_age > timedelta(hours=pending_max_hours):
            stale_pending.append(str(version.get("VersionId", "unknown")))
    if stale_pending:
        raise SecretValidationError("stale AWSPENDING versions: " + ", ".join(stale_pending))

    rotation_enabled = metadata.get("RotationEnabled") is True
    if require_automatic_rotation and not rotation_enabled:
        raise SecretValidationError("automatic Secrets Manager rotation is disabled")
    rotation_schedule_configured = _has_rotation_schedule(metadata)
    if rotation_enabled and not rotation_schedule_configured:
        raise SecretValidationError("rotation is enabled without a schedule")

    return {
        "validationScope": "METADATA_ONLY",
        "secretContentRead": False,
        "secretArn": arn,
        "kmsKeyArn": kms_key_arn,
        "kmsKeyRotation": "ENABLED",
        "currentVersionId": current_version_id,
        "currentVersionAgeDays": round(age.total_seconds() / 86400, 2),
        "automaticRotation": rotation_enabled,
        "rotationScheduleConfigured": rotation_schedule_configured,
        "pendingVersionCount": len(pending),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--stack-name", default="AxonLLMStack")
    parser.add_argument("--secret-id")
    parser.add_argument("--max-age-days", type=int, default=90)
    parser.add_argument("--pending-max-hours", type=int, default=24)
    parser.add_argument("--require-automatic-rotation", action="store_true")
    args = parser.parse_args()
    if args.max_age_days < 1 or args.pending_max_hours < 1:
        parser.error("age limits must be positive")
    try:
        result = validate_secret(
            AwsCli(args.region),
            stack_name=args.stack_name,
            secret_id=args.secret_id,
            max_age_days=args.max_age_days,
            pending_max_hours=args.pending_max_hours,
            require_automatic_rotation=args.require_automatic_rotation,
            now=datetime.now(timezone.utc),
        )
    except (AwsError, SecretValidationError) as exc:
        print(f"secret rotation validation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
