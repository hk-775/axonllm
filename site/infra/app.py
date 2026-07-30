#!/usr/bin/env python3
"""CDK app for the AxonLLM landing page.

Region is pinned to us-east-1 because CloudFront can only attach an ACM
certificate issued there. Without the pin, deploying from a shell whose
default region is anything else builds a stack whose cert is unusable —
and the failure shows up at distribution-creation time, not at synth.
"""

import os

import aws_cdk as cdk

from stack import AxonLLMSiteStack

app = cdk.App()

domain_name = app.node.try_get_context("domain_name")
hosted_zone_id = app.node.try_get_context("hosted_zone_id")

AxonLLMSiteStack(
    app,
    "AxonLLMSiteStack",
    domain_name=domain_name,
    hosted_zone_id=hosted_zone_id,
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region="us-east-1",
    ),
)

app.synth()
