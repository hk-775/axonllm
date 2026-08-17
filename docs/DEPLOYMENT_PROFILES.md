# AxonLLM Deployment Profiles

AxonLLM deployment has two independent dimensions:

- `experience`: the product that owns login, administration, and the
  human-facing UI;
- `execution`: where the AxonLLM routing runtime executes.

The four supported combinations have stable profile aliases:

| Profile | Experience | Execution | Human-facing URL |
|---|---|---|---|
| `standalone` | AxonLLM | Container/ECS | AxonLLM CloudFront URL |
| `standalone-agentcore` | AxonLLM | AgentCore | AxonLLM control-plane URL when managed Cognito is selected |
| `ostiari-embedded` | Ostiari | Ostiari container/process | Ostiari URL |
| `ostiari-agentcore` | Ostiari | AgentCore | Ostiari URL |

These profiles are separate from `AXON_DEPLOYMENT_PROFILE`, which remains the
runtime security setting `development` or `production`. Topology uses
`AXON_EXPERIENCE_OWNER`, `AXON_EXECUTION_TARGET`, and the setup-time
`AXON_TOPOLOGY_PROFILE` variable.

## Feature Parity

Execution target does not select product features. Standalone and AgentCore
must expose the same AxonLLM APIs, UI, identity, provider routing, policy,
quota, cache, audit, usage, administration, SCIM/SAML, and security-event
capabilities. Only the execution adapter and its AWS principal differ.

Customer database querying is an explicit add-on and is absent from every core
profile. Core templates create no Athena resources, Athena VPC endpoints, or
customer datasource-role STS authority. A future add-on must support and pass
the same black-box contract on both execution targets. See
[Customer Database Query Add-On](CUSTOMER_DATABASE_QUERY_ADDON.md).

## Standalone

`deploy-fargate.sh` deploys the `standalone` profile. It creates the complete
AxonLLM web experience and routing data plane on ECS Fargate. The script
defaults `AXON_TOPOLOGY_PROFILE` to `standalone` and rejects every other value
so an Ostiari-owned deployment cannot accidentally expose the AxonLLM control
plane.

## Standalone AgentCore

Generate a schema-v3 setup with:

```bash
uv run axon setup agentcore \
  --deployment-profile standalone-agentcore \
  --identity-mode managed-cognito \
  ...
```

Managed Cognito deploys the AxonLLM identity and control-plane stacks around
the AgentCore runtime. External OIDC is also valid, but it deploys only the
headless AgentCore runtime and canonical bootstrap; the adopter supplies its
own trusted administration surface.

For a clean account, upgrade, or failed-first-create retry, use the same
command:

```bash
./deploy-agentcore.sh --config axonllm-agentcore.json --install
```

The installer reuses a healthy CDK toolkit and automatically recovers only a
failed stack that can be proven to match the reviewed deployment contract.

## Ostiari Embedded

Ostiari sets:

```text
AXON_EXPERIENCE_OWNER=ostiari
AXON_EXECUTION_TARGET=container
```

and embeds `build_gateway_agent()` in its own execution process. AxonLLM
rejects `build_starlette_app()` in an Ostiari-owned process so the deployment
cannot expose a second login or administration UI. Ostiari owns the public URL,
identity, governance UI, and lifecycle.

## Ostiari AgentCore

Generate a schema-v3 setup with:

```bash
uv run axon setup agentcore \
  --deployment-profile ostiari-agentcore \
  --identity-mode external-oidc \
  ...
```

This profile requires external OIDC. Ostiari remains the identity and
experience owner, while AxonLLM runs as the headless AgentCore routing runtime.
The AxonLLM deployer does not create Cognito, CloudFront, or a separate web
control plane and therefore does not invent an AxonLLM UI address.

## Setup Schema

New AgentCore setup files use schema v3:

```json
{
  "schema_version": 3,
  "target": "agentcore",
  "deployment": {
    "experience": "axonllm",
    "execution": "agentcore"
  }
}
```

Schema-v2 files remain accepted and round-trip unchanged. Their historical
meaning is `standalone-agentcore`. During the first upgrade, a retained
AgentCore stack that predates topology outputs may add those bindings
automatically only under `standalone-agentcore`.

Changing an existing production stack from AxonLLM-owned to Ostiari-owned
experience is an ownership migration, not an in-place configuration edit.
Deploy a new reviewed namespace or stack, migrate callers and state
deliberately, and retire the old experience after validation.

The AgentCore stack records `DeploymentExperience` and
`DeploymentExecution`, injects them into the runtime environment, and binds
them into candidate and promotion evidence. A later deployment cannot silently
change either value.
