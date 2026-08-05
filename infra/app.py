#!/usr/bin/env python3
"""CDK app entry point for AxonLLM Fargate deployment.

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
