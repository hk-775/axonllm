#!/usr/bin/env python3
"""CDK app entry point for AxonLLM Fargate deployment."""

import aws_cdk as cdk

from stack import AxonLLMStack

app = cdk.App()

AxonLLMStack(
    app,
    "AxonLLMStack",
    env=cdk.Environment(
        account=app.node.try_get_context("account") or None,
        region=app.node.try_get_context("region") or "us-east-1",
    ),
)

app.synth()
