"""Security contract tests for the checked-in Fargate CDK stack."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import types

import pytest


_REPO = Path(__file__).resolve().parents[2]
_INFRA = _REPO / "infra"
_STACK = _INFRA / "stack.py"
_DEPLOY_SCRIPT = _REPO / "deploy-fargate.sh"
_INFRA_PYTHON = _INFRA / ".venv" / "bin" / "python"
_REQUIRED_PARAMETERS = {
    "ViewerDomainName",
    "ViewerCertificateArn",
    "OriginDomainName",
    "OriginCertificateArn",
    "ApprovedHttpsPrefixListId",
    "BedrockInvokeResourceArns",
    "VerifiedImageUri",
}
_DEPLOY_ENV_BY_PARAMETER = {
    "ViewerDomainName": "AXON_VIEWER_DOMAIN_NAME",
    "ViewerCertificateArn": "AXON_VIEWER_CERTIFICATE_ARN",
    "OriginDomainName": "AXON_ORIGIN_DOMAIN_NAME",
    "OriginCertificateArn": "AXON_ORIGIN_CERTIFICATE_ARN",
    "ApprovedHttpsPrefixListId": "AXON_APPROVED_HTTPS_PREFIX_LIST_ID",
    "BedrockInvokeResourceArns": "AXON_BEDROCK_INVOKE_RESOURCE_ARNS",
    "VerifiedImageUri": "AXON_VERIFIED_IMAGE_URI",
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


def _cutover_guard_handler(
    monkeypatch,
    *,
    desired_count: int = 0,
):
    tree = ast.parse(_STACK.read_text(encoding="utf-8"))
    source = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "_RECOVERY_CUTOVER_GUARD"
            for target in node.targets
        )
    )

    class Scaling:
        @staticmethod
        def describe_scalable_targets(**kwargs):
            return {
                "ScalableTargets": [
                    {
                        "MinCapacity": 0,
                        "SuspendedState": {
                            "DynamicScalingInSuspended": True,
                            "DynamicScalingOutSuspended": True,
                            "ScheduledScalingSuspended": True,
                        },
                    }
                ]
            }

    class Ecs:
        @staticmethod
        def describe_services(**kwargs):
            return {
                "failures": [],
                "services": [
                    {
                        "desiredCount": desired_count,
                        "pendingCount": 0,
                        "runningCount": desired_count,
                    }
                ],
            }

    clients = {
        "application-autoscaling": Scaling(),
        "ecs": Ecs(),
    }
    monkeypatch.setitem(
        sys.modules,
        "boto3",
        types.SimpleNamespace(client=clients.__getitem__),
    )
    namespace: dict[str, object] = {}
    exec(compile(source, "recovery_cutover_guard.py", "exec"), namespace)
    return namespace["handler"]


@pytest.fixture(scope="module")
def synthesized_template(tmp_path_factory: pytest.TempPathFactory) -> dict:
    if not _INFRA_PYTHON.is_file():
        pytest.skip("infra/.venv is required for CDK synthesis tests")

    work_dir = tmp_path_factory.mktemp("infra-stack-security")
    out_dir = work_dir / "cdk.out"
    environment = os.environ.copy()
    environment.update(
        {
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
    return json.loads(
        (out_dir / "AxonLLMStack.template.json").read_text(encoding="utf-8")
    )


def test_required_inputs_have_no_source_defaults():
    tree = ast.parse(_STACK.read_text(encoding="utf-8"))
    parameters: dict[str, ast.Call] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "CfnParameter"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            parameters[node.args[1].value] = node

    assert _REQUIRED_PARAMETERS <= set(parameters)
    for name in _REQUIRED_PARAMETERS:
        call = parameters[name]
        assert all(keyword.arg != "default" for keyword in call.keywords), (
            f"{name} must remain a required deployment input"
        )


def test_required_inputs_have_no_template_defaults(synthesized_template):
    parameters = synthesized_template["Parameters"]
    assert _REQUIRED_PARAMETERS <= set(parameters)
    for name in _REQUIRED_PARAMETERS:
        assert "Default" not in parameters[name], (
            f"{name} must fail deployment when omitted"
        )
    assert parameters["ApprovedHttpsPrefixListId"]["AllowedPattern"] == (
        "^pl-[0-9a-fA-F]+$"
    )
    bedrock = parameters["BedrockInvokeResourceArns"]
    assert bedrock["Type"] == "CommaDelimitedList"
    assert "without wildcards" in bedrock["ConstraintDescription"]
    recovery_table = parameters["RuntimeStateTableName"]
    assert recovery_table["Default"] == ""
    assert recovery_table["AllowedPattern"] == (
        r"^$|^axonllm\-state"
        r"-restore-validation-[A-Za-z0-9_.-]{1,222}$"
    )
    assert "PITR validation table" in recovery_table[
        "ConstraintDescription"
    ]
    cutover_mode = parameters["RecoveryCutoverMode"]
    assert cutover_mode["Default"] == "false"
    assert cutover_mode["AllowedValues"] == ["false", "true"]


def test_deploy_wrapper_requires_and_passes_every_stack_parameter():
    script = _DEPLOY_SCRIPT.read_text(encoding="utf-8")

    for parameter, environment_name in _DEPLOY_ENV_BY_PARAMETER.items():
        assert f"require_env {environment_name}" in script
        assert (
            f'--parameters "AxonLLMStack:{parameter}='
            f"${{{environment_name}}}\""
        ) in script

    assert "ServiceServiceURL" not in script
    assert ".get('CloudFrontURL'" in script
    assert 'DEPLOYMENT_MODE="${AXON_DEPLOYMENT_MODE:-staging}"' in script
    assert (
        '--parameters "AxonLLMStack:DeploymentMode=$DEPLOYMENT_MODE"'
        in script
    )
    assert (
        '--parameters "AxonLLMStack:RuntimeStateTableName='
        '$RUNTIME_TABLE_NAME"'
        in script
    )
    assert (
        '--parameters "AxonLLMStack:RecoveryCutoverMode='
        '$RECOVERY_CUTOVER_MODE"'
        in script
    )
    assert "require_env AXON_OIDC_ISSUER" in script
    assert "require_env AXON_OIDC_CLIENT_SECRET" in script
    assert 'try_get_context("scim_tenants_secret_arn")' in (
        _STACK.read_text(encoding="utf-8")
    )
    assert (
        'CONTEXT+=(--context "scim_tenants_secret_arn=$SCIM_SECRET_ARN")'
        in script
    )


def test_non_global_waf_region_fails_closed(tmp_path):
    if not _INFRA_PYTHON.is_file():
        pytest.skip("infra/.venv is required for CDK synthesis tests")

    script = """
import aws_cdk as cdk
from stack import AxonLLMStack

app = cdk.App()
AxonLLMStack(
    app,
    "WrongRegion",
    env=cdk.Environment(account="111111111111", region="us-west-2"),
)
"""
    environment = os.environ.copy()
    environment.update(
        {
            "JSII_RUNTIME_PACKAGE_CACHE_ROOT": str(tmp_path / "jsii-cache"),
            "PYTHONPYCACHEPREFIX": str(tmp_path / "pycache"),
        }
    )
    completed = subprocess.run(
        [str(_INFRA_PYTHON), "-c", script],
        cwd=_INFRA,
        env=environment,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
    )
    assert completed.returncode != 0
    assert "must be deployed in us-east-1" in completed.stdout


def test_alb_and_tasks_are_private(synthesized_template):
    load_balancer = _one_resource(
        synthesized_template, "AWS::ElasticLoadBalancingV2::LoadBalancer"
    )["Properties"]
    assert load_balancer["Scheme"] == "internal"
    attributes = {
        item["Key"]: item["Value"]
        for item in load_balancer["LoadBalancerAttributes"]
    }
    assert attributes["routing.http.drop_invalid_header_fields.enabled"] == "true"

    service = _one_resource(synthesized_template, "AWS::ECS::Service")[
        "Properties"
    ]
    network = service["NetworkConfiguration"]["AwsvpcConfiguration"]
    assert network["AssignPublicIp"] == "DISABLED"
    assert len(network["SecurityGroups"]) == 1

    subnets = _resources(synthesized_template, "AWS::EC2::Subnet")
    assert len(subnets) == 4
    assert all(
        subnet["Properties"]["MapPublicIpOnLaunch"] is False
        for subnet in subnets
    )


def test_origin_transport_is_tls_only(synthesized_template):
    listener = _one_resource(
        synthesized_template, "AWS::ElasticLoadBalancingV2::Listener"
    )["Properties"]
    assert listener["Port"] == 443
    assert listener["Protocol"] == "HTTPS"
    assert listener["Certificates"] == [
        {"CertificateArn": {"Ref": "OriginCertificateArn"}}
    ]
    assert "TLS13-1-2" in listener["SslPolicy"]

    vpc_origin = _one_resource(
        synthesized_template, "AWS::CloudFront::VpcOrigin"
    )["Properties"]["VpcOriginEndpointConfig"]
    assert vpc_origin["HTTPSPort"] == 443
    assert vpc_origin["OriginProtocolPolicy"] == "https-only"
    assert vpc_origin["OriginSSLProtocols"] == ["TLSv1.2"]


def test_task_egress_and_aws_endpoints_are_explicitly_bounded(
    synthesized_template,
):
    assert "0.0.0.0/0" not in _values_for_key(synthesized_template, "CidrIp")
    assert "::/0" not in _values_for_key(synthesized_template, "CidrIpv6")

    task_groups = [
        resource
        for resource in _resources(
            synthesized_template, "AWS::EC2::SecurityGroup"
        )
        if resource["Properties"]["GroupDescription"].startswith("AxonLLM task")
    ]
    assert len(task_groups) == 1
    dns_rules = task_groups[0]["Properties"]["SecurityGroupEgress"]
    assert {(rule["IpProtocol"], rule["FromPort"]) for rule in dns_rules} == {
        ("tcp", 53),
        ("udp", 53),
    }

    prefix_list_rules = [
        resource["Properties"]
        for resource in _resources(
            synthesized_template, "AWS::EC2::SecurityGroupEgress"
        )
        if resource["Properties"].get("Description")
        == "HTTPS to explicitly approved destinations"
    ]
    assert len(prefix_list_rules) == 1
    rule = prefix_list_rules[0]
    assert rule["Description"] == "HTTPS to explicitly approved destinations"
    assert rule["DestinationPrefixListId"] == {
        "Ref": "ApprovedHttpsPrefixListId"
    }
    assert rule["FromPort"] == rule["ToPort"] == 443
    assert rule["IpProtocol"] == "tcp"
    task_group_ref = rule["GroupId"]["Fn::GetAtt"]
    assert task_group_ref[0].startswith("TaskSecurityGroup")
    assert task_group_ref[1] == "GroupId"

    aws_endpoints = [
        endpoint["Properties"]
        for endpoint in _resources(
            synthesized_template,
            "AWS::EC2::VPCEndpoint",
        )
    ]
    assert len(aws_endpoints) == 3
    assert all(
        endpoint["VpcEndpointType"] == "Interface"
        and endpoint["PrivateDnsEnabled"] is True
        and len(endpoint["SubnetIds"]) == 2
        and all(
            subnet["Ref"].startswith("VpcApplicationSubnet")
            for subnet in endpoint["SubnetIds"]
        )
        for endpoint in aws_endpoints
    )
    sqs_endpoint = next(
        endpoint
        for endpoint in aws_endpoints
        if endpoint["ServiceName"].endswith(".sqs")
    )
    sns_endpoint = next(
        endpoint
        for endpoint in aws_endpoints
        if endpoint["ServiceName"].endswith(".sns")
    )
    logs_endpoint = next(
        endpoint
        for endpoint in aws_endpoints
        if endpoint["ServiceName"].endswith(".logs")
    )
    sqs_statement = sqs_endpoint["PolicyDocument"]["Statement"][0]
    assert _actions(sqs_statement) == {
        "sqs:ChangeMessageVisibility",
        "sqs:DeleteMessage",
        "sqs:GetQueueAttributes",
        "sqs:ReceiveMessage",
        "sqs:SendMessage",
    }
    assert sqs_statement["Resource"]["Fn::GetAtt"][0].startswith(
        "SecurityEventOutboxQueue"
    )
    sns_statement = sns_endpoint["PolicyDocument"]["Statement"][0]
    assert _actions(sns_statement) == {"sns:Publish"}
    assert sns_statement["Resource"]["Ref"].startswith(
        "SecurityEventTopic"
    )
    logs_statement = logs_endpoint["PolicyDocument"]["Statement"][0]
    assert _actions(logs_statement) == {
        "logs:CreateLogStream",
        "logs:PutLogEvents",
    }
    assert logs_statement["Resource"][0]["Fn::GetAtt"][0].startswith(
        "SecurityEventLogGroup"
    )
    assert "SecurityEventLogGroup" in json.dumps(
        logs_statement["Resource"][1]
    )


def test_cloudfront_has_tls_waf_and_preserved_behaviors(synthesized_template):
    web_acl = _one_resource(synthesized_template, "AWS::WAFv2::WebACL")[
        "Properties"
    ]
    assert web_acl["Scope"] == "CLOUDFRONT"
    assert {rule["Name"] for rule in web_acl["Rules"]} == {
        "AWSManagedRulesAmazonIpReputationList",
        "PerIpRateLimit",
    }

    distribution = _one_resource(
        synthesized_template, "AWS::CloudFront::Distribution"
    )["Properties"]["DistributionConfig"]
    assert distribution["Aliases"] == [{"Ref": "ViewerDomainName"}]
    assert distribution["ViewerCertificate"] == {
        "AcmCertificateArn": {"Ref": "ViewerCertificateArn"},
        "MinimumProtocolVersion": "TLSv1.2_2021",
        "SslSupportMethod": "sni-only",
    }
    assert distribution["WebACLId"]["Fn::GetAtt"][1] == "Arn"
    assert distribution["DefaultCacheBehavior"]["ViewerProtocolPolicy"] == (
        "redirect-to-https"
    )
    assert distribution["DefaultCacheBehavior"]["CachePolicyId"] == (
        "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
    )
    static = distribution["CacheBehaviors"]
    assert len(static) == 1
    assert static[0]["PathPattern"] == "/admin/static/*"
    assert static[0]["CachePolicyId"] == (
        "658327ea-f89d-4fab-a63d-7e88639e58f6"
    )


def test_alb_and_cloudfront_access_logs_are_private_and_retained(
    synthesized_template,
):
    buckets = [
        (logical_id, resource)
        for logical_id, resource in synthesized_template["Resources"].items()
        if resource["Type"] == "AWS::S3::Bucket"
    ]
    assert len(buckets) == 1
    bucket_id, bucket = buckets[0]
    properties = bucket["Properties"]

    assert bucket["DeletionPolicy"] == "Retain"
    assert bucket["UpdateReplacePolicy"] == "Retain"
    assert properties["BucketEncryption"] == {
        "ServerSideEncryptionConfiguration": [
            {
                "ServerSideEncryptionByDefault": {
                    "SSEAlgorithm": "AES256",
                }
            }
        ]
    }
    assert properties["OwnershipControls"] == {
        "Rules": [{"ObjectOwnership": "ObjectWriter"}]
    }
    assert properties["PublicAccessBlockConfiguration"] == {
        "BlockPublicAcls": True,
        "BlockPublicPolicy": True,
        "IgnorePublicAcls": True,
        "RestrictPublicBuckets": True,
    }
    assert properties["LifecycleConfiguration"] == {
        "Rules": [
            {
                "AbortIncompleteMultipartUpload": {
                    "DaysAfterInitiation": 7,
                },
                "ExpirationInDays": 365,
                "Id": "ExpireAccessLogs",
                "Status": "Enabled",
            }
        ]
    }

    load_balancer = _one_resource(
        synthesized_template, "AWS::ElasticLoadBalancingV2::LoadBalancer"
    )["Properties"]
    attributes = {
        item["Key"]: item["Value"]
        for item in load_balancer["LoadBalancerAttributes"]
    }
    assert attributes["access_logs.s3.enabled"] == "true"
    assert attributes["access_logs.s3.bucket"] == {"Ref": bucket_id}
    assert attributes["access_logs.s3.prefix"] == "alb"

    distribution = _one_resource(
        synthesized_template, "AWS::CloudFront::Distribution"
    )["Properties"]["DistributionConfig"]
    assert distribution["Logging"] == {
        "Bucket": {"Fn::GetAtt": [bucket_id, "RegionalDomainName"]},
        "IncludeCookies": False,
        "Prefix": "cloudfront/",
    }

    bucket_policy = _one_resource(
        synthesized_template, "AWS::S3::BucketPolicy"
    )["Properties"]["PolicyDocument"]["Statement"]
    assert any(
        statement.get("Effect") == "Deny"
        and statement.get("Condition", {})
        .get("Bool", {})
        .get("aws:SecureTransport")
        == "false"
        for statement in bucket_policy
    )
    assert any(
        statement.get("Effect") == "Allow"
        and statement.get("Principal", {}).get("Service")
        == "delivery.logs.amazonaws.com"
        and "s3:PutObject"
        in (
            [statement["Action"]]
            if isinstance(statement["Action"], str)
            else statement["Action"]
        )
        for statement in bucket_policy
    )


def test_ecs_deployments_roll_back_without_dropping_healthy_capacity(
    synthesized_template,
):
    service = _one_resource(
        synthesized_template,
        "AWS::ECS::Service",
    )["Properties"]

    assert service["DeploymentConfiguration"]["DeploymentCircuitBreaker"] == {
        "Enable": True,
        "Rollback": True,
    }
    assert service["DeploymentConfiguration"]["MinimumHealthyPercent"] == 100
    assert service["DeploymentConfiguration"]["MaximumPercent"] == 200


def test_private_origin_and_public_distribution_are_discoverable(
    synthesized_template,
):
    outputs = synthesized_template["Outputs"]
    assert outputs["CloudFrontURL"]["Value"] == {
        "Fn::Join": [
            "",
            ["https://", {"Ref": "ViewerDomainName"}],
        ]
    }
    assert outputs["InternalALBDomain"]["Value"]["Fn::GetAtt"][1] == "DNSName"


def test_health_check_and_streaming_settings_are_preserved(
    synthesized_template,
):
    target_group = _one_resource(
        synthesized_template, "AWS::ElasticLoadBalancingV2::TargetGroup"
    )["Properties"]
    assert target_group["HealthCheckPath"] == "/ready"
    assert target_group["Matcher"] == {"HttpCode": "200"}
    assert target_group["HealthCheckTimeoutSeconds"] == 5
    assert target_group["TargetGroupAttributes"] == [
        {"Key": "stickiness.enabled", "Value": "true"},
        {"Key": "stickiness.type", "Value": "lb_cookie"},
        {"Key": "stickiness.lb_cookie.duration_seconds", "Value": "3600"},
    ]

    distribution = _one_resource(
        synthesized_template, "AWS::CloudFront::Distribution"
    )["Properties"]["DistributionConfig"]
    origin = distribution["Origins"][0]["VpcOriginConfig"]
    assert origin["OriginKeepaliveTimeout"] == 60
    assert origin["OriginReadTimeout"] == 60


def test_production_oidc_is_conditional_and_bound_to_the_alb(
    synthesized_template,
):
    parameters = synthesized_template["Parameters"]
    assert parameters["DeploymentMode"]["Default"] == "staging"
    assert parameters["DeploymentMode"]["AllowedValues"] == [
        "staging",
        "production",
    ]
    assert parameters["OidcClientSecret"]["NoEcho"] is True

    identity_rule = synthesized_template["Rules"]["ProductionIdentityInputs"]
    assert identity_rule["RuleCondition"] == {
        "Fn::Equals": [{"Ref": "DeploymentMode"}, "production"]
    }
    asserted_parameters = {
        assertion["Assert"]["Fn::Not"][0]["Fn::Equals"][0]["Ref"]
        for assertion in identity_rule["Assertions"]
    }
    assert asserted_parameters == {
        "OidcIssuer",
        "OidcAuthorizationEndpoint",
        "OidcTokenEndpoint",
        "OidcUserInfoEndpoint",
        "OidcClientId",
        "OidcClientSecret",
        "OidcAudience",
    }

    listener_rule = _one_resource(
        synthesized_template,
        "AWS::ElasticLoadBalancingV2::ListenerRule",
    )
    listener_rule_id = next(
        logical_id
        for logical_id, resource in synthesized_template["Resources"].items()
        if resource is listener_rule
    )
    assert listener_rule["Condition"] == "ProductionMode"
    properties = listener_rule["Properties"]
    assert properties["Conditions"] == [
        {
            "Field": "path-pattern",
            "PathPatternConfig": {
                "Values": [
                    "/admin",
                    "/admin/*",
                    "/oauth2/idpresponse",
                ]
            },
        }
    ]
    oidc = properties["Actions"][0]["AuthenticateOidcConfig"]
    assert oidc["ClientSecret"] == {"Ref": "OidcClientSecret"}
    assert oidc["OnUnauthenticatedRequest"] == "authenticate"
    assert oidc["SessionTimeout"] == 28800
    assert properties["Actions"][-1]["Type"] == "forward"

    task_definition = _one_resource(
        synthesized_template,
        "AWS::ECS::TaskDefinition",
    )["Properties"]
    environment = {
        item["Name"]: item["Value"]
        for item in task_definition["ContainerDefinitions"][0]["Environment"]
    }
    assert environment["AXON_OIDC_ISSUER"]["Fn::If"] == [
        "ProductionMode",
        {"Ref": "OidcIssuer"},
        "",
    ]
    assert environment["AXON_OIDC_AUDIENCE"]["Fn::If"] == [
        "ProductionMode",
        {"Ref": "OidcAudience"},
        "",
    ]
    assert environment["AXON_ALB_CLIENT_ID"]["Fn::If"] == [
        "ProductionMode",
        {"Ref": "OidcClientId"},
        "",
    ]
    assert environment["AXON_REQUIRE_CANONICAL_IDENTITY"]["Fn::If"] == [
        "ProductionMode",
        "true",
        "false",
    ]
    assert environment["AXON_DEPLOYMENT_PROFILE"]["Fn::If"] == [
        "ProductionMode",
        "production",
        "development",
    ]
    assert environment["AXON_AWS_ACCOUNT_ID"] == {
        "Ref": "AWS::AccountId"
    }
    selected_table = environment["AXON_DYNAMODB_TABLE"]["Fn::If"]
    assert selected_table[:2] == [
        "UseRecoveredState",
        {"Ref": "RuntimeStateTableName"},
    ]
    primary_table_id = selected_table[2]["Ref"]
    assert synthesized_template["Resources"][primary_table_id]["Type"] == (
        "AWS::DynamoDB::Table"
    )
    assert environment["AXON_EVENT_OUTBOX_QUEUE_URL"]["Ref"].startswith(
        "SecurityEventOutboxQueue"
    )
    target_group_output = synthesized_template["Outputs"][
        "TargetGroupArn"
    ]["Value"]
    target_group_id = target_group_output["Ref"]
    assert synthesized_template["Resources"][target_group_id]["Type"] == (
        "AWS::ElasticLoadBalancingV2::TargetGroup"
    )
    assert environment["AXON_SECURITY_EVENT_SNS_TOPIC_ARN"][
        "Ref"
    ].startswith("SecurityEventTopic")
    assert environment["AXON_SECURITY_EVENT_LOG_GROUP_ARN"][
        "Fn::GetAtt"
    ][0].startswith("SecurityEventLogGroup")
    signer = environment["AXON_ALB_SIGNER_ARN"]["Fn::If"]
    assert signer[0] == "ProductionMode"
    assert signer[1]["Ref"].startswith("LoadBalancer")
    assert signer[2] == ""

    service = _one_resource(synthesized_template, "AWS::ECS::Service")
    assert listener_rule_id not in service.get("DependsOn", [])


def test_alb_only_trusts_cloudfront_and_uses_bounded_egress(
    synthesized_template,
):
    groups = _resources(synthesized_template, "AWS::EC2::SecurityGroup")
    alb_group = next(
        resource
        for resource in groups
        if resource["Properties"]["GroupDescription"].startswith("AxonLLM ALB")
    )
    alb_group_id = next(
        logical_id
        for logical_id, resource in synthesized_template["Resources"].items()
        if resource is alb_group
    )
    assert {
        (rule["IpProtocol"], rule["FromPort"])
        for rule in alb_group["Properties"]["SecurityGroupEgress"]
    } == {("tcp", 53), ("udp", 53)}

    ingress = _resources(
        synthesized_template,
        "AWS::EC2::SecurityGroupIngress",
    )
    cloudfront_ingress = next(
        rule["Properties"]
        for rule in ingress
        if rule["Properties"].get("Description", "").startswith(
            "TLS from the CloudFront"
        )
    )
    assert cloudfront_ingress["FromPort"] == 443
    assert cloudfront_ingress["GroupId"]["Fn::GetAtt"][0] == alb_group_id
    assert cloudfront_ingress["SourcePrefixListId"]["Fn::GetAtt"][1] == (
        "PrefixLists.0.PrefixListId"
    )
    assert all(
        "CidrIp" not in rule["Properties"]
        for rule in ingress
        if rule["Properties"].get("FromPort") == 443
    )

    egress = [
        resource["Properties"]
        for resource in _resources(
            synthesized_template,
            "AWS::EC2::SecurityGroupEgress",
        )
        if resource["Properties"]["GroupId"]["Fn::GetAtt"][0] == alb_group_id
    ]
    assert {rule["Description"] for rule in egress} == {
        "HTTPS to the approved OIDC identity provider",
        "Application traffic to AxonLLM tasks",
    }
    https_rule = next(rule for rule in egress if rule["FromPort"] == 443)
    assert https_rule["DestinationPrefixListId"] == {
        "Ref": "ApprovedHttpsPrefixListId"
    }


def test_private_networking_is_multi_az_and_production_protected(
    synthesized_template,
):
    assert len(_resources(synthesized_template, "AWS::EC2::NatGateway")) == 2

    load_balancer = _one_resource(
        synthesized_template,
        "AWS::ElasticLoadBalancingV2::LoadBalancer",
    )["Properties"]
    attributes = {
        item["Key"]: item["Value"]
        for item in load_balancer["LoadBalancerAttributes"]
    }
    assert attributes["routing.http.desync_mitigation_mode"] == "strictest"
    assert attributes["deletion_protection.enabled"] == {
        "Fn::If": ["ProductionMode", "true", "false"]
    }
    assert all(
        subnet["Ref"].startswith("VpcApplicationSubnet")
        for subnet in load_balancer["Subnets"]
    )


def test_optional_public_dns_uses_the_exact_viewer_name(
    synthesized_template,
):
    assert synthesized_template["Rules"]["PublicDnsInputs"]
    records = _resources(synthesized_template, "AWS::Route53::RecordSet")
    assert len(records) == 2
    assert {record["Properties"]["Type"] for record in records} == {
        "A",
        "AAAA",
    }
    for record in records:
        assert record["Condition"] == "ManagePublicDns"
        properties = record["Properties"]
        assert properties["Name"] == {"Ref": "ViewerDomainName"}
        assert properties["HostedZoneId"] == {"Ref": "PublicHostedZoneId"}
        assert properties["AliasTarget"]["HostedZoneId"] == "Z2FDTNDATAQYW2"
        assert properties["AliasTarget"]["DNSName"]["Fn::GetAtt"][1] == (
            "DomainName"
        )


def test_state_is_kms_encrypted_protected_and_backed_up(
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

    keys = _resources(synthesized_template, "AWS::KMS::Key")
    assert len(keys) == 2
    assert all(key["Properties"]["EnableKeyRotation"] is True for key in keys)
    assert all(key["DeletionPolicy"] == "Retain" for key in keys)

    vault = _one_resource(synthesized_template, "AWS::Backup::BackupVault")
    assert vault["DeletionPolicy"] == "Retain"
    assert vault["UpdateReplacePolicy"] == "Retain"
    assert vault["Properties"]["LockConfiguration"] == {
        "MaxRetentionDays": 365,
        "MinRetentionDays": 30,
    }
    vault_name = vault["Properties"]["BackupVaultName"]
    assert vault_name["Fn::Join"][1][0] == "axon-state"
    assert {"Ref": "AWS::StackId"} in _values_for_key(
        vault_name,
        "Fn::Split",
    )[0]
    plan = _one_resource(synthesized_template, "AWS::Backup::BackupPlan")
    rule = plan["Properties"]["BackupPlan"]["BackupPlanRule"][0]
    assert rule["ScheduleExpression"] == "cron(0 5 * * ? *)"
    assert rule["Lifecycle"] == {
        "DeleteAfterDays": 365,
        "MoveToColdStorageAfterDays": 30,
    }
    selections = _resources(
        synthesized_template,
        "AWS::Backup::BackupSelection",
    )
    assert len(selections) == 2
    primary_selection = next(
        selection
        for selection in selections
        if "Condition" not in selection
    )
    assert primary_selection["Properties"]["BackupSelection"][
        "Resources"
    ][0][
        "Fn::GetAtt"
    ][1] == "Arn"
    recovered_selection = next(
        selection
        for selection in selections
        if selection.get("Condition") == "UseRecoveredState"
    )
    recovered_resource = recovered_selection["Properties"][
        "BackupSelection"
    ]["Resources"][0]["Fn::Join"][1]
    assert recovered_resource[-2:] == [
        ":table/",
        {"Ref": "RuntimeStateTableName"},
    ]

    secret = _one_resource(
        synthesized_template,
        "AWS::SecretsManager::Secret",
    )
    assert secret["DeletionPolicy"] == "Retain"
    assert secret["UpdateReplacePolicy"] == "Retain"
    assert "Name" not in secret["Properties"]
    assert "ProviderSecretArn" in synthesized_template["Outputs"]


def test_task_container_enforces_runtime_hardening(synthesized_template):
    task = _one_resource(
        synthesized_template,
        "AWS::ECS::TaskDefinition",
    )["Properties"]
    container = task["ContainerDefinitions"][0]

    assert container["ReadonlyRootFilesystem"] is True
    assert container["LinuxParameters"] == {
        "Capabilities": {"Drop": ["ALL"]},
        "InitProcessEnabled": True,
    }
    assert container["MountPoints"] == [
        {
            "ContainerPath": "/tmp",
            "ReadOnly": False,
            "SourceVolume": "tmp",
        }
    ]
    assert task["Volumes"] == [{"Name": "tmp"}]


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
    outbox = next(
        queue
        for queue in queues
        if "RedrivePolicy" in queue["Properties"]
    )
    assert outbox["Properties"]["MessageRetentionPeriod"] == 1209600
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
        assert statement["Condition"] == {
            "Bool": {"aws:SecureTransport": "false"}
        }

    security_topic = next(
        topic
        for topic in _resources(synthesized_template, "AWS::SNS::Topic")
        if topic["Properties"].get("DisplayName")
        == "AxonLLM durable security events"
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

    security_log = next(
        log_group
        for log_group in _resources(
            synthesized_template,
            "AWS::Logs::LogGroup",
        )
        if log_group["Properties"].get("RetentionInDays") == 365
    )
    assert "KmsKeyId" in security_log["Properties"]
    assert security_log["DeletionPolicy"] == "Retain"
    assert security_log["UpdateReplacePolicy"] == "Retain"

    outputs = synthesized_template["Outputs"]
    assert outputs["SecurityEventOutboxQueueUrl"]["Value"]["Ref"].startswith(
        "SecurityEventOutboxQueue"
    )
    assert outputs["SecurityEventDeadLetterQueueUrl"]["Value"][
        "Ref"
    ].startswith("SecurityEventDeadLetterQueue")
    assert outputs["SecurityEventTopicArn"]["Value"]["Ref"].startswith(
        "SecurityEventTopic"
    )
    assert outputs["SecurityEventLogGroupArn"]["Value"]["Fn::GetAtt"][
        0
    ].startswith("SecurityEventLogGroup")


def test_inference_iam_is_least_privilege(synthesized_template):
    statements = [
        statement
        for policy in _resources(
            synthesized_template,
            "AWS::IAM::Policy",
        )
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
    ]
    bedrock = next(
        statement
        for statement in statements
        if "bedrock:InvokeModel" in (
            statement["Action"]
            if isinstance(statement["Action"], list)
            else [statement["Action"]]
        )
    )
    assert set(bedrock["Action"]) == {
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
    }
    assert bedrock["Resource"] == {
        "Ref": "BedrockInvokeResourceArns"
    }

    mantle = next(
        statement
        for statement in statements
        if "bedrock-mantle:CreateInference" in (
            statement["Action"]
            if isinstance(statement["Action"], list)
            else [statement["Action"]]
        )
    )
    assert set(mantle["Action"]) == {
        "bedrock-mantle:CreateInference",
        "bedrock-mantle:ListModels",
    }
    assert "bedrock-mantle:*" not in mantle["Action"]


def test_task_role_has_item_permissions_for_atomic_state_transactions(
    synthesized_template,
):
    policies = _resources(synthesized_template, "AWS::IAM::Policy")
    statements = [
        statement
        for policy in policies
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
    ]
    state_access_statements = [
        statement
        for statement in statements
        if "dynamodb:ConditionCheckItem"
        in (
            statement["Action"]
            if isinstance(statement["Action"], list)
            else [statement["Action"]]
        )
    ]
    assert len(state_access_statements) == 1
    state_access = state_access_statements[0]
    assert state_access["Sid"] == "UseSelectedStateTable"
    assert {
        "dynamodb:BatchGetItem",
        "dynamodb:BatchWriteItem",
        "dynamodb:ConditionCheckItem",
        "dynamodb:DeleteItem",
        "dynamodb:DescribeTable",
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:Query",
        "dynamodb:Scan",
        "dynamodb:UpdateItem",
    } == set(state_access["Action"])
    selected_table_conditions = _values_for_key(
        state_access["Resource"],
        "Fn::If",
    )
    assert len(selected_table_conditions) == 2
    assert selected_table_conditions[0] == selected_table_conditions[1]
    selected_table = selected_table_conditions[0]
    assert selected_table[:2] == [
        "UseRecoveredState",
        {"Ref": "RuntimeStateTableName"},
    ]
    primary_table_id = selected_table[2]["Ref"]
    assert synthesized_template["Resources"][primary_table_id]["Type"] == (
        "AWS::DynamoDB::Table"
    )
    assert state_access["Resource"][1]["Fn::Join"][1][-1] == "/index/*"
    all_actions = {
        action
        for statement in statements
        for action in (
            statement["Action"]
            if isinstance(statement["Action"], list)
            else [statement["Action"]]
        )
    }
    assert "dynamodb:TransactGetItems" not in all_actions
    assert "dynamodb:TransactWriteItems" not in all_actions

    publish = next(
        statement
        for statement in statements
        if _actions(statement) == {"sns:Publish"}
    )
    assert publish["Resource"]["Ref"].startswith("SecurityEventTopic")
    security_log_write = next(
        statement
        for statement in statements
        if _actions(statement)
        == {"logs:CreateLogStream", "logs:PutLogEvents"}
        and isinstance(statement["Resource"], dict)
        and "Fn::GetAtt" in statement["Resource"]
        and statement["Resource"]["Fn::GetAtt"][0].startswith(
            "SecurityEventLogGroup"
        )
    )
    assert security_log_write["Resource"]["Fn::GetAtt"][1] == "Arn"


def test_runtime_uses_only_the_release_verified_ecr_digest(
    synthesized_template,
):
    image_parameter = synthesized_template["Parameters"]["VerifiedImageUri"]
    assert "Default" not in image_parameter
    assert image_parameter["AllowedPattern"].endswith(
        r"@sha256:[0-9a-f]{64}$"
    )

    task = _one_resource(
        synthesized_template,
        "AWS::ECS::TaskDefinition",
    )["Properties"]
    assert task["ContainerDefinitions"][0]["Image"] == {
        "Ref": "VerifiedImageUri"
    }
    assert synthesized_template["Outputs"]["RuntimeImageUri"]["Value"] == {
        "Ref": "VerifiedImageUri"
    }
    assert synthesized_template["Outputs"]["DataKeyArn"]["Value"][
        "Fn::GetAtt"
    ][0].startswith("DataKey")
    selected_table = synthesized_template["Outputs"][
        "SelectedRuntimeStateTableName"
    ][
        "Value"
    ]["Fn::If"]
    assert selected_table[:2] == [
        "UseRecoveredState",
        {"Ref": "RuntimeStateTableName"},
    ]
    primary_table_id = selected_table[2]["Ref"]
    assert synthesized_template["Resources"][primary_table_id]["Type"] == (
        "AWS::DynamoDB::Table"
    )
    assert "DockerImageAsset" not in _STACK.read_text(encoding="utf-8")

    execution_policy = next(
        resource
        for logical_id, resource in synthesized_template["Resources"].items()
        if logical_id.startswith("ServiceTaskDefExecutionRoleDefaultPolicy")
    )
    statements = execution_policy["Properties"]["PolicyDocument"]["Statement"]
    pull = next(
        statement
        for statement in statements
        if "ecr:BatchGetImage"
        in (
            statement["Action"]
            if isinstance(statement["Action"], list)
            else [statement["Action"]]
        )
    )
    assert set(pull["Action"]) == {
        "ecr:BatchCheckLayerAvailability",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
    }
    assert "VerifiedImageUri" in json.dumps(pull["Resource"])
    assert ":repository/" in pull["Resource"]["Fn::Join"][1]

    authorization = next(
        statement
        for statement in statements
        if statement["Action"] == "ecr:GetAuthorizationToken"
    )
    assert authorization["Resource"] == "*"


def test_recovery_cutover_requires_a_quiesced_service(
    synthesized_template,
):
    resources = synthesized_template["Resources"]
    guard = resources["RecoveryCutoverGuard"]
    assert guard["Type"] == "AWS::CloudFormation::CustomResource"
    assert guard["Properties"]["ClusterName"] == "axonllm"
    assert guard["Properties"]["CutoverMode"] == {
        "Ref": "RecoveryCutoverMode"
    }
    assert guard["Properties"]["ServiceName"] == "axonllm"
    assert guard["Properties"]["TargetTable"] == {
        "Ref": "RuntimeStateTableName"
    }

    service_id, service = next(
        (logical_id, resource)
        for logical_id, resource in resources.items()
        if resource["Type"] == "AWS::ECS::Service"
    )
    assert service_id
    assert "RecoveryCutoverGuard" in service["DependsOn"]
    assert service["Properties"]["DesiredCount"] == {
        "Fn::If": ["RecoveryCutoverActive", 0, 2]
    }

    handler = next(
        function
        for function in _resources(
            synthesized_template,
            "AWS::Lambda::Function",
        )
        if function["Properties"].get("Description")
        == "Blocks DynamoDB state cutover until AxonLLM is quiesced"
    )
    code = handler["Properties"]["Code"]["ZipFile"]
    assert "RecoveryCutoverMode=true" in code
    assert "previous_cutover_mode" in code
    assert "rollback_from_cutover" in code
    assert "target_changed" in code
    assert 'target.get("MinCapacity") != 0' in code
    assert "DynamicScalingInSuspended" in code
    assert "DynamicScalingOutSuspended" in code
    assert "ScheduledScalingSuspended" in code
    assert '("desiredCount", "pendingCount", "runningCount")' in code

    guard_policy = next(
        policy
        for policy in _resources(
            synthesized_template,
            "AWS::IAM::Policy",
        )
        if any(
            _actions(statement)
            == {"ecs:DescribeServices"}
            for statement in policy["Properties"][
                "PolicyDocument"
            ]["Statement"]
        )
    )
    statements = guard_policy["Properties"]["PolicyDocument"]["Statement"]
    describe_service = next(
        statement
        for statement in statements
        if _actions(statement) == {"ecs:DescribeServices"}
    )
    assert _values_for_key(
        describe_service["Resource"],
        "Fn::Join",
    )[0][1][-1] == ":service/axonllm/axonllm"
    describe_scaling = next(
        statement
        for statement in statements
        if _actions(statement)
        == {
            "application-autoscaling:DescribeScalableTargets"
        }
    )
    assert describe_scaling["Resource"] == "*"


def test_recovery_guard_allows_safe_forward_and_rollback_updates(
    monkeypatch,
):
    handler = _cutover_guard_handler(monkeypatch)
    base = {
        "ClusterName": "axonllm",
        "ServiceName": "axonllm",
    }

    forward = handler(
        {
            "RequestType": "Update",
            "ResourceProperties": {
                **base,
                "CutoverMode": "true",
                "TargetTable": "axonllm-state-restore-validation-safe",
            },
            "OldResourceProperties": {
                **base,
                "CutoverMode": "false",
                "TargetTable": "",
            },
        },
        None,
    )
    rollback = handler(
        {
            "RequestType": "Update",
            "ResourceProperties": {
                **base,
                "CutoverMode": "false",
                "TargetTable": "",
            },
            "OldResourceProperties": {
                **base,
                "CutoverMode": "true",
                "TargetTable": "axonllm-state-restore-validation-safe",
            },
        },
        None,
    )

    assert forward["PhysicalResourceId"] == "AxonLLMRecoveryCutoverGuard"
    assert rollback["PhysicalResourceId"] == "AxonLLMRecoveryCutoverGuard"


def test_recovery_guard_rejects_unguarded_or_running_table_switch(
    monkeypatch,
):
    base = {
        "ClusterName": "axonllm",
        "ServiceName": "axonllm",
    }
    unsafe = {
        "RequestType": "Update",
        "ResourceProperties": {
            **base,
            "CutoverMode": "false",
            "TargetTable": "new-table",
        },
        "OldResourceProperties": {
            **base,
            "CutoverMode": "false",
            "TargetTable": "old-table",
        },
    }
    with pytest.raises(RuntimeError, match="RecoveryCutoverMode"):
        _cutover_guard_handler(monkeypatch)(unsafe, None)

    unsafe["OldResourceProperties"]["CutoverMode"] = "true"
    with pytest.raises(RuntimeError, match="fully quiesced"):
        _cutover_guard_handler(
            monkeypatch,
            desired_count=2,
        )(unsafe, None)


def test_operational_alarms_are_actionable(synthesized_template):
    topics = _resources(synthesized_template, "AWS::SNS::Topic")
    assert len(topics) == 2
    alarm_topic = next(
        topic
        for topic in topics
        if topic["Properties"].get("DisplayName")
        == "AxonLLM production alarms"
    )
    assert "KmsMasterKeyId" in alarm_topic["Properties"]

    data_key = next(
        key
        for key in _resources(synthesized_template, "AWS::KMS::Key")
        if key["Properties"]["Description"].startswith("Encrypts AxonLLM DynamoDB")
    )
    key_statements = data_key["Properties"]["KeyPolicy"]["Statement"]
    cloudwatch_key_access = next(
        statement
        for statement in key_statements
        if statement.get("Sid") == "AllowCloudWatchAlarmEncryption"
    )
    assert cloudwatch_key_access["Principal"] == {
        "Service": "cloudwatch.amazonaws.com"
    }
    assert set(cloudwatch_key_access["Action"]) == {
        "kms:Decrypt",
        "kms:GenerateDataKey*",
    }

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

    alarms = _resources(synthesized_template, "AWS::CloudWatch::Alarm")
    assert len(alarms) == 10
    assert all(alarm["Properties"]["AlarmActions"] for alarm in alarms)
    assert all(alarm["Properties"]["OKActions"] for alarm in alarms)
    metric_names = {
        metric["MetricStat"]["Metric"]["MetricName"]
        for alarm in alarms
        for metric in alarm["Properties"].get("Metrics", [])
        if "MetricStat" in metric
    }
    simple_metric_names = {
        alarm["Properties"].get("MetricName") for alarm in alarms
    }
    assert "RunningTaskCount" in simple_metric_names
    assert "ApproximateNumberOfMessagesVisible" in simple_metric_names
    assert {"ThrottledRequests", "SystemErrors"} <= metric_names
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
    monitored_tables = {
        json.dumps(dimension["Value"], sort_keys=True)
        for alarm in alarms
        for metric in alarm["Properties"].get("Metrics", [])
        for dimension in metric.get("MetricStat", {})
        .get("Metric", {})
        .get("Dimensions", [])
        if dimension["Name"] == "TableName"
    }
    assert monitored_tables == {
        json.dumps(
            {
                "Fn::If": [
                    "UseRecoveredState",
                    {"Ref": "RuntimeStateTableName"},
                    {
                        "Ref": next(
                            logical_id
                            for logical_id, resource in synthesized_template[
                                "Resources"
                            ].items()
                            if resource["Type"] == "AWS::DynamoDB::Table"
                        )
                    },
                ]
            },
            sort_keys=True,
        )
    }
    assert len(
        _resources(synthesized_template, "AWS::CloudWatch::Dashboard")
    ) == 1
