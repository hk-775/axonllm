# AxonLLM Production Runbook

This runbook describes the production controls implemented in the current
repository. It does not certify a checkout, image, AWS account, or deployment.

## Release Status

The canonical SCIM convergence contract is implemented: tenant user/group
transactions advance `SCIM#VERSION`, and `DynamoPersistence` provides strongly
consistent tenant version and snapshot reads.

Focused hardening regressions are green locally. Release evidence uses
schema-v2 with distinct Fargate and AgentCore targets. Controlled publication
copies both signed OCI archives into retained immutable private ECR
repositories, and deployment verification selects and verifies either target.
This is not a release certification. Obtain green required CI for the exact
commit, then execute and retain the real tagged private-ECR/Sigstore flow for
the selected image digest.

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

Bootstrap the first canonical administrator against the same DynamoDB table the
runtime will use:

```bash
LLM_ROUTER_DYNAMODB_ENABLED=true \
AXON_DYNAMODB_TABLE=axonllm-state \
AWS_DEFAULT_REGION=us-east-1 \
uv run axon bootstrap-tenant \
  --tenant tenant-a \
  --project project-a \
  --project-name Production \
  --issuer https://idp.example.com/oauth2/default \
  --subject 00u-admin-subject \
  --user-name admin@example.com \
  --display-name "Tenant A Admin" \
  --email admin@example.com \
  --budget-limit 1000
```

The command conditionally creates or verifies the tenant project and SCIM user,
grants project membership through the canonical CAS transaction, and returns
only after strongly resolving the active `tenant_admin` principal and grant. It
is restartable and refuses to reuse a user name bound to a different issuer or
subject.

Create canonical service credentials with the tenant-qualified key path:

```bash
LLM_ROUTER_DYNAMODB_ENABLED=true \
AXON_DYNAMODB_TABLE=axonllm-state \
AWS_DEFAULT_REGION=us-east-1 \
uv run axon issue-key \
  --tenant tenant-a \
  --project project-a \
  --name production-service
```

The default canonical scopes are `model.list`, `inference.invoke`, and
`query.select`; canonical issuance rejects every legacy `admin:` scope. The raw
key is shown once.

When SCIM provisioning is required, create a Secrets Manager value containing
the complete `AXON_SCIM_TENANTS` JSON map, then pass its complete ARN as
`AXON_SCIM_TENANTS_SECRET_ARN` to `deploy-fargate.sh`. The stack injects that
secret as `AXON_SCIM_TENANTS`; it never stores the credential JSON in a
CloudFormation parameter or task-definition environment value.

After a canonical SCIM user and principal exist,
`POST /admin/projects/{id}/members` accepts the SCIM resource id in `user_id`.
POST/DELETE membership operations use one CAS-guarded transaction to update
`Project.members`, `ScimUser.project_ids`, authoritative
`Principal.project_ids`, both authorization versions, and `SCIM#VERSION`.
Stored and returned member values are normalized to `scim:<id>`. A canonical
project POST rejects non-empty bulk members, and project PUT rejects any
`members` field; use the member routes after the initial CLI bootstrap.

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

## Release Foundation

`infra/release_foundation_stack.py` is restricted to `us-east-1` and creates:

- retained `axonllm/fargate` and `axonllm/agentcore` private ECR repositories;
- immutable tags, scan-on-push, and a retained rotation-enabled KMS key;
- an account-global GitHub Actions OIDC provider;
- an `AxonLLMReleasePublisher` role trusted only by the protected GitHub
  `release` environment;
- an `AxonLLMReleaseVerifier` role trusted only by the protected GitHub
  `production` environment;
- an `AxonLLMOperationsAudit` role that can read recovery, secret-version, and
  key-rotation metadata but cannot read secret values or restore data; and
- an `AxonLLMOperationsRecovery` role that can run PITR validation and remove
  only temporary `*-restore-validation-*` tables.

The publisher can upload and read image layers but cannot delete images or
repositories. The verifier is read-only. Both operations roles trust only the
exact protected `production` environment subject. Deploy this stack once per
target account:

```bash
cd infra
npx cdk deploy AxonLLMReleaseFoundationStack \
  -c deployment_target=release-foundation \
  -c region=us-east-1 \
  --parameters AxonLLMReleaseFoundationStack:FargateStateTableName=axonllm-state \
  --parameters AxonLLMReleaseFoundationStack:AgentCoreStateTableName=axonllm-agentcore-state
```

If either runtime uses a nondefault physical table name, pass the same name to
the foundation parameter. The operations IAM policies are generated from these
parameters and will not reach a different table.

Create two protected GitHub environments:

- `release`: require approval and restrict deployment refs to version tags;
- `production`: require approval and restrict deployment refs to protected
  production branches and tags.

Required reviewers and wait timers for private repositories depend on the
organization's GitHub plan. Verify that GitHub actually returns the reviewer
rule after configuration. A custom branch/tag policy by itself is not an
approval gate; if the plan rejects reviewers, upgrade the plan before claiming
that either environment has separation-of-duties approval.

Configure `release` with:

| Kind | Name | Value |
|---|---|---|
| Secret | `AXON_RELEASE_PUBLISH_ROLE_ARN` | `ReleasePublisherRoleArn` output |
| Variable | `AXON_AWS_ACCOUNT_ID` | Twelve-digit deployment account |
| Variable | `AXON_FARGATE_ECR_REPOSITORY` | `FargateRepositoryUri` output |
| Variable | `AXON_AGENTCORE_ECR_REPOSITORY` | `AgentCoreRepositoryUri` output |

Configure `production` with the same three variables. Set `AWS_ROLE_ARN` to the
`ReleaseVerifierRoleArn` output, `AXON_OPERATIONS_AUDIT_ROLE_ARN` to
`OperationsAuditRoleArn`, and `AXON_OPERATIONS_RECOVERY_ROLE_ARN` to
`OperationsRecoveryRoleArn`. Also set `AXON_FARGATE_STATE_TABLE_NAME` and
`AXON_AGENTCORE_STATE_TABLE_NAME` to the physical names supplied to the
foundation stack. The target data-key variables described in
[Backup And Restore](#backup-and-restore) are additional production-environment
settings.

## Fargate Deployment

`infra/stack.py` is restricted to `us-east-1` and defines:

- CloudFront and WAF in front of an internal TLS ALB;
- private Fargate tasks with customer-managed HTTPS egress;
- Bedrock invocation IAM restricted to required concrete model/profile ARNs;
- ALB OIDC for `/admin/*` in `DeploymentMode=production`;
- canonical identity and enforced authentication in production mode;
- a private ECR image parameter that accepts only `@sha256` URIs;
- KMS-encrypted DynamoDB with deletion protection and PITR;
- daily AWS Backup at 05:00 UTC, 30-day cold transition, 365-day deletion, and
  governance-mode Vault Lock enforcing 30-365 day retention;
- a KMS-encrypted FIFO security-event outbox and DLQ retained for 14 days;
- a managed encrypted FIFO SNS topic, retained encrypted CloudWatch log group,
  and resource-scoped private SQS/SNS/Logs endpoints;
- optional Secrets Manager delivery of the complete `AXON_SCIM_TENANTS` value;
- a controlled restored-table parameter, exact task access, selected-table
  alarms/backups, and a quiescence guard for Fargate recovery cutover;
- alarms, an operations dashboard, and two tasks scaling to ten.

`deploy-fargate.sh` requires `AXON_VERIFIED_IMAGE_URI` and
`AXON_BEDROCK_INVOKE_RESOURCE_ARNS`. It defaults to staging, but is
production-capable: set `AXON_DEPLOYMENT_MODE=production` and all seven
`AXON_OIDC_*` inputs. It also accepts `AXON_SCIM_TENANTS_SECRET_ARN`, paired
hosted-zone inputs, `AXON_RUNTIME_STATE_TABLE_NAME`, and
`AXON_RECOVERY_CUTOVER_MODE` for an approved recovery cutover. Review the CDK
diff before using `--yes`; pass protected secrets through deployment automation
because command arguments can appear in process listings and shell history.

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
  -c scim_tenants_secret_arn="$SCIM_TENANTS_SECRET_ARN" \
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
  --parameters AxonLLMStack:OidcAudience="$OIDC_AUDIENCE" \
  --parameters AxonLLMStack:RuntimeStateTableName="" \
  --parameters AxonLLMStack:RecoveryCutoverMode=false
```

Omit the SCIM context when SCIM is not configured. The approved prefix list must
contain only required IdP, AWS API, and provider destinations. The stack has no
open-egress fallback.

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

`.github/workflows/publish-release.yml` is the controlled publication step. It
runs only in the protected `release` environment, validates the exact tag,
commit, release workflow run and attempt, successful CI, signed manifest, both
target identities, and fixed account/region repository names. It then uses a
checksum-pinned ORAS client to copy each signed OCI digest without rebuilding,
verifies both remote digests and Sigstore bundles, and emits immutable
`@sha256` references. Existing immutable tags are accepted only when their
digest already matches; AWS lookup failures fail closed.

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
workflows publish and verify images but do not deploy either runtime. A real
tagged private-ECR/Sigstore execution remains externally unverified until its
workflow evidence is retained.

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
   ungranted and cross-tenant project claims, and viewer writes.
6. Verify alarms, a confirmed SNS subscription, logs, tenant audit-chain
   verification, and rollback.

The production validation tool evaluates `query.mutate` against the checked-out
authorization policy only. There is no remote query route on which to run that
canary.

Neither stack creates an SNS subscription. Add and confirm alarm and
security-event receivers before launch.

## Backup And Restore

Both AWS stacks enable DynamoDB PITR, daily AWS Backup, and governance-mode
Vault Lock with a 30-day minimum and 365-day maximum retention.
`.github/workflows/operations-security.yml` uses a Fargate/AgentCore matrix for
the daily metadata audit and monthly PITR restore exercise, with separate
least-privilege audit and recovery roles. Configure
`AXON_OPERATIONS_AUDIT_ROLE_ARN`, `AXON_OPERATIONS_RECOVERY_ROLE_ARN`,
`AXON_AWS_ACCOUNT_ID`, `AXON_DATA_KMS_KEY_ARN`, and
`AXON_AGENTCORE_DATA_KMS_KEY_ARN` in the protected production environment.
Set `AXON_FARGATE_STATE_TABLE_NAME` and
`AXON_AGENTCORE_STATE_TABLE_NAME` when either physical name differs from its
documented default. The two KMS variables must contain the data-key ARN for
their respective stack. With `--require-vault-lock`, the validator checks the
exact 30-day minimum, 365-day maximum, and governance mode rather than accepting
an arbitrary locked vault.

Validate Fargate:

```bash
python scripts/operations/validate_state_recovery.py
python scripts/operations/validate_state_recovery.py --require-vault-lock
python scripts/operations/validate_state_recovery.py --exercise-restore
```

The restore exercise creates a temporary table and deletes it unless
`--keep-restored-table` is set. It validates table recovery, not application
cutover. A retained table is returned only after the validator enables and
verifies PITR, `expires_at` TTL, and deletion protection. A manually dispatched
workflow can set `retain_fargate_restore=true`; only the Fargate matrix entry is
retained, and each result is preserved as a 90-day evidence artifact.

For a controlled Fargate cutover rehearsal:

1. Run the validator with `--exercise-restore --keep-restored-table` using the
   recovery role and record
   `restoreExercise.targetTable` from its JSON output.
2. Confirm the table name begins with the deployed primary table name followed
   by `-restore-validation-`. The Fargate parameter and task IAM role reject
   every other table namespace.
3. Enter the approved maintenance window, stop new client traffic, and use the
   recovery helper to preserve the existing scaling state, suspend all three
   scaling paths, set minimum capacity to zero, and prove the service is fully
   quiesced:

```bash
python scripts/operations/fargate_recovery.py \
  quiesce --state-file recovery-cutover.json
```

   The state file is created with mode `0600` and records the original desired
   count, capacity bounds, and suspension state. Run this helper as the
   deployment operator with scoped ECS, Application Auto Scaling, ELB target
   health, and CloudFormation read access. The PITR recovery role intentionally
   remains limited to restoring, protecting, validating, and removing temporary
   tables.
4. Repeat the reviewed production deployment with every original input
   unchanged except
   `AXON_RUNTIME_STATE_TABLE_NAME=$RESTORED_TABLE_NAME` and
   `AXON_RECOVERY_CUTOVER_MODE=true`. CloudFormation rejects the state switch
   unless step 3 is still true and pins the service's declared desired count to
   zero. The update grants the task role only the selected table, moves
   DynamoDB alarms to it, and adds it to the backup plan.
5. Start the recorded number of canary tasks. The helper leaves autoscaling
   suspended and returns only after ECS is stable and at least that many ALB
   targets are healthy:

```bash
python scripts/operations/fargate_recovery.py \
  start \
  --state-file recovery-cutover.json \
  --expected-table "$RESTORED_TABLE_NAME"
```

6. Confirm the selected table and healthy target count, then run the fail-closed
   canary/load harness with credentials supplied only through environment
   variables:

```bash
python scripts/operations/fargate_recovery.py \
  status --minimum-healthy-targets 2
python scripts/operations/run_production_validation.py \
  --config scripts/operations/production_validation.example.json \
  --base-url "$FARGATE_HTTPS_ORIGIN" \
  --output production-validation.json
```

   Customize the example with real signed tenant/project claim variants and
   credential environment names. It requires an authenticated read,
   viewer-write denial, cross-tenant-claim denial, and ungranted-project-claim
   denial before running a bodyless GET/HEAD load. Those ingress-boundary claim
   mismatches return 403; the report's separate source-policy contract proves
   404 concealment once an owned resource reaches authorization. Reports redact
   credential values. One Fargate ALB origin plus
   `status --minimum-healthy-targets 2` proves load through a service with at
   least two healthy targets; it does not identify which task served each
   request and reports that claim as false.
7. After canaries pass, restore the exact recorded autoscaling configuration:

```bash
python scripts/operations/fargate_recovery.py \
  resume \
  --state-file recovery-cutover.json \
  --expected-table "$RESTORED_TABLE_NAME"
```

   Then repeat the unchanged deployment with
   `AXON_RECOVERY_CUTOVER_MODE=false` while retaining
   `AXON_RUNTIME_STATE_TABLE_NAME=$RESTORED_TABLE_NAME`. This reconciles the
   stack's declared desired count with the validated running service. Do not
   leave the stack in cutover mode; a later unrelated stack update would
   correctly reconcile it back to zero tasks.
8. Rehearse rollback with a new state file: quiesce again, redeploy with an empty
   `AXON_RUNTIME_STATE_TABLE_NAME` and
   `AXON_RECOVERY_CUTOVER_MODE=true`, start against the primary table, rerun
   step 6, and resume. Redeploy once more with cutover mode `false`, then verify
   status. Only after rollback is verified and cutover mode is false may the
   recovery role remove the temporary table:

```bash
python scripts/operations/fargate_recovery.py \
  cleanup --table-name "$RESTORED_TABLE_NAME"
```

The runtime role can use only the retained primary table and the exact selected
recovery table. The recovery role cannot deploy the application or read
provider secret values. Keep deployment authority separate. Do not use a
rolling task update for a table switch; the custom resource intentionally fails
that deployment while any old task or scaling path is active.

The scheduled workflow validates both `AxonLLMStack` and
`AxonLLMAgentCoreStack`. For an ad hoc AgentCore restore validation, pass
`--stack-name AxonLLMAgentCoreStack`. The retained-table parameter, quiescence
guard, runtime IAM switch, and `fargate_recovery.py` workflow are Fargate-only;
there is no supported AgentCore application cutover.

The scripts and schedule do not prove that AWS accepted a restore or that the
application cutover succeeded. The first real AWS restore exercise and
application recovery rehearsal remain externally unverified; retain their
evidence before promotion.

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
