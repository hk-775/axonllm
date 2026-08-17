"""Synthesized security contracts for the Bedrock AgentCore deployment."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import types
import zlib

import pytest


_REPO = Path(__file__).resolve().parents[2]
_INFRA = _REPO / "infra"
_STACK = (
    _REPO
    / "src"
    / "gateway"
    / "deployment"
    / "infra"
    / "agentcore_stack.py"
)
_INFRA_PYTHON = _INFRA / ".venv" / "bin" / "python"
_DOCKERFILE = _INFRA / "agentcore-image" / "Dockerfile"
_REQUIRED_PARAMETERS = {
    "AlarmNotificationEmail",
    "CandidateEndpointName",
    "InitialRoutingConfigZlibBase64",
    "OidcIssuer",
    "OidcDiscoveryUrl",
    "OidcClientIds",
    "OidcAudiences",
    "OidcTenantClaim",
    "OidcProjectClaim",
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
_ENABLED_PROVIDERS = (
    _DEFAULT_ENABLED_PROVIDERS | _OPTIONAL_ENABLED_PROVIDERS
)
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
_DYNAMODB_STANDARD_ACTIONS = _DYNAMODB_ACTIONS - {
    "dynamodb:TransactWriteItems"
}


def _resources(template: dict, resource_type: str) -> list[dict]:
    return [
        resource
        for resource in template["Resources"].values()
        if resource["Type"] == resource_type
    ]


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
        and resource["Properties"].get("GroupDescription", "").startswith(
            description_prefix
        )
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
    template = json.loads(
        (out_dir / "AxonLLMAgentCoreStack.template.json").read_text(
            encoding="utf-8"
        )
    )
    assets = json.loads(
        (out_dir / "AxonLLMAgentCoreStack.assets.json").read_text(
            encoding="utf-8"
        )
    )
    return template, assets


@pytest.fixture(scope="module")
def synthesized_template(synthesized: tuple[dict, dict]) -> dict:
    return synthesized[0]


def test_deployment_inputs_are_required_and_bedrock_arns_are_concrete(
    synthesized_template,
):
    parameters = synthesized_template["Parameters"]
    assert _REQUIRED_PARAMETERS <= set(parameters)
    for name in _REQUIRED_PARAMETERS:
        assert "Default" not in parameters[name]

    assert parameters["OidcIssuer"]["AllowedPattern"].startswith("^https://")
    assert parameters["OidcDiscoveryUrl"]["AllowedPattern"].endswith(
        r"/\.well-known/openid-configuration$"
    )
    assert parameters["OidcTenantClaim"]["AllowedPattern"] == r"^\S+$"
    assert parameters["OidcProjectClaim"]["AllowedPattern"] == r"^\S+$"
    assert parameters["ApprovedHttpsPrefixListId"]["AllowedPattern"] == (
        "^pl-[0-9a-fA-F]+$"
    )
    assert "Bedrock Mantle" in parameters["ApprovedHttpsPrefixListId"][
        "Description"
    ]
    assert parameters["VerifiedImageUri"]["AllowedPattern"].endswith(
        r"@sha256:[0-9a-f]{64}$"
    )
    bedrock = parameters["BedrockInvokeResourceArns"]
    assert bedrock["Type"] == "CommaDelimitedList"
    assert "foundation-model" in bedrock["AllowedPattern"]
    assert "inference-profile" in bedrock["AllowedPattern"]
    assert "without wildcards" in bedrock["ConstraintDescription"]
    candidate = parameters["CandidateEndpointName"]
    assert candidate["AllowedPattern"] == "^candidate_[0-9a-f]{32}$"
    assert candidate["MinLength"] == candidate["MaxLength"] == 42
    routing_config = parameters["InitialRoutingConfigZlibBase64"]
    assert routing_config["MaxLength"] == 4096
    assert routing_config["AllowedPattern"] == (
        "^[A-Za-z0-9+/]+={0,2}$"
    )


def test_recovery_parameters_are_scoped_and_phase_gated(
    synthesized_template,
):
    parameters = synthesized_template["Parameters"]
    recovery_table = parameters["RuntimeStateTableName"]
    assert recovery_table["Default"] == ""
    assert recovery_table["AllowedPattern"].startswith(
        r"^$|^axonllm\-agentcore\-state-restore-validation-"
    )
    assert parameters["RecoveryCutoverMode"] == {
        "Type": "String",
        "Default": "normal",
        "AllowedValues": [
            "normal",
            "quiesced",
            "selected",
            "validation",
        ],
        "Description": (
            "AgentCore recovery phase; table changes are accepted only "
            "from quiesced to selected"
        ),
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
    subnets = _resources(synthesized_template, "AWS::EC2::Subnet")
    assert len(subnets) == 4
    assert all(
        subnet["Properties"]["MapPublicIpOnLaunch"] is False
        for subnet in subnets
    )

    runtime = _one_resource(
        synthesized_template,
        "AWS::BedrockAgentCore::Runtime",
    )["Properties"]
    network = runtime["NetworkConfiguration"]
    assert network["NetworkMode"] == "VPC"
    config = network["NetworkModeConfig"]
    assert len(config["SecurityGroups"]) == 1
    assert config["SecurityGroups"][0]["Fn::GetAtt"][0].startswith(
        "RuntimeSecurityGroup"
    )
    assert len(config["Subnets"]) == 2
    assert all(
        subnet["Ref"].startswith("VpcRuntimeSubnet")
        for subnet in config["Subnets"]
    )


def test_endpoint_ingress_is_only_from_runtime_and_routing_seeder_groups(
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
    seeder_group_id, _ = _logical_resource(
        synthesized_template,
        "AWS::EC2::SecurityGroup",
        "One-shot signed routing configuration bootstrap",
    )
    assert "SecurityGroupIngress" not in endpoint_group["Properties"]

    ingress = _resources(
        synthesized_template,
        "AWS::EC2::SecurityGroupIngress",
    )
    assert len(ingress) == 2
    rules = {
        rule["Properties"]["Description"]: rule["Properties"]
        for rule in ingress
    }
    assert set(rules) == {
        "HTTPS from the AgentCore runtime",
        "HTTPS from the routing configuration seeder",
    }
    assert all(
        rule["FromPort"] == rule["ToPort"] == 443
        and rule["GroupId"]
        == {"Fn::GetAtt": [endpoint_group_id, "GroupId"]}
        for rule in rules.values()
    )
    assert rules["HTTPS from the AgentCore runtime"][
        "SourceSecurityGroupId"
    ] == {"Fn::GetAtt": [runtime_group_id, "GroupId"]}
    assert rules["HTTPS from the routing configuration seeder"][
        "SourceSecurityGroupId"
    ] == {"Fn::GetAtt": [seeder_group_id, "GroupId"]}

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
        if resource["Properties"]["GroupId"]["Fn::GetAtt"][0]
        == runtime_group_id
    }
    assert runtime_egress == {
        "HTTPS to explicitly approved external destinations",
        "DynamoDB through the VPC gateway endpoint",
        "AWS services through private interface endpoints",
    }
    assert {
        rule["Description"]
        for rule in runtime_group["Properties"]["SecurityGroupEgress"]
    } == {
        "DNS to the VPC resolver",
        "DNS fallback to the VPC resolver",
    }


def test_routing_seeder_precedes_the_verify_only_runtime(
    synthesized_template,
):
    resources = synthesized_template["Resources"]
    runtime = next(
        resource
        for resource in resources.values()
        if resource["Type"] == "AWS::BedrockAgentCore::Runtime"
    )
    environment = runtime["Properties"]["EnvironmentVariables"]
    assert environment["AXON_ROUTING_CONFIG_SIGNING_MODE"] == "verify"
    assert any(
        dependency.startswith("RoutingConfigSeeder")
        for dependency in runtime["DependsOn"]
    )

    seeder_id, seeder = next(
        (logical_id, resource)
        for logical_id, resource in resources.items()
        if resource["Type"] == "AWS::CloudFormation::CustomResource"
        and logical_id.startswith("RoutingConfigSeeder")
    )
    assert seeder_id
    assert seeder["Properties"]["DeploymentToken"] == {
        "Ref": "CandidateEndpointName"
    }
    assert seeder["Properties"][
        "InitialRoutingConfigZlibBase64"
    ] == {"Ref": "InitialRoutingConfigZlibBase64"}
    assert "RecoveryGuard" in seeder["DependsOn"]
    assert any(
        dependency.startswith("VpcDynamoDbEndpoint")
        for dependency in seeder["DependsOn"]
    )
    assert any(
        dependency.startswith("VpcKmsEndpoint")
        for dependency in seeder["DependsOn"]
    )

    handler = next(
        resource
        for resource in resources.values()
        if resource["Type"] == "AWS::Lambda::Function"
        and resource["Properties"].get("Description")
        == "Seeds or migrates the KMS-signed routing configuration"
    )
    vpc_config = handler["Properties"]["VpcConfig"]
    assert len(vpc_config["SecurityGroupIds"]) == 1
    assert vpc_config["SecurityGroupIds"][0]["Fn::GetAtt"][
        0
    ].startswith("RoutingConfigSeederSecurityGroup")
    assert all(
        subnet["Ref"].startswith("VpcRuntimeSubnet")
        for subnet in vpc_config["SubnetIds"]
    )

    seeder_policy = next(
        resource
        for logical_id, resource in resources.items()
        if logical_id.startswith(
            "RoutingConfigSeederHandlerServiceRoleDefaultPolicy"
        )
    )
    statements = {
        statement["Sid"]: statement
        for statement in seeder_policy["Properties"]["PolicyDocument"][
            "Statement"
        ]
        if "Sid" in statement
    }
    assert _actions(statements["SeedRoutingConfiguration"]) == {
        "dynamodb:GetItem",
        "dynamodb:PutItem",
    }
    assert _actions(
        statements["SignAndVerifyRoutingConfiguration"]
    ) == {"kms:Sign", "kms:Verify"}


def test_service_endpoints_are_private_and_resource_scoped(
    synthesized_template,
):
    endpoints = _resources(synthesized_template, "AWS::EC2::VPCEndpoint")
    assert len(endpoints) == 7
    gateway = next(
        endpoint
        for endpoint in endpoints
        if endpoint["Properties"]["VpcEndpointType"] == "Gateway"
    )["Properties"]
    interfaces = [
        endpoint
        for endpoint in endpoints
        if endpoint["Properties"]["VpcEndpointType"] == "Interface"
    ]
    bedrock_interface = next(
        endpoint["Properties"]
        for endpoint in interfaces
        if endpoint["Properties"]["ServiceName"].endswith(
            ".bedrock-runtime"
        )
    )
    sqs_interface = next(
        endpoint["Properties"]
        for endpoint in interfaces
        if endpoint["Properties"]["ServiceName"].endswith(".sqs")
    )
    sns_interface = next(
        endpoint["Properties"]
        for endpoint in interfaces
        if endpoint["Properties"]["ServiceName"].endswith(".sns")
    )
    logs_interface = next(
        endpoint["Properties"]
        for endpoint in interfaces
        if endpoint["Properties"]["ServiceName"].endswith(".logs")
    )
    secrets_interface = next(
        endpoint["Properties"]
        for endpoint in interfaces
        if endpoint["Properties"]["ServiceName"].endswith(
            ".secretsmanager"
        )
    )
    kms_interface = next(
        endpoint["Properties"]
        for endpoint in interfaces
        if endpoint["Properties"]["ServiceName"].endswith(".kms")
    )

    assert len(gateway["RouteTableIds"]) == 2
    assert all(
        route["Ref"].startswith("VpcRuntimeSubnet")
        for route in gateway["RouteTableIds"]
    )
    dynamodb_statement = gateway["PolicyDocument"]["Statement"][0]
    assert _actions(dynamodb_statement) == _DYNAMODB_ACTIONS
    assert "UseRecoveredState" in json.dumps(
        dynamodb_statement["Resource"]
    )
    assert "RuntimeStateTableName" in json.dumps(
        dynamodb_statement["Resource"]
    )

    assert len(interfaces) == 6
    for interface in (
        bedrock_interface,
        sqs_interface,
        sns_interface,
        logs_interface,
        secrets_interface,
        kms_interface,
    ):
        assert interface["PrivateDnsEnabled"] is True
        assert len(interface["SubnetIds"]) == 2
        assert all(
            subnet["Ref"].startswith("VpcRuntimeSubnet")
            for subnet in interface["SubnetIds"]
        )
    bedrock_statement = bedrock_interface["PolicyDocument"]["Statement"][0]
    assert _actions(bedrock_statement) == _BEDROCK_ACTIONS
    assert bedrock_statement["Resource"] == {
        "Ref": "BedrockInvokeResourceArns"
    }
    kms_statements = kms_interface["PolicyDocument"]["Statement"]
    assert len(kms_statements) == 2
    runtime_kms = next(
        statement
        for statement in kms_statements
        if _actions(statement) == {"kms:Verify"}
    )
    seeder_kms = next(
        statement
        for statement in kms_statements
        if _actions(statement) == {"kms:Sign", "kms:Verify"}
    )
    assert runtime_kms["Resource"]["Fn::GetAtt"][0].startswith(
        "RoutingConfigSigningKey"
    )
    assert runtime_kms["Principal"]["AWS"]["Fn::GetAtt"][0].startswith(
        "RuntimeExecutionRole"
    )
    assert seeder_kms["Resource"]["Fn::GetAtt"][0].startswith(
        "RoutingConfigSigningKey"
    )
    assert seeder_kms["Principal"]["AWS"]["Fn::GetAtt"][0].startswith(
        "RoutingConfigSeederHandlerServiceRole"
    )
    sqs_statement = sqs_interface["PolicyDocument"]["Statement"][0]
    assert _actions(sqs_statement) == _SQS_ACTIONS
    assert sqs_statement["Resource"]["Fn::GetAtt"][0].startswith(
        "SecurityEventOutboxQueue"
    )
    sns_statement = sns_interface["PolicyDocument"]["Statement"][0]
    assert _actions(sns_statement) == {"sns:Publish"}
    assert sns_statement["Resource"]["Ref"].startswith(
        "SecurityEventTopic"
    )
    logs_statement = logs_interface["PolicyDocument"]["Statement"][0]
    assert _actions(logs_statement) == {
        "logs:CreateLogStream",
        "logs:PutLogEvents",
    }
    assert logs_statement["Resource"][0]["Fn::GetAtt"][0].startswith(
        "SecurityEventLogGroup"
    )
    secrets_statement = secrets_interface["PolicyDocument"]["Statement"][0]
    assert _actions(secrets_statement) == {
        "secretsmanager:DescribeSecret",
        "secretsmanager:GetSecretValue",
    }
    assert secrets_statement["Resource"]["Ref"].startswith(
        "ProviderCredentials"
    )
    assert "SecurityEventLogGroup" in json.dumps(
        logs_statement["Resource"][1]
    )


def test_runtime_enforces_jwt_identity_and_bounded_lifecycle(
    synthesized_template,
):
    runtime = _one_resource(
        synthesized_template,
        "AWS::BedrockAgentCore::Runtime",
    )["Properties"]
    authorizer = runtime["AuthorizerConfiguration"][
        "CustomJWTAuthorizer"
    ]
    assert authorizer["DiscoveryUrl"] == {"Ref": "OidcDiscoveryUrl"}
    assert authorizer["AllowedAudience"]["Fn::If"][0] == (
        "RecoveryAccessBlocked"
    )
    assert authorizer["AllowedAudience"]["Fn::If"][2] == {
        "Ref": "OidcAudiences"
    }
    assert authorizer["AllowedClients"]["Fn::If"][0] == (
        "RecoveryAccessBlocked"
    )
    assert authorizer["AllowedClients"]["Fn::If"][2] == {
        "Ref": "OidcClientIds"
    }
    assert runtime["RequestHeaderConfiguration"] == {
        "RequestHeaderAllowlist": ["Authorization"]
    }
    assert runtime["LifecycleConfiguration"] == {
        "IdleRuntimeSessionTimeout": 600,
        "MaxLifetime": 14400,
    }
    assert runtime["ProtocolConfiguration"] == "HTTP"

    environment = runtime["EnvironmentVariables"]
    assert environment["AXON_AUTH_MODE"] == "ENFORCE"
    assert environment["AXON_DEPLOYMENT_PROFILE"] == "production"
    assert environment["AXON_LOAD_DEMO_DATA"] == "false"
    assert environment["AXON_OIDC_ISSUER"] == {"Ref": "OidcIssuer"}
    assert environment["AXON_OIDC_AUDIENCE"] == {
        "Fn::Join": [",", {"Ref": "OidcAudiences"}]
    }
    assert environment["AXON_OIDC_TENANT_CLAIM"] == {
        "Ref": "OidcTenantClaim"
    }
    assert environment["AXON_OIDC_PROJECT_CLAIM"] == {
        "Ref": "OidcProjectClaim"
    }
    assert environment["AXON_REQUIRE_CANONICAL_IDENTITY"] == "true"
    assert environment["AXON_ENABLED_PROVIDERS"] == {
        "Ref": "EnabledProviders"
    }
    default_providers = synthesized_template["Parameters"][
        "EnabledProviders"
    ]["Default"].split(",")
    assert default_providers == sorted(_DEFAULT_ENABLED_PROVIDERS)
    allowed_pattern = synthesized_template["Parameters"][
        "EnabledProviders"
    ]["AllowedPattern"]
    assert all(
        provider in allowed_pattern
        for provider in _OPTIONAL_ENABLED_PROVIDERS
    )
    assert environment["AXON_PROVIDER_SECRET_ARN"]["Ref"].startswith(
        "ProviderCredentials"
    )
    assert environment["AXON_PROVIDER_SECRET_VERSION"] == {
        "Ref": "ProviderSecretVersion"
    }
    assert environment["LLM_ROUTER_DYNAMODB_ENABLED"] == "true"
    assert environment["AXON_ROUTING_CONFIG_SIGNING_MODE"] == "verify"
    assert environment["AXON_ROUTING_CONFIG_SIGNING_KEY_ARN"][
        "Fn::GetAtt"
    ][0].startswith("RoutingConfigSigningKey")
    assert environment["AXON_DYNAMODB_TABLE"]["Fn::If"][:2] == [
        "UseRecoveredState",
        {"Ref": "RuntimeStateTableName"},
    ]
    assert environment["AXON_AWS_ACCOUNT_ID"] == {
        "Ref": "AWS::AccountId"
    }
    assert environment["AXON_EVENT_OUTBOX_QUEUE_URL"]["Ref"].startswith(
        "SecurityEventOutboxQueue"
    )
    assert environment["AXON_SECURITY_EVENT_SNS_TOPIC_ARN"][
        "Ref"
    ].startswith("SecurityEventTopic")
    assert environment["AXON_SECURITY_EVENT_LOG_GROUP_ARN"][
        "Fn::GetAtt"
    ][0].startswith("SecurityEventLogGroup")

    endpoints = _resources(
        synthesized_template,
        "AWS::BedrockAgentCore::RuntimeEndpoint",
    )
    assert len(endpoints) == 3
    production = next(
        endpoint
        for endpoint in endpoints
        if endpoint["Properties"]["Name"] == "production"
    )
    candidate = next(
        endpoint
        for endpoint in endpoints
        if endpoint["Properties"]["Name"]
        == {"Ref": "CandidateEndpointName"}
    )
    recovery = next(
        endpoint
        for endpoint in endpoints
        if endpoint["Properties"]["Name"] == "recovery"
    )
    assert production["Condition"] == (
        "ProductionEndpointEnabled"
    )
    assert candidate["Condition"] == (
        "CandidateEndpointEnabled"
    )
    assert recovery["Condition"] == "RecoveryValidation"
    assert production["Properties"][
        "AgentRuntimeVersion"
    ] == {"Ref": "ProductionRuntimeVersion"}
    assert all(
        endpoint["Properties"]["AgentRuntimeVersion"][
            "Fn::GetAtt"
        ][1]
        == "AgentRuntimeVersion"
        for endpoint in (candidate, recovery)
    )
    assert all(
        any(
            dependency.startswith("RecoveryGuard")
            for dependency in endpoint["DependsOn"]
        )
        for endpoint in endpoints
    )


def test_runtime_role_is_scoped_and_supports_state_transactions(
    synthesized_template,
):
    runtime_policy = next(
        resource
        for logical_id, resource in synthesized_template["Resources"].items()
        if logical_id.startswith("RuntimeExecutionRoleDefaultPolicy")
    )
    statements = runtime_policy["Properties"]["PolicyDocument"]["Statement"]
    bedrock = next(
        statement
        for statement in statements
        if "bedrock:InvokeModel" in _actions(statement)
    )
    assert _actions(bedrock) == _BEDROCK_ACTIONS
    assert bedrock["Resource"] == {"Ref": "BedrockInvokeResourceArns"}

    mantle = next(
        statement
        for statement in statements
        if "bedrock-mantle:CreateInference" in _actions(statement)
    )
    assert _actions(mantle) == _MANTLE_ACTIONS
    assert mantle["Resource"] == "*"

    secret_read = next(
        statement
        for statement in statements
        if "secretsmanager:GetSecretValue" in _actions(statement)
    )
    assert _actions(secret_read) == {
        "secretsmanager:DescribeSecret",
        "secretsmanager:GetSecretValue",
    }
    assert secret_read["Resource"]["Ref"].startswith(
        "ProviderCredentials"
    )
    assert secret_read["Sid"] == "ReadProviderCredentials"

    state_access = next(
        statement
        for statement in statements
        if "dynamodb:ConditionCheckItem" in _actions(statement)
    )
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
    all_actions = {
        action
        for statement in statements
        for action in _actions(statement)
    }
    assert "dynamodb:TransactGetItems" not in all_actions
    assert "dynamodb:TransactWriteItems" not in all_actions

    transaction_policy = next(
        resource
        for logical_id, resource in synthesized_template["Resources"].items()
        if logical_id.startswith("RuntimeDynamoTransactionPolicy")
    )
    assert transaction_policy["Metadata"] == {
        "cfn-lint": {"config": {"ignore_checks": ["W3037"]}}
    }
    transaction_statement = transaction_policy["Properties"][
        "PolicyDocument"
    ]["Statement"][0]
    assert _actions(transaction_statement) == {
        "dynamodb:TransactWriteItems"
    }
    assert transaction_statement["Sid"] == (
        "TransactWithSelectedStateTable"
    )
    assert "UseRecoveredState" in json.dumps(
        transaction_statement["Resource"]
    )

    queue_statements = [
        statement
        for statement in statements
        if any(action.startswith("sqs:") for action in _actions(statement))
    ]
    assert len(queue_statements) == 1
    queue_access = queue_statements[0]
    assert _actions(queue_access) == _SQS_ACTIONS
    assert queue_access["Sid"] == "UseSecurityEventOutbox"
    assert queue_access["Resource"]["Fn::GetAtt"][0].startswith(
        "SecurityEventOutboxQueue"
    )
    publish = next(
        statement
        for statement in statements
        if _actions(statement) == {"sns:Publish"}
    )
    assert publish["Sid"] == "PublishSecurityEvents"
    assert publish["Resource"]["Ref"].startswith("SecurityEventTopic")

    kms_statements = [
        statement
        for statement in statements
        if any(action.startswith("kms:") for action in _actions(statement))
    ]
    assert len(kms_statements) == 4

    def kms_statement(sid: str) -> dict:
        return next(
            statement
            for statement in kms_statements
            if statement["Sid"] == sid
        )

    secret_key = kms_statement("DecryptProviderCredentials")
    assert _actions(secret_key) == {"kms:Decrypt"}
    secret_condition = secret_key["Condition"]["StringEquals"]
    assert set(secret_condition) == {
        "kms:CallerAccount",
        "kms:ViaService",
    }
    assert secret_condition["kms:CallerAccount"] == {
        "Ref": "AWS::AccountId"
    }
    assert "secretsmanager." in json.dumps(
        secret_condition["kms:ViaService"]
    )
    assert "AWS::URLSuffix" in json.dumps(
        secret_condition["kms:ViaService"]
    )

    routing_key = kms_statement("VerifyRoutingConfiguration")
    assert _actions(routing_key) == {"kms:Verify"}
    assert routing_key["Resource"]["Fn::GetAtt"][0].startswith(
        "RoutingConfigSigningKey"
    )

    queue_key = kms_statement("UseSecurityEventOutboxKey")
    assert _actions(queue_key) == {
        "kms:Decrypt",
        "kms:GenerateDataKey*",
    }
    queue_condition = queue_key["Condition"]["StringEquals"]
    assert set(queue_condition) == {
        "kms:CallerAccount",
        "kms:ViaService",
    }
    assert queue_condition["kms:CallerAccount"] == {
        "Ref": "AWS::AccountId"
    }
    assert "sqs." in json.dumps(queue_condition["kms:ViaService"])
    assert "AWS::URLSuffix" in json.dumps(
        queue_condition["kms:ViaService"]
    )

    topic_key = kms_statement("UseSecurityEventTopicKey")
    assert _actions(topic_key) == {
        "kms:Decrypt",
        "kms:GenerateDataKey*",
    }
    topic_condition = topic_key["Condition"]["StringEquals"]
    assert topic_condition["kms:CallerAccount"] == {
        "Ref": "AWS::AccountId"
    }
    assert "sns." in json.dumps(topic_condition["kms:ViaService"])
    assert "AWS::URLSuffix" in json.dumps(
        topic_condition["kms:ViaService"]
    )
    assert "SecurityEventTopic" in json.dumps(
        topic_condition["kms:EncryptionContext:aws:sns:topicArn"]
    )
    assert all(
        "DataKey" in json.dumps(statement["Resource"])
        for statement in kms_statements
        if statement["Sid"] != "VerifyRoutingConfiguration"
    )

    security_log_write = next(
        statement
        for statement in statements
        if _actions(statement)
        == {"logs:CreateLogStream", "logs:PutLogEvents"}
        and isinstance(statement["Resource"], dict)
        and "Fn::GetAtt" in statement["Resource"]
    )
    assert security_log_write["Resource"]["Fn::GetAtt"][0].startswith(
        "SecurityEventLogGroup"
    )

    image_pull = next(
        statement
        for statement in statements
        if "ecr:BatchGetImage" in _actions(statement)
    )
    assert _actions(image_pull) == {
        "ecr:BatchCheckLayerAvailability",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
    }
    assert ":repository/" in image_pull["Resource"]["Fn::Join"][1]
    assert "VerifiedImageUri" in json.dumps(image_pull["Resource"])
    image_authorization = next(
        statement
        for statement in statements
        if "ecr:GetAuthorizationToken" in _actions(statement)
    )
    assert image_authorization["Resource"] == "*"

    runtime_role = next(
        resource
        for resource in _resources(synthesized_template, "AWS::IAM::Role")
        if resource["Properties"].get("Description")
        == "Execution role for Bedrock Agent Core Runtime"
    )
    trust = runtime_role["Properties"]["AssumeRolePolicyDocument"][
        "Statement"
    ][0]
    assert trust["Principal"] == {
        "Service": "bedrock-agentcore.amazonaws.com"
    }
    assert trust["Condition"]["StringEquals"]["aws:SourceAccount"] == {
        "Ref": "AWS::AccountId"
    }
    assert "aws:SourceArn" in trust["Condition"]["ArnLike"]


class _RoutingSeederClientError(RuntimeError):
    def __init__(self) -> None:
        self.response = {
            "Error": {"Code": "ConditionalCheckFailedException"}
        }
        super().__init__("conditional write failed")


class _RoutingSeederDynamo:
    def __init__(self, item: dict | None = None) -> None:
        self.item = copy.deepcopy(item)
        self.conflict_once = False
        self.put_requests: list[dict] = []

    def get_item(self, **_request) -> dict:
        return (
            {}
            if self.item is None
            else {"Item": copy.deepcopy(self.item)}
        )

    def put_item(self, **request) -> dict:
        self.put_requests.append(copy.deepcopy(request))
        if self.conflict_once:
            self.conflict_once = False
            self.item = copy.deepcopy(request["Item"])
            raise _RoutingSeederClientError()
        condition = request["ConditionExpression"]
        if condition.startswith("attribute_not_exists"):
            if self.item is not None:
                raise _RoutingSeederClientError()
        elif (
            self.item is None
            or self.item.get("schema_version") != {"N": "1"}
            or self.item.get("revision")
            != request["ExpressionAttributeValues"][":revision"]
            or self.item.get("document_sha256")
            != request["ExpressionAttributeValues"][":document_sha256"]
        ):
            raise _RoutingSeederClientError()
        self.item = copy.deepcopy(request["Item"])
        return {}


class _RoutingSeederKms:
    def __init__(self) -> None:
        self.sign_requests: list[dict] = []
        self.verify_requests: list[dict] = []

    @staticmethod
    def _signature(message: bytes) -> bytes:
        return b"test-signature:" + message

    def sign(self, **request) -> dict:
        self.sign_requests.append(copy.deepcopy(request))
        return {
            "KeyId": request["KeyId"],
            "SigningAlgorithm": request["SigningAlgorithm"],
            "Signature": self._signature(request["Message"]),
        }

    def verify(self, **request) -> dict:
        self.verify_requests.append(copy.deepcopy(request))
        return {
            "KeyId": request["KeyId"],
            "SigningAlgorithm": request["SigningAlgorithm"],
            "SignatureValid": request["Signature"]
            == self._signature(request["Message"]),
        }


def _routing_seeder_properties() -> dict[str, str]:
    document = json.dumps(
        {"models": []},
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return {
        "DeploymentToken": "candidate_" + "a" * 32,
        "InitialRoutingConfigZlibBase64": base64.b64encode(
            zlib.compress(document.encode("utf-8"), level=9)
        ).decode("ascii"),
        "KeyArn": (
            "arn:aws:kms:us-east-1:123456789012:"
            "key/11111111-2222-3333-4444-555555555555"
        ),
        "TableName": "axonllm-agentcore-state",
    }


def _routing_seeder_handler(
    synthesized_template: dict,
    monkeypatch: pytest.MonkeyPatch,
    *,
    dynamodb: _RoutingSeederDynamo,
    kms: _RoutingSeederKms,
):
    handler = next(
        resource
        for resource in _resources(
            synthesized_template,
            "AWS::Lambda::Function",
        )
        if resource["Properties"].get("Description")
        == "Seeds or migrates the KMS-signed routing configuration"
    )
    clients = {"dynamodb": dynamodb, "kms": kms}
    monkeypatch.setitem(
        sys.modules,
        "boto3",
        types.SimpleNamespace(client=lambda name: clients[name]),
    )
    botocore = types.ModuleType("botocore")
    exceptions = types.ModuleType("botocore.exceptions")
    exceptions.ClientError = _RoutingSeederClientError
    botocore.exceptions = exceptions
    monkeypatch.setitem(sys.modules, "botocore", botocore)
    monkeypatch.setitem(sys.modules, "botocore.exceptions", exceptions)
    namespace: dict[str, object] = {}
    exec(
        compile(
            handler["Properties"]["Code"]["ZipFile"],
            "routing_config_seeder.py",
            "exec",
        ),
        namespace,
    )
    return namespace["handler"]


def test_routing_seeder_seeds_and_reverifies_the_exact_key(
    synthesized_template,
    monkeypatch,
):
    dynamodb = _RoutingSeederDynamo()
    kms = _RoutingSeederKms()
    handler = _routing_seeder_handler(
        synthesized_template,
        monkeypatch,
        dynamodb=dynamodb,
        kms=kms,
    )
    properties = _routing_seeder_properties()

    created = handler(
        {
            "RequestType": "Create",
            "ResourceProperties": properties,
        },
        None,
    )
    verified = handler(
        {
            "RequestType": "Update",
            "ResourceProperties": properties,
        },
        None,
    )

    assert created["Data"] == {"Revision": "1", "Status": "seeded"}
    assert verified["Data"] == {
        "Revision": "1",
        "Status": "verified",
    }
    assert dynamodb.item["schema_version"] == {"N": "2"}
    assert dynamodb.item["signing_key_arn"] == {
        "S": properties["KeyArn"]
    }
    assert len(kms.sign_requests) == 1
    assert len(kms.verify_requests) == 1


def test_routing_seeder_migrates_legacy_and_handles_a_seed_race(
    synthesized_template,
    monkeypatch,
):
    document = json.dumps(
        {"models": []},
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    legacy = {
        "PK": {"S": "MODEL_REGISTRY"},
        "SK": {"S": "CONFIG"},
        "entity_type": {"S": "model_registry"},
        "schema_version": {"N": "1"},
        "revision": {"N": "4"},
        "document": {"S": document},
        "document_sha256": {
            "S": hashlib.sha256(document.encode("utf-8")).hexdigest()
        },
    }
    dynamodb = _RoutingSeederDynamo(legacy)
    kms = _RoutingSeederKms()
    handler = _routing_seeder_handler(
        synthesized_template,
        monkeypatch,
        dynamodb=dynamodb,
        kms=kms,
    )
    properties = _routing_seeder_properties()

    migrated = handler(
        {
            "RequestType": "Update",
            "ResourceProperties": properties,
        },
        None,
    )

    assert migrated["Data"] == {
        "Revision": "4",
        "Status": "migrated",
    }
    assert dynamodb.item["schema_version"] == {"N": "2"}

    raced_dynamodb = _RoutingSeederDynamo()
    raced_dynamodb.conflict_once = True
    raced_kms = _RoutingSeederKms()
    raced_handler = _routing_seeder_handler(
        synthesized_template,
        monkeypatch,
        dynamodb=raced_dynamodb,
        kms=raced_kms,
    )
    raced = raced_handler(
        {
            "RequestType": "Create",
            "ResourceProperties": properties,
        },
        None,
    )
    assert raced["Data"] == {
        "Revision": "1",
        "Status": "verified_after_conflict",
    }


def test_routing_seeder_rejects_a_tampered_signed_row(
    synthesized_template,
    monkeypatch,
):
    dynamodb = _RoutingSeederDynamo()
    kms = _RoutingSeederKms()
    handler = _routing_seeder_handler(
        synthesized_template,
        monkeypatch,
        dynamodb=dynamodb,
        kms=kms,
    )
    properties = _routing_seeder_properties()
    handler(
        {
            "RequestType": "Create",
            "ResourceProperties": properties,
        },
        None,
    )
    dynamodb.item["signature"] = {
        "S": base64.b64encode(b"tampered").decode("ascii")
    }

    with pytest.raises(RuntimeError, match="verification failed"):
        handler(
            {
                "RequestType": "Update",
                "ResourceProperties": properties,
            },
            None,
        )


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
    assert transaction_deny["Metadata"] == {
        "cfn-lint": {"config": {"ignore_checks": ["W3037"]}}
    }
    transaction_statement = transaction_deny["Properties"][
        "PolicyDocument"
    ]["Statement"][0]
    assert transaction_statement["Effect"] == "Deny"
    assert _actions(transaction_statement) == {
        "dynamodb:TransactWriteItems"
    }
    assert transaction_statement["Resource"] == statement["Resource"]

    guard = resources["RecoveryGuard"]
    assert deny_id in guard["DependsOn"]
    assert transaction_deny_id in guard["DependsOn"]
    assert guard["Properties"]["MinimumQuiescenceSeconds"] == "14700"
    assert guard["Properties"]["Mode"] == {
        "Ref": "RecoveryCutoverMode"
    }
    assert guard["Properties"]["PrimaryTable"]["Ref"].startswith(
        "StateTable"
    )
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
        if logical_id.startswith("RecoveryGuardHandler")
        and resource["Type"] == "AWS::Lambda::Function"
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
        if logical_id.startswith(
            "RecoveryGuardHandlerServiceRoleDefaultPolicy"
        )
    )
    guard_actions = {
        action
        for policy_statement in guard_role_policy["Properties"][
            "PolicyDocument"
        ]["Statement"]
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
    assert outputs["RuntimeEndpointArn"]["Condition"] == (
        "ProductionEndpointEnabled"
    )
    assert outputs["RuntimeEndpointName"]["Condition"] == (
        "ProductionEndpointEnabled"
    )
    assert outputs["ProductionRuntimeVersion"]["Condition"] == (
        "ProductionEndpointEnabled"
    )
    assert outputs["CandidateRuntimeEndpointArn"]["Condition"] == (
        "CandidateEndpointEnabled"
    )
    assert outputs["CandidateRuntimeEndpointName"]["Condition"] == (
        "CandidateEndpointEnabled"
    )
    assert outputs["CandidateRuntimeVersion"]["Condition"] == (
        "CandidateEndpointEnabled"
    )
    assert outputs["CandidateRuntimeEndpointName"]["Value"] == {
        "Ref": "CandidateEndpointName"
    }
    assert outputs["ApprovedHttpsPrefixListId"]["Value"] == {
        "Ref": "ApprovedHttpsPrefixListId"
    }
    assert outputs["BedrockInvokeResourceArns"]["Value"] == {
        "Fn::Join": [",", {"Ref": "BedrockInvokeResourceArns"}]
    }
    assert len(outputs["AthenaConfigurationFingerprint"]["Value"]) == 64
    assert outputs["AlarmNotificationEmail"]["Value"] == {
        "Ref": "AlarmNotificationEmail"
    }
    assert outputs["RecoveryRuntimeEndpointArn"]["Condition"] == (
        "RecoveryValidation"
    )
    selected = outputs["SelectedRuntimeStateTableName"]["Value"]["Fn::If"]
    assert selected[:2] == [
        "UseRecoveredState",
        {"Ref": "RuntimeStateTableName"},
    ]
    assert outputs["RecoveryCutoverMode"]["Value"] == {
        "Ref": "RecoveryCutoverMode"
    }
    assert outputs["RecoveryMinimumQuiescenceSeconds"]["Value"] == "14700"


def test_recovery_guard_allows_only_quiesced_reviewed_table_switches(
    synthesized_template,
    monkeypatch,
):
    handler = next(
        resource
        for logical_id, resource in synthesized_template[
            "Resources"
        ].items()
        if logical_id.startswith("RecoveryGuardHandler")
        and resource["Type"] == "AWS::Lambda::Function"
    )

    class _ClientError(Exception):
        def __init__(self):
            self.response = {
                "Error": {
                    "Code": "ValidationError",
                    "Message": (
                        "Stack with id AxonLLMControlPlaneStack "
                        "does not exist"
                    ),
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
    assert created["PhysicalResourceId"] == (
        "AxonLLMAgentCoreRecoveryGuard"
    )

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
        "SelectedTable": (
            "axonllm-agentcore-state-restore-validation-safe"
        ),
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


def test_state_and_backups_are_encrypted_retained_and_recoverable(
    synthesized_template,
):
    table = _one_resource(synthesized_template, "AWS::DynamoDB::Table")
    properties = table["Properties"]
    assert properties["DeletionProtectionEnabled"] is True
    assert properties["PointInTimeRecoverySpecification"] == {
        "PointInTimeRecoveryEnabled": True
    }
    assert properties["SSESpecification"]["SSEEnabled"] is True
    assert properties["SSESpecification"]["SSEType"] == "KMS"
    assert properties["TimeToLiveSpecification"] == {
        "AttributeName": "expires_at",
        "Enabled": True,
    }
    assert table["DeletionPolicy"] == "Retain"
    assert table["UpdateReplacePolicy"] == "Retain"

    keys = _resources(synthesized_template, "AWS::KMS::Key")
    assert len(keys) == 3
    signing_key = next(
        key
        for key in keys
        if key["Properties"].get("KeyUsage") == "SIGN_VERIFY"
    )
    assert signing_key["Properties"]["KeySpec"] == "ECC_NIST_P256"
    assert "EnableKeyRotation" not in signing_key["Properties"]
    encryption_keys = [key for key in keys if key is not signing_key]
    assert all(
        key["Properties"]["EnableKeyRotation"] is True
        for key in encryption_keys
    )
    assert all(key["DeletionPolicy"] == "Retain" for key in keys)

    vault = _one_resource(
        synthesized_template,
        "AWS::Backup::BackupVault",
    )
    assert vault["DeletionPolicy"] == "Retain"
    assert vault["Properties"]["LockConfiguration"] == {
        "MaxRetentionDays": 365,
        "MinRetentionDays": 30,
    }
    vault_name = vault["Properties"]["BackupVaultName"]
    assert vault_name["Fn::Join"][1][0] == "axon-agent"
    assert {"Ref": "AWS::StackId"} in _values_for_key(
        vault_name,
        "Fn::Split",
    )[0]
    rule = _one_resource(
        synthesized_template,
        "AWS::Backup::BackupPlan",
    )["Properties"]["BackupPlan"]["BackupPlanRule"][0]
    assert rule["ScheduleExpression"] == "cron(30 5 * * ? *)"
    assert rule["Lifecycle"] == {
        "DeleteAfterDays": 365,
        "MoveToColdStorageAfterDays": 30,
    }
    selections = _resources(
        synthesized_template,
        "AWS::Backup::BackupSelection",
    )
    assert len(selections) == 2
    primary = next(
        selection
        for selection in selections
        if "Condition" not in selection
    )
    recovered = next(
        selection
        for selection in selections
        if selection.get("Condition") == "UseRecoveredState"
    )
    assert primary["Properties"]["BackupSelection"]["Resources"][0][
        "Fn::GetAtt"
    ][1] == "Arn"
    assert "RuntimeStateTableName" in json.dumps(
        recovered["Properties"]["BackupSelection"]["Resources"]
    )


def test_backup_service_role_is_scoped_to_state_tables_and_key(
    synthesized_template,
):
    role_id, role = next(
        (logical_id, resource)
        for logical_id, resource in synthesized_template[
            "Resources"
        ].items()
        if resource["Type"] == "AWS::IAM::Role"
        and resource["Properties"].get("Description")
        == "AWS Backup service role scoped to AxonLLM AgentCore state"
    )
    assert "ManagedPolicyArns" not in role["Properties"]
    trust = role["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]
    assert trust["Principal"] == {"Service": "backup.amazonaws.com"}
    assert trust["Condition"] == {
        "StringEquals": {"aws:SourceAccount": {"Ref": "AWS::AccountId"}}
    }

    policy = next(
        resource
        for resource in _resources(
            synthesized_template,
            "AWS::IAM::Policy",
        )
        if {"Ref": role_id} in resource["Properties"]["Roles"]
        and "dynamodb:StartAwsBackupJob"
        in json.dumps(resource["Properties"]["PolicyDocument"])
    )
    statements = policy["Properties"]["PolicyDocument"]["Statement"]
    table_access = next(
        statement
        for statement in statements
        if "dynamodb:StartAwsBackupJob" in _actions(statement)
    )
    assert _actions(table_access) == {
        "dynamodb:DescribeContinuousBackups",
        "dynamodb:DescribeTable",
        "dynamodb:ListTagsOfResource",
        "dynamodb:StartAwsBackupJob",
    }
    serialized_resources = json.dumps(table_access["Resource"])
    assert "StateTable" in serialized_resources
    assert "restore-validation-*" in serialized_resources

    key_access = next(
        statement
        for statement in statements
        if "kms:GenerateDataKey*" in _actions(statement)
    )
    assert _actions(key_access) == {
        "kms:Decrypt",
        "kms:GenerateDataKey*",
    }
    key_condition = key_access["Condition"]["StringEquals"]
    assert key_condition["kms:CallerAccount"] == {
        "Ref": "AWS::AccountId"
    }
    assert "dynamodb." in json.dumps(key_condition["kms:ViaService"])
    assert "AWS::URLSuffix" in json.dumps(
        key_condition["kms:ViaService"]
    )
    assert "DataKey" in json.dumps(key_access["Resource"])


def test_security_event_outbox_is_fifo_encrypted_and_redriven(
    synthesized_template,
):
    queues = _resources(synthesized_template, "AWS::SQS::Queue")
    assert len(queues) == 2
    assert all(queue["Properties"]["FifoQueue"] is True for queue in queues)
    assert all(
        queue["Properties"]["ContentBasedDeduplication"] is False
        for queue in queues
    )
    assert all("KmsMasterKeyId" in queue["Properties"] for queue in queues)
    assert all(
        queue["Properties"]["MessageRetentionPeriod"] == 1209600
        for queue in queues
    )
    outbox = next(
        queue
        for queue in queues
        if "RedrivePolicy" in queue["Properties"]
    )
    assert outbox["Properties"]["ReceiveMessageWaitTimeSeconds"] == 20
    assert outbox["Properties"]["VisibilityTimeout"] == 300
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
        assert statement["Condition"] == {
            "Bool": {"aws:SecureTransport": "false"}
        }

    security_topic = next(
        topic
        for topic in _resources(synthesized_template, "AWS::SNS::Topic")
        if topic["Properties"].get("DisplayName")
        == "AxonLLM AgentCore durable security events"
    )
    assert security_topic["Properties"]["FifoTopic"] is True
    assert security_topic["Properties"]["ContentBasedDeduplication"] is False
    assert "KmsMasterKeyId" in security_topic["Properties"]
    security_topic_policy = next(
        policy
        for policy in _resources(
            synthesized_template,
            "AWS::SNS::TopicPolicy",
        )
        if policy["Properties"]["PolicyDocument"]["Statement"][0].get("Sid")
        == "AllowPublishThroughSSLOnly"
    )
    transport_statement = security_topic_policy["Properties"][
        "PolicyDocument"
    ]["Statement"][0]
    assert transport_statement["Effect"] == "Deny"
    assert transport_statement["Condition"] == {
        "Bool": {"aws:SecureTransport": "false"}
    }

    outputs = synthesized_template["Outputs"]
    assert outputs["SecurityEventOutboxQueueUrl"]["Value"]["Ref"].startswith(
        "SecurityEventOutboxQueue"
    )
    assert outputs["SecurityEventDeadLetterQueueUrl"]["Value"][
        "Ref"
    ].startswith("SecurityEventDeadLetterQueue")
    assert outputs["SecurityEventDeadLetterQueueArn"]["Value"][
        "Fn::GetAtt"
    ][0].startswith("SecurityEventDeadLetterQueue")
    assert outputs["SecurityEventDeadLettersAlarmArn"]["Value"][
        "Fn::GetAtt"
    ][0].startswith("SecurityEventDeadLettersAlarm")
    assert outputs["SecurityEventTopicArn"]["Value"]["Ref"].startswith(
        "SecurityEventTopic"
    )
    assert outputs["SecurityEventLogGroupArn"]["Value"]["Fn::GetAtt"][
        0
    ].startswith("SecurityEventLogGroup")
    assert outputs["DataKeyArn"]["Value"]["Fn::GetAtt"][0].startswith(
        "DataKey"
    )
    assert outputs["RoutingConfigSigningKeyArn"]["Value"][
        "Fn::GetAtt"
    ][0].startswith("RoutingConfigSigningKey")
    assert outputs["ProviderSecretArn"]["Value"]["Ref"].startswith(
        "ProviderCredentials"
    )

    provider_secret = _one_resource(
        synthesized_template,
        "AWS::SecretsManager::Secret",
    )
    assert provider_secret["DeletionPolicy"] == "Retain"
    assert provider_secret["UpdateReplacePolicy"] == "Retain"
    assert "Name" not in provider_secret["Properties"]
    template = json.loads(
        provider_secret["Properties"]["GenerateSecretString"][
            "SecretStringTemplate"
        ]
    )
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
    assert len(retained_logs) == 7
    assert all(
        log_group["Properties"]["RetentionInDays"] == 365
        for log_group in retained_logs
    )
    assert all(
        "KmsKeyId" in log_group["Properties"]
        for log_group in retained_logs
    )
    delivery_types = {
        resource["Properties"]["LogType"]
        for resource in _resources(
            synthesized_template,
            "AWS::Logs::DeliverySource",
        )
    }
    assert delivery_types == {"APPLICATION_LOGS", "TRACES", "USAGE_LOGS"}

    data_key = next(
        key
        for key in _resources(synthesized_template, "AWS::KMS::Key")
        if key["Properties"]["Description"].endswith("state and logs")
    )
    key_statements = data_key["Properties"]["KeyPolicy"]["Statement"]
    statements_by_sid = {
        statement.get("Sid"): statement for statement in key_statements
    }
    assert statements_by_sid["AllowCloudWatchLogsEncryption"][
        "Principal"
    ]["Service"]["Fn::Join"][1] == [
        "logs.us-east-1.",
        {"Ref": "AWS::URLSuffix"},
    ]
    assert statements_by_sid["AllowCloudWatchAlarmEncryption"][
        "Principal"
    ] == {"Service": "cloudwatch.amazonaws.com"}

    topic = next(
        topic
        for topic in _resources(synthesized_template, "AWS::SNS::Topic")
        if topic["Properties"].get("DisplayName")
        == "AxonLLM AgentCore production alarms"
    )
    assert "KmsMasterKeyId" in topic["Properties"]
    topic_policy = next(
        policy
        for policy in _resources(
            synthesized_template,
            "AWS::SNS::TopicPolicy",
        )
        if policy["Properties"]["PolicyDocument"]["Statement"][0].get("Sid")
        == "AllowAccountCloudWatchAlarms"
    )["Properties"]["PolicyDocument"]["Statement"][0]
    assert topic_policy["Action"] == "sns:Publish"
    assert topic_policy["Principal"] == {
        "Service": "cloudwatch.amazonaws.com"
    }
    assert topic_policy["Condition"]["StringEquals"][
        "aws:SourceAccount"
    ] == {"Ref": "AWS::AccountId"}
    subscription = _one_resource(
        synthesized_template,
        "AWS::SNS::Subscription",
    )
    assert subscription["Properties"]["Protocol"] == "email"
    assert subscription["Properties"]["Endpoint"] == {
        "Ref": "AlarmNotificationEmail"
    }
    assert subscription["Properties"]["TopicArn"]["Ref"].startswith(
        "AlarmTopic"
    )

    alarms = _resources(synthesized_template, "AWS::CloudWatch::Alarm")
    assert len(alarms) == 4
    assert all(alarm["Properties"]["AlarmActions"] for alarm in alarms)
    assert all(alarm["Properties"]["OKActions"] for alarm in alarms)
    assert {
        alarm["Properties"].get("MetricName") for alarm in alarms
    } >= {
        "ApproximateNumberOfMessagesVisible",
        "SystemErrors",
        "Throttles",
    }
    monitored_operations = {
        dimension["Value"]
        for alarm in alarms
        for metric in alarm["Properties"].get("Metrics", [])
        for dimension in metric.get("MetricStat", {})
        .get("Metric", {})
        .get("Dimensions", [])
        if dimension["Name"] == "Operation"
    }
    assert {
        "TransactGetItems",
        "TransactWriteItems",
    } <= monitored_operations
    assert len(
        _resources(synthesized_template, "AWS::CloudWatch::Dashboard")
    ) == 1


def test_runtime_image_is_release_verified_digest_pinned_and_non_root(
    synthesized,
):
    template, assets = synthesized
    assert assets["dockerImages"] == {}

    runtime = _one_resource(
        template,
        "AWS::BedrockAgentCore::Runtime",
    )["Properties"]
    assert runtime["AgentRuntimeArtifact"] == {
        "ContainerConfiguration": {
            "ContainerUri": {"Ref": "VerifiedImageUri"}
        }
    }
    assert template["Outputs"]["RuntimeImageUri"]["Value"] == {
        "Ref": "VerifiedImageUri"
    }
    assert "from_asset" not in _STACK.read_text(encoding="utf-8")

    dockerfile = _DOCKERFILE.read_text(encoding="utf-8")
    assert (
        "# syntax=docker/dockerfile:1.7@sha256:"
        "a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e"
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
