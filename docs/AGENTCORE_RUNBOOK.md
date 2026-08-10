# AxonLLM AgentCore Runbook

This runbook describes the checked-in Amazon Bedrock AgentCore adapter, image,
and CDK stack. It does not certify a checkout or AWS deployment.

## Current Status

The AgentCore application, private-network CDK stack, retained Cognito identity
stack, and first-adopter deployment workflow are implemented.
`DynamoPersistence` provides canonical per-tenant SCIM version and strongly
consistent snapshot reads used during startup and runtime convergence.

Focused hardening regressions are green locally. The release workflow records
Fargate and AgentCore as distinct schema-v3 targets, controlled publication
copies both signed OCI archives to immutable private ECR repositories, and
deployment verification can select and verify the AgentCore ARM64 target. This
is not a production certification. `v0.2.4` completed the KMS-backed
private-ECR publication and current-policy verification flow for the AgentCore
digest recorded in the
[production release status](PRODUCTION_RUNBOOK.md#release-status). No hardened
AgentCore stack is deployed, and a real AWS restore exercise remains
unverified.

## Runtime Surface

`agentcore_agent.py` exposes:

| Surface | Behavior |
|---|---|
| `chat` action | Validates and invokes the AxonLLM chat pipeline through Bedrock |
| `list_models` action | Lists Bedrock models allowed for the resolved project |
| `health` action | Liveness only; returns `ready: false` and checks no dependencies |
| `GET /ready` | Bounded runtime, OIDC/JWKS, canonical principal-store, and security-event outbox readiness |

Default lifecycle bounds are 60 seconds for initialization, 5 seconds for
readiness, a 5-second readiness cache, and 10 seconds for shutdown. The CDK
runtime uses a 10-minute idle session timeout and 4-hour maximum lifetime.

AgentCore does not mount the Starlette admin console or HTTP API. It has no
query action. `query.select` is only authorization vocabulary; no SQL parser,
datasource adapter, or backend query contract ships.

Chat accepts `model`, `messages`, `system`, `temperature`, `max_tokens`, `top_p`,
`stop`, `stream`, `tools`, and `tool_choice`. Payload identity fields such as
tenant, project, user, roles, or scopes are rejected.

## Identity And RBAC

`infra/agentcore_stack.py` configures an AgentCore JWT authorizer and forwards
the `Authorization` header. AxonLLM verifies the token again, requires signed
issuer, subject, tenant, and project claims, discards token roles/scopes, and
strongly resolves the canonical DynamoDB principal and tenant-owned project.

The principal must be active and hold the requested project grant.
`tenant_admin`, `tenant_member`, and `tenant_auditor` can list models and invoke
chat only with that grant. A `service` principal also needs `model.list` or
`inference.invoke` in server-held scopes. Cross-tenant and ungranted projects
are concealed as 404; identity and authority-store failures fail closed.
Canonical roles are authoritative: tenant viewers cannot elevate through legacy
admin roles/scopes, canonical services gain no control-plane authority from
`admin:*`, and canonical key issuance rejects legacy admin scopes.

AgentCore exposes no bootstrap or identity-administration action. The
first-adopter deployer performs this out of band: managed Cognito invites the
first user and resolves its generated `sub`; external OIDC requires an existing
user and exact `sub`. Both paths then run the same restartable canonical
bootstrap against the deployed AgentCore table.

For manual recovery, provision the administrator directly:

```bash
LLM_ROUTER_DYNAMODB_ENABLED=true \
AXON_DYNAMODB_TABLE=axonllm-agentcore-state \
AWS_DEFAULT_REGION=us-east-1 \
uv run axon bootstrap-tenant \
  --tenant tenant-a \
  --project project-a \
  --project-name Production \
  --issuer "$OIDC_ISSUER" \
  --subject "$ADMIN_SUBJECT" \
  --user-name "$ADMIN_USER_NAME"
```

The restartable command conditionally creates or verifies the project and
SCIM-backed administrator, grants canonical membership, and strongly verifies
the active `tenant_admin` principal and project grant. After bootstrap, the
HTTP admin project-member
routes atomically synchronize `Project.members`, `ScimUser.project_ids`,
authoritative `Principal.project_ids`, authorization versions, and the tenant
SCIM version. Members are stored as `scim:<id>`; canonical create/PUT bulk
member writes are rejected. Canonical SCIM rows share
`PK=TENANT#{tenant_id}` with `SCIM#USER#{id}`, `SCIM#GROUP#{id}`,
`SCIM#USERNAME#{hash}`, and `SCIM#VERSION` sort keys. AgentCore itself exposes
no membership-management action.

AgentCore accepts OIDC JWTs at this boundary, not AxonLLM API keys. Tenant and
project claims are signed routing hints only. Cognito or the external IdP does
not grant AxonLLM roles: the strongly resolved DynamoDB principal remains the
authority for status, role, scopes, and project membership.

## Infrastructure

The AgentCore stack provides:

- a VPC runtime in private subnets across two Availability Zones;
- restricted runtime egress through a customer-managed HTTPS prefix list;
- DynamoDB gateway plus Bedrock Runtime, SQS, SNS, and CloudWatch Logs
  interface endpoints;
- IAM restricted to supplied concrete Bedrock model/profile ARNs;
- a private regional ECR image identified by `@sha256`;
- canonical identity and enforced authentication;
- a KMS-encrypted DynamoDB table with deletion protection and PITR;
- daily AWS Backup at 05:30 UTC, 30-day cold transition, 365-day deletion, and
  governance-mode Vault Lock enforcing 30-365 day retention;
- a KMS-encrypted FIFO security-event outbox and DLQ retained for 14 days;
- a managed encrypted FIFO SNS event topic and retained encrypted CloudWatch
  event log group;
- encrypted application and usage logs retained for one year;
- runtime/throttle/DynamoDB alarms and an operations dashboard.

The alarm and security-event topics have no subscriptions. Add confirmed
receivers before traffic.

The stack sets `AXON_ENABLED_PROVIDERS=bedrock`. The allowlist is applied when
the runtime constructs its provider factory, so `list_models` and `chat`
advertise and invoke only standard Bedrock mappings. Bedrock Mantle and every
HTTP provider remain excluded even if the image contains their metadata or the
runtime receives credentials for them. The stack injects no provider secrets.

`AXON_ENABLED_PROVIDERS` is the general comma-separated runtime provider
allowlist. An unset value adds no allowlist beyond configured credentials; an
empty value or unknown provider fails startup. Do not broaden the AgentCore
value without reviewing secret delivery, IAM, egress, model disclosure, and
deployment tests. `SessionManager` and AgentCore Memory are not wired; do not
claim durable conversation memory.

The stack injects `AXON_AWS_ACCOUNT_ID`, `AXON_EVENT_OUTBOX_QUEUE_URL`,
`AXON_SECURITY_EVENT_SNS_TOPIC_ARN`, and
`AXON_SECURITY_EVENT_LOG_GROUP_ARN`. The latter two restrict managed AWS
destinations to the stack-created resources; they do not create a tenant
destination. Configure destinations through a trusted Starlette tenant control
plane connected to the same AgentCore state table before relying on AgentCore
event delivery. Resolve
`SecurityEventOutboxQueueUrl`, `SecurityEventDeadLetterQueueUrl`,
`SecurityEventTopicArn`, and `SecurityEventLogGroupArn` from the AgentCore stack
outputs. Use the
[production runbook](PRODUCTION_RUNBOOK.md#security-event-delivery) for delivery
semantics, monitoring, and DLQ recovery.

The generated `.bedrock_agentcore.yaml` is not the production source of truth.
It points at another local checkout and uses public networking without the CDK
stack's JWT/header/private-network controls.

Gateway construction runs in `asyncio.to_thread`. Python cannot forcibly stop
that synchronous worker if it outlives the initialization timeout. Retain an
external process/runtime startup deadline.

## Release Evidence

`.github/workflows/release-security.yml` creates the ARM64 image with the
AgentCore Dockerfile, scans it, emits an image SBOM, captures BuildKit metadata,
and records its digest in KMS-signed SLSA provenance. Its schema-v3 release
manifest records both deployment targets. The evidence bundle contains:

- `axonllm-agentcore-linux-arm64.oci.tar`;
- `agentcore-build-metadata.json`;
- `agentcore-image-security.json`;
- `agentcore-image.cyclonedx.json`;
- `provenance.intoto.json`;
- `provenance-kms-signature.json`;
- `release-manifest.json`;
- `manifest-kms-signature.json`.

`deploy-verification.yml` accepts `target=agentcore`, binds the supplied private
ECR digest to the AgentCore subject, ARM64 platform, metadata, scan, SBOM, source
commit, release tag, CI result, signed manifest, and signed provenance, verifies
the remote digest, and performs a fresh image scan.

The consumer obtains the exact signing key ARN from the manifest and accepts it
only when it belongs to `AXON_AWS_ACCOUNT_ID` and is the target of a retained
`alias/axonllm/release-signing-v*` alias. It does not use the signer's current
`AXON_RELEASE_SIGNING_KEY_ARN` repository variable.

`publish-release.yml` verifies the tagged release lineage and both signed target
records, then copies the original OCI archives into the release-foundation
repositories without rebuilding. It verifies the remote digest and target
evidence before emitting the immutable image references. Deploy only the
AgentCore reference that subsequently passes `deploy-verification.yml`. The
`v0.2.4` AgentCore reference completed this flow; repeat it for every promoted
release and retain the evidence.

## First-Adopter Setup

Deploy the release foundation and configure the protected `release` and
`production` GitHub environments as described in the
[production runbook](PRODUCTION_RUNBOOK.md#release-foundation). After a tagged
release is published, CI is green, and the `agentcore` target has passed
deployment verification, choose one identity path. There is no unauthenticated
AgentCore mode.

### Managed Cognito

The managed option creates `AxonLLMIdentityStack` separately from the runtime.
Its user pool, public client, and hosted domain are retained and deletion
protected. Self-signup and direct password/SRP client flows are disabled; TOTP
MFA is required. The client has no secret and supports authorization code only.

Set the common release and network inputs, then generate a reviewable setup
file:

```bash
export AWS_DEFAULT_REGION=us-east-1
export AXON_VERIFIED_IMAGE_URI='123456789012.dkr.ecr.us-east-1.amazonaws.com/axonllm/agentcore@sha256:<verified-arm64-digest>'
export AXON_BEDROCK_INVOKE_RESOURCE_ARNS='arn:aws:bedrock:us-east-1::foundation-model/<model-id>'
export AXON_APPROVED_HTTPS_PREFIX_LIST_ID='pl-0123456789abcdef0'

uv run axon setup agentcore \
  --identity-mode managed-cognito \
  --tenant tenant-a \
  --project project-a \
  --project-name Production \
  --budget-limit 1000 \
  --admin-user-name admin@example.com \
  --admin-email admin@example.com \
  --admin-display-name "Tenant A Admin" \
  --hosted-ui-domain-prefix axonllm-123456789012 \
  --oauth-callback-url https://app.example.com/oauth/callback \
  --output axonllm-agentcore.json
```

The domain prefix must be globally available. Every callback is HTTPS. The
adopting OAuth client must generate a verifier and send
`code_challenge_method=S256`; AxonLLM does not ship an OAuth callback
application. Invoke AgentCore with the returned **ID token**, which carries
`custom:tenant_id` and `custom:project_id`. Do not substitute the Cognito access
token, which does not contain those user attributes by default.

The deployer sends the initial invitation through Cognito, never handles a
temporary password, and refuses to reassign an existing user to another tenant
or project. A rerun verifies the same Cognito `sub` and then idempotently
verifies canonical authority.

### Existing OIDC

The existing-IdP option deploys no identity resources. Provision the user and
public client in the IdP first. Its signed token must contain `iss`, `sub`,
`aud`, `exp`, and non-empty tenant/project string claims:

```bash
uv run axon setup agentcore \
  --identity-mode external-oidc \
  --tenant tenant-a \
  --project project-a \
  --project-name Production \
  --admin-user-name admin@example.com \
  --admin-email admin@example.com \
  --admin-subject 00u-admin-subject \
  --oidc-issuer https://idp.example.com/oauth2/default \
  --oidc-discovery-url https://idp.example.com/oauth2/default/.well-known/openid-configuration \
  --oidc-client-id axonllm-agentcore \
  --oidc-audience api://axonllm \
  --oidc-tenant-claim https://axonllm.example/tenant \
  --oidc-project-claim https://axonllm.example/project \
  --output axonllm-agentcore.json
```

This command also reads the three common `AXON_*` release/network variables
shown above. The setup rejects client secrets; the runtime only needs public
verification metadata. The discovery URL must be the exact issuer followed by
`/.well-known/openid-configuration`. The external administrator must already
exist, and `--admin-subject` must exactly match its immutable token `sub`.

### Validate And Deploy

Review the generated JSON, CDK templates, and diff before deployment:

```bash
./deploy-agentcore.sh --config axonllm-agentcore.json --validate-only

# First deployment in an account/region:
./deploy-agentcore.sh \
  --config axonllm-agentcore.json \
  --bootstrap-cdk
```

The wrapper requires Python 3.11+, uv, AWS credentials, and Node.js 22 or newer.
It installs hash-pinned CDK dependencies when needed. Without `--yes`, CDK
retains its security-change approval prompt; noninteractive runs must explicitly
pass `--yes` after review.

The operation is restartable:

1. Deploy or update retained identity when `managed-cognito` is selected.
2. Invite or strongly verify the same first Cognito administrator.
3. Deploy the authenticated AgentCore runtime from the immutable image digest.
4. Create or verify the canonical project, `tenant_admin`, and project grant.

Setup JSON contains no password, token, or client secret and is written mode
`0600`. CDK outputs are kept under `.axonllm/agentcore` by default. Protect
those operational files even though they contain identifiers rather than
credentials.

For a local anonymous evaluation, use only the explicitly acknowledged
development command:

```bash
uv run axon setup local-demo --start --acknowledge-non-production
```

It forces the development profile, fictional seed data, in-memory persistence,
and `LOG_ONLY`. It is not a deployment input and cannot select AgentCore.

### Manual CDK

The deployer is the preferred path because it binds identity outputs, invitation,
runtime deployment, and canonical bootstrap. For controlled manual deployment,
the AgentCore stack still consumes standard OIDC inputs:

```bash
cd infra
uv venv
uv pip install -r requirements.txt
npx cdk bootstrap -c deployment_target=agentcore -c region="$AWS_REGION"

npx cdk synth AxonLLMAgentCoreStack \
  -c deployment_target=agentcore -c region="$AWS_REGION"

npx cdk deploy AxonLLMAgentCoreStack \
  -c deployment_target=agentcore -c region="$AWS_REGION" \
  --parameters AxonLLMAgentCoreStack:VerifiedImageUri="$VERIFIED_ARM64_IMAGE_URI" \
  --parameters AxonLLMAgentCoreStack:OidcIssuer="$OIDC_ISSUER" \
  --parameters AxonLLMAgentCoreStack:OidcDiscoveryUrl="$OIDC_DISCOVERY_URL" \
  --parameters AxonLLMAgentCoreStack:OidcClientId="$OIDC_CLIENT_ID" \
  --parameters AxonLLMAgentCoreStack:OidcAudience="$OIDC_AUDIENCE" \
  --parameters AxonLLMAgentCoreStack:OidcTenantClaim="$OIDC_TENANT_CLAIM" \
  --parameters AxonLLMAgentCoreStack:OidcProjectClaim="$OIDC_PROJECT_CLAIM" \
  --parameters AxonLLMAgentCoreStack:ApprovedHttpsPrefixListId="$APPROVED_HTTPS_PREFIX_LIST_ID" \
  --parameters AxonLLMAgentCoreStack:BedrockInvokeResourceArns="$BEDROCK_INVOKE_RESOURCE_ARNS"
```

`BEDROCK_INVOKE_RESOURCE_ARNS` is a comma-separated list of concrete ARNs; the
parameter rejects wildcards. The image must be a private ECR digest in the
deployment region. Include the OIDC origin and every deliberately enabled
external HTTPS destination in the approved prefix list. A manual managed
Cognito deployment must first synthesize/deploy `AxonLLMIdentityStack` with
`deployment_target=identity`, consume its outputs, invite the user, and run
`axon bootstrap-tenant`; skipping any of those steps is not equivalent to the
first-adopter workflow.

## Verification

Before traffic:

1. Verify the runtime endpoint uses the JWT authorizer, VPC mode, and
   `Authorization` header allowlist.
2. Verify the deployed ECR digest and AgentCore-specific release evidence.
3. Confirm DynamoDB deletion protection/PITR, a recent backup, KMS rotation,
   one-year logs, outbox/DLQ encryption and TLS policies, and topic
   subscriptions.
4. Require `GET /ready` to return 200; do not use `health` as readiness.
5. Run `list_models` and `chat` with an active, project-granted principal.
6. Verify failures for missing/invalid tokens, payload identity fields, inactive
   membership, missing grants, cross-tenant projects, and missing service scopes.
7. Exercise streaming and any response control that requires buffering.
8. Enforce the external startup/termination deadline.
9. Deliver one security event through each enabled destination, verify the
   outbox drains, and test the DLQ alarm and controlled redrive procedure.

`GET /ready` does not prove model availability, provider credentials, backup
freshness, alarm delivery, or an end-to-end completion. Keep those as canaries.

## Backup And Recovery

Validate the AgentCore table and vault explicitly:

```bash
python scripts/operations/validate_state_recovery.py \
  --stack-name AxonLLMAgentCoreStack

python scripts/operations/validate_state_recovery.py \
  --stack-name AxonLLMAgentCoreStack \
  --exercise-restore
```

The scheduled operations workflow audits both Fargate and AgentCore daily and
exercises PITR for both targets monthly. In the protected GitHub `production`
environment, set `AXON_AGENTCORE_DATA_KMS_KEY_ARN` to the ARN of the AgentCore
data key (the key behind `alias/axonllm/agentcore-data`); the audit and recovery
job matrices both require it. The stack configures governance-mode Vault Lock
with 30-365 day retention. A restore exercise validates a temporary table and
the workflow retains its JSON result as a 90-day evidence artifact.

There is no AgentCore restored-table runtime parameter, quiescence guard, or
application cutover workflow. `retain_fargate_restore` applies only to Fargate;
do not use the Fargate recovery helper or claim AgentCore cutover. The first real
AgentCore AWS restore exercise remains externally unverified.

Use the incident and key-rotation procedure in the
[Production Runbook](PRODUCTION_RUNBOOK.md#rotation-and-incident-response).
