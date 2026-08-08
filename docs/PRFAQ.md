# PRFAQ: AxonLLM Production and AgentCore

> Working-backwards draft reconciled with the repository on 2026-08-07. This
> document describes implemented behavior; it is not a production certification.

## Press Release

### AxonLLM Provides a Tenant-Aware Control Plane for Enterprise LLM Access

AxonLLM provides one OpenAI-compatible HTTP gateway for routing requests across
configured LLM providers while applying model access, rate and budget controls,
guardrails, caching, audit, and cost attribution. Its Starlette application
includes the chat APIs and an administrative control plane. A separate Amazon
Bedrock AgentCore entrypoint exposes governed chat and model-list actions.

For shared deployments, canonical identity replaces token-supplied authority
with strongly read DynamoDB principals and tenant-owned projects. Tenant
administrators can read and write their tenant configuration. Tenant members
and auditors can view it but cannot mutate it. Project data-plane access still
requires an explicit grant. Service principals additionally require server-held
action scopes.

The AWS CDK paths require private networking, customer-approved HTTPS egress,
immutable private-ECR digests, encrypted durable state, readiness checks,
backups, alarms, and release evidence. These controls do not make an arbitrary
checkout production-ready: CI, evidence verification, identity bootstrap,
canaries, restore testing, alarm delivery, and operational approval remain
release requirements.

The production implementation includes tenant SCIM version/snapshot reads and
transactional version increments. Focused hardening regressions are green
locally, and schema-v2 evidence plus target-aware deployment verification cover
both Fargate and AgentCore. Promotion still requires successful repository CI
for the exact commit. A real tagged private-ECR/Sigstore flow for the deployed
digest and a real AWS restore exercise remain externally unverified.

## Frequently Asked Questions

### Product And Runtime

**Q: What application surfaces ship?**

A: The Starlette application exposes:

- `/api/chat`, `/api/chat/stream`, and `/api/models`;
- OpenAI-compatible `/v1/chat/completions` and `/v1/models`;
- the `/admin/*` API and dashboard;
- SAML and SCIM routes;
- public `/health` liveness and `/ready` persistence/outbox readiness.

The AgentCore application is separate. It exposes `chat`, `list_models`, and
`health` invocation actions plus `GET /ready`. It does not mount the Starlette
admin console or HTTP API.

**Q: What is the difference between single-user and multi-tenant mode?**

A: "Single-user" means one isolated trust domain, not literally one human.

| Mode | Identity behavior | Intended use |
|---|---|---|
| Legacy / single trust domain | `AXON_DEPLOYMENT_PROFILE=development`, `AXON_REQUIRE_CANONICAL_IDENTITY=false`; verified claims may supply roles, scopes, tenant, and project | Local development or an isolated deployment whose callers share one trust boundary |
| Canonical multi-tenant | `AXON_DEPLOYMENT_PROFILE=production`, `AXON_AUTH_MODE=ENFORCE`, DynamoDB enabled, `AXON_REQUIRE_CANONICAL_IDENTITY=true` | Shared deployment where tenants do not trust each other; startup rejects weaker settings |

Canonical mode strongly resolves the principal and project. Credential roles,
scopes, status, and project grants are replaced, not merged. Missing project
context is 400, cross-tenant or ungranted projects are concealed as 404, and an
unavailable authority store returns 503.

**Q: How does tenant RBAC work?**

A:

| Role | Tenant configuration | Project actions |
|---|---|---|
| `tenant_admin` | Read/write tenant-owned resources | Explicit grant required |
| `tenant_member` | Read only | Explicit grant required |
| `tenant_auditor` | Read only | Explicit grant required |
| `service` | No canonical admin access; legacy admin scopes are ignored and canonical key issuance rejects them | Explicit grant and action scope required |
| `platform_admin` | Platform resources; tenant control-plane access requires a break-glass reason plus the authoritative `X-Axon-Target-Tenant` selector | No ordinary project data-plane access is exposed |

Models, health, architecture, catalogue/pricing drift, production readiness, and
region topology are platform-global. Tenant viewer roles can read platform views
that do not add a stricter handler check, while platform writes and region
topology are restricted. Canonical roles are authoritative: viewers remain
read-only and services remain control-plane denied even if a legacy admin scope
is present. The legacy `admin` role and `admin:*` scopes remain only for
noncanonical migration mode and must not be used as an untrusted tenant boundary.

**Q: Can viewers run read-only SQL queries?**

A: No SQL or datasource endpoint ships. `query.select` is only a stable action
name in the authorization vocabulary. There is no parser, adapter, HTTP route,
AgentCore action, or backend contract to invoke. `query.mutate` always denies.
Caller-defined tools named `db_query` are transported to a model but are
executed by the caller, not AxonLLM.

**Q: What must exist before canonical mode is enabled?**

A: The tenant-owned project, canonical principal, active membership, tenant
role, project grants, and any service scopes must already exist in DynamoDB.
OIDC tokens must provide signed issuer/subject identity and the required tenant
and project hints.

The repository has no supported tenant/principal bootstrap command.
After a canonical SCIM user and principal exist,
`POST /admin/projects/{id}/members` takes the SCIM resource id in `user_id`.
POST/DELETE member operations atomically update `Project.members`,
`ScimUser.project_ids`, authoritative `Principal.project_ids`, their
authorization versions with compare-and-swap conditions, and tenant
`SCIM#VERSION`. Stored and returned members use `scim:<id>`. Canonical project
creation rejects non-empty bulk members, and project PUT rejects any `members`
field. Initial tenant/admin bootstrap remains an external deployment
responsibility.

Canonical SCIM uses tenant-bound credentials in `AXON_SCIM_TENANTS` and
stores users, groups, username uniqueness edges, and the version under
`PK=TENANT#{tenant_id}` with `SCIM#USER#{id}`, `SCIM#GROUP#{id}`,
`SCIM#USERNAME#{hash}`, and `SCIM#VERSION` sort keys. Mutations are
transactional; version and tenant-snapshot reads are strongly consistent. The
Fargate stack does not inject those SCIM credentials, so secret delivery and
initial bootstrap are deployment responsibilities.

**Q: Are limits and tenant state shared across replicas?**

A: With DynamoDB enabled, canonical projects, user configuration, usage/spend,
policies, quota state, webhooks, audit chains, API keys, SCIM, rate limits, and
budget reservations have tenant-aware durable paths. Rate limiting uses atomic
fixed-window counters and budget admission uses idempotent reserve/finalize
transactions. Authority and admission failures fail closed.

Provider health and exact/semantic response-cache contents remain process-local.
Cache keys include tenant and project, so process-local caches are isolated but
not shared. Usage records are durable, while some aggregate reads refresh from
scans and can lag.

**Q: What API-key guarantees exist?**

A: Raw `axon_` values are returned once and only SHA-256 hashes are stored.
Canonical tenant keys default to 90 days and cannot exceed 365 days. Canonical
issue, revoke, and rotate operations transactionally update the tenant key and
service principal; revocation/rotation also advances the tenant epoch. Replicas
poll that epoch every five seconds. If polling fails, a cached key can remain
accepted until the 300-second cache TTL. Legacy/in-memory rotation remains
revoke then issue.

### Deployment And Operations

**Q: Which deployment paths are production candidates?**

A:

| Path | Current role |
|---|---|
| ECS Fargate CDK | Full Starlette surface; production mode adds ALB OIDC and canonical identity |
| AgentCore CDK | Governed `chat`/`list_models` actions with private runtime infrastructure |
| App Runner `deploy.sh` | Legacy reference path; lacks the canonical, private-network, digest-gate, backup, and readiness controls of the CDK stacks |
| Docker / local | Development, testing, or a separately hardened self-hosted deployment |

`deploy-fargate.sh` does not supply the immutable image and production identity
parameters and is not the production deployment command.

**Q: What networking and image controls do the CDK stacks enforce?**

A: Fargate uses CloudFront/WAF, an internal TLS ALB, and private tasks. AgentCore
uses VPC runtime mode with DynamoDB and Bedrock endpoints. Both stacks disable
unrestricted security-group egress and require a customer-managed HTTPS prefix
list. Both accept only private regional ECR references ending in `@sha256`.

Fargate is restricted to `us-east-1` because its CloudFront WebACL is global.
AgentCore accepts a regional private ECR digest and concrete, wildcard-free
Bedrock model/profile ARNs.

**Q: What does readiness prove?**

A:

- Starlette `/health`: process liveness only.
- Starlette `/ready`: DynamoDB reachability when persistence is enabled and
  FIFO security-event outbox access when configured.
- `/admin/production-checklist`: configuration posture, not dependency health.
- AgentCore `health`: liveness only and deliberately not ready.
- AgentCore `/ready`: initialized runtime, OIDC/JWKS, principal-store health,
  and configured security-event outbox access/worker state.

None proves model availability, provider credentials, alarm delivery, backup
freshness, or a successful end-to-end completion. Use authenticated positive
and negative canaries before traffic.

**Q: How are security events delivered in the AWS deployments?**

A: Each matching tenant destination is snapshotted into a KMS-encrypted FIFO
SQS outbox. A lifecycle-managed worker delivers HTTPS webhooks, FIFO SNS, or
CloudWatch Logs with deterministic idempotency identities and bounded retries;
SQS moves a message to the retained DLQ after five failed receives. The stacks
restrict SNS and Logs delivery to their managed output ARNs. They do not create
tenant destination records or topic subscriptions, so operators must configure
both and rehearse the DLQ procedure in the production runbook.

**Q: How are backup and restore handled?**

A: Both CDK stacks enable DynamoDB PITR, deletion protection, KMS encryption,
and daily AWS Backup with cold transition after 30 days and deletion after 365
days. Vault Lock is not configured by CDK.

`scripts/operations/validate_state_recovery.py` verifies table/PITR/vault
posture and can create a temporary PITR restore. It does not cut the application
over to that table. The operations workflow uses a Fargate/AgentCore matrix to
audit both targets daily and run both temporary-table restore exercises monthly
with a separate recovery role. An ad hoc AgentCore invocation passes
`--stack-name AxonLLMAgentCoreStack`. A real AWS execution and application
cutover rehearsal remain externally unverified.

**Q: What is the incident and rotation procedure?**

A: Revoke affected tenant keys, verify rejection on every replica, rotate
provider and IdP credentials, rotate tenant SCIM tokens when implicated, verify
the tenant audit chain, and rerun authorization canaries before restoring
traffic. Fargate tasks read provider secrets at startup, so rotate the secret
and replace the tasks.

`scripts/operations/check_secret_rotation.py` checks the Fargate
Anthropic/OpenAI secret, KMS rotation, version age, pending versions, and
optional automatic rotation. It does not perform rotation and does not cover
AgentCore.

### AgentCore

**Q: How does AgentCore identity work?**

A: The CDK runtime configures a JWT authorizer and forwards `Authorization`.
AxonLLM verifies the JWT again, requires signed issuer, subject, tenant, and
project claims, discards token roles/scopes, and resolves canonical principal
and project state. Payload-supplied identity is rejected. This boundary accepts
OIDC JWTs, not AxonLLM API keys.

**Q: What AgentCore constraints remain?**

A:

- no admin console or Starlette route surface;
- no SQL/query action;
- no wired `SessionManager` or AgentCore Memory;
- no tenant/principal bootstrap command;
- no non-Bedrock provider-secret injection in the CDK stack;
- no automatic SNS alarm or security-event topic subscription;
- a synchronous bootstrap worker cannot be forcibly canceled by Python;
- no image publication or deployment step in the evidence and verification
  workflows; the real tagged private-ECR/Sigstore flow remains externally
  unverified.

The checked-in `.bedrock_agentcore.yaml` is generated local state with public
networking and no production JWT/header contract. `infra/agentcore_stack.py` is
the infrastructure source of truth.

**Q: Is ARM64 release evidence generated?**

A: Yes. `release-security.yml` builds, scans, SBOMs, and keylessly attests both
images. Its schema-v2 manifest records distinct `fargate` and `agentcore`
targets. `deploy-verification.yml` selects either target, binds the supplied
private ECR digest to that target's metadata, scan, SBOM, source commit, release
tag, CI result, and Sigstore bundle, verifies the remote image, and rescans it.
The implementation is locally tested; a real tagged private-ECR/Sigstore run
remains externally unverified.

### Release Governance

**Q: What evidence is required for a release?**

A: Green CI for the exact commit, a `v*` release ref, source/image scans, SBOMs,
immutable image digests, verified keyless attestations, private ECR publication,
a fresh remote scan, deployment approval, readiness, authorization canaries,
alarm delivery, and recovery evidence. The release workflow creates evidence
but publishes and deploys neither image. The first real tagged
private-ECR/Sigstore flow and real AWS restore exercise remain externally
unverified.

**Q: Is the current worktree production-ready?**

A: No. Local focused regressions and implemented controls do not certify a
specific artifact or environment. Required CI for the exact release commit, a
real tagged private-ECR/Sigstore verification for the deployed digest, a real
AWS restore exercise, canaries, alarm delivery, and operational approval still
need retained evidence.

See the [Production Runbook](PRODUCTION_RUNBOOK.md) and
[AgentCore Runbook](AGENTCORE_RUNBOOK.md) for commands and release checks.
