# AxonLLM Production Runbook

This runbook describes the production controls implemented in the current
repository. It does not certify a checkout, image, AWS account, or deployment.

## Release Status

The canonical SCIM convergence contract is implemented: tenant user/group
transactions advance `SCIM#VERSION`, and `DynamoPersistence` provides strongly
consistent tenant version and snapshot reads.

Focused hardening regressions are green locally. Release evidence uses
schema-v3 with distinct Fargate and AgentCore targets. Controlled publication
copies both signed OCI archives into retained immutable private ECR
repositories, and deployment verification selects and verifies either target.

The repository also contains the newer shared HTTP/AgentCore Athena query
service, credential-free datasource administration, and managed-Cognito
shared-state control-plane stack. These changes postdate `v0.2.4`; they do not
yet have tagged release evidence, a deployed Athena canary, or a deployed
control-plane canary.

The protected AgentCore launch orchestrator now certifies isolated external
OIDC and managed-Cognito qualification deployments, runs seven launch gates,
records signed teardown evidence, and only then invokes the production leaf.
That leaf deploys a separate candidate, starts and validates a backup plus
restore, certifies every enabled provider and the identity/query contract,
promotes the exact runtime version, and persists KMS-signed schema-v5
deployment evidence under S3 Object Lock. This implemented path still requires
a successful target-account run for the exact release before launch.

`v0.2.4` is the first completed KMS-backed release. Release evidence
[run 31434900128](https://github.com/AxonLLM/axonllm/actions/runs/31434900128)
and publication
[run 31435171504](https://github.com/AxonLLM/axonllm/actions/runs/31435171504)
succeeded for commit `2dcee34619b22a8288d734993eb3005757bda52c`.
Current-policy verification also succeeded for
[Fargate](https://github.com/AxonLLM/axonllm/actions/runs/31435684849) and
[AgentCore](https://github.com/AxonLLM/axonllm/actions/runs/31435686001).
The published target digests are:

- Fargate:
  `sha256:b0e6e063c1851c2f19a86a16e7c012c3fe926402fb75f3b5d43e5fa3845c91b2`
- AgentCore:
  `sha256:e368b7b4522f4838f3ebb4dcc04967682c73cb73e7e40ce16421a6a1ffda6147`

The GitHub evidence artifact expires on 2026-11-08; retain it in the approved
evidence system or produce a newer release before relying on it. These runs
validate the release supply chain, not a runtime deployment.

The immutable `v0.2.2` and `v0.2.3` tags are not promotable. For `v0.2.2`,
GitHub rejected attestation persistence for the private organization plan
before any evidence artifact was uploaded. For `v0.2.3`, every build, scan, and
schema-v3 self-check passed, but AWS rejected the legacy name-only OIDC trust
subject before signing; no signatures or evidence artifact were created. The
immutable-ID trust fix merged after that tag. Do not move or reuse either tag;
the first successful KMS-backed release is `v0.2.4`.

The operational workflow implements daily recovery metadata audits and a
monthly temporary-table PITR exercise with separate audit and recovery roles.
A real AWS restore exercise has not yet been externally verified. Configure the
production roles and both target KMS keys, run the exercise in AWS, and retain
recovery and application-cutover evidence before promotion.

A read-only target-account audit on 2026-08-10 found no promotable AxonLLM
runtime. The Fargate service is a stopped legacy/demo deployment with a public
HTTP origin, mutable CDK asset image, open task egress, noncanonical runtime
settings, and no hardened backup or customer-managed data-key posture. The
current state table has PITR but lacks deletion protection, customer-managed
encryption, TTL, an AWS Backup vault, and recovery points. The legacy provider
secret lacks customer-managed encryption and rotation.

The hardened AgentCore, retained identity, shared control plane, and state
stacks are absent, so there is no deployed AxonLLM AgentCore target on which to
run release canaries, identity validation, datasource/query validation,
recovery validation, or rollback checks.

The account also lacks the selected control-plane endpoint prerequisites,
control-plane HTTPS egress prefix list, AgentCore HTTPS prefix list, dedicated
OIDC configuration, and confirmed alarm/event subscribers. A custom-domain
control plane requires the production DNS zone, regional ACM certificate, and
approved ingress prefix list. The CloudFront alternative removes those three
requirements but requires reviewed public IPv4 viewer CIDRs. The protected
GitHub `production` environment lacks both target data-key variables because
the hardened stacks have not produced those keys.
Until those prerequisites are supplied and a reviewed deployment is complete,
restore/cutover, authenticated RBAC, load, security-event, and multi-replica
canaries remain blocked.

For the audited Fargate stack, synthesize with the existing physical table name
as `-c table_name=...`. The 2026-08-10 synthesis preserved the table's
CloudFormation logical identity and applied encryption, TTL, deletion
protection, and backup controls as updates. Review the real change set before
deployment and abort if it proposes replacing or deleting the state table or
provider secret.

The private-repository GitHub plan currently rejects rulesets and required
environment reviewers, and the repository has only one administrator. This is
workable for a single-maintainer preproduction flow, but it is not independent
enterprise separation of duties. Upgrade the plan and add an independent
release approver before granting write access to additional maintainers or
claiming multi-writer release governance.

## Operating Modes

| Mode | Required settings | Security boundary |
|---|---|---|
| Local seeded demo | `axon setup local-demo --start --acknowledge-non-production` | Forces development, `LOG_ONLY`, fictional data, and in-memory persistence. Anonymous and never promotable. |
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

For AgentCore, `deploy-agentcore.sh` performs this same canonical bootstrap
after deploying the runtime. With `managed-cognito`, it first deploys retained
identity and invites the administrator, then deploys the shared-state web
control plane after bootstrap. With `external-oidc`, it requires the
already-provisioned immutable subject and deploys AgentCore/bootstrap only; it
does not deploy the Cognito-authenticated web control plane. The manual command
remains the recovery path.

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
| `tenant_admin` | Read/write tenant-owned configuration and datasource metadata; no write access to platform-global resources | Explicit project grant required for model listing, inference, and `query.select` |
| `tenant_member` | Read only, including datasource metadata with the role ARN concealed | Explicit project grant required |
| `tenant_auditor` | Read only, including datasource metadata with the role ARN concealed | Explicit project grant required |
| `service` | No canonical admin or datasource access; legacy admin scopes are ignored and canonical key issuance rejects them | Explicit project grant plus server-held action scope required |
| `platform_admin` | Platform resources; tenant control-plane access requires `X-Axon-Break-Glass-Reason` | No ordinary project data-plane access is exposed |

Platform-global resources include models, health, architecture, catalogue and
pricing drift, production readiness, and region topology. Tenant viewers can
read platform views that do not impose a stricter handler check; platform writes
and region topology are restricted. Canonical viewers cannot use legacy admin
roles/scopes to mutate, and canonical services cannot use them to enter the
control plane. Legacy `admin` roles and `admin:*` scopes remain compatibility
only for noncanonical migration mode.

`query.select` gates both normal-Starlette `POST /v1/query` and the AgentCore
`query` action. Both use the same canonical `QueryService`. `query.mutate`
always denies.

## Query And Shared Control Plane

Datasource administration is exposed at `/admin/datasources`. Records contain
tenant/project ownership, display metadata, exact IAM role ARN, region,
catalog, database, workgroup, enabled state, timestamps, and a CAS revision.
They contain no credentials. Create/update/delete require `tenant_admin`; member
and auditor reads conceal the role ARN; `service` is denied. Lists accept
`limit` 1-100 and an opaque continuation cursor. Creates transactionally enforce
the tenant datasource cap. Mutations append durable redacted request/result
audit records; role changes are represented by hashes rather than raw ARNs.

Query execution requires all of these gates:

1. Canonical `query.select` authority and an explicit project grant.
2. A tenant/project-owned, enabled datasource.
3. An exact deployment binding for `(tenant_id, project_id, role_arn)`.
4. One Athena `SELECT` AST accepted by `sqlglot`; multiple statements, DDL,
   DML, commands, `SELECT INTO`, table functions, and out-of-datasource
   catalog/database references are rejected.
5. An enabled workgroup that enforces configuration, publishes CloudWatch
   metrics, uses an enforced KMS-encrypted S3 result location, and has a
   positive scan cutoff no greater than AxonLLM's configured ceiling.
6. Fleet-wide principal/project RPM, expiring concurrency slots, and aggregate
   scan-byte reservation capacity in the canonical DynamoDB table.

Defaults are 30 seconds, 1,000 rows, a 1 MiB compact serialized
columns-and-rows result set including JSON structure/nulls, and 1 GiB scanned.
Admission defaults are 30 project RPM, 10 principal RPM, five project slots,
two principal slots, 5 GiB project scan reservation per minute, and 2 GiB per
principal. `request_id` is unique within the tenant/project. Accepted state,
Athena execution id, terminal status, and actual scan bytes are durable. Query
request, result, and rejection audit records store a query hash, not SQL
literals. Workgroups are validated immediately before execution; AgentCore
`/ready` does not enumerate datasource roles or validate workgroups.

A fenced periodic reconciler claims expired accepted/running lifecycle records
across replicas. It closes pre-execution interruptions, cancels or observes known
Athena executions, atomically reconciles admission slots and scan reservations,
and retries pending terminal audit writes. It defers a running record when the
datasource or exact deployment binding cannot be re-established; alert on
repeated deferrals and restore authority before expecting closure.

The AgentCore runtime role and private STS endpoint permit
`sts:AssumeRole`, `sts:TagSession`, and `sts:SetSourceIdentity` only for exact
configured datasource role ARNs. The datasource role trust must name the exact
AgentCore runtime execution role and permit all three actions. Its Athena
permissions should be restricted to the intended workgroup, catalog/database,
tables, result bucket, and KMS key.

The execution role is deterministically named
`axonllm-agentcore-runtime-<region>`. Operators can preconfigure trust to the
exact account/region ARN before deployment, then verify it against the
`RuntimeExecutionRoleArn` stack output printed by the first-adopter deployer.

Managed Cognito also deploys `AxonLLMControlPlaneStack`, a verified AMD64 image
on private Fargate tasks. The default custom-domain mode uses a
Cognito-authenticated public TLS ALB and stable Route 53 alias. CloudFront mode
uses an AWS-generated hostname and certificate, IPv4 WAF allowlist, VPC origin,
internal ALB, and application-managed Cognito PKCE sessions. Both use
AgentCore's verified `StateTableName` output and import the data key, outbox,
SNS topic, and CloudWatch event log.
`AXON_CONTROL_PLANE_ONLY=true` suppresses chat, model, OpenAI-compatible, and
query execution routes. The task receives binding metadata for datasource
validation but has no Athena or STS authority.

See [Features And Flows](FEATURES_AND_FLOWS.md) for request sequences.

## Release Foundation

`infra/release_foundation_stack.py` is restricted to `us-east-1` and creates:

- retained `axonllm/fargate` and `axonllm/agentcore` private ECR repositories;
- immutable tags, scan-on-push, a retained rotation-enabled ECR KMS key, and
  retained P-256 asymmetric release-signing keys with version aliases named
  `alias/axonllm/release-signing-v*`;
- a versioned, customer-KMS-encrypted, Object-Locked deployment-evidence bucket
  with separate prerequisite, transition-intent, transition-terminal, and
  storage keys;
- an account-global GitHub Actions OIDC provider;
- an `AxonLLMReleaseFoundationDeployRole` trusted only by the protected
  `release-foundation` environment and permitted to enter only the dedicated
  `axrel` CDK trust domain;
- an `AxonLLMReleaseSigner` role trusted only by `v*` tag refs and permitted
  only to sign and verify with the release-signing key;
- an `AxonLLMReleasePublisher` role trusted only by the protected GitHub
  `release` environment;
- an `AxonLLMReleaseVerifier` role trusted only by the protected GitHub
  `production` environment;
- an `AxonLLMOperationsAudit` role that can read recovery, secret-version, and
  key-rotation metadata but cannot read secret values or restore data;
- an `AxonLLMOperationsRecovery` role that can run PITR validation and remove
  only temporary `*-restore-validation-*` tables;
- separate AgentCore qualification, external-certification, launch-gates,
  rehearsal-evidence, production-deploy, and transition-watchdog roles;
- a retained, versioned Step Functions launch coordinator with fenced leases,
  a rehearsal-control ledger, a qualification-mutation authorization table,
  action/cleanup activities and worker roles, service-generated runtime
  identity, scheduled cleanup/watchdog invocations, KMS encryption, alarms,
  and a scheduler DLQ;
- retained numeric versions of qualification and production mutation brokers;
  and
- a retained terminal-record signing key that the watchdog uses independently
  from the transition-intent key.

The signer cannot access ECR. The publisher can verify evidence and upload and
read image layers, but cannot sign or delete images or repositories. The
verifier can verify signatures and read images but cannot sign or write. Both
operations roles trust only the exact protected `production` environment
subject.

The foundation does not use the account's shared `hnb659fds` CDK execution
role. It uses the `axrel` qualifier, five repository-generated execution
policies, a boundary on the CloudFormation execution and CDK deploy roles, and
a separate boundary on all foundation service roles. Do not remove or modify
the shared `hnb659fds` roles while other applications, including AgentLasso,
still use them.

The IAM trust subjects use GitHub's immutable organization and repository IDs,
not rename-sensitive names. Before deploying after a repository transfer,
compare `_GITHUB_SUBJECT_PREFIX` with:

```bash
gh api repos/AxonLLM/axonllm/actions/oidc/customization/sub
```

The returned `sub_claim_prefix` must match exactly.

### One-Time `axrel` Migration

The steady-state workflow cannot perform the first migration because its OIDC
role is created by the foundation stack. From a reviewed `main` commit, use a
time-limited privileged operator and Node 22 for this one migration only:

```bash
npm ci \
  --prefix src/gateway/deployment/infra \
  --ignore-scripts \
  --no-audit \
  --no-fund
uv run --frozen --no-sync python \
  -m src.gateway.deployment.release_foundation_bootstrap \
  install --apply --region us-east-1
```

The installer creates or updates the seven repository-owned managed policies,
bootstraps `AxonLLMToolkit-axrel`, applies the deploy-role boundary omitted by
the stock CDK template, and verifies exact trust, attached policy, inline
policy, boundary, tag, toolkit, and bootstrap-version contracts. It retains
the previous managed-policy version for rollback. It never changes the shared
`hnb659fds` toolkit.

Create a non-executing change set with the repository-pinned CLI. Include all
ten business parameters and explicitly migrate `BootstrapVersion`:

```bash
cd infra
../src/gateway/deployment/infra/node_modules/.bin/cdk deploy \
  AxonLLMReleaseFoundationStack \
  --app ".venv/bin/python app.py" \
  -c deployment_target=release-foundation \
  -c region=us-east-1 \
  --lookups false \
  --method prepare-change-set \
  --change-set-name AxonLLMReleaseFoundation-initial-axrel \
  --require-approval never \
  --parameters AxonLLMReleaseFoundationStack:FargateStateTableName=axonllm-state \
  --parameters AxonLLMReleaseFoundationStack:AgentCoreStateTableName=axonllm-agentcore-state \
  --parameters AxonLLMReleaseFoundationStack:ProductionProviderSourceSecretArn="$PRODUCTION_PROVIDER_SOURCE_SECRET_ARN" \
  --parameters AxonLLMReleaseFoundationStack:ProductionProviderSourceKmsKeyArn="$PRODUCTION_PROVIDER_SOURCE_KMS_KEY_ARN" \
  --parameters AxonLLMReleaseFoundationStack:QualificationProviderSourceSecretArn="$QUALIFICATION_PROVIDER_SOURCE_SECRET_ARN" \
  --parameters AxonLLMReleaseFoundationStack:QualificationProviderSourceKmsKeyArn="$QUALIFICATION_PROVIDER_SOURCE_KMS_KEY_ARN" \
  --parameters AxonLLMReleaseFoundationStack:ExternalOidcProviderSourceSecretArn="$EXTERNAL_PROVIDER_SOURCE_SECRET_ARN" \
  --parameters AxonLLMReleaseFoundationStack:ExternalOidcProviderSourceKmsKeyArn="$EXTERNAL_PROVIDER_SOURCE_KMS_KEY_ARN" \
  --parameters AxonLLMReleaseFoundationStack:GitHubOidcSubjectPrefix="$GITHUB_OIDC_SUBJECT_PREFIX" \
  --parameters AxonLLMReleaseFoundationStack:LaunchAlarmEmail="$LAUNCH_ALARM_EMAIL" \
  --parameters AxonLLMReleaseFoundationStack:BootstrapVersion=/cdk-bootstrap/axrel/version
```

Review the complete change set, especially replacements, IAM trust, KMS key
policies, retained resources, tags, and permissions boundaries. Execute only
the reviewed change set, wait for `UPDATE_COMPLETE`, then require:

```bash
aws cloudformation execute-change-set \
  --stack-name AxonLLMReleaseFoundationStack \
  --change-set-name AxonLLMReleaseFoundation-initial-axrel
aws cloudformation wait stack-update-complete \
  --stack-name AxonLLMReleaseFoundationStack
cd ..
uv run --frozen --no-sync python \
  -m src.gateway.deployment.release_foundation_bootstrap \
  verify --region us-east-1
```

Confirm that the foundation stack's `RoleARN` is
`cdk-axrel-cfn-exec-role-<account>-us-east-1`, all 18 service roles carry the
`axrel` boundary and trust-domain tag, and no `axrel` role has
`AdministratorAccess`. If migration fails, inspect stack events before using
`continue-update-rollback`; do not delete retained keys, evidence, or policy
versions.

If either runtime uses a nondefault physical table name, pass the same name to
the foundation parameter. The operations IAM policies are generated from these
parameters and will not reach a different table.

Create nine protected GitHub environments:

- `release-foundation`: require approval and restrict deployment refs to
  protected `main`;
- `release`: require approval and restrict deployment refs to version tags;
- `production`: require approval and restrict deployment refs to protected
  production branches and tags;
- `agentcore-qualification`: protect the isolated managed-Cognito staging and
  teardown authority;
- `agentcore-external-oidc-production-like`: protect the external fixture
  broker and isolated external certification authority;
- `agentcore-production-launch-gates`: protect coordinator execution;
- `agentcore-production-evidence`: protect prerequisite evidence publication;
- `agentcore-production-deploy`: protect production mutation; and
- `agentcore-production-watchdog`: protect independent transition
  reconciliation.

Create an active repository tag ruleset targeting `refs/tags/v*`. Restrict tag
creation to the release-manager bypass list and block tag updates and deletion.
The signer role trusts version-tag OIDC subjects directly, so environment
protection does not substitute for this tag ruleset.

Required reviewers and wait timers for private repositories depend on the
organization's GitHub plan. Verify that GitHub actually returns the reviewer
rule after configuration. A custom branch/tag policy by itself is not an
approval gate; if the plan rejects reviewers, upgrade the plan before claiming
that either environment has separation-of-duties approval.

Configure these repository variables for the tag-triggered signing workflow:

| Name | Value |
|---|---|
| `AXON_RELEASE_SIGN_ROLE_ARN` | `ReleaseSignerRoleArn` output |
| `AXON_RELEASE_SIGNING_KEY_ARN` | Current exact `ReleaseSigningKeyArn` output; never an alias |
| `AXON_AWS_ACCOUNT_ID` | Twelve-digit deployment account |

After the one-time migration, configure `release-foundation` with the
`AXON_RELEASE_FOUNDATION_DEPLOY_ROLE_ARN` secret from
`ReleaseFoundationDeployRoleArn` and the `AXON_AWS_ACCOUNT_ID` variable. Only
then enable `.github/workflows/deploy-release-foundation.yml`. Every dispatch
must name an approved change and use a successful `main` push CI run. Dispatch
`prepare` first, record and review the complete
`AxonLLMReleaseFoundation-<commit>` change set, then dispatch `execute` from
the same commit and change approval. The execute dispatch rejects absent,
stale, already-run, differently scoped, or non-`axrel` change sets. It
preserves the ten live business parameters, forces the `axrel` bootstrap
parameter, and finishes by verifying the stack execution role and bootstrap
contract.

Configure `release` with:

| Kind | Name | Value |
|---|---|---|
| Secret | `AXON_RELEASE_PUBLISH_ROLE_ARN` | `ReleasePublisherRoleArn` output |
| Variable | `AXON_AWS_ACCOUNT_ID` | Twelve-digit deployment account |
| Variable | `AXON_FARGATE_ECR_REPOSITORY` | `FargateRepositoryUri` output |
| Variable | `AXON_AGENTCORE_ECR_REPOSITORY` | `AgentCoreRepositoryUri` output |

Configure `production` with `AXON_AWS_ACCOUNT_ID`. Set
`AXON_RELEASE_VERIFY_ROLE_ARN` to the `ReleaseVerifierRoleArn` output,
`AXON_OPERATIONS_AUDIT_ROLE_ARN` to
`OperationsAuditRoleArn`, and `AXON_OPERATIONS_RECOVERY_ROLE_ARN` to
`OperationsRecoveryRoleArn`. Also set `AXON_FARGATE_STATE_TABLE_NAME` and
`AXON_AGENTCORE_STATE_TABLE_NAME` to the physical names supplied to the
foundation stack. The target data-key variables described in
[Backup And Restore](#backup-and-restore) are additional production-environment
settings.

AgentCore launch additionally requires the qualification,
external-certification, launch-gates, rehearsal-evidence, production-deploy,
and transition-watchdog roles; reviewed S3 document locations; provider source
secret; evidence bucket and prefixes; distinct deployment signing/storage KMS
keys; external fixture broker settings; and self-hosted runner controls listed
in
[Protected Launch Prerequisites](AGENTCORE_RUNBOOK.md#protected-launch-prerequisites).
Treat that list as part of the release foundation. The production runner must
have Playwright Chromium system libraries and network access to the deployed
control plane and Cognito; installing the Python package and browser binary
alone is insufficient. Store the watchdog role ARN in the protected
`agentcore-production-watchdog` secret
`AXON_AGENTCORE_TRANSITION_WATCHDOG_ROLE_ARN`. Configure the exact intent key,
terminal key, and numeric broker-version outputs as documented in
[Protected Launch Prerequisites](AGENTCORE_RUNBOOK.md#protected-launch-prerequisites).

`AXON_RELEASE_SIGNING_KEY_ARN` is a repository variable used only by the
tag-producing signer. Publication and production do not read it. Their trust
root is `AXON_AWS_ACCOUNT_ID` plus retained KMS aliases matching
`alias/axonllm/release-signing-v*`. Each consumer obtains the exact key ARN from
the schema-v3 manifest, requires that ARN to belong to the trusted account and
to be the target of one of those version aliases, and only then verifies both
KMS signatures.

Rotate the asymmetric signing key through a reviewed migration:

1. Create the new retained P-256 signing key.
2. Create the next retained version alias, such as
   `alias/axonllm/release-signing-v2`, and point it to the new key before
   changing `AXON_RELEASE_SIGNING_KEY_ARN`.
3. Set the repository variable to the new key's exact ARN and cut a new release
   tag. The manifest records that exact ARN.
4. Retain every historical signing key and version alias. Never repoint or
   delete an existing `release-signing-v*` alias; doing so invalidates the trust
   record needed to verify rollback evidence.

## Fargate Deployment

`infra/stack.py` is restricted to `us-east-1` and defines:

- CloudFront and WAF in front of an internal TLS ALB;
- private Fargate tasks with customer-managed HTTPS egress;
- Bedrock invocation IAM restricted to required concrete model/profile ARNs;
- ALB OIDC for `/admin/*` in `DeploymentMode=production`;
- canonical identity and enforced authentication in production mode;
- a private ECR image parameter that accepts only `@sha256` URIs;
- KMS-encrypted DynamoDB with deletion protection and PITR;
- KMS-signed routing snapshots with a private KMS endpoint and exact-key
  sign/verify task authority;
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
permissions. A cross-region inference profile requires its own ARN plus every
regional foundation-model ARN in `GetInferenceProfile.models`; omitting a
destination can produce an intermittent `AccessDenied` when Bedrock routes
there.

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
including an unknown provider fails startup. AgentCore supports thirteen
provider adapters but defaults to nine: Bedrock, Bedrock Mantle, Anthropic,
OpenAI, Google AI Studio, xAI, Groq, Together, and Fireworks. Direct `ai21`,
Azure OpenAI, Cohere, and Vertex AI must be explicitly enabled; AI21 Jamba 1.5
remains available through the default `bedrock` provider. Direct HTTP providers
are advertised only when their credentials load from the retained
KMS-encrypted `ProviderSecretArn`. Its runtime egress prefix list must cover
`bedrock-mantle.<region>.api.aws` and every credentialled provider hostname.
Mantle authenticates with SigV4 and does not use a provider secret.

The default Google path is Google AI Studio using `GOOGLE_AI_API_KEY` in the
`x-goog-api-key` header, never a URL key. It does not use Vertex. Optional
Vertex ignores static `GCP_ACCESS_TOKEN`; use ADC or a
`GCP_CREDENTIALS_JSON` service-account/AWS external-account document with
`GCP_PROJECT_ID` and `GCP_LOCATION`. Prefer AWS workload identity to a
long-lived service-account key. Google access tokens are initialized during
bounded startup and refreshed off the event loop; an unavailable token fails
requests closed.

## AgentCore First-Adopter Deployment

AgentCore has two production identity choices and no unauthenticated mode:

| Identity mode | Operator responsibility | Automated work |
|---|---|---|
| `managed-cognito` | Choose a hosted-UI prefix and AgentCore callback, verified ARM64 AgentCore and AMD64 control-plane digests, AgentCore/control-plane egress prefix lists, tenant/project/admin, Bedrock ARNs, at least one exact Athena role and certification datasource/workgroup, optional complete SCIM secret ARN, protected SAML landing path, tenant-specific Cognito SAML configuration, and one control-plane endpoint contract: custom domain/ACM/Route 53/ingress prefix list or generated CloudFront plus reviewed viewer CIDRs | Deploy retained/deletion-protected Cognito, invite the user, deploy AgentCore, bootstrap canonical authority, and deploy the shared-state web control plane |
| `external-oidc` | Provision the IdP user/client and supply exact issuer, discovery URL, client, audience, immutable subject, tenant/project claim names, runtime egress, Bedrock ARNs, and at least one exact Athena role plus certification datasource/workgroup | Deploy AgentCore and bootstrap canonical authority; expose read-only viewer and CAS administrator project-config actions, but no Cognito-authenticated web control plane |

Generate the strict schema-v2 setup file with `uv run axon setup agentcore`,
validate it with `./deploy-agentcore.sh --config FILE --validate-only`, then
deploy it. Managed-Cognito schema v2 requires the `control_plane` object;
regenerate or migrate schema-v1 files. Use `--bootstrap-cdk` only for the first
deployment in an account/region and add `--yes` only in reviewed
noninteractive automation. The setup rejects mutable image tags, wildcard
Bedrock ARNs, non-HTTPS identity metadata, client secrets, and missing claim
mappings.

For the first account/region bootstrap, use a separate IAM bootstrap principal
with permission to create/read the repository-owned
`AxonLLMAgentCoreCloudFormationExecution-<qualifier>-<region>-part1` through
`part3` policy set and create or update the isolated CDK bootstrap stack
resources. `--bootstrap-cdk` generates and verifies those exact bounded
policies, supplies all three as the only CloudFormation execution policies,
and enables bootstrap-stack termination protection. Routine deployment
verifies every canonical policy document and rejects missing, extra, or inline
policies on the CDK execution role. Do not bootstrap AgentCore with
`AdministratorAccess`, and do not give the one-time bootstrap principal to the
routine deployment workflow.

The repository policy includes bounded CloudFront distribution/function/VPC
origin and WAFv2 lifecycle actions plus condition-scoped CloudFront
service-linked-role creation. Accounts bootstrapped with an older policy must
run the reviewed bootstrap update before selecting `cloudfront`; the normal
deployer rejects the stale policy rather than broadening itself.

Before changing AWS resources, the wrapper resolves every supplied managed
prefix list and requires a stable, nonempty, customer-owned IPv4 list in the
deployment account. Entries must be strict globally routable CIDRs. Runtime and
control-plane HTTPS egress allow `/16` or narrower CIDRs and no more than
1,048,576 total addresses per list; control-plane ingress allows `/24` or
narrower CIDRs and no more than 65,536 total addresses. AWS-owned lists, IPv6,
private/reserved ranges, and broader entries are rejected. Re-resolve every
hostname's A records after provider, IdP, Mantle, ALB-key, or Cognito DNS
changes.

The reusable schema and stack can omit Athena, but that configuration is not
launchable through the current protected AgentCore workflow. Fixture
preparation requires the certification datasource role to match a reviewed
Athena binding, and certification always runs a governed `SELECT`; there is no
query-disabled certification mode.

The wrapper reads the deployed AgentCore stack's verified `StateTableName`
output, passes it as the control plane's required `PrimaryStateTableName`
parameter, and compares the resulting outputs. A manual control-plane
deployment must pass that exact output; `PrimaryStateTableName` has no default.

The managed AgentCore client is authorization-code only and has no client
secret. The adopting application must use S256 PKCE and submit the Cognito
**ID token** to AgentCore because that token carries `custom:tenant_id` and
`custom:project_id`. The custom attributes select resources but grant no role;
canonical DynamoDB principals remain authoritative.

The public AgentCore client is secretless. The endpoint contract is:

| `control_plane.endpoint_mode` | Ingress and identity contract |
|---|---|
| `custom-domain` (default) | Stable lowercase DNS name, regional ACM certificate, containing public Route 53 zone, ingress prefix list, confidential ALB Cognito client, and internet-facing TLS ALB |
| `cloudfront` | `us-east-1`, one or more public canonical IPv4 viewer networks no broader than `/24`, generated CloudFront hostname/certificate, WAF default deny and rate limit, IPv6 disabled, VPC origin, internal ALB, and a secretless Cognito browser client |

Both modes require the control-plane HTTPS egress prefix list. CloudFront mode
forbids the four custom-domain fields. An existing retained stack cannot change
endpoint mode in place; legacy stacks without the parameter are
`custom-domain`. Use a new reviewed namespace/stack for an architecture
migration.

The AWS-managed `cloudfront.net` certificate does not permit a custom minimum
viewer TLS policy. Select `custom-domain` when compliance requires enforcing
TLS 1.2 or newer at the viewer boundary.

When configured, `control_plane.scim_tenants_secret_arn` injects the complete
`AXON_SCIM_TENANTS` value. Only the execution role and scoped private Secrets
Manager endpoint can read that complete ARN. There is no SAML secret injection.
The stack sets `AXON_SAML_FEDERATION_MODE=managed-cognito` and maps the validated
`control_plane.saml_login_path` to `AXON_SAML_LOGIN_PATH`, defaulting to
`/admin/dashboard`.

In custom-domain mode only `/scim/*` bypasses ALB Cognito and the ALB owns the
browser session. In CloudFront mode every request traverses CloudFront/WAF;
AxonLLM performs authorization code with S256 PKCE, binds one-time state to the
initiating browser with a short-lived host-only cookie, validates the Cognito
ID token and nonce, and stores only an opaque host-only browser session cookie
backed by a revisioned, expiring DynamoDB row. Unsafe cookie requests require
the double-submit CSRF token. Cognito remains the SAML service provider and owns
assertion signature, issuer, audience, destination, recipient, time,
request-correlation, replay, and RelayState validation. `/saml/acs` and
`/saml/metadata` return `410` and never accept direct-SP traffic.

The first-adopter deployer does not ingest tenant-specific IdP metadata. Before
launch, configure the SAML IdP on the retained Cognito pool and enable it on the
confidential ALB client for custom-domain or the generated browser client for
CloudFront. Enable it on the public AgentCore client when federated users invoke
AgentCore. Configure the enterprise IdP with Cognito's SP entity ID and SAML
response endpoint. Provision every canonical user with the exact Cognito issuer
and Cognito `sub`; if SCIM creates the user, those values must be its tenant
issuer and `externalId`. SAML claims, groups, roles, tenant values, and project
values never grant authority. The production canary gate requires the
Cognito-native provider and accepts additional browser-client providers only
when Cognito reports each one as configured SAML.

The complete commands, external-IdP example, manual CDK fallback, and invitation
behavior are in the
[AgentCore Runbook](AGENTCORE_RUNBOOK.md#first-adopter-setup). Before traffic,
also configure alarm/event subscriptions, run authenticated and negative RBAC
canaries, and retain restore evidence. Successful setup is not production
certification.

The AgentCore config actions cover runtime project settings only. They do not
administer principals, project grants, datasources, API keys, Cedar policies,
webhooks, provider secrets, or security-event destinations. Managed Cognito
uses the shared web control plane for those resources; an external-OIDC adopter
must provide a separately trusted operator/control-plane path where needed.

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

The Fargate alarm topic and both stacks' security-event topics require
operator-configured receivers. The AgentCore stack automatically requests an
email subscription for the reviewed administrator address, and its deployer
fails until that exact subscription is confirmed. Configure and exercise tenant
security-event destinations before traffic.

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
- creates a schema-v3 manifest and multi-target SLSA provenance record with
  distinct Fargate and AgentCore image digests;
- signs both records with the retained asymmetric AWS KMS key and immediately
  verifies the signatures;
- stores private evidence for 90 days.

`.github/workflows/publish-release.yml` is the controlled publication step. It
runs only in the protected `release` environment, validates the exact tag,
commit, release workflow run and attempt, successful CI, signed manifest, both
target identities, KMS signatures, and fixed account/region repository names.
It then uses a checksum-pinned ORAS client to copy each signed OCI digest
without rebuilding, verifies both remote digests, and emits immutable
`@sha256` references. Existing immutable tags are accepted only when their
digest already matches; AWS lookup failures fail closed.

`.github/workflows/deploy-verification.yml` requires a release tag, successful
CI for the exact commit, signed evidence, an immutable private ECR digest, and a
fresh image scan. Its `target` input selects `fargate` or `agentcore`; the
schema-v3 verifier binds the supplied digest to the selected target, source
commit, release tag, workflow run, platform, manifest, and SLSA provenance.
The workflow verifies both KMS signatures, the selected target's remote
private ECR digest, and then rescans that exact image.

Publication and deployment take the exact signing key ARN from the manifest,
not from the current signer variable. They accept it only when its account is
`AXON_AWS_ACCOUNT_ID` and a retained
`alias/axonllm/release-signing-v*` resolves to that exact key.

This KMS-backed flow works for a private repository without GitHub's paid
artifact-attestation API and does not send private release identities or
artifact hashes to a public transparency log.

Never deploy a mutable tag or bypass a failing CI/evidence check. Record the
commit, release tag, workflow run, ECR URI and digest, SBOMs, scan results,
KMS signature verification, selected target, approvals, and canary results. The
release, publication, and target-verification workflows do not deploy a
runtime. AgentCore production promotion is a separate protected workflow with
candidate certification and signed deployment evidence. `v0.2.4` has retained
KMS/private-ECR workflow evidence as listed in [Release Status](#release-status);
repeat the complete release and deployment-evidence flow for every promoted
release.

Dispatch `.github/workflows/launch-agentcore-production.yml` from protected
`main`; it is the only manual production-launch entry point.
`deploy-agentcore-production.yml` is `workflow_call` only. Before dispatch,
pre-stage namespace `managed` with the exact release and setup, certify and
promote its candidate, deploy its control plane, and collect its exact
stack/runtime/resource bindings. An independent reviewer must put those values
into the schema-v2 gate configuration, upload a versioned copy, and approve a
review window no longer than 48 hours. Teardown deletes that namespace, so
pre-stage and review must be repeated for every later launch. See
[Reviewed Gate-Config Pre-Stage](AGENTCORE_RUNBOOK.md#reviewed-gate-config-pre-stage)
for the complete procedure and example document.

Before production mutation, the orchestrator produces three additive signed
inputs. The detailed launch-rehearsal report must cover all seven gates:
initialization replacement, query boundaries/reconciliation, recovery
cutover/rollback, security-event delivery/DLQ, provider routing, provider
fallback/recovery, and control-plane fault recovery. The external-OIDC
schema-v3 report must prove issuer/JWKS validation, completion and streaming
for every launch provider, tools for each provider that declares
`tool_calling`, governed query, viewer write denial, and an administrator
tenant-config mutate-confirm-rollback flow. The qualification-teardown receipt
must prove both fixture sets existed, their identities were revoked, exactly
two launch workers stopped, and all four `managed` qualification stacks are
absent.

A provider declaring tool support must separately prove automatic selection,
required selection, tool-result continuation, `tool_choice=none`, and streamed
tool calling. Cohere passes the required-selection gate only by AxonLLM
rejecting its unsupported required/named selection before provider invocation
with sanitized `400 unsupported_provider_feature`. Production consumes every
report and KMS signature by exact S3 URI, VersionId, SHA-256, checksum, KMS
identity, content metadata, encryption posture, and COMPLIANCE Object Lock
retention; a compatibility projection or mutable/latest object is not accepted.
Final schema-v5 deployment evidence embeds normalized references and content
for all three prerequisite evidence sets.

The candidate name contains 128 random bits and is a temporary bearer
capability in addition to the shared runtime JWT. It limits discoverability but
does not provide endpoint-specific authorization: any principal with an
accepted runtime JWT can invoke the candidate after learning its qualifier.
Keep candidates short-lived; use a separate runtime or qualifier-aware
authorization when an independent candidate boundary is required.

The independent
`.github/workflows/reconcile-agentcore-production-transition.yml` watchdog must
be enabled before promotion. It runs after deployment completion, every five
minutes, and manually under the deployment concurrency lock. Using the separate
`AXON_AGENTCORE_TRANSITION_WATCHDOG_ROLE_ARN`, it finds a single signed
nonterminal journal, verifies deployment evidence with the intent key, invokes
the exact numeric production mutation-broker version until the bounded
transition is complete, then appends a terminal record signed by the distinct
terminal key. The watchdog itself cannot mutate CloudFormation, pass roles, or
change load-balancer attributes. This covers runner loss and cancellation paths
in which the deployment job cannot execute cleanup.

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
4. Run authenticated model-list, completion, and stream canaries for every
   configured launch provider. Run the five-part tool contract for each
   provider that declares `tool_calling`.
5. Run negative canaries for missing credentials, inactive membership,
   ungranted and cross-tenant project claims, and viewer writes.
6. Through AgentCore, read the canonical tenant-project config as admin and
   viewer, deny the viewer update, then perform and confirm an admin CAS
   mutation and rollback. Require the original value after rollback.
7. For AgentCore production launch, run AgentCore `SELECT` canaries plus
   mutation, cross-project, missing-scope, unbound-role, unsafe-workgroup,
   row/result/scan-limit, audit-record, interrupted-lifecycle reconciliation,
   and missing-authority deferral checks. Also run the HTTP `SELECT` canary
   when launching the normal Starlette data plane. AgentCore query
   certification is mandatory, not an optional launch gate.
8. For managed Cognito, verify S256 PKCE, required TOTP enrollment, the exact
   ID-token issuer/audience/tenant/project claims, and access-token rejection.
9. Verify the shared control-plane canonical URL, datasource RBAC, shared state,
   suppressed execution routes, and lack of Athena/STS task authority. For
   custom-domain, verify DNS/TLS, ALB Cognito, and ingress restrictions. For
   CloudFront, verify WAF allow/deny and rate-limit behavior, IPv6 disabled,
   cache disabled, VPC-origin/internal-ALB isolation, stripped viewer identity
   headers, cross-replica opaque-session refresh, CSRF denial, and POST logout.
   For SAML, exercise signed and invalid assertions at Cognito,
   issuer/audience/destination/recipient/time failures, request correlation,
   replay rejection, safe return handling, certificate rollover, exact Cognito
   issuer/`sub` canonical lookup, the configured local landing path, and direct
   ACS and metadata `410` responses. Skip this only for external OIDC, which
   does not deploy the control plane.
10. Verify alarms, a confirmed SNS subscription, logs, tenant audit-chain
    verification, and rollback.
11. Verify the independent transition watchdog can assume its protected role
    and retain a signed terminal record for a rehearsal commit or compensation.

AgentCore `/ready` does not enumerate datasource roles or validate Athena
workgroups. Workgroup validation occurs immediately before each execution, so
an authenticated query canary is required. The production validation tool's
`query.mutate` policy check alone is not a remote query canary.

AgentCore creates and verifies its administrator-email alarm subscription.
Fargate alarms and both stacks' tenant security-event topics still require
configured and tested receivers before launch.

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

   `production_validation.example.json` is the custom-domain/ALB-cookie
   contract. For `EndpointMode=cloudfront`, use the checked
   `scripts/operations/production_validation.cloudfront.example.json`; every
   request in that file requires `browser-session-cookie`. The session
   preparation command resolves `EndpointMode`, drives the appropriate Cognito
   browser flow with Playwright, and exports only owner-readable cookie and CSRF
   environment files. The workflow rejects a validation config whose cookie
   type does not match the deployed endpoint.

   Customize the selected example with real tenant/project fixture identities
   and credential environment names. It requires an authenticated read,
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
`--stack-name AxonLLMAgentCoreStack`; a manual dispatch can set
`retain_agentcore_restore=true`.

AgentCore uses its own four-phase selector and
`scripts/operations/agentcore_recovery.py`, not the Fargate helper. It removes
the production endpoint, explicitly denies runtime DynamoDB access, blocks JWT
invocation, quiesces the shared control plane, and waits the four-hour maximum
session lifetime plus five minutes before permitting a reviewed table switch.
The switch updates the runtime environment, exact table IAM, VPC endpoint
policy, metrics, and backup selection while access remains denied. A separate
`recovery` endpoint is then created for canaries before promotion.

The finite operator sequence is:

```bash
python scripts/operations/agentcore_recovery.py \
  quiesce --state-file agentcore-cutover.json --approval-id CHG-2026-001
python scripts/operations/agentcore_recovery.py \
  select --state-file agentcore-cutover.json \
  --expected-table "$RESTORED_TABLE_NAME"
python scripts/operations/agentcore_recovery.py \
  start --state-file agentcore-cutover.json \
  --expected-table "$RESTORED_TABLE_NAME"
# Retain authenticated recovery-endpoint canary evidence.
python scripts/operations/agentcore_recovery.py \
  promote --state-file agentcore-cutover.json \
  --expected-table "$RESTORED_TABLE_NAME"
python scripts/operations/agentcore_recovery.py \
  resume-control-plane --state-file agentcore-cutover.json
```

Use a new state file and approval to repeat the same sequence back to the
primary table, then run `resume-control-plane` with that rollback state file
before cleaning up the restore. The helper updates only the deployed reviewed
template and refuses stacks without a CloudFormation execution role. See
[AgentCore Backup And Recovery](AGENTCORE_RUNBOOK.md#backup-and-recovery) for
phase invariants, IAM separation, abort, rollback, and cleanup commands.

The managed web control plane now follows the reviewed AgentCore-selected
primary or restored table through its environment, IAM, transaction and
endpoint policies. Both planes remain stopped and explicitly denied state
access during selection; resume is allowed only after they agree on the same
table and recovery ownership.

The protected AgentCore deployment fails unless AWS completes a fresh backup
and PITR restore, up to 25 restored items match strongly consistent source
reads, the temporary table is removed, and that proof is bound into signed
deployment evidence. The scheduled exercises provide separate recurring
coverage. A full application cutover to a retained restored table and back is a
distinct recovery rehearsal; retain its evidence before launch.

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
3. Rotate affected provider secrets and force new ECS tasks or publish a new
   AgentCore runtime version because provider secrets are read at startup.
4. Rotate or disable IdP clients/signing keys in the IdP and Cognito, and rotate
   tenant SCIM tokens when implicated.
5. Verify and export the affected tenant audit chain.
6. Re-run positive and negative authorization canaries before restoring traffic.

For external-OIDC certification, rotate the broker bearer credential
independently from IdP signing keys. Install the replacement at the broker,
update `AXON_EXTERNAL_OIDC_FIXTURE_BROKER_TOKEN` in the protected environment,
run certification through successful fixture cleanup, then revoke the old
credential. During IdP signing-key rotation, publish both keys in JWKS for at
least the maximum 15-minute fixture lifetime, verify fresh discovery/JWKS cache
headers, and rerun external certification before removing the old key.
Re-resolve and review both runner and AgentCore prefix-list destinations after
an IdP, provider, Mantle, Cognito, or control-plane DNS change.

Validate Fargate provider-secret posture with:

```bash
python scripts/operations/check_secret_rotation.py \
  --secret-id "$PROVIDER_SECRET_ARN"
python scripts/operations/check_secret_rotation.py \
  --secret-id "$PROVIDER_SECRET_ARN" \
  --require-automatic-rotation
```

The script inspects secret metadata, KMS rotation, version age, and pending
versions without reading secret content. Pass either the Fargate or AgentCore
`ProviderSecretArn`. It does not rotate secrets or prove that individual
provider fields are populated; live provider canaries are required.
