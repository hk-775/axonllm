"""CDK contracts for the request-driven AgentCore web control plane."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest


_REPO = Path(__file__).resolve().parents[2]
_INFRA = _REPO / "infra"
_INFRA_PYTHON = _INFRA / ".venv" / "bin" / "python"


@pytest.fixture(scope="module")
def serverless_control_template(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict:
    if not _INFRA_PYTHON.is_file():
        pytest.skip("infra/.venv is required for CDK synthesis tests")
    work_dir = tmp_path_factory.mktemp("serverless-control")
    out_dir = work_dir / "cdk.out"
    environment = os.environ.copy()
    environment.update(
        {
            "CDK_CONTEXT_JSON": json.dumps(
                {
                    "deployment_namespace": "contract",
                    "deployment_target": "serverless-control-plane",
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
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return json.loads(
        (out_dir / "AxonLLMServerlessControlPlaneStack-contract.template.json").read_text(encoding="utf-8")
    )


def _resources(template: dict, resource_type: str) -> list[tuple[str, dict]]:
    return [
        (logical_id, resource)
        for logical_id, resource in template["Resources"].items()
        if resource["Type"] == resource_type
    ]


def _one(template: dict, resource_type: str) -> tuple[str, dict]:
    resources = _resources(template, resource_type)
    assert len(resources) == 1
    return resources[0]


def test_serverless_stack_has_no_container_or_customer_network_resources(
    serverless_control_template,
) -> None:
    forbidden_prefixes = (
        "AWS::EC2",
        "AWS::ECS",
        "AWS::ElasticLoadBalancing",
    )
    forbidden_exact = {
        "AWS::AutoScaling::AutoScalingGroup",
        "AWS::ElasticLoadBalancingV2::LoadBalancer",
        "AWS::ElasticLoadBalancingV2::TargetGroup",
    }

    assert not [
        resource["Type"]
        for resource in serverless_control_template["Resources"].values()
        if resource["Type"].startswith(forbidden_prefixes) or resource["Type"] in forbidden_exact
    ]


def test_api_gateway_requires_cloudfront_origin_credential(
    serverless_control_template,
) -> None:
    methods = _resources(
        serverless_control_template,
        "AWS::ApiGateway::Method",
    )
    _, api_key = _one(
        serverless_control_template,
        "AWS::ApiGateway::ApiKey",
    )
    _, distribution = _one(
        serverless_control_template,
        "AWS::CloudFront::Distribution",
    )
    origins = distribution["Properties"]["DistributionConfig"]["Origins"]
    api_origin = next(origin for origin in origins if "CustomOriginConfig" in origin)
    custom_headers = {header["HeaderName"]: header["HeaderValue"] for header in api_origin["OriginCustomHeaders"]}

    assert methods
    assert all(method["Properties"]["ApiKeyRequired"] is True for _, method in methods)
    assert custom_headers["x-api-key"] == api_key["Properties"]["Value"]
    assert "{{resolve:secretsmanager:" in json.dumps(custom_headers["x-api-key"])
    assert not _resources(
        serverless_control_template,
        "AWS::Lambda::Url",
    )


def test_api_gateway_lambda_permissions_are_api_scoped(
    serverless_control_template,
) -> None:
    permissions = _resources(
        serverless_control_template,
        "AWS::Lambda::Permission",
    )

    assert len(permissions) == 2
    assert all(
        permission["Properties"]["Action"] == "lambda:InvokeFunction"
        and permission["Properties"]["Principal"] == "apigateway.amazonaws.com"
        and ":execute-api:" in json.dumps(permission["Properties"]["SourceArn"])
        for _, permission in permissions
    )


def test_control_lambda_is_arm64_control_only_and_cycle_free(
    serverless_control_template,
) -> None:
    functions = _resources(
        serverless_control_template,
        "AWS::Lambda::Function",
    )
    logical_id, function = next(
        item
        for item in functions
        if item[1]["Properties"].get("Handler") == "src.gateway.serverless_control.lambda_handler"
    )
    properties = function["Properties"]
    environment = properties["Environment"]["Variables"]

    assert properties["Architectures"] == ["arm64"]
    assert properties["MemorySize"] == 1024
    assert properties["ReservedConcurrentExecutions"] == 20
    assert properties["Timeout"] == 30
    assert "VpcConfig" not in properties
    assert environment["AXON_CONTROL_PLANE_ONLY"] == "true"
    assert environment["AXON_AUTH_MODE"] == "ENFORCE"
    assert "AXON_CONTROL_PLANE_URL" not in environment
    assert "AXON_OIDC_AUDIENCE" not in environment
    assert environment["AXON_EXPORT_BUCKET_NAME"] == {"Ref": "ExportBucketName"}
    assert environment["AXON_EXPORT_QUEUE_URL"] == {"Ref": "ExportQueueUrl"}

    client_logical_id, _ = _one(
        serverless_control_template,
        "AWS::Cognito::UserPoolClient",
    )
    assert client_logical_id not in json.dumps(function)
    assert logical_id


def test_control_lambda_can_only_queue_and_download_bound_exports(
    serverless_control_template,
) -> None:
    parameters = serverless_control_template["Parameters"]
    assert {
        "ExportBucketName",
        "ExportQueueArn",
        "ExportQueueUrl",
    } <= set(parameters)
    statements = [
        statement
        for _, policy in _resources(
            serverless_control_template,
            "AWS::IAM::Policy",
        )
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
    ]
    by_sid = {statement["Sid"]: statement for statement in statements if "Sid" in statement}

    assert set(by_sid["QueueAndInspectExports"]["Action"]) == {
        "sqs:GetQueueAttributes",
        "sqs:SendMessage",
    }
    assert by_sid["QueueAndInspectExports"]["Resource"] == {"Ref": "ExportQueueArn"}
    assert by_sid["DownloadCompletedExports"]["Action"] == ("s3:GetObject")
    assert by_sid["DownloadCompletedExports"]["Resource"]["Fn::Join"][1][-1] == "/exports/*"
    assert not _resources(
        serverless_control_template,
        "AWS::SQS::Queue",
    )


def test_browser_client_depends_on_distribution_not_lambda(
    serverless_control_template,
) -> None:
    distribution_id, _ = _one(
        serverless_control_template,
        "AWS::CloudFront::Distribution",
    )
    _, client = _one(
        serverless_control_template,
        "AWS::Cognito::UserPoolClient",
    )
    properties = client["Properties"]

    assert properties["AllowedOAuthFlows"] == ["code"]
    assert properties["GenerateSecret"] is False
    assert distribution_id in json.dumps(properties["CallbackURLs"])
    assert distribution_id in json.dumps(properties["LogoutURLs"])


def test_cloudfront_routes_static_and_dynamic_origins_separately(
    serverless_control_template,
) -> None:
    _, distribution = _one(
        serverless_control_template,
        "AWS::CloudFront::Distribution",
    )
    config = distribution["Properties"]["DistributionConfig"]
    behaviors = {behavior["PathPattern"]: behavior for behavior in config["CacheBehaviors"]}

    assert config["DefaultRootObject"] == "index.html"
    assert config["WebACLId"]["Fn::GetAtt"][1] == "Arn"
    assert behaviors["/admin/*"]["AllowedMethods"] == [
        "GET",
        "HEAD",
        "OPTIONS",
        "PUT",
        "PATCH",
        "POST",
        "DELETE",
    ]
    assert behaviors["/admin/*"]["CachePolicyId"] == ("4135ea2d-6df8-44a3-9df3-4b5a84be39ad")
    assert behaviors["/admin/static/*"]["AllowedMethods"] == [
        "GET",
        "HEAD",
        "OPTIONS",
    ]
    assert behaviors["/admin/dashboard"]["FunctionAssociations"]
    assert {
        "/admin/*",
        "/admin/dashboard",
        "/admin/static/*",
        "/auth/*",
        "/health",
        "/ready",
        "/saml/*",
        "/scim/*",
    }.issubset(behaviors)


def test_private_site_bucket_is_readable_only_by_exact_distribution(
    serverless_control_template,
) -> None:
    distribution_id, _ = _one(
        serverless_control_template,
        "AWS::CloudFront::Distribution",
    )
    policies = _resources(
        serverless_control_template,
        "AWS::S3::BucketPolicy",
    )
    cloudfront_statements = [
        statement
        for _, policy in policies
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
        if statement.get("Principal") == {"Service": "cloudfront.amazonaws.com"}
    ]

    assert len(cloudfront_statements) == 1
    statement = cloudfront_statements[0]
    assert statement["Action"] == "s3:GetObject"
    assert distribution_id in json.dumps(statement["Condition"])


def test_static_site_uses_a_rotating_customer_managed_key(
    serverless_control_template,
) -> None:
    site_bucket_id, site_bucket = next(
        item
        for item in _resources(
            serverless_control_template,
            "AWS::S3::Bucket",
        )
        if item[0].startswith("StaticSiteBucket")
    )
    key_id, key = _one(
        serverless_control_template,
        "AWS::KMS::Key",
    )
    encryption = site_bucket["Properties"]["BucketEncryption"][
        "ServerSideEncryptionConfiguration"
    ]

    assert encryption == [
        {
            "BucketKeyEnabled": True,
            "ServerSideEncryptionByDefault": {
                "KMSMasterKeyID": {
                    "Fn::GetAtt": [
                        key_id,
                        "Arn",
                    ]
                },
                "SSEAlgorithm": "aws:kms",
            },
        }
    ]
    assert key["Properties"]["Description"] == (
        "Encrypts AxonLLM serverless static-site objects"
    )
    assert key["Properties"]["EnableKeyRotation"] is True
    cloudfront_decrypt = next(
        statement
        for statement in key["Properties"]["KeyPolicy"]["Statement"]
        if statement.get("Principal") == {
            "Service": "cloudfront.amazonaws.com"
        }
    )
    source_arn = cloudfront_decrypt["Condition"]["ArnLike"][
        "AWS:SourceArn"
    ]["Fn::Join"][1]
    assert cloudfront_decrypt["Action"] == "kms:Decrypt"
    assert {"Ref": "AWS::AccountId"} in source_arn
    assert source_arn[-1] == ":distribution/*"
    assert site_bucket_id


def test_external_state_is_parameterized_without_imports_or_state_ownership(
    serverless_control_template,
) -> None:
    serialized = json.dumps(serverless_control_template)
    stateful_types = {
        "AWS::Backup::BackupPlan",
        "AWS::Backup::BackupVault",
        "AWS::DynamoDB::Table",
        "AWS::SNS::Topic",
        "AWS::SQS::Queue",
    }

    assert "Fn::ImportValue" not in serialized
    assert not [
        resource["Type"]
        for resource in serverless_control_template["Resources"].values()
        if resource["Type"] in stateful_types
    ]
    assert "ApplicationStateDataKeyArn" in (serverless_control_template["Parameters"])
    origin_secrets = _resources(
        serverless_control_template,
        "AWS::SecretsManager::Secret",
    )
    assert len(origin_secrets) == 1
    assert "origin credential" in origin_secrets[0][1]["Properties"]["Description"]


def test_waf_is_fail_closed_with_managed_and_rate_protection(
    serverless_control_template,
) -> None:
    _, web_acl = _one(
        serverless_control_template,
        "AWS::WAFv2::WebACL",
    )
    properties = web_acl["Properties"]
    rules = {rule["Name"]: rule for rule in properties["Rules"]}

    assert properties["DefaultAction"] == {"Block": {}}
    assert (
        rules["AWSManagedCommonProtections"]["Statement"]["ManagedRuleGroupStatement"]["Name"]
        == "AWSManagedRulesCommonRuleSet"
    )
    assert rules["PerViewerRateLimit"]["Statement"]["RateBasedStatement"]["Limit"] == 2_000
    assert rules["ReviewedViewerNetworks"]["Action"] == {"Allow": {}}


def test_artifacts_are_bound_to_version_and_hash_parameters(
    serverless_control_template,
) -> None:
    _, control_function = next(
        item
        for item in _resources(
            serverless_control_template,
            "AWS::Lambda::Function",
        )
        if item[1]["Properties"].get("Handler") == "src.gateway.serverless_control.lambda_handler"
    )
    code = control_function["Properties"]["Code"]

    assert code["S3ObjectVersion"] == {"Ref": "ControlApiCodeObjectVersion"}
    assert "ControlApiCodeSha256" in json.dumps(control_function["Properties"]["Description"])
    parameters = serverless_control_template["Parameters"]
    assert "StaticAssetsSha256" in parameters
    assert "StaticAssetsObjectVersion" in parameters

    _, deployment = _one(
        serverless_control_template,
        "Custom::AxonLLMStaticAssets",
    )
    properties = deployment["Properties"]
    assert properties["SourceVersion"] == {"Ref": "StaticAssetsObjectVersion"}
    assert properties["SourceSha256"] == {"Ref": "StaticAssetsSha256"}
    assert properties["SourceKey"] == {"Ref": "StaticAssetsObjectKey"}
    assert not _resources(
        serverless_control_template,
        "Custom::CDKBucketDeployment",
    )


def test_static_deployer_has_exact_version_and_distribution_permissions(
    serverless_control_template,
) -> None:
    functions = _resources(
        serverless_control_template,
        "AWS::Lambda::Function",
    )
    _, deployer = next(
        item
        for item in functions
        if item[1]["Properties"].get("Handler") == "index.handler"
        and "exact-version static-site deployment" in item[1]["Properties"]["Code"].get("ZipFile", "")
    )
    assert deployer["Properties"]["Timeout"] == 900
    assert deployer["Properties"]["Architectures"] == ["arm64"]
    policies = _resources(
        serverless_control_template,
        "AWS::IAM::Policy",
    )
    statements = [
        statement for _, policy in policies for statement in policy["Properties"]["PolicyDocument"]["Statement"]
    ]
    by_sid = {statement["Sid"]: statement for statement in statements if "Sid" in statement}

    assert by_sid["ReadExactStaticArtifactVersion"]["Action"] == ("s3:GetObjectVersion")
    assert by_sid["ListPrivateStaticSite"]["Action"] == "s3:ListBucket"
    assert set(by_sid["ManagePrivateStaticObjects"]["Action"]) == {
        "s3:DeleteObject",
        "s3:PutObject",
    }
    assert by_sid["InvalidateExactDistribution"]["Action"] == ("cloudfront:CreateInvalidation")
