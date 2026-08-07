"""Security contract tests for the checked-in Fargate CDK stack."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import subprocess

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
}
_DEPLOY_ENV_BY_PARAMETER = {
    "ViewerDomainName": "AXON_VIEWER_DOMAIN_NAME",
    "ViewerCertificateArn": "AXON_VIEWER_CERTIFICATE_ARN",
    "OriginDomainName": "AXON_ORIGIN_DOMAIN_NAME",
    "OriginCertificateArn": "AXON_ORIGIN_CERTIFICATE_ARN",
    "ApprovedHttpsPrefixListId": "AXON_APPROVED_HTTPS_PREFIX_LIST_ID",
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

    assert set(parameters) == _REQUIRED_PARAMETERS
    for name, call in parameters.items():
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


def test_task_egress_is_explicitly_bounded(synthesized_template):
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
        if "DestinationPrefixListId" in resource["Properties"]
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
    assert target_group["HealthCheckPath"] == "/health"
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
