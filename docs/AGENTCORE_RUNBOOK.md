# AxonLLM AgentCore Runbook

This runbook describes the checked-in Amazon Bedrock AgentCore adapter, image,
and CDK stack. It does not certify a checkout or AWS deployment.

## Current Status

The AgentCore application, private-network CDK stack, retained Cognito identity
stack, bounded Athena query path, shared-state Fargate control plane, and
schema-v2 first-adopter deployment workflow are implemented.
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
unverified. The query and shared control-plane changes postdate `v0.2.4`; no
tagged evidence, deployed Athena query canary, or shared-control-plane canary
has been retained for them.

## Runtime Surface

`agentcore_agent.py` exposes:

| Surface | Behavior |
|---|---|
| `chat` action | Validates and invokes the AxonLLM chat pipeline through any enabled, credentialled provider |
| `list_models` action | Lists production-eligible models allowed for the resolved project |
| `query` action | Runs a bounded Athena `SELECT` through the canonical `QueryService` |
| `health` action | Liveness only; returns `ready: false` and checks no dependencies |
| `GET /ready` | Bounded runtime, OIDC/JWKS, canonical principal-store, security-event outbox, and configured query-reconciler readiness |

Default lifecycle bounds are 60 seconds for initialization, 5 seconds for
readiness, a 5-second readiness cache, and 10 seconds for shutdown. A watchdog
thread owns the initialization deadline independently of the asyncio loop. If
bootstrap ownership is not safely resolved by that deadline, the process exits
with status `124`; AgentCore must replace the failed container. The CDK runtime
uses a 10-minute idle session timeout and 4-hour maximum lifetime. Shutdown is
single-flight, retains cleanup ownership when its caller is cancelled, and has
the same fail-stop process boundary when cleanup outlives 10 seconds.

AgentCore does not mount the Starlette admin console or HTTP API. Its `query`
action uses the same datasource repository, SQL policy, Athena executor,
limits, and durable audit contract as Starlette `POST /v1/query`. The action is
available only when exact query-role bindings are deployed.

Chat accepts `model`, `messages`, `system`, `temperature`, `max_tokens`, `top_p`,
`stop`, `stream`, `tools`, and `tool_choice`. Payload identity fields such as
tenant, project, user, roles, or scopes are rejected.

Query accepts `datasource_id`, `sql`, optional `max_rows`, and optional
`request_id`. Tenant and project always come from verified identity, never the
payload. `query.mutate` is unsupported and always denied.

## Identity And RBAC

`infra/agentcore_stack.py` configures an AgentCore JWT authorizer and forwards
the `Authorization` header. AxonLLM verifies the token again, requires signed
issuer, subject, tenant, and project claims, discards token roles/scopes, and
strongly resolves the canonical DynamoDB principal and tenant-owned project.

The principal must be active and hold the requested project grant.
`tenant_admin`, `tenant_member`, and `tenant_auditor` can list models and invoke
chat or query only with that grant. A `service` principal also needs
`model.list`, `inference.invoke`, or `query.select` in server-held scopes.
Cross-tenant and ungranted projects are concealed as 404; identity and
authority-store failures fail closed.
Canonical roles are authoritative: tenant viewers cannot elevate through legacy
admin roles/scopes, canonical services gain no control-plane authority from
`admin:*`, and canonical key issuance rejects legacy admin scopes.

AgentCore exposes no bootstrap or identity-administration action. The
first-adopter deployer performs this out of band: managed Cognito invites the
first user and resolves its generated `sub`; external OIDC requires an existing
user and exact `sub`. Both paths run the same restartable canonical bootstrap
against the deployed AgentCore table. Managed Cognito then deploys the
Cognito-authenticated shared-state web control plane; external OIDC currently
does not.

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
- Bedrock Mantle access through its regional public HTTPS endpoint and
  least-privilege `CreateInference` and `ListModels` IAM actions;
- a retained KMS-encrypted provider secret, resource-scoped private Secrets
  Manager endpoint, and read-only runtime access;
- optional private STS and Athena endpoints when exact query-role bindings are
  configured;
- IAM restricted to supplied concrete Bedrock model/profile ARNs;
- query IAM restricted to the exact datasource role ARNs and
  `sts:AssumeRole`, `sts:TagSession`, and `sts:SetSourceIdentity`;
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

The stack allowlists all thirteen providers. The allowlist is applied when the
runtime constructs its provider factory. Bedrock and Mantle are always
credentialled through the runtime role; an HTTP provider is advertised only
when its credential loads from the retained secret referenced by
`AXON_PROVIDER_SECRET_ARN`. Mantle uses AWS SigV4 and needs no stored provider
key.

The provider secret accepts these fields:

`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `AZURE_OPENAI_API_KEY`,
`AZURE_OPENAI_ENDPOINT`, `GCP_CREDENTIALS_JSON`, `GCP_PROJECT_ID`,
`GCP_LOCATION`, `VERTEX_AI_ENDPOINT`, `GOOGLE_AI_API_KEY`, `COHERE_API_KEY`,
`XAI_API_KEY`, `GROQ_API_KEY`, `TOGETHER_API_KEY`, `FIREWORKS_API_KEY`, and
`AI21_API_KEY`.

Environment values override the secret, and the secret overrides checked-in
metadata. Unknown secret fields are ignored; malformed known fields fail
startup. Direct AI21 Jamba 1.6 uses `AI21_API_KEY`; Jamba 1.5 uses standard
Bedrock IAM and no AI21 key.

Google AI sends `GOOGLE_AI_API_KEY` only in the `x-goog-api-key` header; the
key never enters completion, streaming, or model-list URLs. Vertex accepts only
refreshable credentials: supply Google ADC or set `GCP_CREDENTIALS_JSON` to a
JSON-encoded `external_account` AWS workload-identity configuration or
`service_account` document. In the AgentCore provider secret, that field is a
string containing the credential JSON, while `GCP_PROJECT_ID` and
`GCP_LOCATION` select the Vertex resource. AWS workload identity avoids a
long-lived Google private key and is preferred.

Vertex performs one bounded credential exchange during synchronous bootstrap,
then refreshes five minutes ahead of expiry on a daemon thread. Request and
model-availability paths read only the cached token. An unexpectedly expired
token fails closed immediately while background refresh runs. Secrets Manager
startup calls use a 3-second connect timeout, 5-second read timeout, and at
most three attempts.

`AXON_ENABLED_PROVIDERS` is the general comma-separated runtime provider
allowlist. An unset value adds no allowlist beyond configured credentials; an
empty value or unknown provider fails startup. `SessionManager` and AgentCore
Memory are not wired; do not claim durable conversation memory.

When query bindings are present, each exact tuple contains `tenant_id`,
`project_id`, and a concrete role ARN. Their compact JSON value must remain
within AgentCore's 2,048-character environment-value limit. The datasource
role trust policy must
name the exact AgentCore runtime execution role and permit
`sts:AssumeRole`, `sts:TagSession`, and `sts:SetSourceIdentity`. The runtime
role and private STS endpoint are limited to the same role ARN set. The Athena
endpoint admits only those approved roles for `GetWorkGroup`,
`StartQueryExecution`, `GetQueryExecution`, `GetQueryResults`, and
`StopQueryExecution`.

The runtime role is deterministically named
`axonllm-agentcore-runtime-<region>`. Preconfigure datasource trust to its exact
account/region ARN, then verify the deployed value from the
`RuntimeExecutionRoleArn` stack output; the first-adopter deployer prints that
output.

Before every execution, AxonLLM requires an enabled workgroup that enforces its
configuration, publishes CloudWatch metrics, uses an enforced KMS-encrypted S3
result location, and has a positive `BytesScannedCutoffPerQuery` no greater
than the deployed AxonLLM scan limit. Defaults are 30 seconds, 1,000 rows, a
1 MiB compact serialized columns-and-rows result set including JSON
structure/nulls, and 1 GiB scanned. Query request/result/rejection audit stores
SHA-256 values and execution statistics, not SQL literals. DynamoDB also
enforces principal/project RPM, expiring concurrency slots, aggregate scan
reservations, duplicate request ids, and durable accepted/running/terminal
lifecycle state. A fenced periodic worker claims expired records across
replicas, closes accepted records, cancels or observes known Athena executions,
atomically reconciles slots/reservations, and replays pending terminal audit
writes. It defers a running record rather than guessing when the datasource or
exact deployment binding cannot be re-established.

The stack injects `AXON_AWS_ACCOUNT_ID`, `AXON_EVENT_OUTBOX_QUEUE_URL`,
`AXON_SECURITY_EVENT_SNS_TOPIC_ARN`, and
`AXON_SECURITY_EVENT_LOG_GROUP_ARN`. The latter two restrict managed AWS
destinations to the stack-created resources; they do not create a tenant
destination. Managed Cognito supplies a trusted shared-state Starlette control
plane for this administration. External OIDC must provide another trusted
control plane connected to the same AgentCore table. Resolve
`SecurityEventOutboxQueueUrl`, `SecurityEventDeadLetterQueueUrl`,
`SecurityEventTopicArn`, and `SecurityEventLogGroupArn` from the AgentCore stack
outputs. Use the
[production runbook](PRODUCTION_RUNBOOK.md#security-event-delivery) for delivery
semantics, monitoring, and DLQ recovery.

The generated `.bedrock_agentcore.yaml` is not the production source of truth.
It points at another local checkout and uses public networking without the CDK
stack's JWT/header/private-network controls.

Gateway construction runs in `asyncio.to_thread`. Python cannot forcibly stop
that synchronous worker, so the runtime watchdog terminates the complete
process if bootstrap outlives its initialization deadline. Do not catch or
override exit status `124`; investigate the blocked dependency and let
AgentCore replace the failed container. The watchdog also remains armed when a
startup dependency probe times out because cancellation cannot stop its native
SDK worker. Python watchdog threads still depend on interpreter scheduling, so
the AgentCore platform startup/termination deadline remains required for a
native call that holds the GIL.

### Shared-State Control Plane

`AxonLLMControlPlaneStack` runs a separate verified AMD64 server image on
private Fargate tasks behind an HTTPS ALB, Cognito authentication, and a stable
Route 53 alias. It uses AgentCore's verified `StateTableName` output and imports
the data key, outbox, SNS topic, and CloudWatch event log. It creates no second
DynamoDB table.

The stack sets `AXON_CONTROL_PLANE_ONLY=true`. It retains tenant admin,
datasource, managed-SAML handoff, SCIM, health, and readiness routes while
suppressing chat, model-list, OpenAI-compatible, and `/v1/query` routes. Although
it receives the same role-binding metadata so datasource writes can be
validated, its task role and endpoints grant no Athena or STS execution
authority.

A higher-priority ALB rule forwards only `/scim/*` without Cognito because SCIM
authenticates its own bearer token. Every `/saml/*` route remains behind the
default ALB Cognito action. The stack optionally injects the complete
`AXON_SCIM_TENANTS` value, sets
`AXON_SAML_FEDERATION_MODE=managed-cognito`, and passes a validated
`AXON_SAML_LOGIN_PATH`. It never receives SAML metadata, certificates, or
assertions. Cognito is the SAML SP, the ALB owns the browser session, and
AxonLLM accepts only the resulting ALB-signed OIDC identity.

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

The generated first-adopter file uses schema version 2. In managed-Cognito
mode, its `control_plane` object is required; schema-v1 files must be regenerated
or migrated before deployment.

### Managed Cognito

The managed option creates `AxonLLMIdentityStack` separately from the runtime.
Its user pool, public AgentCore client, confidential ALB client, and hosted
domain are retained and deletion protected. Self-signup and direct password/SRP
client flows are disabled; TOTP MFA is required. The AgentCore client has no
secret and supports authorization code only.

Set the common release and network inputs, then generate a reviewable setup
file:

```bash
export AWS_DEFAULT_REGION=us-east-1
export AXON_VERIFIED_IMAGE_URI='123456789012.dkr.ecr.us-east-1.amazonaws.com/axonllm/agentcore@sha256:<verified-arm64-digest>'
export AXON_BEDROCK_INVOKE_RESOURCE_ARNS='arn:aws:bedrock:us-east-1::foundation-model/<model-id>'
export AXON_APPROVED_HTTPS_PREFIX_LIST_ID='pl-0123456789abcdef0'
export AXON_CONTROL_PLANE_DOMAIN_NAME='admin.example.com'
export AXON_CONTROL_PLANE_VERIFIED_IMAGE_URI='123456789012.dkr.ecr.us-east-1.amazonaws.com/axonllm/fargate@sha256:<verified-amd64-digest>'
export AXON_CONTROL_PLANE_CERTIFICATE_ARN='arn:aws:acm:us-east-1:123456789012:certificate/<id>'
export AXON_CONTROL_PLANE_PUBLIC_HOSTED_ZONE_ID='Z0123456789EXAMPLE'
export AXON_CONTROL_PLANE_APPROVED_INGRESS_PREFIX_LIST_ID='pl-0123456789abcdef1'
export AXON_CONTROL_PLANE_APPROVED_HTTPS_PREFIX_LIST_ID='pl-0123456789abcdef2'
# Optional SCIM credential map and managed-SAML landing path:
export AXON_CONTROL_PLANE_SCIM_TENANTS_SECRET_ARN='arn:aws:secretsmanager:us-east-1:123456789012:secret:axonllm/scim-AbCd12'
export AXON_CONTROL_PLANE_SAML_LOGIN_PATH='/admin/dashboard'

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
  --athena-query-role-arn arn:aws:iam::123456789012:role/AxonAthenaReader \
  --output axonllm-agentcore.json
```

The hosted-UI prefix must be globally available. The control-plane domain must
be a stable lowercase hostname in the supplied public Route 53 zone; its ACM
certificate must be regional. The control-plane image is a verified AMD64
digest, distinct from the AgentCore ARM64 digest. Ingress and HTTPS egress use
the supplied managed prefix lists. Every callback is HTTPS. The adopting OAuth
client must generate a verifier and send
`code_challenge_method=S256`; AxonLLM does not ship an OAuth callback
application. Invoke AgentCore with the returned **ID token**, which carries
`custom:tenant_id` and `custom:project_id`. Do not substitute the Cognito access
token, which does not contain those user attributes by default.

Omit the optional SCIM secret ARN when SCIM is disabled. Its complete
`SecretString` is the tenant map accepted by `AXON_SCIM_TENANTS`; the
control-plane execution role and private Secrets Manager endpoint are scoped to
that exact ARN. There is no AxonLLM SAML secret.

Before enabling enterprise SAML, configure the retained Cognito user pool as
the service provider:

1. Add the enterprise SAML IdP to Cognito from reviewed metadata and require a
   signed response or assertion.
2. Configure the IdP with Cognito's SP entity ID and SAML response endpoint, not
   AxonLLM `/saml/acs` or `/saml/metadata`.
3. Enable the SAML IdP on the confidential ALB client and on the public PKCE
   client if federated users also invoke AgentCore.
4. Verify Cognito attribute mappings, certificate rotation, logout, and
   IdP-initiated and SP-initiated policy according to the enterprise IdP.
5. Provision canonical principals using the exact Cognito issuer and Cognito
   `sub`. If SCIM performs provisioning, its tenant issuer must be that Cognito
   issuer and `externalId` must equal the Cognito `sub`.

This tenant-specific Cognito configuration is an operator prerequisite; the
first-adopter deployer creates the retained pool and clients but does not ingest
IdP metadata. Cognito validates SAML signature, issuer, audience, destination,
recipient, timestamps, request correlation, replay, and RelayState. SAML
groups, roles, tenant values, and project values do not grant AxonLLM authority;
the canonical principal and project records do.

`GET /saml/login` is only a safe local handoff. An unauthenticated request is
first intercepted by the ALB and sent through Cognito; after authentication the
route redirects to the configured protected landing path. `/saml/acs` and
`/saml/metadata` are permanent `410` tombstones for the retired direct-SP
surface.

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
for the AgentCore runtime shown above. It does not accept or deploy a
`control_plane` object. The setup rejects client secrets; the runtime only
needs public verification metadata. The discovery URL must be the exact issuer
followed by `/.well-known/openid-configuration`. The external administrator
must already exist, and `--admin-subject` must exactly match its immutable token
`sub`.
This path deploys AgentCore and canonical bootstrap only. It does not deploy
the Cognito-authenticated shared web control plane.

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
3. Deploy the authenticated AgentCore runtime from the immutable ARM64 digest.
4. Create or verify the canonical project, `tenant_admin`, and project grant.
5. For managed Cognito, deploy the authenticated shared-state control plane
   from the immutable AMD64 digest.

External OIDC performs steps 3 and 4 only. It prints an explicit notice that no
web control plane was deployed.

The schema-v2 setup JSON contains no password, token, or client secret and is
written mode `0600`. CDK outputs are kept under `.axonllm/agentcore` by default.
Protect those operational files even though they contain identifiers rather
than credentials. Deployment prints both the runtime ARN and
`RuntimeExecutionRoleArn`; compare the latter with every datasource trust
policy before enabling query.

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
deployment region. Include the OIDC origin,
`bedrock-mantle.<region>.api.aws`, and every deliberately enabled external
HTTPS destination in the approved prefix list. Because an EC2 managed prefix
list stores CIDRs rather than hostnames, verify that every current A record for
the Mantle endpoint is covered before deployment and after AWS publishes an
address change.

After the first deployment, resolve the generated secret:

```bash
PROVIDER_SECRET_ARN="$(
  aws cloudformation describe-stacks \
    --stack-name AxonLLMAgentCoreStack \
    --query "Stacks[0].Outputs[?OutputKey=='ProviderSecretArn'].OutputValue | [0]" \
    --output text
)"
```

Populate only the required fields through an approved secret-management
workflow, then deploy a new AgentCore runtime version so startup reloads the
secret. Never pass secret values as CloudFormation parameters or commit a
populated provider YAML file. The secret is retained across stack deletion; a
replacement stack emits a different ARN.

To enable query manually, pass the same contexts to AgentCore and the control
plane:

```bash
ATHENA_BINDINGS='[{"tenant_id":"tenant-a","project_id":"project-a","role_arn":"arn:aws:iam::123456789012:role/AxonAthenaReader"}]'

npx cdk synth AxonLLMAgentCoreStack \
  -c deployment_target=agentcore -c region="$AWS_REGION" \
  -c athena_query_bindings="$ATHENA_BINDINGS" \
  -c athena_query_timeout_seconds=30 \
  -c athena_query_max_rows=1000 \
  -c athena_query_max_result_bytes=1048576 \
  -c athena_query_max_bytes_scanned=1073741824 \
  -c athena_query_project_rpm=30 \
  -c athena_query_principal_rpm=10 \
  -c athena_query_project_concurrency=5 \
  -c athena_query_principal_concurrency=2 \
  -c athena_query_project_scan_bytes_per_minute=5368709120 \
  -c athena_query_principal_scan_bytes_per_minute=2147483648 \
  -c athena_query_max_datasources_per_tenant=500
```

Repeat those query contexts on each AgentCore/control-plane deploy command. A
manual managed-Cognito deployment must first deploy
`AxonLLMIdentityStack`, consume its outputs, invite the user, deploy AgentCore,
and run `axon bootstrap-tenant`. Then deploy `AxonLLMControlPlaneStack` with
`deployment_target=control-plane`, the same Athena contexts, the AgentCore and
identity stack names, `ControlPlaneVerifiedImageUri`, `CertificateArn`,
`PublicHostedZoneId`, `ApprovedIngressPrefixListId`, and
`ApprovedHttpsPrefixListId`, plus the validated `SamlLoginPath` when overriding
its `/admin/dashboard` default. `PrimaryStateTableName` has no default: resolve
the deployed AgentCore stack's verified `StateTableName` output and pass that
exact value. The deployment wrapper performs this lookup, parameter binding,
and post-deployment output comparison automatically. Skipping any of those
steps is not equivalent to the schema-v2 first-adopter workflow.

## Verification

Before traffic:

1. Verify the runtime endpoint uses the JWT authorizer, VPC mode, and
   `Authorization` header allowlist.
2. Verify the deployed ECR digest and AgentCore-specific release evidence.
3. Confirm DynamoDB deletion protection/PITR, a recent backup, KMS rotation,
   one-year logs, outbox/DLQ encryption and TLS policies, and topic
   subscriptions.
4. Require `GET /ready` to return 200; do not use `health` as readiness.
5. Run `list_models`, `chat`, and, when configured, `query` with an active,
   project-granted principal. Require one completion and one streaming
   completion from every credentialled provider.
6. Verify query rejection for mutation/multiple statements, out-of-datasource
   references, unbound roles, unsafe workgroups, and missing service scope.
   Exercise one interrupted lifecycle and verify terminal reconciliation,
   accounting release, and exactly one durable result audit. Verify an
   unavailable datasource/binding is deferred without accounting release.
7. Verify the datasource role trust names the exact runtime role and permits
   `sts:AssumeRole`, `sts:TagSession`, and `sts:SetSourceIdentity`.
8. For managed Cognito, verify the stable control-plane hostname, ALB login,
   datasource admin RBAC, shared AgentCore table, suppressed data routes, and
   absence of Athena/STS task authority. For SAML, also verify Cognito SP
   metadata and certificate rollover, signed assertion rejection, issuer,
   audience, destination/recipient and time rejection, request/replay handling,
   safe RelayState, exact Cognito issuer/`sub` canonical resolution, and that
   `/saml/*` never matches the unauthenticated listener rule.
9. Verify failures for missing/invalid tokens, payload identity fields, inactive
   membership, missing grants, cross-tenant projects, and missing service scopes.
10. Exercise streaming and any response control that requires buffering.
11. Verify an initialization timeout exits the container with status `124` and
    AgentCore replaces it; retain the platform startup deadline as defense in
    depth.
12. Deliver one security event through each enabled destination, verify the
   outbox drains, and test the DLQ alarm and controlled redrive procedure.

`GET /ready` does not prove model availability, provider credentials, backup
freshness, alarm delivery, or an end-to-end completion. It does not enumerate
datasource roles or validate Athena workgroups; workgroup validation runs
immediately before each execution. Keep those as canaries.

## Backup And Recovery

Validate the AgentCore table and vault explicitly:

```bash
python scripts/operations/validate_state_recovery.py \
  --stack-name AxonLLMAgentCoreStack

python scripts/operations/validate_state_recovery.py \
  --stack-name AxonLLMAgentCoreStack \
  --exercise-restore \
  --keep-restored-table
```

The scheduled operations workflow audits both Fargate and AgentCore daily and
exercises PITR for both targets monthly. In the protected GitHub `production`
environment, set `AXON_AGENTCORE_DATA_KMS_KEY_ARN` to the ARN of the AgentCore
data key (the key behind `alias/axonllm/agentcore-data`); the audit and recovery
job matrices both require it. The stack configures governance-mode Vault Lock
with 30-365 day retention. A restore exercise validates a temporary table and
the workflow retains its JSON result as a 90-day evidence artifact.

For a manually dispatched restore, set `exercise_restore=true` and
`retain_agentcore_restore=true`. The recovery role creates, validates,
protects, and returns a table named
`<primary>-restore-validation-<timestamp>-<random>`. It cannot update the
AgentCore stack. The deployment operator performs cutover with
`scripts/operations/agentcore_recovery.py`; do not use the Fargate helper.

The selector has four fail-closed phases:

- `normal`: only `production` exists and the runtime can use only the selected
  table.
- `quiesced`: endpoints are removed, JWT client/audience values are blocked,
  and explicit IAM deny prevents every runtime DynamoDB operation.
- `selected`: the approved table, exact runtime IAM, VPC endpoint policy,
  metrics, and backup selection have changed while access remains blocked.
- `validation`: only `recovery` exists and can use the selected table.
  Promotion replaces it with `production` without another table change.

The guard accepts a table change only from `quiesced` to `selected`, bound to a
new 3-128 character change/incident ID. It permits only the primary table or a
restore in that table's namespace. It also requires every AgentCore endpoint
absent and, when deployed, the web control plane at zero desired, pending, and
running tasks with all scaling paths suspended. Quiescence lasts 14,700
seconds: the four-hour maximum session lifetime plus a five-minute IAM margin.

Deploy the updated stack in `normal` mode through the reviewed CDK path first.
Confirm `describe-stacks` returns a nonempty `RoleARN`; the helper refuses to
update a stack without a CloudFormation execution role. Stop new traffic, then
run:

```bash
python scripts/operations/agentcore_recovery.py \
  --stack-name AxonLLMAgentCoreStack \
  quiesce \
  --state-file agentcore-cutover.json \
  --approval-id CHG-2026-001
```

The mode-`0600` state file is created before mutation. If the managed control
plane exists, the helper records its task/scaling state, suspends scaling, and
stops it. The helper uses the already-reviewed template and preserves every
unrelated stack parameter. Keep that control plane stopped throughout recovery.
Selection, validation start, promotion, and control-plane resume record
restartable checkpoints before cross-stack updates. `quiesce` is also
restartable after a partial ECS stop or selector update: rerun it with the same
state file and approval ID. For later phases, rerun the same command with the
same state file and expected table after an interruption. Before validation,
`abort` completes any partial control-plane stop and reverses a partially
completed selection in the required safe order.

After the reported quiescence interval, select the retained table while access
remains denied, then open only the recovery endpoint:

```bash
python scripts/operations/agentcore_recovery.py \
  --stack-name AxonLLMAgentCoreStack \
  select \
  --state-file agentcore-cutover.json \
  --expected-table "$RESTORED_TABLE_NAME"

python scripts/operations/agentcore_recovery.py \
  --stack-name AxonLLMAgentCoreStack \
  start \
  --state-file agentcore-cutover.json \
  --expected-table "$RESTORED_TABLE_NAME"

python scripts/operations/agentcore_recovery.py \
  --stack-name AxonLLMAgentCoreStack status
```

Selection rechecks ACTIVE status, encryption by the exact AgentCore data KMS
key, deletion protection, PITR, `expires_at` TTL, key schema, namespace, stack
identity, approval ID, and control-plane quiescence. Invoke the `recovery`
qualifier with approved JWT canaries. Verify readiness,
administrator/viewer behavior, cross-tenant denials, chat/streaming, event
delivery, and selected-table audit integrity. Retain that evidence, then
promote and resume the control plane on the same selected table:

```bash
python scripts/operations/agentcore_recovery.py \
  --stack-name AxonLLMAgentCoreStack \
  promote \
  --state-file agentcore-cutover.json \
  --expected-table "$RESTORED_TABLE_NAME"

python scripts/operations/agentcore_recovery.py \
  --stack-name AxonLLMAgentCoreStack \
  resume-control-plane \
  --state-file agentcore-cutover.json
```

Resume requires recorded `promoted` or `aborted` evidence, both stacks in
`normal`/blocked-compatible phases on the same selected table, matching stack
ownership, and matching recovery approval where applicable. It restores the
recorded ECS desired count, scaling range, and suspension state only after the
control-plane stack follows AgentCore.

Rollback uses a new state file and approval ID. Repeat `quiesce`, `select`,
`start`, and `promote`, passing the primary table as `--expected-table`, rerun
all canaries, and resume with the rollback state file. Only then remove the
unused restore using the recovery role:

```bash
python scripts/operations/agentcore_recovery.py \
  --stack-name AxonLLMAgentCoreStack \
  resume-control-plane \
  --state-file agentcore-rollback.json

python scripts/operations/agentcore_recovery.py \
  --stack-name AxonLLMAgentCoreStack \
  cleanup --table-name "$RESTORED_TABLE_NAME"
```

Before validation, `abort --state-file agentcore-cutover.json` safely returns
both selectors to the pre-cutover table while access remains blocked. Then run
`resume-control-plane` with that same state file. After validation starts, use
the full approved rollback flow.

The deployment operator needs read/update access to the exact AgentCore stack,
target-table metadata reads, AgentCore endpoint reads, and, when present,
describe/update access to the exact control-plane ECS service and scalable
target. CloudFormation mutates infrastructure through the stack execution role.
Keep this operator separate from the PITR recovery role, which is limited to
restore/protection/cleanup and data-key use. Neither role needs provider-secret
value access.

The managed control-plane stack now has its own phase-gated selector. It follows
the reviewed AgentCore-selected primary or restored table through environment,
task IAM, transaction policy, DynamoDB endpoint policy, explicit blocked-phase
deny, desired count, and scaling suspension. Keep both planes stopped during
selection and resume only through the helper. The first real AWS execution and
measured recovery time remain external launch evidence.

Use the incident and key-rotation procedure in the
[Production Runbook](PRODUCTION_RUNBOOK.md#rotation-and-incident-response).
