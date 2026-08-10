# AxonLLM Enterprise Hardening Status

This document describes the current repository as of 2026-08-07. It separates
controls implemented in the repository from deployment and operational evidence
that must still be produced in the target AWS account.

Use these documents for detailed procedures:

- [Production runbook](docs/PRODUCTION_RUNBOOK.md)
- [AgentCore runbook](docs/AGENTCORE_RUNBOOK.md)
- [Feature catalog](README.md#features)
- [Architecture and request flow](README.md#architecture)
- [Product requirements](docs/PRD.md)

## Release Status

The repository now contains the code-level controls required for a shared
multi-tenant deployment:

- canonical, server-held tenant identity backed by strongly consistent DynamoDB
  reads;
- tenant-qualified control-plane and data-plane persistence;
- role and action enforcement for tenant administrators, members, auditors,
  services, and platform administrators;
- project ownership checks before project grants are evaluated;
- canonical API-key and SCIM lifecycle integration;
- tenant-qualified quotas, policies, usage, audit, webhooks, and caches;
- durable, conditional multi-instance state updates and refresh;
- tenant-bound security-event delivery through a KMS/TLS FIFO SQS outbox,
  bounded retries, native DLQ redrive, and managed SNS/Logs allowlists;
- Fargate and AgentCore CDK stacks with private networking, encryption,
  retained backups, alarms, immutable image parameters, and production runtime
  profiles; and
- locked CI, release-evidence, deployment-verification, recovery, and security
  workflows for both deployment targets.

This is not production certification. Promotion still requires green protected
CI for the exact release commit, target-account configuration, verified release
evidence for the deployed image digest, authenticated canaries, and retained
recovery evidence.

## Production Contract

Every production runtime must start with:

```bash
AXON_DEPLOYMENT_PROFILE=production
AXON_AUTH_MODE=ENFORCE
AXON_REQUIRE_CANONICAL_IDENTITY=true
AXON_LOAD_DEMO_DATA=false
LLM_ROUTER_DYNAMODB_ENABLED=true
```

`AppConfig` rejects a production profile that does not enforce authentication,
canonical identity, and DynamoDB persistence. The Fargate production mode and
AgentCore stack set this contract explicitly. Docker Compose selects the
development profile and is not a production deployment path.

Direct OIDC also requires an exact HTTPS issuer and audience. Fargate production
mode requires the ALB OIDC endpoints, client identity, client secret, issuer,
audience, signer, and listener integration configured by the stack. AgentCore
requires its JWT authorizer inputs and independently verifies the forwarded
token before resolving canonical authority.

`AXON_ENABLED_PROVIDERS` is an optional comma-separated runtime provider
allowlist. Providers outside it are neither advertised nor invoked, and empty or
unknown values fail startup. The AgentCore stack sets it to `bedrock`, limiting
that target to standard Bedrock mappings even if other provider metadata or
credentials are present.

Both AWS stacks also inject the deployment account, FIFO outbox queue URL, and
exact managed SNS and CloudWatch destination ARNs. Queue access participates in
readiness. Those ARN values are allowlists, not tenant destination
configuration; administrators still create the desired destinations through
the tenant control plane.

## Canonical Identity

Authentication verifies the credential first and extracts only identity hints.
Canonical resolution then replaces credential-provided tenant, principal, role,
scope, membership, project grants, and authorization version with the
authoritative DynamoDB principal.

A canonical principal contains:

| Field | Requirement |
|---|---|
| `issuer`, `subject` | Exact verified credential identity |
| `tenant_id`, `principal_id` | Non-empty server-owned identifiers |
| `roles` | Canonical `TenantRole` values |
| `membership_status` | `active` to authorize |
| `project_ids` | Explicit grants for project-scoped data-plane actions |
| `scopes` | Server-held action scopes for service principals |
| `authorization_version` | Positive version used by conditional updates |

Missing, inactive, malformed, ambiguous, or mismatched canonical records deny
access. An unavailable authority store returns `503`; it does not fall back to
token claims or legacy in-memory authority.

## RBAC Model

| Role | Effective access |
|---|---|
| `tenant_member` | Tenant configuration reads plus granted-project model listing, inference, and reserved `query.select` |
| `tenant_auditor` | Member access plus tenant membership, key, webhook, usage, audit, policy, and quota reads and exports |
| `tenant_admin` | Auditor access plus tenant-owned membership, API-key, policy, quota, webhook, project, and configuration writes |
| `service` | Granted-project data-plane actions explicitly present in server-held scopes; no control-plane access |
| `platform_admin` | Platform resources; tenant access only through validated and audited break glass |

Canonical project access has two independent gates:

1. A strongly consistent lookup must prove the project belongs to the selected
   tenant.
2. The principal must have the project grant and action required by policy.

Cross-tenant and ungranted project resources return `404` to conceal existence.
Ordinary same-tenant denials return `403`. Canonical mode default-denies unmapped
`/api/*` and `/v1/*` routes.

Legacy `admin` and `admin:*` authority exists only for noncanonical migration
contexts. It cannot elevate a canonical viewer or service identity.

`query.select` is reserved authorization vocabulary. AxonLLM does not currently
ship a SQL parser, datasource adapter, or query endpoint. `query.mutate` always
denies.

## Request Flow

For Starlette and AgentCore data-plane requests:

1. Verify the credential and reject duplicate or ambiguous credential sources.
2. Resolve canonical identity from DynamoDB.
3. Require explicit tenant and project context.
4. Resolve authoritative project ownership.
5. Evaluate role, project grant, and service scope.
6. Apply distributed rate and budget reservations.
7. Run request validation, policy, PII, guardrail, cache, routing, and provider
   stages.
8. Persist tenant-qualified usage and audit records.

AgentCore rejects payload-supplied identity and reads identity only from trusted
runtime headers. Its `health` action is liveness only. Use `/ready`,
authenticated `list_models`, and a low-cost authenticated completion as release
canaries.

## Control Plane And Break Glass

Tenant control-plane routes use the canonical tenant selected by middleware.
Tenant members and auditors can read but cannot mutate. Tenant administrators
can mutate tenant-owned configuration. Region topology and other platform
resources require `platform_admin`.

Platform access to tenant resources additionally requires:

- a canonical platform principal;
- a non-empty break-glass reason;
- a validated `X-Axon-Target-Tenant` header; and
- a successful append-only audit record before tenant dispatch.

The target tenant is syntax-checked and checked for consistency at middleware.
Handlers then perform resource-specific, tenant-qualified authoritative lookups.

## Durable Multi-Instance State

DynamoDB is authoritative in production. Conditional writes and revisioned
compare-and-swap updates protect topology, webhooks, policies, quotas, budgets,
rate windows, API keys, SCIM state, principals, audit chains, and configuration
epochs from lost updates.

Fleet refresh rejects stale revisions. Topology parsing completes before the
live object is mutated, and a shared mutation/refresh lock prevents hybrid
snapshots. Health status remains live state and is preserved across durable
topology publication.

Store failures on security-relevant writes fail closed with sanitized `503`
responses. They do not report success for an in-memory-only mutation.

Security events are durably enqueued once per matching tenant destination.
Strict envelopes bind event and destination to one tenant, deterministic
delivery identities support idempotent receivers, and repeated failures redrive
to a retained DLQ after five receives. See the production runbook before
redriving because each message retains its enqueue-time destination snapshot.

## Deployment Controls

Both checked-in AWS stacks require immutable private-ECR image URIs ending in a
digest. CI synthesizes and lints both stacks under Node 22, rejects local CDK
Docker assets, scans both templates, and validates all workflow action pins.

The Fargate stack also requires `BedrockInvokeResourceArns`; deployment through
`deploy-fargate.sh` supplies it from
`AXON_BEDROCK_INVOKE_RESOURCE_ARNS`. Values must be concrete model or
inference-profile ARNs without wildcards. Its retained provider secret has a
CloudFormation-generated physical name, so operator tooling must use the
`ProviderSecretArn` stack output for updates, rotation, and monitoring.

Both stacks provision retained KMS-encrypted FIFO event queues, a managed FIFO
SNS event topic, a retained encrypted CloudWatch event group, private
SQS/SNS/Logs endpoints, scoped runtime IAM, and DLQ alarms. Operators must use
the event resource outputs, add and confirm topic subscribers, and rehearse the
controlled DLQ recovery procedure.

The release workflow builds separate Fargate AMD64 and AgentCore ARM64 images,
scans them, creates SBOMs and target-qualified schema-v3 evidence, and KMS-signs
the multi-target SLSA provenance and manifest. Deployment verification binds a
selected private-ECR digest to the exact commit, release tag, workflow run, and
target, verifies both KMS signatures, and performs a fresh image scan. The
tag-producing signer records its current exact key ARN; consumers accept that
manifest identity only when it belongs to `AXON_AWS_ACCOUNT_ID` and is targeted
by a retained `alias/axonllm/release-signing-v*` alias.

The release-foundation stack creates retained KMS-encrypted immutable ECR
repositories, a retained asymmetric evidence-signing key, and separate GitHub
OIDC signer, publisher, verifier, metadata-audit, and PITR-recovery roles. The
tag-only signer cannot access ECR, and the publisher cannot sign. Audit cannot
read secret values or restore data; recovery cannot read secrets and is limited
to the two state tables and their temporary restore-validation namespaces.
Fargate recovery cutover uses an exact selected table policy and fails
deployment unless autoscaling and every old task are quiesced; cutover mode
pins the declared task count to zero until validation,
and its alarms and backup selection follow the selected table. The
protected publication workflow copies the original signed OCI archives without
rebuilding, validates fixed destinations, and verifies both remote digests and
KMS evidence. No workflow deploys a runtime automatically; deploy only the
digest returned by deployment verification.

## Known Design Residuals

These limitations are explicit and must not be represented as completed:

- API-key state mutation and hash-chain audit append are separate DynamoDB
  transactions. Failed issue or rotation audit triggers credential containment.
  Revocation can succeed while its audit append returns `503`; operators must
  reconcile that result from the durable key state.
- There is no standalone authoritative tenant registry. Break-glass target
  syntax and request consistency are validated before dispatch, then each
  handler proves tenant ownership through its authoritative resource lookup.
- AgentCore conversation memory is not enabled. Any future memory namespace
  must include canonical tenant, principal, project, and an opaque
  server-controlled session identifier.
- `GET /api/users` remains unavailable in canonical mode because the legacy
  aggregate has no safe tenant selector or canonical action mapping.

## Promotion Evidence Still Required

Before production traffic:

1. Deploy the release foundation. Configure the protected `release` environment
   with the publisher role, account ID, and fixed ECR variables, and configure
   the protected `production` environment with the verifier role,
   `AXON_OPERATIONS_AUDIT_ROLE_ARN`,
   `AXON_OPERATIONS_RECOVERY_ROLE_ARN`, `AXON_AWS_ACCOUNT_ID`, and
   both target data-key variables: `AXON_DATA_KMS_KEY_ARN` and
   `AXON_AGENTCORE_DATA_KMS_KEY_ARN`. Configure the repository with the
   tag-only signer role, current exact signing-key ARN, and account ID. Protect
   `refs/tags/v*` with a ruleset that restricts creation and blocks update and
   deletion. Confirm the IAM trust prefix matches GitHub's immutable OIDC
   `sub_claim_prefix`. Create the new retained
   `alias/axonllm/release-signing-v*` alias before changing the current key
   variable, and never repoint or delete historical version aliases.
2. Produce green required CI for the exact release commit.
3. Execute the real tagged private-ECR and KMS-signature flow for the selected
   Fargate or AgentCore image digest.
4. Run authenticated positive and negative tenant canaries, including wrong
   tenant, wrong project, inactive membership, viewer write denial, service
   scope denial, and break-glass attribution.
5. Execute a real AWS restore exercise and an application cutover and rollback
   rehearsal. Retain the recovery evidence.
6. Exercise each security-event destination, its receiver, DLQ alarm, and
   controlled redrive procedure.
7. Validate load, throttling, quotas, audit continuity, revocation propagation,
   and topology refresh with the intended replica count.

Never recover production access by switching to `LOG_ONLY` or disabling
canonical identity. Roll back application code while retaining `ENFORCE`,
canonical records, and durable state.
