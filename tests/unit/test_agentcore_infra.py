"""Synthesized security contracts for the Bedrock AgentCore deployment."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import types

import pytest


_REPO = Path(__file__).resolve().parents[2]
_INFRA = _REPO / "infra"
_PACKAGED_INFRA = _REPO / "src" / "gateway" / "deployment" / "infra"
_STACK = _REPO / "src" / "gateway" / "deployment" / "infra" / "agentcore_stack.py"
_INFRA_PYTHON = _INFRA / ".venv" / "bin" / "python"
_DOCKERFILE = _INFRA / "agentcore-image" / "Dockerfile"
_SUPPORTED_AZS = (
    _PACKAGED_INFRA
    / "agentcore-supported-availability-zones-v1.json"
)
_REQUIRED_PARAMETERS = {
    "AlarmNotificationEmail",
    "CandidateEndpointName",
    "OidcIssuer",
    "OidcDiscoveryUrl",
    "OidcClientIds",
    "OidcAudiences",
    "OidcTenantClaim",
    "OidcProjectClaim",
    "DeploymentExperience",
    "ApprovedHttpsPrefixListId",
    "BedrockInvokeResourceArns",
    "VerifiedImageUri",
}
_BEDROCK_ACTIONS = {
    "bedrock:InvokeModel",
    "bedrock:InvokeModelWithResponseStream",
}
_MANTLE_ACTIONS = {
    "bedrock-mantle:CreateInference",
    "bedrock-mantle:ListModels",
}
_DEFAULT_ENABLED_PROVIDERS = {
    "anthropic",
    "bedrock",
    "bedrock-mantle",
    "fireworks",
    "google_ai",
    "groq",
    "openai",
    "together",
    "xai",
}
_OPTIONAL_ENABLED_PROVIDERS = {
    "ai21",
    "azure_openai",
    "cohere",
    "vertex_ai",
}
_ENABLED_PROVIDERS = _DEFAULT_ENABLED_PROVIDERS | _OPTIONAL_ENABLED_PROVIDERS
_SQS_ACTIONS = {
    "sqs:ChangeMessageVisibility",
    "sqs:DeleteMessage",
    "sqs:GetQueueAttributes",
    "sqs:ReceiveMessage",
    "sqs:SendMessage",
}
_DYNAMODB_ACTIONS = {
    "dynamodb:BatchGetItem",
    "dynamodb:BatchWriteItem",
    "dynamodb:ConditionCheckItem",
    "dynamodb:DeleteItem",
    "dynamodb:DescribeTable",
    "dynamodb:GetItem",
    "dynamodb:PutItem",
    "dynamodb:Query",
    "dynamodb:Scan",
    "dynamodb:TransactWriteItems",
    "dynamodb:UpdateItem",
}
_DYNAMODB_STANDARD_ACTIONS = _DYNAMODB_ACTIONS - {"dynamodb:TransactWriteItems"}


def _resources(template: dict, resource_type: str) -> list[dict]:
    return [resource for resource in template["Resources"].values() if resource["Type"] == resource_type]


def _one_resource(template: dict, resource_type: str) -> dict:
    resources = _resources(template, resource_type)
    assert len(resources) == 1, f"expected one {resource_type}, got {len(resources)}"
    return resources[0]


def _logical_resource(
    template: dict,
    resource_type: str,
    description_prefix: str,
) -> tuple[str, dict]:
    matches = [
        (logical_id, resource)
        for logical_id, resource in template["Resources"].items()
        if resource["Type"] == resource_type
        and resource["Properties"].get("GroupDescription", "").startswith(description_prefix)
    ]
    assert len(matches) == 1
    return matches[0]


def _actions(statement: dict) -> set[str]:
    actions = statement["Action"]
    if isinstance(actions, str):
        return {actions}
    return set(actions)


def _values_for_key(value: object, wanted: str) -> list[object]:
    if isinstance(value, dict):
        found = [child for key, child in value.items() if key == wanted]
        for child in value.values():
            found.extend(_values_for_key(child, wanted))
        return found
    if isinstance(value, list):
        found: list[object] = []
        for child in value:
            found.extend(_values_for_key(child, wanted))
        return found
    return []


@pytest.fixture(scope="module")
def synthesized(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[dict, dict]:
    if not _INFRA_PYTHON.is_file():
        pytest.skip("infra/.venv is required for CDK synthesis tests")

    work_dir = tmp_path_factory.mktemp("agentcore-infra")
    out_dir = work_dir / "cdk.out"
    environment = os.environ.copy()
    environment.update(
        {
            "CDK_CONTEXT_JSON": json.dumps(
                {
                    "deployment_target": "agentcore",
                    "region": "us-east-1",
                }
            ),
            "CDK_OUTDIR": str(out_dir),
            "JSII_RUNTIME_PACKAGE_CACHE_ROOT": str(work_dir / "jsii-cache"),
            "PYTHONPYCACHEPREFIX": str(work_dir / "pycache"),
        }
    )
    completed = subprocess.run(
        [str(_INFRA_PYTHON), "app.py"],
        cwd=_INFRA,
        env=environment,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stdout
    template = json.loads((out_dir / "AxonLLMAgentCoreStack.template.json").read_text(encoding="utf-8"))
    assets = json.loads((out_dir / "AxonLLMAgentCoreStack.assets.json").read_text(encoding="utf-8"))
    return template, assets


@pytest.fixture(scope="module")
def synthesized_template(synthesized: tuple[dict, dict]) -> dict:
    return synthesized[0]


def _synthesize_external_template(
    *,
    infra: Path,
    work_dir: Path,
) -> dict:
    out_dir = work_dir / "cdk.out"
    environment = os.environ.copy()
    environment.update(
        {
            "CDK_CONTEXT_JSON": json.dumps(
                {
                    "deployment_namespace": "external",
                    "deployment_target": "agentcore",
                    "region": "us-east-1",
                }
            ),
            "CDK_OUTDIR": str(out_dir),
            "JSII_RUNTIME_PACKAGE_CACHE_ROOT": str(work_dir / "jsii-cache"),
            "PYTHONPYCACHEPREFIX": str(work_dir / "pycache"),
        }
    )
    completed = subprocess.run(
        [str(_INFRA_PYTHON), "app.py"],
        cwd=infra,
        env=environment,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stdout
    return json.loads((out_dir / "AxonLLMAgentCoreStack-external.template.json").read_text(encoding="utf-8"))


def _synthesize_agentcore_with_zone_ids(
    *,
    work_dir: Path,
    zone_ids: list[str],
) -> tuple[subprocess.CompletedProcess[str], Path]:
    out_dir = work_dir / "cdk.out"
    environment = os.environ.copy()
    environment.update(
        {
            "CDK_CONTEXT_JSON": json.dumps(
                {
                    "agentcore_availability_zone_ids": zone_ids,
                    "deployment_target": "agentcore",
                    "region": "us-east-1",
                }
            ),
            "CDK_OUTDIR": str(out_dir),
            "JSII_RUNTIME_PACKAGE_CACHE_ROOT": str(
                work_dir / "jsii-cache"
            ),
            "PYTHONPYCACHEPREFIX": str(work_dir / "pycache"),
        }
    )
    completed = subprocess.run(
        [str(_INFRA_PYTHON), "app.py"],
        cwd=_INFRA,
        env=environment,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
    )
    return completed, out_dir


@pytest.fixture(scope="module")
def external_synthesized_template(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict:
    if not _INFRA_PYTHON.is_file():
        pytest.skip("infra/.venv is required for CDK synthesis tests")
    return _synthesize_external_template(
        infra=_INFRA,
        work_dir=tmp_path_factory.mktemp("agentcore-external-infra"),
    )


@pytest.fixture(scope="module")
def packaged_external_synthesized_template(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict:
    if not _INFRA_PYTHON.is_file():
        pytest.skip("infra/.venv is required for CDK synthesis tests")
    return _synthesize_external_template(
        infra=_PACKAGED_INFRA,
        work_dir=tmp_path_factory.mktemp("packaged-agentcore-external-infra"),
    )


def test_deployment_inputs_are_required_and_bedrock_arns_are_concrete(
    synthesized_template,
):
    parameters = synthesized_template["Parameters"]
    assert _REQUIRED_PARAMETERS <= set(parameters)
    for name in _REQUIRED_PARAMETERS:
        assert "Default" not in parameters[name]

    assert parameters["OidcIssuer"]["AllowedPattern"].startswith("^https://")
    assert parameters["OidcDiscoveryUrl"]["AllowedPattern"].endswith(r"/\.well-known/openid-configuration$")
    assert parameters["OidcTenantClaim"]["AllowedPattern"] == r"^\S+$"
    assert parameters["OidcProjectClaim"]["AllowedPattern"] == r"^\S+$"
    assert parameters["DeploymentExperience"]["AllowedValues"] == [
        "axonllm",
        "ostiari",
    ]
    assert parameters["ApprovedHttpsPrefixListId"]["AllowedPattern"] == ("^pl-[0-9a-fA-F]+$")
    assert "Bedrock Mantle" in parameters["ApprovedHttpsPrefixListId"]["Description"]
    assert parameters["VerifiedImageUri"]["AllowedPattern"].endswith(r"@sha256:[0-9a-f]{64}$")
    bedrock = parameters["BedrockInvokeResourceArns"]
    assert bedrock["Type"] == "CommaDelimitedList"
    assert "foundation-model" in bedrock["AllowedPattern"]
    assert "inference-profile" in bedrock["AllowedPattern"]
    assert "without wildcards" in bedrock["ConstraintDescription"]
    candidate = parameters["CandidateEndpointName"]
    assert candidate["AllowedPattern"] == "^candidate_[0-9a-f]{32}$"
    assert candidate["MinLength"] == candidate["MaxLength"] == 42


def test_recovery_parameters_are_scoped_and_phase_gated(
    synthesized_template,
):
    parameters = synthesized_template["Parameters"]
    recovery_table = parameters["RuntimeStateTableName"]
    assert recovery_table["Default"] == ""
    assert recovery_table["AllowedPattern"].startswith(r"^$|^axonllm\-agentcore\-state-restore-validation-")
    assert parameters["RecoveryCutoverMode"] == {
        "Type": "String",
        "Default": "normal",
        "AllowedValues": [
            "normal",
            "quiesced",
            "selected",
            "validation",
        ],
        "Description": ("AgentCore recovery phase; table changes are accepted only from quiesced to selected"),
    }
    approval = parameters["RecoveryApprovalId"]
    assert approval["Default"] == ""
    assert approval["MaxLength"] == 128
    assert "change/incident ID" in approval["ConstraintDescription"]

    conditions = synthesized_template["Conditions"]
    assert conditions["UseRecoveredState"]["Fn::Not"]
    assert conditions["RecoveryAccessBlocked"] == {
        "Fn::Or": [
            {"Condition": "RecoveryQuiesced"},
            {"Condition": "RecoverySelected"},
        ]
    }


def test_runtime_uses_two_private_azs_and_explicit_security_groups(
    synthesized_template,
):
    assert len(_resources(synthesized_template, "AWS::EC2::NatGateway")) == 2
    subnets_by_id = {
        logical_id: resource
        for logical_id, resource in synthesized_template[
            "Resources"
        ].items()
        if resource["Type"] == "AWS::EC2::Subnet"
    }
    assert set(subnets_by_id) == {
        "VpcPublicSubnet1Subnet5C2D37C4",
        "VpcPublicSubnet2Subnet691E08A3",
        "VpcRuntimeSubnet1SubnetE207618E",
        "VpcRuntimeSubnet2Subnet0DEB747D",
    }
    subnets = list(subnets_by_id.values())
    assert len(subnets) == 4
    assert all(subnet["Properties"]["MapPublicIpOnLaunch"] is False for subnet in subnets)
    assert all("AvailabilityZone" not in subnet["Properties"] for subnet in subnets)
    zone_ids = [
        subnet["Properties"]["AvailabilityZoneId"]
        for subnet in subnets
    ]
    assert set(zone_ids) == {"use1-az1", "use1-az2"}
    assert zone_ids.count("use1-az1") == 2
    assert zone_ids.count("use1-az2") == 2

    runtime = _one_resource(
        synthesized_template,
        "AWS::BedrockAgentCore::Runtime",
    )["Properties"]
    network = runtime["NetworkConfiguration"]
    assert network["NetworkMode"] == "VPC"
    config = network["NetworkModeConfig"]
    assert len(config["SecurityGroups"]) == 1
    assert config["SecurityGroups"][0]["Fn::GetAtt"][0].startswith("RuntimeSecurityGroup")
    assert len(config["Subnets"]) == 2
    assert all(subnet["Ref"].startswith("VpcRuntimeSubnet") for subnet in config["Subnets"])


def test_agentcore_supported_availability_zone_document_is_versioned():
    document = json.loads(_SUPPORTED_AZS.read_text(encoding="utf-8"))
    assert document["schema"] == (
        "axonllm.agentcore-supported-availability-zones/v1"
    )
    assert document["regions"]["us-east-1"] == [
        "use1-az1",
        "use1-az2",
        "use1-az4",
    ]


def test_agentcore_availability_zone_context_override(
    tmp_path,
):
    if not _INFRA_PYTHON.is_file():
        pytest.skip("infra/.venv is required for CDK synthesis tests")

    completed, out_dir = _synthesize_agentcore_with_zone_ids(
        work_dir=tmp_path,
        zone_ids=["use1-az4", "use1-az1"],
    )
    assert completed.returncode == 0, completed.stdout
    template = json.loads(
        (
            out_dir / "AxonLLMAgentCoreStack.template.json"
        ).read_text(encoding="utf-8")
    )
    subnets = _resources(template, "AWS::EC2::Subnet")
    zone_ids = [
        subnet["Properties"]["AvailabilityZoneId"]
        for subnet in subnets
    ]
    assert set(zone_ids) == {"use1-az4", "use1-az1"}
    assert zone_ids.count("use1-az4") == 2
    assert zone_ids.count("use1-az1") == 2


def test_agentcore_rejects_unsupported_availability_zone_id(
    tmp_path,
):
    if not _INFRA_PYTHON.is_file():
        pytest.skip("infra/.venv is required for CDK synthesis tests")

    completed, _ = _synthesize_agentcore_with_zone_ids(
        work_dir=tmp_path,
        zone_ids=["use1-az4", "use1-az6"],
    )
    assert completed.returncode != 0
    assert (
        "unsupported AgentCore Availability Zone IDs: use1-az6"
        in completed.stdout
    )


def test_endpoint_ingress_is_only_from_the_runtime_security_group(
    synthesized_template,
):
    runtime_group_id, runtime_group = _logical_resource(
        synthesized_template,
        "AWS::EC2::SecurityGroup",
        "AxonLLM AgentCore",
    )
    endpoint_group_id, endpoint_group = _logical_resource(
        synthesized_template,
        "AWS::EC2::SecurityGroup",
        "Private AWS service endpoints",
    )
    assert "SecurityGroupIngress" not in endpoint_group["Properties"]

    ingress = _resources(
        synthesized_template,
        "AWS::EC2::SecurityGroupIngress",
    )
    assert len(ingress) == 1
    rule = ingress[0]["Properties"]
    assert rule["Description"] == "HTTPS from the AgentCore runtime"
    assert rule["FromPort"] == rule["ToPort"] == 443
    assert rule["GroupId"] == {"Fn::GetAtt": [endpoint_group_id, "GroupId"]}
    assert rule["SourceSecurityGroupId"] == {"Fn::GetAtt": [runtime_group_id, "GroupId"]}

    security_group_resources = {
        logical_id: resource
        for logical_id, resource in synthesized_template["Resources"].items()
        if resource["Type"]
        in {
            "AWS::EC2::SecurityGroup",
            "AWS::EC2::SecurityGroupEgress",
            "AWS::EC2::SecurityGroupIngress",
        }
    }
    assert "0.0.0.0/0" not in _values_for_key(
        security_group_resources,
        "CidrIp",
    )
    assert "::/0" not in _values_for_key(
        security_group_resources,
        "CidrIpv6",
    )

    runtime_egress = {
        resource["Properties"]["Description"]
        for resource in _resources(
            synthesized_template,
            "AWS::EC2::SecurityGroupEgress",
        )
        if resource["Properties"]["GroupId"]["Fn::GetAtt"][0] == runtime_group_id
    }
    assert runtime_egress == {
        "HTTPS to explicitly approved external destinations",
        "DynamoDB through the VPC gateway endpoint",
        "AWS services through private interface endpoints",
    }
    assert {rule["Description"] for rule in runtime_group["Properties"]["SecurityGroupEgress"]} == {
        "DNS to the VPC resolver",
        "DNS fallback to the VPC resolver",
    }


def test_service_endpoints_are_private_and_resource_scoped(
    synthesized_template,
):
    endpoints = _resources(synthesized_template, "AWS::EC2::VPCEndpoint")
    assert len(endpoints) == 6
    gateway = next(endpoint for endpoint in endpoints if endpoint["Properties"]["VpcEndpointType"] == "Gateway")[
        "Properties"
    ]
    interfaces = [endpoint for endpoint in endpoints if endpoint["Properties"]["VpcEndpointType"] == "Interface"]
    bedrock_interface = next(
        endpoint["Properties"]
        for endpoint in interfaces
        if endpoint["Properties"]["ServiceName"].endswith(".bedrock-runtime")
    )
    sqs_interface = next(
        endpoint["Properties"] for endpoint in interfaces if endpoint["Properties"]["ServiceName"].endswith(".sqs")
    )
    sns_interface = next(
        endpoint["Properties"] for endpoint in interfaces if endpoint["Properties"]["ServiceName"].endswith(".sns")
    )
    logs_interface = next(
        endpoint["Properties"] for endpoint in interfaces if endpoint["Properties"]["ServiceName"].endswith(".logs")
    )
    secrets_interface = next(
        endpoint["Properties"]
        for endpoint in interfaces
        if endpoint["Properties"]["ServiceName"].endswith(".secretsmanager")
    )

    assert len(gateway["RouteTableIds"]) == 2
    assert all(route["Ref"].startswith("VpcRuntimeSubnet") for route in gateway["RouteTableIds"])
    dynamodb_statement = gateway["PolicyDocument"]["Statement"][0]
    assert _actions(dynamodb_statement) == _DYNAMODB_ACTIONS
    assert "UseRecoveredState" in json.dumps(dynamodb_statement["Resource"])
    assert "RuntimeStateTableName" in json.dumps(dynamodb_statement["Resource"])

    assert len(interfaces) == 5
    for interface in (
        bedrock_interface,
        sqs_interface,
        sns_interface,
        logs_interface,
        secrets_interface,
    ):
        assert interface["PrivateDnsEnabled"] is True
        assert len(interface["SubnetIds"]) == 2
        assert all(subnet["Ref"].startswith("VpcRuntimeSubnet") for subnet in interface["SubnetIds"])
    bedrock_statement = bedrock_interface["PolicyDocument"]["Statement"][0]
    assert _actions(bedrock_statement) == _BEDROCK_ACTIONS
    assert bedrock_statement["Resource"] == {"Ref": "BedrockInvokeResourceArns"}
    sqs_statement = sqs_interface["PolicyDocument"]["Statement"][0]
    assert _actions(sqs_statement) == _SQS_ACTIONS
    assert sqs_statement["Resource"]["Fn::GetAtt"][0].startswith("SecurityEventOutboxQueue")
    sns_statement = sns_interface["PolicyDocument"]["Statement"][0]
    assert _actions(sns_statement) == {"sns:Publish"}
    assert sns_statement["Resource"]["Ref"].startswith("SecurityEventTopic")
    logs_statement = logs_interface["PolicyDocument"]["Statement"][0]
    assert _actions(logs_statement) == {
        "logs:CreateLogStream",
        "logs:PutLogEvents",
    }
    assert logs_statement["Resource"][0]["Fn::GetAtt"][0].startswith("SecurityEventLogGroup")
    secrets_statement = secrets_interface["PolicyDocument"]["Statement"][0]
    assert _actions(secrets_statement) == {
        "secretsmanager:DescribeSecret",
        "secretsmanager:GetSecretValue",
    }
    assert secrets_statement["Resource"]["Ref"].startswith("ProviderCredentials")
    assert "SecurityEventLogGroup" in json.dumps(logs_statement["Resource"][1])


def test_production_runtime_uses_iam_facade_and_bounded_lifecycle(
    synthesized_template,
):
    runtime = _one_resource(
        synthesized_template,
        "AWS::BedrockAgentCore::Runtime",
    )["Properties"]
    assert "AuthorizerConfiguration" not in runtime
    assert runtime["RequestHeaderConfiguration"] == {
        "RequestHeaderAllowlist": ["X-Amzn-Bedrock-AgentCore-Runtime-Custom-Identity-Token"]
    }
    assert runtime["LifecycleConfiguration"] == {
        "IdleRuntimeSessionTimeout": 600,
        "MaxLifetime": 14400,
    }
    assert runtime["ProtocolConfiguration"] == "HTTP"

    environment = runtime["EnvironmentVariables"]
    assert environment["AXON_AUTH_MODE"] == "ENFORCE"
    assert environment["AXON_DEPLOYMENT_PROFILE"] == "production"
    assert environment["AXON_EXPERIENCE_OWNER"] == {"Ref": "DeploymentExperience"}
    assert environment["AXON_EXECUTION_TARGET"] == "agentcore"
    assert environment["AXON_LOAD_DEMO_DATA"] == "false"
    assert environment["AXON_OIDC_ISSUER"] == {"Ref": "OidcIssuer"}
    assert environment["AXON_OIDC_AUDIENCE"] == {"Fn::Join": [",", {"Ref": "OidcAudiences"}]}
    assert environment["AXON_OIDC_TENANT_CLAIM"] == {"Ref": "OidcTenantClaim"}
    assert environment["AXON_OIDC_PROJECT_CLAIM"] == {"Ref": "OidcProjectClaim"}
    assert environment["AXON_REQUIRE_CANONICAL_IDENTITY"] == "true"
    assert environment["AXON_AGENTCORE_FACADE_IDENTITY_ALLOWED"] == "true"
    assert environment["AXON_ENABLED_PROVIDERS"] == {"Ref": "EnabledProviders"}
    default_providers = synthesized_template["Parameters"]["EnabledProviders"]["Default"].split(",")
    assert default_providers == sorted(_DEFAULT_ENABLED_PROVIDERS)
    allowed_pattern = synthesized_template["Parameters"]["EnabledProviders"]["AllowedPattern"]
    assert all(provider in allowed_pattern for provider in _OPTIONAL_ENABLED_PROVIDERS)
    assert environment["AXON_PROVIDER_SECRET_ARN"]["Ref"].startswith("ProviderCredentials")
    assert environment["AXON_PROVIDER_SECRET_VERSION"] == {"Ref": "ProviderSecretVersion"}
    assert environment["LLM_ROUTER_DYNAMODB_ENABLED"] == "true"
    assert environment["AXON_DYNAMODB_TABLE"]["Fn::If"][:2] == [
        "UseRecoveredState",
        {"Ref": "RuntimeStateTableName"},
    ]
    assert environment["AXON_AWS_ACCOUNT_ID"] == {"Ref": "AWS::AccountId"}
    assert environment["AXON_EVENT_OUTBOX_QUEUE_URL"]["Ref"].startswith("SecurityEventOutboxQueue")
    assert environment["AXON_SECURITY_EVENT_SNS_TOPIC_ARN"]["Ref"].startswith("SecurityEventTopic")
    assert environment["AXON_SECURITY_EVENT_LOG_GROUP_ARN"]["Fn::GetAtt"][0].startswith("SecurityEventLogGroup")
    assert synthesized_template["Outputs"]["RuntimeIngressMode"] == {"Value": "facade"}

    endpoints = _resources(
        synthesized_template,
        "AWS::BedrockAgentCore::RuntimeEndpoint",
    )
    assert len(endpoints) == 3
    production = next(endpoint for endpoint in endpoints if endpoint["Properties"]["Name"] == "production")
    candidate = next(
        endpoint for endpoint in endpoints if endpoint["Properties"]["Name"] == {"Ref": "CandidateEndpointName"}
    )
    recovery = next(endpoint for endpoint in endpoints if endpoint["Properties"]["Name"] == "recovery")
    assert production["Condition"] == ("ProductionEndpointEnabled")
    assert candidate["Condition"] == ("CandidateEndpointEnabled")
    assert recovery["Condition"] == "RecoveryValidation"
    assert production["Properties"]["AgentRuntimeVersion"] == {"Ref": "ProductionRuntimeVersion"}
    assert all(
        endpoint["Properties"]["AgentRuntimeVersion"]["Fn::GetAtt"][1] == "AgentRuntimeVersion"
        for endpoint in (candidate, recovery)
    )
    assert all(
        any(dependency.startswith("RecoveryGuard") for dependency in endpoint["DependsOn"]) for endpoint in endpoints
    )


def test_external_runtime_keeps_direct_jwt_authorizer(
    external_synthesized_template,
):
    runtime = _one_resource(
        external_synthesized_template,
        "AWS::BedrockAgentCore::Runtime",
    )["Properties"]
    authorizer = runtime["AuthorizerConfiguration"]["CustomJWTAuthorizer"]
    assert authorizer["DiscoveryUrl"] == {"Ref": "OidcDiscoveryUrl"}
    assert runtime["RequestHeaderConfiguration"] == {"RequestHeaderAllowlist": ["Authorization"]}
    assert runtime["EnvironmentVariables"]["AXON_AGENTCORE_FACADE_IDENTITY_ALLOWED"] == "false"
    assert external_synthesized_template["Outputs"]["RuntimeIngressMode"] == {"Value": "direct-jwt"}


def test_runtime_role_is_scoped_and_supports_state_transactions(
    synthesized_template,
):
    runtime_policy = next(
        resource
        for logical_id, resource in synthesized_template["Resources"].items()
        if logical_id.startswith("RuntimeExecutionRoleDefaultPolicy")
    )
    statements = runtime_policy["Properties"]["PolicyDocument"]["Statement"]
    bedrock = next(statement for statement in statements if "bedrock:InvokeModel" in _actions(statement))
    assert _actions(bedrock) == _BEDROCK_ACTIONS
    assert bedrock["Resource"] == {"Ref": "BedrockInvokeResourceArns"}

    mantle = next(statement for statement in statements if "bedrock-mantle:CreateInference" in _actions(statement))
    assert _actions(mantle) == _MANTLE_ACTIONS
    assert mantle["Resource"] == "*"

    secret_read = next(statement for statement in statements if "secretsmanager:GetSecretValue" in _actions(statement))
    assert _actions(secret_read) == {
        "secretsmanager:DescribeSecret",
        "secretsmanager:GetSecretValue",
    }
    assert secret_read["Resource"]["Ref"].startswith("ProviderCredentials")
    assert secret_read["Sid"] == "ReadProviderCredentials"

    state_access = next(statement for statement in statements if "dynamodb:ConditionCheckItem" in _actions(statement))
    assert {
        "dynamodb:ConditionCheckItem",
        "dynamodb:DeleteItem",
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
    } <= _actions(state_access)
    assert "dynamodb:TransactWriteItems" not in _actions(state_access)
    assert state_access["Sid"] == "UseSelectedStateTable"
    assert "UseRecoveredState" in json.dumps(state_access["Resource"])
    assert "RuntimeStateTableName" in json.dumps(state_access["Resource"])
    all_actions = {action for statement in statements for action in _actions(statement)}
    assert "dynamodb:TransactGetItems" not in all_actions
    assert "dynamodb:TransactWriteItems" not in all_actions

    transaction_policy = next(
        resource
        for logical_id, resource in synthesized_template["Resources"].items()
        if logical_id.startswith("RuntimeDynamoTransactionPolicy")
    )
    assert transaction_policy["Metadata"] == {"cfn-lint": {"config": {"ignore_checks": ["W3037"]}}}
    transaction_statement = transaction_policy["Properties"]["PolicyDocument"]["Statement"][0]
    assert _actions(transaction_statement) == {"dynamodb:TransactWriteItems"}
    assert transaction_statement["Sid"] == ("TransactWithSelectedStateTable")
    assert "UseRecoveredState" in json.dumps(transaction_statement["Resource"])

    queue_statements = [
        statement for statement in statements if any(action.startswith("sqs:") for action in _actions(statement))
    ]
    assert len(queue_statements) == 1
    queue_access = queue_statements[0]
    assert _actions(queue_access) == _SQS_ACTIONS
    assert queue_access["Sid"] == "UseSecurityEventOutbox"
    assert queue_access["Resource"]["Fn::GetAtt"][0].startswith("SecurityEventOutboxQueue")
    publish = next(statement for statement in statements if _actions(statement) == {"sns:Publish"})
    assert publish["Sid"] == "PublishSecurityEvents"
    assert publish["Resource"]["Ref"].startswith("SecurityEventTopic")

    kms_statements = [
        statement for statement in statements if any(action.startswith("kms:") for action in _actions(statement))
    ]
    assert len(kms_statements) == 1

    def kms_statement(sid: str) -> dict:
        return next(statement for statement in kms_statements if statement["Sid"] == sid)

    topic_key = kms_statement("UseSecurityEventTopicKey")
    assert _actions(topic_key) == {
        "kms:Decrypt",
        "kms:GenerateDataKey*",
    }
    topic_condition = topic_key["Condition"]["StringEquals"]
    assert topic_condition["kms:CallerAccount"] == {"Ref": "AWS::AccountId"}
    assert "sns." in json.dumps(topic_condition["kms:ViaService"])
    assert "AWS::URLSuffix" in json.dumps(topic_condition["kms:ViaService"])
    assert "SecurityEventTopic" in json.dumps(topic_condition["kms:EncryptionContext:aws:sns:topicArn"])
    assert topic_key["Resource"] == "*"

    security_log_write = next(
        statement
        for statement in statements
        if _actions(statement) == {"logs:CreateLogStream", "logs:PutLogEvents"}
        and isinstance(statement["Resource"], dict)
        and "Fn::GetAtt" in statement["Resource"]
    )
    assert security_log_write["Resource"]["Fn::GetAtt"][0].startswith("SecurityEventLogGroup")

    image_pull = next(statement for statement in statements if "ecr:BatchGetImage" in _actions(statement))
    assert _actions(image_pull) == {
        "ecr:BatchCheckLayerAvailability",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
    }
    assert ":repository/" in image_pull["Resource"]["Fn::Join"][1]
    assert "VerifiedImageUri" in json.dumps(image_pull["Resource"])
    image_authorization = next(
        statement for statement in statements if "ecr:GetAuthorizationToken" in _actions(statement)
    )
    assert image_authorization["Resource"] == "*"

    runtime_role = next(
        resource
        for resource in _resources(synthesized_template, "AWS::IAM::Role")
        if resource["Properties"].get("Description") == "Execution role for Bedrock Agent Core Runtime"
    )
    trust = runtime_role["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]
    assert trust["Principal"] == {"Service": "bedrock-agentcore.amazonaws.com"}
    assert trust["Condition"]["StringEquals"]["aws:SourceAccount"] == {"Ref": "AWS::AccountId"}
    assert "aws:SourceArn" in trust["Condition"]["ArnLike"]


def test_recovery_guard_blocks_access_and_checks_runtime_and_control_plane(
    synthesized_template,
):
    resources = synthesized_template["Resources"]
    deny_id, deny = next(
        (logical_id, resource)
        for logical_id, resource in resources.items()
        if logical_id.startswith("RecoveryStateAccessDeny")
    )
    statement = deny["Properties"]["PolicyDocument"]["Statement"][0]
    assert statement["Effect"] == "Deny"
    assert _actions(statement) == _DYNAMODB_STANDARD_ACTIONS
    deny_target = statement["Resource"]["Fn::If"]
    assert deny_target[0] == "RecoveryAccessBlocked"
    assert deny_target[1] == "*"
    assert "recovery_access_not_blocked" in json.dumps(deny_target[2])

    transaction_deny_id, transaction_deny = next(
        (logical_id, resource)
        for logical_id, resource in resources.items()
        if logical_id.startswith("RecoveryStateTransactionAccessDeny")
    )
    assert transaction_deny["Metadata"] == {"cfn-lint": {"config": {"ignore_checks": ["W3037"]}}}
    transaction_statement = transaction_deny["Properties"]["PolicyDocument"]["Statement"][0]
    assert transaction_statement["Effect"] == "Deny"
    assert _actions(transaction_statement) == {"dynamodb:TransactWriteItems"}
    assert transaction_statement["Resource"] == statement["Resource"]

    guard = resources["RecoveryGuard"]
    assert deny_id in guard["DependsOn"]
    assert transaction_deny_id in guard["DependsOn"]
    assert guard["Properties"]["MinimumQuiescenceSeconds"] == "14700"
    assert guard["Properties"]["Mode"] == {"Ref": "RecoveryCutoverMode"}
    assert guard["Properties"]["PrimaryTable"]["Ref"].startswith("StateTable")
    assert guard["Properties"]["SelectedTable"]["Fn::If"][:2] == [
        "UseRecoveredState",
        {"Ref": "RuntimeStateTableName"},
    ]
    runtime = _one_resource(
        synthesized_template,
        "AWS::BedrockAgentCore::Runtime",
    )
    assert "RecoveryGuard" in runtime["DependsOn"]

    handler = next(
        resource
        for logical_id, resource in resources.items()
        if logical_id.startswith("RecoveryGuardHandler") and resource["Type"] == "AWS::Lambda::Function"
    )
    code = handler["Properties"]["Code"]["ZipFile"]
    assert '("quiesced", "selected")' in code
    assert '("validation", "normal")' in code
    assert "_assert_no_runtime_endpoints" in code
    assert "_assert_control_plane_quiesced" in code
    assert "MinimumQuiescenceSeconds" in code

    guard_role_policy = next(
        resource
        for logical_id, resource in resources.items()
        if logical_id.startswith("RecoveryGuardHandlerServiceRoleDefaultPolicy")
    )
    guard_actions = {
        action
        for policy_statement in guard_role_policy["Properties"]["PolicyDocument"]["Statement"]
        for action in _actions(policy_statement)
    }
    assert guard_actions == {
        "application-autoscaling:DescribeScalableTargets",
        "bedrock-agentcore:ListAgentRuntimeEndpoints",
        "bedrock-agentcore:ListAgentRuntimes",
        "cloudformation:DescribeStacks",
        "ecs:DescribeServices",
    }

    outputs = synthesized_template["Outputs"]
    assert outputs["RuntimeEndpointArn"]["Condition"] == ("ProductionEndpointEnabled")
    assert outputs["RuntimeEndpointName"]["Condition"] == ("ProductionEndpointEnabled")
    assert outputs["ProductionRuntimeVersion"]["Condition"] == ("ProductionEndpointEnabled")
    assert outputs["CandidateRuntimeEndpointArn"]["Condition"] == ("CandidateEndpointEnabled")
    assert outputs["CandidateRuntimeEndpointName"]["Condition"] == ("CandidateEndpointEnabled")
    assert outputs["CandidateRuntimeVersion"]["Condition"] == ("CandidateEndpointEnabled")
    assert outputs["CandidateRuntimeEndpointName"]["Value"] == {"Ref": "CandidateEndpointName"}
    assert outputs["ApprovedHttpsPrefixListId"]["Value"] == {"Ref": "ApprovedHttpsPrefixListId"}
    assert outputs["BedrockInvokeResourceArns"]["Value"] == {"Fn::Join": [",", {"Ref": "BedrockInvokeResourceArns"}]}
    assert "AthenaConfigurationFingerprint" not in outputs
    assert outputs["AlarmNotificationEmail"]["Value"] == {"Ref": "AlarmNotificationEmail"}
    assert outputs["DeploymentExperience"]["Value"] == {"Ref": "DeploymentExperience"}
    assert outputs["DeploymentExecution"]["Value"] == "agentcore"
    assert outputs["RecoveryRuntimeEndpointArn"]["Condition"] == ("RecoveryValidation")
    selected = outputs["SelectedRuntimeStateTableName"]["Value"]["Fn::If"]
    assert selected[:2] == [
        "UseRecoveredState",
        {"Ref": "RuntimeStateTableName"},
    ]
    assert outputs["RecoveryCutoverMode"]["Value"] == {"Ref": "RecoveryCutoverMode"}
    assert outputs["RecoveryMinimumQuiescenceSeconds"]["Value"] == "14700"


def test_recovery_guard_allows_only_quiesced_reviewed_table_switches(
    synthesized_template,
    monkeypatch,
):
    handler = next(
        resource
        for logical_id, resource in synthesized_template["Resources"].items()
        if logical_id.startswith("RecoveryGuardHandler") and resource["Type"] == "AWS::Lambda::Function"
    )

    class _ClientError(Exception):
        def __init__(self):
            self.response = {
                "Error": {
                    "Code": "ValidationError",
                    "Message": ("Stack with id AxonLLMControlPlaneStack does not exist"),
                }
            }

    class _Control:
        def list_agent_runtimes(self, **_kwargs):
            return {
                "agentRuntimes": [
                    {
                        "agentRuntimeName": "axonllm",
                        "agentRuntimeId": "axonllm-abcdefghij",
                    }
                ]
            }

        def list_agent_runtime_endpoints(self, **_kwargs):
            return {"runtimeEndpoints": []}

    class _CloudFormation:
        def describe_stacks(self, **_kwargs):
            raise _ClientError()

    clients = {
        "bedrock-agentcore-control": _Control(),
        "cloudformation": _CloudFormation(),
        "ecs": object(),
        "application-autoscaling": object(),
    }
    monkeypatch.setitem(
        sys.modules,
        "boto3",
        types.SimpleNamespace(client=lambda name: clients[name]),
    )
    botocore = types.ModuleType("botocore")
    exceptions = types.ModuleType("botocore.exceptions")
    exceptions.ClientError = _ClientError
    botocore.exceptions = exceptions
    monkeypatch.setitem(sys.modules, "botocore", botocore)
    monkeypatch.setitem(sys.modules, "botocore.exceptions", exceptions)

    namespace: dict[str, object] = {}
    exec(
        compile(
            handler["Properties"]["Code"]["ZipFile"],
            "agentcore_recovery_guard.py",
            "exec",
        ),
        namespace,
    )
    clock = types.SimpleNamespace(now=100)
    monkeypatch.setattr(namespace["time"], "time", lambda: clock.now)
    base = {
        "AgentCoreStackName": "AxonLLMAgentCoreStack",
        "ApprovalId": "",
        "ControlPlaneStackName": "AxonLLMControlPlaneStack",
        "MinimumQuiescenceSeconds": "14700",
        "Mode": "normal",
        "PrimaryTable": "axonllm-agentcore-state",
        "RuntimeName": "axonllm",
        "SelectedTable": "axonllm-agentcore-state",
    }
    created = namespace["handler"](
        {"RequestType": "Create", "ResourceProperties": base},
        None,
    )
    assert created["PhysicalResourceId"] == ("AxonLLMAgentCoreRecoveryGuard")

    quiesced = {
        **base,
        "ApprovalId": "INC-2026-001",
        "Mode": "quiesced",
    }
    entered = namespace["handler"](
        {
            "RequestType": "Update",
            "PhysicalResourceId": created["PhysicalResourceId"],
            "OldResourceProperties": base,
            "ResourceProperties": quiesced,
        },
        None,
    )
    assert entered["PhysicalResourceId"].endswith(":100")

    selected = {
        **quiesced,
        "Mode": "selected",
        "SelectedTable": ("axonllm-agentcore-state-restore-validation-safe"),
    }
    clock.now = 14799
    with pytest.raises(RuntimeError, match="too recent"):
        namespace["handler"](
            {
                "RequestType": "Update",
                "PhysicalResourceId": entered["PhysicalResourceId"],
                "OldResourceProperties": quiesced,
                "ResourceProperties": selected,
            },
            None,
        )
    clock.now = 14800
    allowed = namespace["handler"](
        {
            "RequestType": "Update",
            "PhysicalResourceId": entered["PhysicalResourceId"],
            "OldResourceProperties": quiesced,
            "ResourceProperties": selected,
        },
        None,
    )
    assert allowed["Data"]["QuiescedAt"].startswith("1970-01-01")

    with pytest.raises(RuntimeError, match="require a blocked"):
        namespace["handler"](
            {
                "RequestType": "Update",
                "PhysicalResourceId": created["PhysicalResourceId"],
                "OldResourceProperties": base,
                "ResourceProperties": {
                    **base,
                    "SelectedTable": selected["SelectedTable"],
                },
            },
            None,
        )


def test_state_uses_aws_managed_encryption_and_pitr_without_aws_backup(
    synthesized_template,
):
    table = _one_resource(synthesized_template, "AWS::DynamoDB::Table")
    properties = table["Properties"]
    assert properties["DeletionProtectionEnabled"] is True
    assert properties["PointInTimeRecoverySpecification"] == {"PointInTimeRecoveryEnabled": True}
    assert properties["SSESpecification"] == {"SSEEnabled": True}
    assert properties["TimeToLiveSpecification"] == {
        "AttributeName": "expires_at",
        "Enabled": True,
    }
    assert table["DeletionPolicy"] == "Retain"
    assert table["UpdateReplacePolicy"] == "Retain"

    assert _resources(synthesized_template, "AWS::KMS::Key") == []
    assert _resources(synthesized_template, "AWS::KMS::Alias") == []
    assert not any(
        resource["Type"].startswith("AWS::Backup::") for resource in synthesized_template["Resources"].values()
    )
    outputs = synthesized_template["Outputs"]
    assert "DataKeyArn" not in outputs
    assert "StateBackupVaultArn" not in outputs
    assert "StateBackupRoleArn" not in outputs


def test_external_namespace_uses_pitr_without_scheduled_backups(
    external_synthesized_template,
):
    table = _one_resource(
        external_synthesized_template,
        "AWS::DynamoDB::Table",
    )
    assert table["Properties"]["PointInTimeRecoverySpecification"] == {"PointInTimeRecoveryEnabled": True}
    assert not _resources(external_synthesized_template, "AWS::KMS::Key")
    assert not _resources(external_synthesized_template, "AWS::KMS::Alias")
    assert not _resources(
        external_synthesized_template,
        "AWS::Backup::BackupVault",
    )
    assert not _resources(
        external_synthesized_template,
        "AWS::Backup::BackupPlan",
    )
    assert not _resources(
        external_synthesized_template,
        "AWS::Backup::BackupSelection",
    )
    assert all(
        resource["Properties"].get("Description") != "AWS Backup service role scoped to AxonLLM AgentCore state"
        for resource in _resources(
            external_synthesized_template,
            "AWS::IAM::Role",
        )
    )
    outputs = external_synthesized_template["Outputs"]
    assert "StateBackupVaultArn" not in outputs
    assert "StateBackupRoleArn" not in outputs


def test_packaged_external_namespace_uses_pitr_without_scheduled_backups(
    packaged_external_synthesized_template,
):
    assert not _resources(packaged_external_synthesized_template, "AWS::KMS::Key")
    assert not _resources(packaged_external_synthesized_template, "AWS::KMS::Alias")
    assert not _resources(
        packaged_external_synthesized_template,
        "AWS::Backup::BackupVault",
    )
    assert not _resources(
        packaged_external_synthesized_template,
        "AWS::Backup::BackupPlan",
    )
    assert not _resources(
        packaged_external_synthesized_template,
        "AWS::Backup::BackupSelection",
    )


def test_no_aws_backup_service_role_or_policy_is_created(
    synthesized_template,
):
    serialized = json.dumps(synthesized_template)
    assert "backup.amazonaws.com" not in serialized
    assert "dynamodb:StartAwsBackupJob" not in serialized
    assert "AWS::Backup::" not in serialized


def test_security_event_outbox_is_fifo_encrypted_and_redriven(
    synthesized_template,
):
    queues = _resources(synthesized_template, "AWS::SQS::Queue")
    assert len(queues) == 2
    assert all(queue["Properties"]["FifoQueue"] is True for queue in queues)
    assert all(queue["Properties"]["ContentBasedDeduplication"] is False for queue in queues)
    assert all(queue["Properties"]["SqsManagedSseEnabled"] is True for queue in queues)
    assert all("KmsMasterKeyId" not in queue["Properties"] for queue in queues)
    assert all(queue["Properties"]["MessageRetentionPeriod"] == 1209600 for queue in queues)
    outbox = next(queue for queue in queues if "RedrivePolicy" in queue["Properties"])
    assert outbox["Properties"]["ReceiveMessageWaitTimeSeconds"] == 20
    assert outbox["Properties"]["VisibilityTimeout"] == 120
    assert outbox["Properties"]["RedrivePolicy"]["maxReceiveCount"] == 5
    assert outbox["DeletionPolicy"] == "Retain"
    assert outbox["UpdateReplacePolicy"] == "Retain"

    queue_policies = _resources(
        synthesized_template,
        "AWS::SQS::QueuePolicy",
    )
    assert len(queue_policies) == 2
    for policy in queue_policies:
        statement = policy["Properties"]["PolicyDocument"]["Statement"][0]
        assert statement["Effect"] == "Deny"
        assert statement["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}

    security_topic = next(
        topic
        for topic in _resources(synthesized_template, "AWS::SNS::Topic")
        if topic["Properties"].get("DisplayName") == "AxonLLM AgentCore durable security events"
    )
    assert security_topic["Properties"]["FifoTopic"] is True
    assert security_topic["Properties"]["ContentBasedDeduplication"] is False
    assert "alias/aws/sns" in json.dumps(security_topic["Properties"]["KmsMasterKeyId"])
    security_topic_policy = next(
        policy
        for policy in _resources(
            synthesized_template,
            "AWS::SNS::TopicPolicy",
        )
        if policy["Properties"]["PolicyDocument"]["Statement"][0].get("Sid") == "AllowPublishThroughSSLOnly"
    )
    transport_statement = security_topic_policy["Properties"]["PolicyDocument"]["Statement"][0]
    assert transport_statement["Effect"] == "Deny"
    assert transport_statement["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}

    outputs = synthesized_template["Outputs"]
    assert outputs["SecurityEventOutboxQueueUrl"]["Value"]["Ref"].startswith("SecurityEventOutboxQueue")
    assert outputs["SecurityEventDeadLetterQueueUrl"]["Value"]["Ref"].startswith("SecurityEventDeadLetterQueue")
    assert outputs["SecurityEventDeadLetterQueueArn"]["Value"]["Fn::GetAtt"][0].startswith(
        "SecurityEventDeadLetterQueue"
    )
    assert outputs["SecurityEventDeadLettersAlarmArn"]["Value"]["Fn::GetAtt"][0].startswith(
        "SecurityEventDeadLettersAlarm"
    )
    assert outputs["SecurityEventTopicArn"]["Value"]["Ref"].startswith("SecurityEventTopic")
    assert outputs["SecurityEventLogGroupArn"]["Value"]["Fn::GetAtt"][0].startswith("SecurityEventLogGroup")
    assert "DataKeyArn" not in outputs
    assert outputs["ProviderSecretArn"]["Value"]["Ref"].startswith("ProviderCredentials")

    provider_secret = _one_resource(
        synthesized_template,
        "AWS::SecretsManager::Secret",
    )
    assert provider_secret["DeletionPolicy"] == "Retain"
    assert provider_secret["UpdateReplacePolicy"] == "Retain"
    assert "Name" not in provider_secret["Properties"]
    template = json.loads(provider_secret["Properties"]["GenerateSecretString"]["SecretStringTemplate"])
    assert {
        "ANTHROPIC_API_KEY",
        "GCP_CREDENTIALS_JSON",
        "OPENAI_API_KEY",
        "GOOGLE_AI_API_KEY",
        "XAI_API_KEY",
        "GROQ_API_KEY",
        "TOGETHER_API_KEY",
        "FIREWORKS_API_KEY",
        "AI21_API_KEY",
    } <= set(template)
    assert "GCP_ACCESS_TOKEN" not in template


def test_encrypted_logs_and_alarm_delivery_have_service_permissions(
    synthesized_template,
):
    retained_logs = [
        log_group
        for log_group in _resources(
            synthesized_template,
            "AWS::Logs::LogGroup",
        )
        if log_group["DeletionPolicy"] == "Retain"
    ]
    assert len(retained_logs) == 5
    assert all(log_group["Properties"]["RetentionInDays"] == 365 for log_group in retained_logs)
    assert all("KmsKeyId" not in log_group["Properties"] for log_group in retained_logs)
    delivery_types = {
        resource["Properties"]["LogType"]
        for resource in _resources(
            synthesized_template,
            "AWS::Logs::DeliverySource",
        )
    }
    assert delivery_types == {"APPLICATION_LOGS", "TRACES", "USAGE_LOGS"}

    topic = next(
        topic
        for topic in _resources(synthesized_template, "AWS::SNS::Topic")
        if topic["Properties"].get("DisplayName") == "AxonLLM AgentCore production alarms"
    )
    assert "KmsMasterKeyId" not in topic["Properties"]
    assert _resources(synthesized_template, "AWS::KMS::Key") == []
    topic_policy = next(
        policy
        for policy in _resources(
            synthesized_template,
            "AWS::SNS::TopicPolicy",
        )
        if policy["Properties"]["PolicyDocument"]["Statement"][0].get("Sid") == "AllowAccountCloudWatchAlarms"
    )["Properties"]["PolicyDocument"]["Statement"][0]
    assert topic_policy["Action"] == "sns:Publish"
    assert topic_policy["Principal"] == {"Service": "cloudwatch.amazonaws.com"}
    assert topic_policy["Condition"]["StringEquals"]["aws:SourceAccount"] == {"Ref": "AWS::AccountId"}
    subscription = _one_resource(
        synthesized_template,
        "AWS::SNS::Subscription",
    )
    assert subscription["Properties"]["Protocol"] == "email"
    assert subscription["Properties"]["Endpoint"] == {"Ref": "AlarmNotificationEmail"}
    assert subscription["Properties"]["TopicArn"]["Ref"].startswith("AlarmTopic")

    alarms = _resources(synthesized_template, "AWS::CloudWatch::Alarm")
    assert len(alarms) == 4
    assert all(alarm["Properties"]["AlarmActions"] for alarm in alarms)
    assert all(alarm["Properties"]["OKActions"] for alarm in alarms)
    assert {alarm["Properties"].get("MetricName") for alarm in alarms} >= {
        "ApproximateNumberOfMessagesVisible",
        "SystemErrors",
        "Throttles",
    }
    monitored_operations = {
        dimension["Value"]
        for alarm in alarms
        for metric in alarm["Properties"].get("Metrics", [])
        for dimension in metric.get("MetricStat", {}).get("Metric", {}).get("Dimensions", [])
        if dimension["Name"] == "Operation"
    }
    assert {
        "TransactGetItems",
        "TransactWriteItems",
    } <= monitored_operations
    assert len(_resources(synthesized_template, "AWS::CloudWatch::Dashboard")) == 1


def test_runtime_image_is_release_verified_digest_pinned_and_non_root(
    synthesized,
):
    template, assets = synthesized
    assert assets["dockerImages"] == {}

    runtime = _one_resource(
        template,
        "AWS::BedrockAgentCore::Runtime",
    )["Properties"]
    assert runtime["AgentRuntimeArtifact"] == {"ContainerConfiguration": {"ContainerUri": {"Ref": "VerifiedImageUri"}}}
    assert template["Outputs"]["RuntimeImageUri"]["Value"] == {"Ref": "VerifiedImageUri"}
    assert "from_asset" not in _STACK.read_text(encoding="utf-8")

    dockerfile = _DOCKERFILE.read_text(encoding="utf-8")
    assert (
        "# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e"
    ) in dockerfile
    assert (
        "FROM docker.io/library/python:3.12-slim@sha256:"
        "0568e6111802e74c03e8dda76565cdf4b88881d77de0d9b769846e9dfcb8d80a"
    ) in dockerfile
    assert (
        "COPY --from=ghcr.io/astral-sh/uv:0.10.7@sha256:"
        "5fe7b2b0499a485ee86e1e0d2154e1556a08dffeb5f72b204895af5212a2069c"
    ) in dockerfile
    assert "COPY --from=project pyproject.toml uv.lock ./" in dockerfile
    assert "uv sync --frozen --no-dev" in dockerfile
    assert "UV_NO_CACHE=1" in dockerfile
    assert "HOME=/tmp" in dockerfile
    assert "chmod -R a-w /app" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "/ready" in dockerfile
