from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "ci"))

import validate_workflows  # noqa: E402


CHECKOUT_PIN = validate_workflows.APPROVED_ACTION_PINS["actions/checkout"]
OPERATIONS_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "operations-security.yml"


def _load_operations_workflow() -> dict[str, Any]:
    loaded = yaml.load(
        OPERATIONS_WORKFLOW.read_text(encoding="utf-8"),
        Loader=validate_workflows.WorkflowLoader,
    )
    assert isinstance(loaded, dict)
    return loaded


def _session_policy(job: dict[str, Any]) -> dict[str, Any]:
    for step in job["steps"]:
        if str(step.get("uses", "")).startswith("aws-actions/configure-aws-credentials@"):
            policy = json.loads(step["with"]["inline-session-policy"])
            assert isinstance(policy, dict)
            return policy
    raise AssertionError("AWS credential step is missing")


def _policy_actions(policy: dict[str, Any]) -> set[str]:
    actions: set[str] = set()
    for statement in policy["Statement"]:
        value = statement["Action"]
        if isinstance(value, str):
            actions.add(value)
        else:
            actions.update(value)
    return actions


def _policy_resources(policy: dict[str, Any]) -> set[str]:
    resources: set[str] = set()
    for statement in policy["Statement"]:
        value = statement["Resource"]
        if isinstance(value, str):
            resources.add(value)
        else:
            resources.update(value)
    return resources


class WorkflowPolicyTests(unittest.TestCase):
    def _write(self, body: str) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "workflow.yml"
        path.write_text(body, encoding="utf-8")
        return path

    def test_workflow_loader_does_not_mutate_safe_loader(self) -> None:
        self.assertIs(
            yaml.safe_load("enabled: false\n")["enabled"],
            False,
        )

    def test_accepts_pinned_action_and_restricted_permissions(self) -> None:
        path = self._write(
            f"""
name: Test
on:
  push:
permissions:
  contents: read
jobs:
  test:
    steps:
      - uses: actions/checkout@{CHECKOUT_PIN}
        with:
          persist-credentials: false
"""
        )
        self.assertEqual(validate_workflows.validate_workflow(path), 1)

    def test_rejects_mutable_action_tag(self) -> None:
        path = self._write(
            """
name: Test
on:
  push:
permissions:
  contents: read
jobs:
  test:
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: false
"""
        )
        with self.assertRaisesRegex(
            validate_workflows.WorkflowPolicyError,
            "not pinned",
        ):
            validate_workflows.validate_workflow(path)

    def test_rejects_checkout_credentials(self) -> None:
        path = self._write(
            f"""
name: Test
on:
  push:
permissions:
  contents: read
jobs:
  test:
    steps:
      - uses: actions/checkout@{CHECKOUT_PIN}
"""
        )
        with self.assertRaisesRegex(
            validate_workflows.WorkflowPolicyError,
            "persist-credentials",
        ):
            validate_workflows.validate_workflow(path)

    def test_rejects_unapproved_action(self) -> None:
        path = self._write(
            f"""
name: Test
on:
  push:
permissions:
  contents: read
jobs:
  test:
    steps:
      - uses: example/unreviewed-action@{"a" * 40}
"""
        )
        with self.assertRaisesRegex(
            validate_workflows.WorkflowPolicyError,
            "not approved",
        ):
            validate_workflows.validate_workflow(path)

    def test_rejects_unapproved_action_pin(self) -> None:
        path = self._write(
            f"""
name: Test
on:
  push:
permissions:
  contents: read
jobs:
  test:
    steps:
      - uses: actions/checkout@{"a" * 40}
        with:
          persist-credentials: false
"""
        )
        with self.assertRaisesRegex(
            validate_workflows.WorkflowPolicyError,
            "pin is not approved",
        ):
            validate_workflows.validate_workflow(path)

    def test_rejects_repository_write_permission(self) -> None:
        path = self._write(
            """
name: Test
on:
  push:
permissions:
  contents: write
jobs:
  test:
    steps:
      - run: true
"""
        )
        with self.assertRaisesRegex(
            validate_workflows.WorkflowPolicyError,
            "write permission is not allowlisted",
        ):
            validate_workflows.validate_workflow(path)

    def test_rejects_github_attestation_write_permission(self) -> None:
        path = self._write(
            """
name: Test
on:
  push:
permissions:
  attestations: write
jobs:
  test:
    steps:
      - run: true
"""
        )
        with self.assertRaisesRegex(
            validate_workflows.WorkflowPolicyError,
            "write permission is not allowlisted",
        ):
            validate_workflows.validate_workflow(path)

    def test_rejects_pull_request_target(self) -> None:
        path = self._write(
            """
name: Test
on:
  pull_request_target:
permissions:
  contents: read
jobs:
  test:
    steps:
      - run: true
"""
        )
        with self.assertRaisesRegex(
            validate_workflows.WorkflowPolicyError,
            "prohibited",
        ):
            validate_workflows.validate_workflow(path)

    def test_operations_audit_role_is_metadata_only(self) -> None:
        workflow = _load_operations_workflow()
        audit = workflow["jobs"]["audit"]
        policy = _session_policy(audit)
        actions = _policy_actions(policy)

        self.assertEqual(
            audit["steps"][2]["with"]["role-to-assume"],
            "${{ secrets.AXON_OPERATIONS_AUDIT_ROLE_ARN }}",
        )
        self.assertNotIn("secretsmanager:GetSecretValue", actions)
        self.assertNotIn("kms:Decrypt", actions)
        self.assertIn("secretsmanager:DescribeSecret", actions)
        self.assertIn("secretsmanager:ListSecretVersionIds", actions)
        self.assertFalse(any(action.startswith("backup:") for action in actions))
        self.assertFalse(any(action.startswith("kms:") for action in actions))
        self.assertEqual(
            {action for action in actions if action.startswith("dynamodb:")},
            {
                "dynamodb:DescribeContinuousBackups",
                "dynamodb:DescribeTable",
            },
        )
        self.assertNotIn("*", _policy_resources(policy))
        secret_step = next(step for step in audit["steps"] if step.get("name") == "Validate provider-secret rotation")
        self.assertEqual(
            secret_step["if"],
            "${{ matrix.validate_secret && !cancelled() }}",
        )
        targets = {item["target"] for item in audit["strategy"]["matrix"]["include"]}
        self.assertEqual(targets, {"fargate", "agentcore"})
        table_names = {item["target"]: item["table_name"] for item in audit["strategy"]["matrix"]["include"]}
        self.assertEqual(
            table_names,
            {
                "fargate": ("${{ vars.AXON_FARGATE_STATE_TABLE_NAME || 'axonllm-state' }}"),
                "agentcore": ("${{ vars.AXON_AGENTCORE_STATE_TABLE_NAME || 'axonllm-agentcore-state' }}"),
            },
        )

    def test_operations_restore_is_monthly_and_uses_separate_role(self) -> None:
        workflow = _load_operations_workflow()
        schedules = {entry["cron"] for entry in workflow["on"]["schedule"]}
        self.assertEqual(schedules, {"30 10 * * *", "30 11 1 * *"})

        audit = workflow["jobs"]["audit"]
        recovery = workflow["jobs"]["recovery"]
        self.assertIn("30 10 * * *", audit["if"])
        self.assertNotIn("30 11 1 * *", audit["if"])
        self.assertIn("30 11 1 * *", recovery["if"])
        self.assertIn("inputs.exercise_restore", recovery["if"])
        dispatch_inputs = workflow["on"]["workflow_dispatch"]["inputs"]
        self.assertIn("retain_fargate_restore", dispatch_inputs)
        self.assertEqual(
            dispatch_inputs["retain_fargate_restore"]["default"],
            "false",
        )
        self.assertIn("retain_agentcore_restore", dispatch_inputs)
        self.assertEqual(
            dispatch_inputs["retain_agentcore_restore"]["default"],
            "false",
        )
        self.assertGreaterEqual(recovery["timeout-minutes"], 60)
        self.assertEqual(
            recovery["steps"][2]["with"]["role-to-assume"],
            "${{ secrets.AXON_OPERATIONS_RECOVERY_ROLE_ARN }}",
        )
        role_duration = recovery["steps"][2]["with"]["role-duration-seconds"]
        self.assertGreater(
            role_duration,
            recovery["timeout-minutes"] * 60,
        )
        targets = {item["target"] for item in recovery["strategy"]["matrix"]["include"]}
        self.assertEqual(targets, {"fargate", "agentcore"})
        restore = next(step for step in recovery["steps"] if step.get("id") == "restore")
        self.assertIn(
            "matrix.target == 'fargate'",
            restore["env"]["RETAIN_RESTORE"],
        )
        self.assertIn(
            "matrix.target == 'agentcore'",
            restore["env"]["RETAIN_RESTORE"],
        )
        self.assertIn("--keep-restored-table", restore["run"])
        evidence = next(step for step in recovery["steps"] if step.get("name") == "Preserve recovery evidence")
        self.assertTrue(evidence["uses"].startswith("actions/upload-artifact@"))
        self.assertEqual(evidence["with"]["retention-days"], 90)
        self.assertEqual(
            {item["target"]: item["table_name"] for item in recovery["strategy"]["matrix"]["include"]},
            {
                "fargate": ("${{ vars.AXON_FARGATE_STATE_TABLE_NAME || 'axonllm-state' }}"),
                "agentcore": ("${{ vars.AXON_AGENTCORE_STATE_TABLE_NAME || 'axonllm-agentcore-state' }}"),
            },
        )
        recovery_commands = "\n".join(step.get("run", "") for step in recovery["steps"])
        self.assertIn("--exercise-restore", recovery_commands)
        audit_commands = "\n".join(step.get("run", "") for step in audit["steps"])
        self.assertNotIn("--exercise-restore", audit_commands)

    def test_operations_recovery_policy_is_resource_scoped(self) -> None:
        workflow = _load_operations_workflow()
        policy = _session_policy(workflow["jobs"]["recovery"])
        actions = _policy_actions(policy)
        resources = _policy_resources(policy)
        account = "${{ vars.AXON_AWS_ACCOUNT_ID }}"
        source_table = f"arn:aws:dynamodb:us-east-1:{account}:table/${{{{ matrix.table_name }}}}"
        restore_tables = source_table + "-restore-validation-*"

        self.assertIn("dynamodb:RestoreTableToPointInTime", actions)
        self.assertIn(source_table, resources)
        self.assertIn(restore_tables, resources)
        self.assertNotIn("*", resources)
        self.assertFalse(any(action.startswith("secretsmanager:") for action in actions))
        self.assertFalse(any(action.startswith("backup:") for action in actions))
        self.assertFalse(any(action.startswith("kms:") for action in actions))

        restore_statement = next(
            statement for statement in policy["Statement"] if statement["Resource"] == restore_tables
        )
        self.assertEqual(
            set(restore_statement["Action"]),
            {
                "dynamodb:DeleteTable",
                "dynamodb:DescribeContinuousBackups",
                "dynamodb:DescribeTable",
                "dynamodb:DescribeTimeToLive",
                "dynamodb:RestoreTableToPointInTime",
                "dynamodb:Scan",
                "dynamodb:UpdateContinuousBackups",
                "dynamodb:UpdateTable",
                "dynamodb:UpdateTimeToLive",
            },
        )

    def test_operations_session_policies_fit_the_sts_limit(self) -> None:
        workflow = _load_operations_workflow()
        for job_name in ("audit", "recovery"):
            policy = next(
                step["with"]["inline-session-policy"]
                for step in workflow["jobs"][job_name]["steps"]
                if "inline-session-policy" in step.get("with", {})
            )
            self.assertLessEqual(
                len(policy.encode("utf-8")),
                2048,
                job_name,
            )

    def test_operations_workflow_passes_repository_policy(self) -> None:
        self.assertEqual(
            validate_workflows.validate_workflow(OPERATIONS_WORKFLOW),
            7,
        )


if __name__ == "__main__":
    unittest.main()
