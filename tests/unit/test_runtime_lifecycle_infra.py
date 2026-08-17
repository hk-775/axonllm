"""CDK contracts for empty AgentCore lifecycle stack shells."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest


_REPO = Path(__file__).resolve().parents[2]
_INFRA = _REPO / "infra"
_INFRA_PYTHON = _INFRA / ".venv" / "bin" / "python"
_STACKS = {
    "agentcore-parked": "AxonLLMAgentCoreStack",
    "managed-network-parked": "AxonLLMManagedNetworkStack",
}


def _synth(
    tmp_path: Path,
    *,
    target: str,
    namespace: str = "",
) -> tuple[str, dict]:
    if not _INFRA_PYTHON.is_file():
        pytest.skip("infra/.venv is required for CDK synthesis tests")
    out_dir = tmp_path / target
    context = {
        "account": "123456789012",
        "deployment_target": target,
        "region": "us-east-1",
    }
    if namespace:
        context["deployment_namespace"] = namespace
    environment = os.environ.copy()
    environment.update(
        {
            "CDK_CONTEXT_JSON": json.dumps(context),
            "CDK_OUTDIR": str(out_dir),
            "JSII_RUNTIME_PACKAGE_CACHE_ROOT": str(tmp_path / "jsii-cache"),
            "PYTHONPYCACHEPREFIX": str(tmp_path / "pycache"),
        }
    )
    completed = subprocess.run(
        [str(_INFRA_PYTHON), "app.py"],
        cwd=_INFRA,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    stack_name = _STACKS[target]
    if namespace:
        stack_name = f"{stack_name}-{namespace}"
    path = out_dir / f"{stack_name}.template.json"
    return stack_name, json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("target", sorted(_STACKS))
def test_parked_shell_reuses_active_stack_name_and_creates_nothing(
    tmp_path: Path,
    target: str,
) -> None:
    stack_name, template = _synth(tmp_path, target=target)

    assert stack_name == _STACKS[target]
    assert template["Resources"] == {
        "ParkedSentinel": {
            "Type": "AWS::CloudFormation::WaitConditionHandle",
            "Condition": "NeverCreateParkedSentinel",
        }
    }
    assert template["Conditions"] == {"NeverCreateParkedSentinel": {"Fn::Equals": ["parked", "active"]}}
    assert "Outputs" not in template
    assert {resource["Type"] for resource in template["Resources"].values()} == {
        "AWS::CloudFormation::WaitConditionHandle"
    }


@pytest.mark.parametrize("target", sorted(_STACKS))
def test_parked_shell_contains_no_retained_or_chargeable_resource(
    tmp_path: Path,
    target: str,
) -> None:
    _, template = _synth(tmp_path, target=target)
    serialized = json.dumps(template, sort_keys=True)

    for forbidden in (
        "AWS::BedrockAgentCore::Runtime",
        "AWS::EC2::",
        "AWS::ECS::",
        "AWS::ElasticLoadBalancingV2::",
        "AWS::DynamoDB::Table",
        "AWS::KMS::Key",
        "AWS::S3::Bucket",
        "AWS::SecretsManager::Secret",
        "AWS::SQS::Queue",
    ):
        assert forbidden not in serialized
    assert "DeletionPolicy" not in serialized
    assert "UpdateReplacePolicy" not in serialized


def test_parked_shell_preserves_qualification_namespace(
    tmp_path: Path,
) -> None:
    stack_name, template = _synth(
        tmp_path,
        target="agentcore-parked",
        namespace="qualification",
    )

    assert stack_name == "AxonLLMAgentCoreStack-qualification"
    assert template["Parameters"]["BootstrapVersion"]["Default"] == ("/cdk-bootstrap/axqual/version")
