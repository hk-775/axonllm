# AxonLLM Production Runbook

This runbook describes the production controls implemented in the current
repository. It does not certify a checkout, image, AWS account, or deployment.

## Release Status

The canonical SCIM convergence contract is implemented: tenant user/group
transactions advance `SCIM#VERSION`, and `DynamoPersistence` provides strongly
consistent tenant version and snapshot reads.

Focused hardening regressions are green locally. Release evidence uses
schema-v2 with distinct Fargate and AgentCore targets, and deployment verification
selects and verifies either target. This is not a release certification. Obtain
green required CI for the exact commit, then execute and retain the real tagged
private-ECR/Sigstore flow for the selected image digest.

The operational workflow implements daily recovery metadata audits and a
monthly temporary-table PITR exercise with separate audit and recovery roles.
A real AWS restore exercise has not yet been externally verified. Configure the
production roles and both target KMS keys, run the exercise in AWS, and retain
recovery and application-cutover evidence before promotion.

## Operating Modes

| Mode | Required settings | Security boundary |
|---|---|---|
| Single-user / legacy | `AXON_DEPLOYMENT_PROFILE=development`, `AXON_REQUIRE_CANONICAL_IDENTITY=false`; DynamoDB optional | Intended for local development or one isolated trust domain. Verified credential claims may supply roles, scopes, tenant, and project authority. It is not a boundary between untrusted tenants. |
| Multi-tenant | `AXON_DEPLOYMENT_PROFILE=production`, `AXON_AUTH_MODE=ENFORCE`, `LLM_ROUTER_DYNAMODB_ENABLED=true`, `AXON_REQUIRE_CANONICAL_IDENTITY=true`, exact OIDC issuer and audience | Roles, scopes, status, tenant membership, and project grants come from strongly read DynamoDB principals. Production startup rejects any weaker combination. |

Canonical data-plane requests require signed tenant and project hints. The
credential identifies the principal; it does not grant authority. AxonLLM
replaces claimed roles, scopes, and grants with the canonical principal, then
strongly resolves the tenant-owned project. Missing project context is 400,
cross-tenant or ungranted resources are concealed as 404, and authority-store
failure is 503.

Platform operators enter a tenant only through HTTP break-glass. Send exactly
one validated `X-Axon-Target-Tenant` header and a non-empty
`X-Axon-Break-Glass-Reason`. The server-resolved platform principal is evaluated
against that target through the shared authorization kernel, the immutable
audit event is written for that same tenant before dispatch, and only then is
the handler context rebound. Missing, duplicate, malformed, or conflicting
tenant selectors are rejected.

There is no supported tenant/principal bootstrap CLI. Before enabling canonical
mode, provision:

- the tenant-owned project row;
- a canonical principal keyed by issuer, subject, authentication method, and
  tenant;
- active membership, tenant role, project grants, and service scopes;
- tenant-bound SCIM credentials through `AXON_SCIM_TENANTS` when SCIM is used.

The Fargate stack does not inject `AXON_SCIM_TENANTS`. Its Secrets Manager
integration injects only Anthropic and OpenAI provider keys. Add a reviewed
tenant bootstrap and SCIM-secret delivery process before relying on automated
provisioning.

After a canonical SCIM user and principal exist,
`POST /admin/projects/{id}/members` accepts the SCIM resource id in `user_id`.
POST/DELETE membership operations use one CAS-guarded transaction to update
`Project.members`, `ScimUser.project_ids`, authoritative
`Principal.project_ids`, both authorization versions, and `SCIM#VERSION`.
Stored and returned member values are normalized to `scim:<id>`. A canonical
project POST rejects non-empty bulk members, and project PUT rejects any
`members` field; use the member routes. Initial tenant and administrator
bootstrap remains external.

Canonical SCIM users, groups, username uniqueness edges, and convergence state
share `PK=TENANT#{tenant_id}` with sort keys `SCIM#USER#{id}`,
`SCIM#GROUP#{id}`, `SCIM#USERNAME#{hash}`, and `SCIM#VERSION`. Version point
reads and paginated tenant snapshot queries set `ConsistentRead=True`.

## RBAC

There is no role literally named `viewer`. Use `tenant_member` or
`tenant_auditor` for tenant read-only access.

| Role | Tenant control plane | Project data plane |
|---|---|---|
| `tenant_admin` | Read/write tenant-owned configuration; no write access to platform-global resources | Explicit project grant required for model listing, inference, and reserved `query.select` |
| `tenant_member` | Read only | Explicit project grant required |
| `tenant_auditor` | Read only | Explicit project grant required |
| `service` | No canonical admin access; legacy admin scopes are ignored and canonical key issuance rejects them | Explicit project grant plus server-held action scope required |
| `platform_admin` | Platform resources; tenant control-plane access requires `X-Axon-Break-Glass-Reason` | No ordinary project data-plane access is exposed |

Platform-global resources include models, health, architecture, catalogue and
pricing drift, production readiness, and region topology. Tenant viewers can
read platform views that do not impose a stricter handler check; platform writes
and region topology are restricted. Canonical viewers cannot use legacy admin
roles/scopes to mutate, and canonical services cannot use them to enter the
control plane. Legacy `admin` roles and `admin:*` scopes remain compatibility
only for noncanonical migration mode.

`query.select` is authorization vocabulary only. AxonLLM ships no SQL parser,
datasource adapter, HTTP route, AgentCore action, or backend query contract.
`query.mutate` always denies.

## Fargate Deployment

`infra/stack.py` is restricted to `us-east-1` and defines:

- CloudFront and WAF in front of an internal TLS ALB;
- private Fargate tasks with customer-managed HTTPS egress;
- Bedrock invocation IAM restricted to required concrete model/profile ARNs;
- ALB OIDC for `/admin/*` in `DeploymentMode=production`;
- canonical identity and enforced authentication in production mode;
- a private ECR image parameter that accepts only `@sha256` URIs;
- KMS-encrypted DynamoDB with deletion protection and PITR;
- daily AWS Backup at 05:00 UTC, 30-day cold transition, and 365-day deletion;
- a KMS-encrypted FIFO security-event outbox and DLQ retained for 14 days;
- a managed encrypted FIFO SNS topic, retained encrypted CloudWatch log group,
  and resource-scoped private SQS/SNS/Logs endpoints;
- alarms, an operations dashboard, and two tasks scaling to ten.

`deploy-fargate.sh` requires `AXON_VERIFIED_IMAGE_URI` and
`AXON_BEDROCK_INVOKE_RESOURCE_ARNS`, but leaves `DeploymentMode` at `staging`
and does not supply production OIDC parameters. It is not the production
deployment command.

Install CDK dependencies and bootstrap the target account:

```bash
cd infra
uv venv
uv pip install -r requirements.txt
npx cdk bootstrap -c deployment_target=fargate -c region=us-east-1
```

Synthesize, review, and deploy the immutable image:

```bash
npx cdk synth AxonLLMStack \
  -c deployment_target=fargate -c region=us-east-1

npx cdk deploy AxonLLMStack \
  -c deployment_target=fargate -c region=us-east-1 \
  --parameters AxonLLMStack:DeploymentMode=production \
  --parameters AxonLLMStack:VerifiedImageUri="$VERIFIED_IMAGE_URI" \
  --parameters AxonLLMStack:ViewerDomainName="$VIEWER_DOMAIN_NAME" \
  --parameters AxonLLMStack:ViewerCertificateArn="$VIEWER_CERTIFICATE_ARN" \
  --parameters AxonLLMStack:OriginDomainName="$ORIGIN_DOMAIN_NAME" \
  --parameters AxonLLMStack:OriginCertificateArn="$ORIGIN_CERTIFICATE_ARN" \
  --parameters AxonLLMStack:ApprovedHttpsPrefixListId="$APPROVED_HTTPS_PREFIX_LIST_ID" \
  --parameters AxonLLMStack:BedrockInvokeResourceArns="$AXON_BEDROCK_INVOKE_RESOURCE_ARNS" \
  --parameters AxonLLMStack:OidcIssuer="$OIDC_ISSUER" \
  --parameters AxonLLMStack:OidcAuthorizationEndpoint="$OIDC_AUTHORIZATION_ENDPOINT" \
  --parameters AxonLLMStack:OidcTokenEndpoint="$OIDC_TOKEN_ENDPOINT" \
  --parameters AxonLLMStack:OidcUserInfoEndpoint="$OIDC_USER_INFO_ENDPOINT" \
  --parameters AxonLLMStack:OidcClientId="$OIDC_CLIENT_ID" \
  --parameters AxonLLMStack:OidcClientSecret="$OIDC_CLIENT_SECRET" \
  --parameters AxonLLMStack:OidcAudience="$OIDC_AUDIENCE"
```

Pass the OIDC client secret through protected deployment automation; command
arguments can be exposed in process listings and shell history. The approved
prefix list must contain only required IdP, AWS API, and provider destinations.
The stack has no open-egress fallback.

`AXON_BEDROCK_INVOKE_RESOURCE_ARNS` must be a comma-separated list of concrete
Bedrock model or inference-profile ARNs. The CloudFormation parameter rejects
wildcards and the resulting list scopes the task role's Bedrock invoke
permissions.

The stack gives its retained provider secret a CloudFormation-generated physical
name. Consumers must resolve the `ProviderSecretArn` stack output rather than
hardcoding a physical name:

```bash
PROVIDER_SECRET_ARN="$(
  aws cloudformation describe-stacks --stack-name AxonLLMStack --region us-east-1 \
    --query "Stacks[0].Outputs[?OutputKey=='ProviderSecretArn'].OutputValue | [0]" \
    --output text
)"
```

Use that ARN for secret updates, rotation automation, and monitoring. The secret
uses `RETAIN`; deleting the stack does not delete it, and a replacement stack
creates a different generated secret and output.

`AXON_ENABLED_PROVIDERS` is an optional comma-separated runtime allowlist.
Providers outside it are neither advertised nor invoked. Leaving it unset adds
no allowlist beyond provider configuration and credentials; setting it empty or
including an unknown provider fails startup. The AgentCore stack fixes this
value to `bedrock`, so that target advertises and invokes standard Bedrock
models only.

## Security Event Delivery

Both CDK stacks inject four runtime controls:

| Variable | Stack output / value | Purpose |
|---|---|---|
| `AXON_AWS_ACCOUNT_ID` | Deployment account | Reject cross-account SNS and CloudWatch destinations |
| `AXON_EVENT_OUTBOX_QUEUE_URL` | `SecurityEventOutboxQueueUrl` | Durable FIFO enqueue and retry processing |
| `AXON_SECURITY_EVENT_SNS_TOPIC_ARN` | `SecurityEventTopicArn` | Exact allowed SNS event topic |
| `AXON_SECURITY_EVENT_LOG_GROUP_ARN` | `SecurityEventLogGroupArn` | Exact allowed CloudWatch event log group |

These values do not create tenant destinations. A tenant administrator must add
an SNS, CloudWatch Logs, or webhook destination through `/admin/webhooks`.
Managed SNS and CloudWatch destinations must use the stack output ARN. The
CloudWatch stream created by the stack is named `events`. Webhooks must use
HTTPS, resolve only to public addresses, reject redirects, and cannot override
AxonLLM's host, content, event-id, or idempotency headers.

For every matching tenant destination, dispatch writes a strict tenant-bound
snapshot to the FIFO queue before returning. The destination-specific message
group preserves ordering, and the SHA-256 delivery identity is reused for SQS
deduplication, FIFO SNS deduplication, and the webhook `Idempotency-Key`.
Delivery remains at-least-once, so consumers must still be idempotent.

The worker retries failures with SQS visibility delays from 5 to 300 seconds.
After five receives, SQS moves the message to the retained DLQ. Both queues use
KMS encryption, require TLS, and retain messages for 14 days. The worker starts
only after queue readiness succeeds, and `/ready` reports
`security_event_outbox=unavailable` or returns 503 when the configured queue
cannot be reached. A DLQ depth of one or more raises
`SecurityEventDeadLettersAlarm`.

Neither the event topic nor the alarm topic has an automatic subscription. Add
and confirm the required receivers before traffic.

### DLQ Recovery

Set the deployed stack name to `AxonLLMStack` or
`AxonLLMAgentCoreStack`, then resolve and inspect the DLQ:

```bash
STACK_NAME=AxonLLMStack
AWS_REGION=us-east-1

DLQ_URL="$(
  aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$AWS_REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='SecurityEventDeadLetterQueueUrl'].OutputValue | [0]" \
    --output text
)"
DLQ_ARN="$(
  aws sqs get-queue-attributes \
    --queue-url "$DLQ_URL" \
    --attribute-names QueueArn \
    --query "Attributes.QueueArn" \
    --output text
)"

aws sqs get-queue-attributes \
  --queue-url "$DLQ_URL" \
  --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible

aws sqs receive-message \
  --queue-url "$DLQ_URL" \
  --max-number-of-messages 1 \
  --message-system-attribute-names ApproximateReceiveCount SentTimestamp \
  --visibility-timeout 30
```

Correlate `delivery_id`, `tenant_id`, event type, and destination with
application logs. Fix the underlying network, permission, subscription, or
receiver failure first. A queued message contains the destination snapshot from
enqueue time. Redrive only when that snapshot is still valid; changing the
current destination configuration does not rewrite an existing message.
Quarantine and handle stale or malformed snapshots under the incident process
instead of repeatedly redriving them.

For a validated transient failure, redrive slowly and monitor the DLQ, source
queue, receiver, and alarm:

```bash
aws sqs start-message-move-task \
  --source-arn "$DLQ_ARN" \
  --max-number-of-messages-per-second 1

aws sqs list-message-move-tasks --source-arn "$DLQ_ARN"
```

Do not purge the DLQ as routine recovery. Retain the incident timeline, sample
delivery IDs, root cause, redrive task result, and receiver confirmation.

## Release Evidence

`.github/workflows/release-security.yml` runs for `v*` tags or manual dispatch.
It:

- scans source for high/critical vulnerabilities and secrets;
- builds AMD64 Fargate and ARM64 AgentCore OCI images;
- scans both images and emits CycloneDX SBOMs;
- creates a deterministic source archive;
- creates a schema-v2 manifest with distinct Fargate and AgentCore target
  identities and keylessly attests both image digests and the manifest;
- stores private evidence for 90 days.

The workflow does not publish an image. Publication to private ECR is a separate
controlled operation.

`.github/workflows/deploy-verification.yml` requires a release tag, successful
CI for the exact commit, signed evidence, an immutable private ECR digest, and a
fresh image scan. Its `target` input selects `fargate` or `agentcore`; the
schema-v2 verifier binds the supplied digest to the selected target and emits
the matching subject, platform, and Sigstore bundle. The workflow verifies the
manifest attestation, selected target's remote private ECR digest and keyless
attestation, then rescans that exact image.

Never deploy a mutable tag or bypass a failing CI/evidence check. Record the
commit, release tag, workflow run, ECR URI and digest, SBOMs, scan results,
attestation verification, selected target, approvals, and canary results. The
workflows implement this gate but do not publish or deploy either image. A real
tagged private-ECR/Sigstore execution remains externally unverified.

## Readiness And Traffic Shift

- `GET /health` is liveness only.
- Starlette `GET /ready` checks DynamoDB when persistence is enabled and the
  security-event outbox when configured.
- `GET /admin/production-checklist` checks configuration posture, not live
  dependencies.
- AgentCore `health` is liveness; its separate `GET /ready` checks runtime,
  OIDC/JWKS, the principal store, and the configured security-event outbox.

Before shifting traffic:

1. Require green CI and verified release evidence for the exact digest.
2. Resolve every production-checklist `FAIL`; investigate every `UNKNOWN`.
3. Confirm readiness on every target.
4. Run authenticated model-list and completion canaries.
5. Run negative canaries for missing credentials, inactive membership,
   ungranted and cross-tenant projects, viewer writes, and `query.mutate`.
6. Verify alarms, a confirmed SNS subscription, logs, tenant audit-chain
   verification, and rollback.

Neither stack creates an SNS subscription. Add and confirm alarm and
security-event receivers before launch.

## Backup And Restore

Both AWS stacks enable DynamoDB PITR and daily AWS Backup. CDK does not configure
Vault Lock; enable it separately where immutable recovery points are required.
`.github/workflows/operations-security.yml` uses a Fargate/AgentCore matrix for
the daily metadata audit and monthly PITR restore exercise, with separate
least-privilege audit and recovery roles. Configure
`AXON_OPERATIONS_AUDIT_ROLE_ARN`, `AXON_OPERATIONS_RECOVERY_ROLE_ARN`,
`AXON_AWS_ACCOUNT_ID`, `AXON_DATA_KMS_KEY_ARN`, and
`AXON_AGENTCORE_DATA_KMS_KEY_ARN` in the protected production environment. The
two KMS variables must contain the data-key ARN for their respective stack.

Validate Fargate:

```bash
python scripts/operations/validate_state_recovery.py
python scripts/operations/validate_state_recovery.py --require-vault-lock
python scripts/operations/validate_state_recovery.py --exercise-restore
```

The restore exercise creates a temporary table and deletes it unless
`--keep-restored-table` is set. It validates table recovery, not application
cutover. Rehearse restored-table selection, authorization and tenant-integrity
checks, traffic shift, and rollback separately.

The scheduled workflow validates both `AxonLLMStack` and
`AxonLLMAgentCoreStack`. For an ad hoc AgentCore run, pass
`--stack-name AxonLLMAgentCoreStack`.

The scripts and schedule do not prove that AWS accepted a restore or that the
application can cut over. The first real AWS restore exercise and application
recovery rehearsal remain externally unverified; retain their evidence before
promotion.

## Rotation And Incident Response

Canonical tenant API keys default to 90 days and cannot exceed 365 days.
Canonical issue, revoke, and rotation update key and service-principal state
transactionally. Legacy/in-memory rotation is revoke then issue. Replicas poll
the tenant revocation epoch every five seconds; if the read fails, cached
acceptance can last up to the 300-second key-cache TTL.

For a credential or authorization incident:

1. Isolate the affected caller or tenant.
2. Revoke the tenant key; verify rejection on every reachable replica after a
   successful epoch poll.
3. Rotate affected provider secrets and force new ECS tasks because task secrets
   are read at startup.
4. Rotate or disable IdP clients/signing keys and tenant SCIM tokens when
   implicated.
5. Verify and export the affected tenant audit chain.
6. Re-run positive and negative authorization canaries before restoring traffic.

Validate Fargate provider-secret posture with:

```bash
python scripts/operations/check_secret_rotation.py \
  --secret-id "$PROVIDER_SECRET_ARN"
python scripts/operations/check_secret_rotation.py \
  --secret-id "$PROVIDER_SECRET_ARN" \
  --require-automatic-rotation
```

The script inspects the Fargate secret, KMS rotation, version age, pending
versions, and current Anthropic/OpenAI values. It does not rotate secrets and
does not cover AgentCore, which injects no provider secret.
