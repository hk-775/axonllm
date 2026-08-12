from __future__ import annotations

import json
import sys
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "operations"))

import check_secret_rotation  # noqa: E402
import validate_state_recovery  # noqa: E402


NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)


class FakeAws:
    def __init__(self, responses: dict[tuple[str, str], dict[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, tuple[str, ...]]] = []

    def json(self, service: str, operation: str, *arguments: str) -> dict[str, Any]:
        self.calls.append((service, operation, arguments))
        return self.responses[(service, operation)]


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


def _restore_item(*, document: str = '{"name":"Production"}') -> dict[str, Any]:
    return {
        "PK": {"S": "TENANT#tenant-a"},
        "SK": {"S": "PROJECT#project-a"},
        "document": {"S": document},
        "entity_type": {"S": "project"},
        "revision": {"N": "1"},
    }


class RestoreAws:
    def __init__(
        self,
        *,
        restored_items: list[dict[str, Any]] | None = None,
        source_items: list[dict[str, Any]] | None = None,
        cleanup_error: bool = False,
        deletion_protection: bool = False,
    ) -> None:
        self.calls: list[tuple[str, str, tuple[str, ...]]] = []
        self.restored_items = deepcopy([_restore_item()] if restored_items is None else restored_items)
        self.source_items = deepcopy(self.restored_items if source_items is None else source_items)
        self.cleanup_error = cleanup_error
        self.deletion_protection = deletion_protection
        self.pitr_enabled = False
        self.ttl_enabled = False

    def json(self, service: str, operation: str, *arguments: str) -> dict[str, Any]:
        self.calls.append((service, operation, arguments))
        if operation == "restore-table-to-point-in-time":
            return {}
        if operation == "describe-table":
            target = arguments[arguments.index("--table-name") + 1]
            return {
                "Table": {
                    "TableName": target,
                    "TableStatus": "ACTIVE",
                    "DeletionProtectionEnabled": self.deletion_protection,
                    "SSEDescription": {"Status": "ENABLED"},
                    "KeySchema": [
                        {"AttributeName": "PK", "KeyType": "HASH"},
                        {"AttributeName": "SK", "KeyType": "RANGE"},
                    ],
                }
            }
        if operation == "scan":
            table = arguments[arguments.index("--table-name") + 1]
            if "-restore-validation-" not in table:
                raise AssertionError("only the restored table may be scanned")
            return {"Items": deepcopy(self.restored_items)}
        if operation == "get-item":
            table = arguments[arguments.index("--table-name") + 1]
            if table != "axonllm-state":
                raise AssertionError("sample lookups must use the source table")
            key = json.loads(arguments[arguments.index("--key") + 1])
            for item in self.source_items:
                if {
                    "PK": item.get("PK"),
                    "SK": item.get("SK"),
                } == key:
                    return {"Item": deepcopy(item)}
            return {}
        if operation == "delete-table":
            if self.cleanup_error:
                raise validate_state_recovery.AwsError("cleanup denied")
            return {}
        if operation == "update-continuous-backups":
            self.pitr_enabled = True
            return {}
        if operation == "describe-continuous-backups":
            return {
                "ContinuousBackupsDescription": {
                    "PointInTimeRecoveryDescription": {
                        "PointInTimeRecoveryStatus": ("ENABLED" if self.pitr_enabled else "DISABLED")
                    }
                }
            }
        if operation == "describe-time-to-live":
            return {
                "TimeToLiveDescription": {
                    "TimeToLiveStatus": ("ENABLED" if self.ttl_enabled else "DISABLED"),
                    "AttributeName": ("expires_at" if self.ttl_enabled else None),
                }
            }
        if operation == "update-time-to-live":
            self.ttl_enabled = True
            return {}
        if operation == "update-table":
            self.deletion_protection = "--deletion-protection-enabled" in arguments
            return {}
        raise AssertionError(f"unexpected AWS call: {service} {operation}")


def _stack_resources() -> list[dict[str, str]]:
    return [
        {
            "ResourceType": "AWS::DynamoDB::Table",
            "PhysicalResourceId": "axonllm-state",
        },
        {
            "ResourceType": "AWS::Backup::BackupVault",
            "PhysicalResourceId": "axonllm-state",
        },
        {
            "ResourceType": "AWS::SecretsManager::Secret",
            "PhysicalResourceId": "axonllm/api-keys-AbCdEf",
        },
    ]


class SecretRotationTests(unittest.TestCase):
    def _aws(self, *, age_days: int = 10) -> FakeAws:
        current = NOW - timedelta(days=age_days)
        return FakeAws(
            {
                (
                    "cloudformation",
                    "list-stack-resources",
                ): {"StackResourceSummaries": _stack_resources()},
                ("secretsmanager", "describe-secret"): {
                    "ARN": ("arn:aws:secretsmanager:us-east-1:123456789012:secret:axonllm/api-keys-AbCdEf"),
                    "KmsKeyId": "arn:aws:kms:us-east-1:123456789012:key/abc",
                    "RotationEnabled": False,
                    "VersionIdsToStages": {"current": ["AWSCURRENT"]},
                },
                ("kms", "describe-key"): {
                    "KeyMetadata": {
                        "Arn": "arn:aws:kms:us-east-1:123456789012:key/abc",
                        "KeyManager": "CUSTOMER",
                        "KeyState": "Enabled",
                    }
                },
                ("kms", "get-key-rotation-status"): {
                    "KeyRotationEnabled": True,
                },
                ("secretsmanager", "list-secret-version-ids"): {
                    "Versions": [
                        {
                            "VersionId": "current",
                            "VersionStages": ["AWSCURRENT"],
                            "CreatedDate": current.isoformat(),
                        }
                    ]
                },
            }
        )

    def test_valid_manual_rotation_state(self) -> None:
        aws = self._aws()
        result = check_secret_rotation.validate_secret(
            aws,
            stack_name="AxonLLMStack",
            secret_id=None,
            max_age_days=90,
            pending_max_hours=24,
            require_automatic_rotation=False,
            now=NOW,
        )
        self.assertEqual(result["validationScope"], "METADATA_ONLY")
        self.assertFalse(result["secretContentRead"])
        self.assertFalse(result["automaticRotation"])
        self.assertNotIn("configuredProviderKeys", result)
        self.assertNotIn(
            "get-secret-value",
            [operation for _, operation, _ in aws.calls],
        )

    def test_stale_current_version_fails(self) -> None:
        with self.assertRaisesRegex(
            check_secret_rotation.SecretValidationError,
            "limit is 90",
        ):
            check_secret_rotation.validate_secret(
                self._aws(age_days=91),
                stack_name="AxonLLMStack",
                secret_id=None,
                max_age_days=90,
                pending_max_hours=24,
                require_automatic_rotation=False,
                now=NOW,
            )

    def test_automatic_rotation_can_be_required(self) -> None:
        with self.assertRaisesRegex(
            check_secret_rotation.SecretValidationError,
            "automatic Secrets Manager rotation is disabled",
        ):
            check_secret_rotation.validate_secret(
                self._aws(),
                stack_name="AxonLLMStack",
                secret_id=None,
                max_age_days=90,
                pending_max_hours=24,
                require_automatic_rotation=True,
                now=NOW,
            )

    def test_customer_managed_kms_key_is_required(self) -> None:
        aws = self._aws()
        key_metadata = aws.responses[("kms", "describe-key")]["KeyMetadata"]
        key_metadata["KeyManager"] = "AWS"
        with self.assertRaisesRegex(
            check_secret_rotation.SecretValidationError,
            "customer-managed KMS key",
        ):
            check_secret_rotation.validate_secret(
                aws,
                stack_name="AxonLLMStack",
                secret_id=None,
                max_age_days=90,
                pending_max_hours=24,
                require_automatic_rotation=False,
                now=NOW,
            )

    def test_kms_rotation_is_required(self) -> None:
        aws = self._aws()
        rotation = aws.responses[("kms", "get-key-rotation-status")]
        rotation["KeyRotationEnabled"] = False
        with self.assertRaisesRegex(
            check_secret_rotation.SecretValidationError,
            "KMS key rotation is disabled",
        ):
            check_secret_rotation.validate_secret(
                aws,
                stack_name="AxonLLMStack",
                secret_id=None,
                max_age_days=90,
                pending_max_hours=24,
                require_automatic_rotation=False,
                now=NOW,
            )

    def test_stale_pending_version_fails(self) -> None:
        aws = self._aws()
        versions = aws.responses[("secretsmanager", "list-secret-version-ids")]["Versions"]
        versions.append(
            {
                "VersionId": "pending",
                "VersionStages": ["AWSPENDING"],
                "CreatedDate": (NOW - timedelta(hours=25)).isoformat(),
            }
        )
        with self.assertRaisesRegex(
            check_secret_rotation.SecretValidationError,
            "stale AWSPENDING versions: pending",
        ):
            check_secret_rotation.validate_secret(
                aws,
                stack_name="AxonLLMStack",
                secret_id=None,
                max_age_days=90,
                pending_max_hours=24,
                require_automatic_rotation=False,
                now=NOW,
            )

    def test_enabled_rotation_requires_schedule_metadata(self) -> None:
        aws = self._aws()
        secret = aws.responses[("secretsmanager", "describe-secret")]
        secret["RotationEnabled"] = True
        with self.assertRaisesRegex(
            check_secret_rotation.SecretValidationError,
            "rotation is enabled without a schedule",
        ):
            check_secret_rotation.validate_secret(
                aws,
                stack_name="AxonLLMStack",
                secret_id=None,
                max_age_days=90,
                pending_max_hours=24,
                require_automatic_rotation=False,
                now=NOW,
            )

    def test_describe_and_version_stage_metadata_must_agree(self) -> None:
        aws = self._aws()
        secret = aws.responses[("secretsmanager", "describe-secret")]
        secret["VersionIdsToStages"] = {"current": ["AWSPREVIOUS"]}
        with self.assertRaisesRegex(
            check_secret_rotation.SecretValidationError,
            "metadata is inconsistent",
        ):
            check_secret_rotation.validate_secret(
                aws,
                stack_name="AxonLLMStack",
                secret_id=None,
                max_age_days=90,
                pending_max_hours=24,
                require_automatic_rotation=False,
                now=NOW,
            )

    def test_checker_has_no_secret_content_read_path(self) -> None:
        source = Path(check_secret_rotation.__file__).read_text(encoding="utf-8")
        self.assertNotIn("get-secret-value", source)
        self.assertNotIn("SecretString", source)


class RecoveryValidationTests(unittest.TestCase):
    def _aws(self, *, backup_age_hours: int = 4) -> FakeAws:
        table_arn = "arn:aws:dynamodb:us-east-1:123456789012:table/axonllm-state"
        return FakeAws(
            {
                (
                    "cloudformation",
                    "list-stack-resources",
                ): {"StackResourceSummaries": _stack_resources()},
                ("cloudformation", "describe-stacks"): {
                    "Stacks": [
                        {
                            "Outputs": [
                                {
                                    "OutputKey": "StateBackupVaultArn",
                                    "OutputValue": ("arn:aws:backup:us-east-1:123456789012:backup-vault:axonllm-state"),
                                }
                            ]
                        }
                    ]
                },
                ("dynamodb", "describe-table"): {
                    "Table": {
                        "TableName": "axonllm-state",
                        "TableArn": table_arn,
                        "TableStatus": "ACTIVE",
                        "DeletionProtectionEnabled": True,
                        "SSEDescription": {"Status": "ENABLED"},
                        "KeySchema": [
                            {"AttributeName": "PK", "KeyType": "HASH"},
                            {"AttributeName": "SK", "KeyType": "RANGE"},
                        ],
                    }
                },
                ("dynamodb", "describe-continuous-backups"): {
                    "ContinuousBackupsDescription": {
                        "PointInTimeRecoveryDescription": {
                            "PointInTimeRecoveryStatus": "ENABLED",
                            "LatestRestorableDateTime": (NOW - timedelta(minutes=5)).isoformat(),
                        }
                    }
                },
                ("backup", "describe-backup-vault"): {
                    "EncryptionKeyArn": ("arn:aws:kms:us-east-1:123456789012:key/backup"),
                    "Locked": False,
                },
                ("backup", "list-recovery-points-by-backup-vault"): {
                    "RecoveryPoints": [
                        {
                            "Status": "COMPLETED",
                            "CreationDate": (NOW - timedelta(hours=backup_age_hours)).isoformat(),
                        }
                    ]
                },
            }
        )

    def _backup_aws(self, *, state: str) -> FakeAws:
        aws = self._aws()
        table_arn = "arn:aws:dynamodb:us-east-1:123456789012:table/axonllm-state"
        role_arn = "arn:aws:iam::123456789012:role/axonllm-state-backup"
        recovery_point_arn = "arn:aws:backup:us-east-1:123456789012:recovery-point:deployment-backup"
        stack = aws.responses[("cloudformation", "describe-stacks")]["Stacks"][0]
        stack["Outputs"].append(
            {
                "OutputKey": "StateBackupRoleArn",
                "OutputValue": role_arn,
            }
        )
        aws.responses[("backup", "start-backup-job")] = {
            "BackupJobId": "deployment-job-123",
            "RecoveryPointArn": recovery_point_arn,
        }
        description: dict[str, Any] = {
            "BackupJobId": "deployment-job-123",
            "State": state,
        }
        if state == "COMPLETED":
            description.update(
                {
                    "BackupVaultName": "axonllm-state",
                    "ResourceArn": table_arn,
                    "RecoveryPointArn": recovery_point_arn,
                    "CreationDate": (NOW - timedelta(minutes=2)).isoformat(),
                    "CompletionDate": NOW.isoformat(),
                }
            )
        aws.responses[("backup", "describe-backup-job")] = description
        aws.responses[("backup", "list-recovery-points-by-backup-vault")] = {
            "RecoveryPoints": [
                {
                    "Status": "COMPLETED",
                    "RecoveryPointArn": recovery_point_arn,
                    "CreationDate": (NOW - timedelta(minutes=2)).isoformat(),
                }
            ]
        }
        return aws

    def test_valid_backup_controls(self) -> None:
        aws = self._aws()
        result = validate_state_recovery.validate_recovery(
            aws,
            stack_name="AxonLLMStack",
            table_name=None,
            max_backup_age_hours=30,
            require_vault_lock=False,
            exercise_restore=False,
            keep_restored_table=False,
            now=NOW,
        )
        self.assertEqual(result["pointInTimeRecovery"], "ENABLED")
        self.assertEqual(result["backupVault"], "axonllm-state")
        self.assertNotIn("deploymentBackup", result)
        self.assertNotIn(
            "start-backup-job",
            [operation for _, operation, _ in aws.calls],
        )

    def test_start_backup_completes_and_records_metadata(self) -> None:
        aws = self._backup_aws(state="COMPLETED")
        clock = FakeClock()
        restore_result = {
            "targetTable": "restored-table",
            "status": "validated",
            "retained": False,
            "sampledItemCount": 1,
            "sampledItemsSha256": "a" * 64,
        }

        with mock.patch.object(
            validate_state_recovery,
            "_exercise_restore",
            return_value=restore_result,
        ) as exercise:
            result = validate_state_recovery.validate_recovery(
                aws,
                stack_name="AxonLLMStack",
                table_name=None,
                max_backup_age_hours=30,
                require_vault_lock=False,
                exercise_restore=False,
                keep_restored_table=False,
                now=NOW,
                start_backup=True,
                backup_job_poll_interval=1,
                backup_job_timeout_seconds=10,
                sleep=clock.sleep,
                monotonic=clock.monotonic,
            )

        self.assertEqual(
            result["deploymentBackup"],
            {
                "backupJobId": "deployment-job-123",
                "status": "COMPLETED",
                "backupVault": "axonllm-state",
                "resourceArn": ("arn:aws:dynamodb:us-east-1:123456789012:table/axonllm-state"),
                "recoveryPointArn": ("arn:aws:backup:us-east-1:123456789012:recovery-point:deployment-backup"),
                "creationDate": (NOW - timedelta(minutes=2)).isoformat(),
                "completionDate": NOW.isoformat(),
            },
        )
        self.assertEqual(result["restoreExercise"], restore_result)
        exercise.assert_called_once()
        self.assertEqual(
            exercise.call_args.kwargs["timeout_seconds"],
            validate_state_recovery._RESTORE_TIMEOUT_SECONDS,
        )
        start_call = next(call for call in aws.calls if call[1] == "start-backup-job")
        self.assertEqual(
            start_call[2],
            (
                "--backup-vault-name",
                "axonllm-state",
                "--resource-arn",
                ("arn:aws:dynamodb:us-east-1:123456789012:table/axonllm-state"),
                "--iam-role-arn",
                ("arn:aws:iam::123456789012:role/axonllm-state-backup"),
                "--lifecycle",
                "MoveToColdStorageAfterDays=30,DeleteAfterDays=365",
                "--recovery-point-tags",
                ("Application=AxonLLM,Runtime=AgentCore,Trigger=Deployment"),
            ),
        )
        operations = [operation for _, operation, _ in aws.calls]
        self.assertLess(
            operations.index("describe-backup-job"),
            operations.index("list-recovery-points-by-backup-vault"),
        )

    def test_recovery_timeouts_fit_workflow_credential_envelope(self) -> None:
        self.assertEqual(
            validate_state_recovery._WORKFLOW_CREDENTIAL_ENVELOPE_SECONDS,
            3 * 60 * 60,
        )
        self.assertEqual(
            validate_state_recovery._BACKUP_JOB_TIMEOUT_SECONDS,
            75 * 60,
        )
        self.assertEqual(
            validate_state_recovery._RESTORE_TIMEOUT_SECONDS,
            45 * 60,
        )
        self.assertEqual(
            validate_state_recovery._BACKUP_JOB_TIMEOUT_SECONDS
            + validate_state_recovery._RESTORE_TIMEOUT_SECONDS
            + validate_state_recovery._RECOVERY_WORKFLOW_MARGIN_SECONDS,
            validate_state_recovery._WORKFLOW_CREDENTIAL_ENVELOPE_SECONDS,
        )

    def test_start_backup_fails_on_terminal_job_failure(self) -> None:
        aws = self._backup_aws(state="FAILED")
        clock = FakeClock()

        with self.assertRaisesRegex(
            validate_state_recovery.RecoveryValidationError,
            "ended in FAILED",
        ):
            validate_state_recovery.validate_recovery(
                aws,
                stack_name="AxonLLMStack",
                table_name=None,
                max_backup_age_hours=30,
                require_vault_lock=False,
                exercise_restore=False,
                keep_restored_table=False,
                now=NOW,
                start_backup=True,
                backup_job_poll_interval=1,
                backup_job_timeout_seconds=10,
                sleep=clock.sleep,
                monotonic=clock.monotonic,
            )
        self.assertNotIn(
            "list-recovery-points-by-backup-vault",
            [operation for _, operation, _ in aws.calls],
        )

    def test_start_backup_fails_closed_on_timeout(self) -> None:
        aws = self._backup_aws(state="RUNNING")
        clock = FakeClock()

        with self.assertRaisesRegex(
            validate_state_recovery.RecoveryValidationError,
            "did not complete before timeout",
        ):
            validate_state_recovery.validate_recovery(
                aws,
                stack_name="AxonLLMStack",
                table_name=None,
                max_backup_age_hours=30,
                require_vault_lock=False,
                exercise_restore=False,
                keep_restored_table=False,
                now=NOW,
                start_backup=True,
                backup_job_poll_interval=1,
                backup_job_timeout_seconds=3,
                sleep=clock.sleep,
                monotonic=clock.monotonic,
            )
        describe_calls = [call for call in aws.calls if call[1] == "describe-backup-job"]
        self.assertEqual(len(describe_calls), 3)
        self.assertEqual(clock.value, 3)

    def test_stale_backup_fails(self) -> None:
        with self.assertRaisesRegex(
            validate_state_recovery.RecoveryValidationError,
            "outside the allowed age",
        ):
            validate_state_recovery.validate_recovery(
                self._aws(backup_age_hours=31),
                stack_name="AxonLLMStack",
                table_name=None,
                max_backup_age_hours=30,
                require_vault_lock=False,
                exercise_restore=False,
                keep_restored_table=False,
                now=NOW,
            )

    def test_vault_lock_can_be_required(self) -> None:
        with self.assertRaisesRegex(
            validate_state_recovery.RecoveryValidationError,
            "Vault Lock",
        ):
            validate_state_recovery.validate_recovery(
                self._aws(),
                stack_name="AxonLLMStack",
                table_name=None,
                max_backup_age_hours=30,
                require_vault_lock=True,
                exercise_restore=False,
                keep_restored_table=False,
                now=NOW,
            )

    def test_required_vault_lock_checks_governance_retention(self) -> None:
        aws = self._aws()
        vault = aws.responses[("backup", "describe-backup-vault")]
        vault.update(
            Locked=True,
            MinRetentionDays=30,
            MaxRetentionDays=365,
        )

        result = validate_state_recovery.validate_recovery(
            aws,
            stack_name="AxonLLMStack",
            table_name=None,
            max_backup_age_hours=30,
            require_vault_lock=True,
            exercise_restore=False,
            keep_restored_table=False,
            now=NOW,
        )

        self.assertEqual(result["backupVaultLockMode"], "GOVERNANCE")
        self.assertEqual(result["backupVaultMinRetentionDays"], 30)
        self.assertEqual(result["backupVaultMaxRetentionDays"], 365)

    def test_required_vault_lock_rejects_wrong_retention(self) -> None:
        aws = self._aws()
        vault = aws.responses[("backup", "describe-backup-vault")]
        vault.update(
            Locked=True,
            MinRetentionDays=7,
            MaxRetentionDays=365,
        )

        with self.assertRaisesRegex(
            validate_state_recovery.RecoveryValidationError,
            "30-365",
        ):
            validate_state_recovery.validate_recovery(
                aws,
                stack_name="AxonLLMStack",
                table_name=None,
                max_backup_age_hours=30,
                require_vault_lock=True,
                exercise_restore=False,
                keep_restored_table=False,
                now=NOW,
            )

    def test_required_vault_lock_accepts_compliance_mode(self) -> None:
        aws = self._aws()
        vault = aws.responses[("backup", "describe-backup-vault")]
        vault.update(
            Locked=True,
            MinRetentionDays=30,
            MaxRetentionDays=365,
            LockDate=NOW.isoformat(),
        )

        result = validate_state_recovery.validate_recovery(
            aws,
            stack_name="AxonLLMStack",
            table_name=None,
            max_backup_age_hours=30,
            require_vault_lock=True,
            exercise_restore=False,
            keep_restored_table=False,
            now=NOW,
        )

        self.assertEqual(result["backupVaultLockMode"], "COMPLIANCE")
        self.assertEqual(result["backupVaultMinRetentionDays"], 30)
        self.assertEqual(result["backupVaultMaxRetentionDays"], 365)

    def test_required_vault_lock_rejects_missing_or_invalid_lock_state(
        self,
    ) -> None:
        for locked in (None, False, "true"):
            with self.subTest(locked=locked):
                vault = {
                    "MinRetentionDays": 30,
                    "MaxRetentionDays": 365,
                }
                if locked is not None:
                    vault["Locked"] = locked

                with self.assertRaisesRegex(
                    validate_state_recovery.RecoveryValidationError,
                    "Vault Lock is not enabled",
                ):
                    validate_state_recovery._vault_lock_posture(
                        vault,
                        required=True,
                    )

    def test_restore_exercise_validates_and_cleans_up_temporary_table(self) -> None:
        aws = RestoreAws()
        result = validate_state_recovery._exercise_restore(
            aws,
            {
                "TableName": "axonllm-state",
                "KeySchema": [
                    {"AttributeName": "PK", "KeyType": "HASH"},
                    {"AttributeName": "SK", "KeyType": "RANGE"},
                ],
            },
            keep=False,
            poll_interval=0,
            timeout_seconds=10,
            sleep=lambda _: None,
        )
        self.assertEqual(result["status"], "validated")
        self.assertFalse(result["retained"])
        self.assertEqual(result["sampledItemCount"], 1)
        self.assertEqual(len(result["sampledItemsSha256"]), 64)
        self.assertNotIn("TENANT#tenant-a", json.dumps(result))
        scan_call = next(call for call in aws.calls if call[1] == "scan")
        self.assertEqual(
            scan_call[2][2:],
            (
                "--limit",
                str(validate_state_recovery._RESTORE_SAMPLE_LIMIT),
                "--consistent-read",
            ),
        )
        get_call = next(call for call in aws.calls if call[1] == "get-item")
        self.assertEqual(
            json.loads(get_call[2][get_call[2].index("--key") + 1]),
            {
                "PK": {"S": "TENANT#tenant-a"},
                "SK": {"S": "PROJECT#project-a"},
            },
        )
        self.assertIn("--consistent-read", get_call[2])
        self.assertIn(
            "delete-table",
            [operation for _, operation, _ in aws.calls],
        )

    def test_restore_validation_failure_still_cleans_up(self) -> None:
        aws = RestoreAws(restored_items=[])
        with self.assertRaisesRegex(
            validate_state_recovery.RecoveryValidationError,
            "contains no items",
        ):
            validate_state_recovery._exercise_restore(
                aws,
                {
                    "TableName": "axonllm-state",
                    "KeySchema": [
                        {"AttributeName": "PK", "KeyType": "HASH"},
                        {"AttributeName": "SK", "KeyType": "RANGE"},
                    ],
                },
                keep=False,
                poll_interval=0,
                timeout_seconds=10,
                sleep=lambda _: None,
            )
        self.assertIn(
            "delete-table",
            [operation for _, operation, _ in aws.calls],
        )

    def test_restore_rejects_sample_content_mismatch_and_cleans_up(
        self,
    ) -> None:
        aws = RestoreAws(
            source_items=[_restore_item(document='{"name":"Changed"}')],
        )

        with self.assertRaisesRegex(
            validate_state_recovery.RecoveryValidationError,
            "content differs",
        ):
            validate_state_recovery._exercise_restore(
                aws,
                {
                    "TableName": "axonllm-state",
                    "KeySchema": [
                        {"AttributeName": "PK", "KeyType": "HASH"},
                        {"AttributeName": "SK", "KeyType": "RANGE"},
                    ],
                },
                keep=False,
                poll_interval=0,
                timeout_seconds=10,
                sleep=lambda _: None,
            )

        self.assertIn(
            "delete-table",
            [operation for _, operation, _ in aws.calls],
        )

    def test_restore_rejects_sample_missing_from_source_and_cleans_up(
        self,
    ) -> None:
        aws = RestoreAws(source_items=[])

        with self.assertRaisesRegex(
            validate_state_recovery.RecoveryValidationError,
            "missing from the source",
        ):
            validate_state_recovery._exercise_restore(
                aws,
                {
                    "TableName": "axonllm-state",
                    "KeySchema": [
                        {"AttributeName": "PK", "KeyType": "HASH"},
                        {"AttributeName": "SK", "KeyType": "RANGE"},
                    ],
                },
                keep=False,
                poll_interval=0,
                timeout_seconds=10,
                sleep=lambda _: None,
            )

        self.assertIn(
            "delete-table",
            [operation for _, operation, _ in aws.calls],
        )

    def test_retained_restore_enables_production_data_protections(
        self,
    ) -> None:
        aws = RestoreAws()
        result = validate_state_recovery._exercise_restore(
            aws,
            {
                "TableName": "axonllm-state",
                "KeySchema": [
                    {"AttributeName": "PK", "KeyType": "HASH"},
                    {"AttributeName": "SK", "KeyType": "RANGE"},
                ],
            },
            keep=True,
            poll_interval=0,
            timeout_seconds=10,
            sleep=lambda _: None,
        )

        self.assertTrue(result["retained"])
        self.assertEqual(result["pointInTimeRecovery"], "ENABLED")
        self.assertEqual(result["timeToLive"], "ENABLED")
        self.assertTrue(result["deletionProtection"])
        operations = [operation for _, operation, _ in aws.calls]
        self.assertIn("update-continuous-backups", operations)
        self.assertIn("update-time-to-live", operations)
        self.assertIn("update-table", operations)
        self.assertNotIn("delete-table", operations)

    def test_restore_cleanup_failure_fails_closed(self) -> None:
        aws = RestoreAws(cleanup_error=True)
        with self.assertRaisesRegex(
            validate_state_recovery.RecoveryValidationError,
            "cleanup failed",
        ):
            validate_state_recovery._exercise_restore(
                aws,
                {
                    "TableName": "axonllm-state",
                    "KeySchema": [
                        {"AttributeName": "PK", "KeyType": "HASH"},
                        {"AttributeName": "SK", "KeyType": "RANGE"},
                    ],
                },
                keep=False,
                poll_interval=0,
                timeout_seconds=10,
                sleep=lambda _: None,
            )

    def test_restore_rejects_source_name_that_cannot_preserve_scope(
        self,
    ) -> None:
        aws = RestoreAws()
        with self.assertRaisesRegex(
            validate_state_recovery.RecoveryValidationError,
            "too long",
        ):
            validate_state_recovery._exercise_restore(
                aws,
                {
                    "TableName": "x" * 215,
                    "KeySchema": [
                        {"AttributeName": "PK", "KeyType": "HASH"},
                        {"AttributeName": "SK", "KeyType": "RANGE"},
                    ],
                },
                keep=False,
                poll_interval=0,
                timeout_seconds=10,
                sleep=lambda _: None,
            )
        self.assertEqual(aws.calls, [])

    def test_restore_cleanup_disables_deletion_protection_before_delete(self) -> None:
        aws = RestoreAws(deletion_protection=True)
        validate_state_recovery._exercise_restore(
            aws,
            {
                "TableName": "axonllm-state",
                "KeySchema": [
                    {"AttributeName": "PK", "KeyType": "HASH"},
                    {"AttributeName": "SK", "KeyType": "RANGE"},
                ],
            },
            keep=False,
            poll_interval=0,
            timeout_seconds=10,
            sleep=lambda _: None,
        )
        operations = [operation for _, operation, _ in aws.calls]
        self.assertLess(
            operations.index("update-table"),
            operations.index("delete-table"),
        )


if __name__ == "__main__":
    unittest.main()
