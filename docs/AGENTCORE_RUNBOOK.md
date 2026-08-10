# AxonLLM AgentCore Runbook

This runbook describes the checked-in Amazon Bedrock AgentCore adapter, image,
and CDK stack. It does not certify a checkout or AWS deployment.

## Current Status

The AgentCore application and private-network CDK stack are implemented.
`DynamoPersistence` provides canonical per-tenant SCIM version and strongly
consistent snapshot reads used during startup and runtime convergence.

Focused hardening regressions are green locally. The release workflow records
Fargate and AgentCore as distinct schema-v3 targets, controlled publication
copies both signed OCI archives to immutable private ECR repositories, and
deployment verification can select and verify the AgentCore ARM64 target. This
is not a production certification. Required CI must be green for the exact
release commit. A real tagged private-ECR/KMS-signature flow for the AgentCore
digest and a real AWS restore exercise remain externally unverified.

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

AgentCore exposes no bootstrap action. Provision the first administrator out of
band with the repository CLI against the AgentCore table before traffic:

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

AgentCore accepts OIDC JWTs at this boundary, not AxonLLM API keys.

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
first real tagged private-ECR/KMS-signature execution remains externally
unverified.

## CDK Setup

Deploy the release foundation and configure the protected `release` and
`production` GitHub environments as described in the
[production runbook](PRODUCTION_RUNBOOK.md#release-foundation). After a tagged
release is published, CI is green, and the `agentcore` target has passed
deployment verification:

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
  --parameters AxonLLMAgentCoreStack:ApprovedHttpsPrefixListId="$APPROVED_HTTPS_PREFIX_LIST_ID" \
  --parameters AxonLLMAgentCoreStack:BedrockInvokeResourceArns="$BEDROCK_INVOKE_RESOURCE_ARNS"
```

`BEDROCK_INVOKE_RESOURCE_ARNS` is a comma-separated list of concrete ARNs; the
parameter rejects wildcards. The image must be a private ECR digest in the
deployment region. Include the OIDC origin and every deliberately enabled
external HTTPS destination in the approved prefix list.

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
