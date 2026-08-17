#!/usr/bin/env python3
"""CDK app entry point for AxonLLM Fargate and AgentCore deployments.

``cdk.json`` invokes this as ``.venv/bin/python3 app.py`` rather than ``python3
app.py``, and the explicit interpreter is the point. ``aws-cdk-lib`` is installed
into ``infra/.venv`` — it is not a dependency of the root project, because
nothing at runtime imports it — so a bare ``python3`` resolves to whatever is on
PATH, does not see that venv, and dies here with ``ModuleNotFoundError: No module
named 'aws_cdk'``.

That failed for the documented install sequence while ``deploy-fargate.sh``
worked, because the script activates the venv first and the README's step 1 does
not. Naming the interpreter makes both paths work, and makes ``npx cdk synth``
work from a shell with nothing activated.

The relative path is deliberate: the CDK CLI runs this command with the directory
containing ``cdk.json`` as the working directory, so ``.venv/bin/python3``
resolves against ``infra/`` no matter where the caller was.
"""

import re

import aws_cdk as cdk
from aws_cdk import aws_iam as iam


_NAMESPACE_PATTERN = re.compile(r"^[a-z](?:[a-z0-9-]{0,14}[a-z0-9])?$")
_CDK_QUALIFIERS = {
    "production": "axprod",
    "qualification": "axqual",
    "external": "axext",
}


def deployment_namespace(app: cdk.App) -> str:
    value = app.node.try_get_context("deployment_namespace") or ""
    if not isinstance(value, str) or (value and _NAMESPACE_PATTERN.fullmatch(value) is None):
        raise ValueError(
            "deployment_namespace must be 1-16 lowercase letters, digits, "
            "or internal hyphens, start with a letter, and end with a letter "
            "or digit"
        )
    return value


def stack_name(base: str, namespace: str) -> str:
    return f"{base}-{namespace}" if namespace else base


def cdk_qualifier(app: cdk.App, namespace: str) -> str:
    domain = (
        "production" if not namespace else "external" if namespace in {"external", "external-oidc"} else "qualification"
    )
    expected = _CDK_QUALIFIERS[domain]
    configured = app.node.try_get_context("cdk_qualifier")
    if configured is not None and configured != expected:
        raise ValueError(f"cdk_qualifier must be {expected!r} for the selected namespace")
    return expected


def apply_service_boundary(
    stack: cdk.Stack,
    *,
    qualifier: str,
    region: str,
) -> None:
    boundary = iam.ManagedPolicy.from_managed_policy_arn(
        stack,
        "RequiredServiceRoleBoundary",
        (
            f"arn:{cdk.Aws.PARTITION}:iam::{cdk.Aws.ACCOUNT_ID}:policy/"
            "AxonLLMAgentCoreServiceBoundary-"
            f"{qualifier}-{region}"
        ),
    )
    iam.PermissionsBoundary.of(stack).apply(boundary)
    required_tags = {
        "Application": "AxonLLM",
        "AxonLLMTrustDomain": qualifier,
    }
    for construct in stack.node.find_all():
        if not isinstance(construct, iam.CfnRole):
            continue
        for key, value in required_tags.items():
            construct.tags.set_tag(key, value, priority=1_000)


app = cdk.App()
namespace = deployment_namespace(app)
qualifier = cdk_qualifier(app, namespace)

environment = cdk.Environment(
    account=app.node.try_get_context("account") or None,
    region=app.node.try_get_context("region") or "us-east-1",
)
region = app.node.try_get_context("region") or "us-east-1"
deployment_target = (app.node.try_get_context("deployment_target") or "fargate").lower()
deployment_stack: cdk.Stack | None = None

if deployment_target == "fargate":
    from stack import AxonLLMStack

    AxonLLMStack(
        app,
        "AxonLLMStack",
        env=environment,
    )
elif deployment_target == "agentcore":
    from agentcore_stack import AxonLLMAgentCoreStack

    deployment_stack = AxonLLMAgentCoreStack(
        app,
        stack_name("AxonLLMAgentCoreStack", namespace),
        bootstrap_qualifier=qualifier,
        deployment_namespace=namespace,
        env=environment,
        synthesizer=cdk.DefaultStackSynthesizer(qualifier=qualifier),
    )
elif deployment_target == "identity":
    from identity_stack import AxonLLMIdentityStack

    deployment_stack = AxonLLMIdentityStack(
        app,
        stack_name("AxonLLMIdentityStack", namespace),
        deployment_namespace=namespace,
        env=environment,
        synthesizer=cdk.DefaultStackSynthesizer(qualifier=qualifier),
    )
elif deployment_target == "control-plane":
    from control_plane_stack import AxonLLMControlPlaneStack

    deployment_stack = AxonLLMControlPlaneStack(
        app,
        stack_name("AxonLLMControlPlaneStack", namespace),
        deployment_namespace=namespace,
        env=environment,
        synthesizer=cdk.DefaultStackSynthesizer(qualifier=qualifier),
    )
elif deployment_target == "release-foundation":
    from release_foundation_stack import AxonLLMReleaseFoundationStack

    AxonLLMReleaseFoundationStack(
        app,
        "AxonLLMReleaseFoundationStack",
        env=environment,
        termination_protection=True,
    )
elif deployment_target == "launch-workers":
    from launch_workers_stack import AxonLLMLaunchWorkersStack

    deployment_stack = AxonLLMLaunchWorkersStack(
        app,
        stack_name("AxonLLMLaunchWorkersStack", namespace),
        deployment_namespace=namespace,
        env=environment,
        synthesizer=cdk.DefaultStackSynthesizer(qualifier=qualifier),
    )
else:
    raise ValueError(
        "deployment_target must be 'fargate', 'agentcore', 'identity', "
        "'control-plane', 'release-foundation', or 'launch-workers'"
    )

if deployment_stack is not None:
    apply_service_boundary(
        deployment_stack,
        qualifier=qualifier,
        region=region,
    )

app.synth()
