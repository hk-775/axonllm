# PRFAQ: AxonLLM Production and AgentCore

> Working-backwards draft reconciled with the repository on 2026-08-11. This
> document describes implemented behavior; it is not a production certification.

## Press Release

### AxonLLM Provides a Tenant-Aware Control Plane for Enterprise LLM Access

AxonLLM provides one OpenAI-compatible HTTP gateway for routing requests across
configured LLM providers while applying model access, rate and budget controls,
guardrails, caching, audit, and cost attribution. Its Starlette application
includes chat, bounded Athena query, and an administrative control plane. A
separate Amazon Bedrock AgentCore entrypoint exposes governed chat, model-list,
and query actions through the same canonical query service. Managed-Cognito
adopters also receive a dedicated shared-state Fargate control plane for tenant
administration; it suppresses execution routes and has no Athena/STS authority.

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
locally, and schema-v3 evidence plus target-aware deployment verification cover
both Fargate and AgentCore. Promotion still requires successful repository CI
for the exact commit. `v0.2.4` completed the private-ECR/KMS-signature flow for
both target digests, but predates the query and shared control-plane work. Those
additions have no tagged evidence or deployed canary. A hardened runtime
deployment and real AWS restore exercise remain externally unverified.

## Frequently Asked Questions

### Product And Runtime

**Q: What application surfaces ship?**

A: The Starlette application exposes:

- `/api/chat`, `/api/chat/stream`, and `/api/models`;
- OpenAI-compatible `/v1/chat/completions` and `/v1/models`, plus governed
  `POST /v1/query` when Athena query configuration is enabled;
- the `/admin/*` API and dashboard, including `/admin/datasources`;
- managed-SAML handoff/direct-SP tombstones and SCIM routes;
- public `/health` liveness and `/ready` persistence/outbox readiness.

The AgentCore application is separate. It exposes `chat`, `list_models`,
optional `query`, and `health` invocation actions plus `GET /ready`. It does
not mount the Starlette admin console or HTTP API. The managed-Cognito
shared-state control plane is a separate Starlette Fargate process with
`AXON_CONTROL_PLANE_ONLY=true`.

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
| `tenant_admin` | Read/write tenant-owned resources and datasource metadata | Explicit grant required |
| `tenant_member` | Read only, including datasource metadata with role ARN concealed | Explicit grant required |
| `tenant_auditor` | Read only, including datasource metadata with role ARN concealed | Explicit grant required |
| `service` | No canonical admin or datasource access; legacy admin scopes are ignored and canonical key issuance rejects them | Explicit grant and action scope required |
| `platform_admin` | Platform resources; tenant control-plane access requires a break-glass reason plus the authoritative `X-Axon-Target-Tenant` selector | No ordinary project data-plane access is exposed |

Models, health, architecture, catalogue/pricing drift, production readiness, and
region topology are platform-global. Tenant viewer roles can read platform views
that do not add a stricter handler check, while platform writes and region
topology are restricted. Canonical roles are authoritative: viewers remain
read-only and services remain control-plane denied even if a legacy admin scope
is present. The legacy `admin` role and `admin:*` scopes remain only for
noncanonical migration mode and must not be used as an untrusted tenant boundary.

**Q: Can viewers run read-only SQL queries?**

A: Yes, when query is enabled and the viewer has an explicit grant for the
authenticated project. A normal Starlette data-plane process exposes
`POST /v1/query`, and AgentCore exposes `query`; both use the same
`QueryService`. A canonical service also needs the server-held `query.select`
scope. `query.mutate` always denies.

AxonLLM accepts one Athena `SELECT` AST, enforces the datasource
catalog/database and exact deployment role binding, validates the
KMS-encrypted workgroup immediately before execution, bounds timeout/rows/the
compact serialized result set/scan, and durably audits hashes rather than SQL
literals. DynamoDB enforces fleet RPM, expiring concurrency, aggregate scan
reservations, duplicate request IDs, and durable execution lifecycle. A fenced
periodic worker reconciles interrupted records and pending terminal audit,
deferring rather than guessing when datasource authority cannot be restored. A
caller-defined model tool named `db_query` remains caller-executed;
AxonLLM does not automatically invoke tools emitted by a model.

**Q: What must exist before canonical mode is enabled?**

A: OIDC tokens must provide signed issuer/subject identity and the required
tenant and project hints. Bootstrap the initial authority against the same
DynamoDB table the runtime uses:

```bash
LLM_ROUTER_DYNAMODB_ENABLED=true AXON_DYNAMODB_TABLE=axonllm-state \
AWS_DEFAULT_REGION=us-east-1 \
uv run axon bootstrap-tenant \
  --tenant tenant-a --project project-a \
  --issuer https://idp.example.com/oauth2/default \
  --subject 00u-admin-subject --user-name admin@example.com
```

The restartable command conditionally creates or verifies the tenant project and
SCIM-backed administrator, grants membership through the canonical transaction,
and strongly verifies active `tenant_admin` authority and the project grant.
Conflicting issuer/subject ownership fails closed. Canonical service keys use
`axon issue-key --tenant tenant-a --project project-a`; their default scopes are
`model.list`, `inference.invoke`, and `query.select`, and `admin:` scopes are
rejected.

**Q: What identity choice does a first AgentCore adopter get?**

A: Two authenticated production choices. Schema-v2 `managed-cognito` deploys a
retained and deletion-protected pool with required TOTP, a public AgentCore
authorization-code client, and a confidential ALB client. It requires stable
control-plane DNS, a regional ACM certificate, Route 53 public zone, a verified
AMD64 control-plane image, and managed ingress/HTTPS egress prefix lists, in
addition to the verified ARM64 AgentCore image and runtime inputs. The
application uses S256 PKCE and sends the Cognito ID token, whose tenant/project
attributes are routing hints.

`external-oidc` requires exact issuer, discovery, client, audience,
first-admin subject, and tenant/project claim names. It deploys AgentCore and
canonical bootstrap only; it does not receive the Cognito-authenticated web
control plane.

`axon setup agentcore` writes a strict schema-v2 configuration with no password,
token, or client secret. Managed Cognito requires its `control_plane` object;
schema-v1 files must be regenerated or migrated. `deploy-agentcore.sh` validates
the file, deploys identity when selected, invites or verifies the first user,
deploys AgentCore, establishes canonical `tenant_admin` authority, and then
deploys the shared-state control plane for managed Cognito. Token claims never
grant the role; DynamoDB remains authoritative. Anonymous use remains local
development only.

**Q: How does enterprise SAML login work?**

A: Cognito, not AxonLLM, is the SAML service provider. The operator configures
the tenant IdP metadata and signing certificate on the retained Cognito pool,
uses Cognito's SP entity ID and SAML response endpoint at the IdP, and enables
the provider on the confidential ALB client and any public client used by
federated AgentCore users. The first-adopter deployer does not ingest this
tenant-specific configuration.

Cognito validates the signed response or assertion, issuer, audience,
destination, recipient, time conditions, request correlation, replay, and
RelayState. The ALB establishes the browser session and sends an ALB-signed OIDC
identity to AxonLLM. AxonLLM verifies that trust chain and resolves the exact
Cognito issuer and Cognito `sub` through canonical DynamoDB authority. SAML
roles, groups, scopes, tenant values, and project values grant nothing.

Only `/scim/*` bypasses ALB Cognito. `GET /saml/login` is an authenticated local
handoff to a validated protected path; `/saml/acs` and `/saml/metadata` return
`410`. No SAML secret, certificate, metadata, or assertion enters the AxonLLM
container. SCIM-created principals must use the Cognito issuer and Cognito `sub`
as their immutable issuer and `externalId`.

After bootstrap,
`POST /admin/projects/{id}/members` takes the SCIM resource id in `user_id`.
POST/DELETE member operations atomically update `Project.members`,
`ScimUser.project_ids`, authoritative `Principal.project_ids`, their
authorization versions with compare-and-swap conditions, and tenant
`SCIM#VERSION`. Stored and returned members use `scim:<id>`. Canonical project
creation rejects non-empty bulk members, and project PUT rejects any `members`
field.

Canonical SCIM uses tenant-bound credentials in `AXON_SCIM_TENANTS` and
stores users, groups, username uniqueness edges, and the version under
`PK=TENANT#{tenant_id}` with `SCIM#USER#{id}`, `SCIM#GROUP#{id}`,
`SCIM#USERNAME#{hash}`, and `SCIM#VERSION` sort keys. Mutations are
transactional; version and tenant-snapshot reads are strongly consistent. The
Fargate stack can inject the complete map from Secrets Manager when
`AXON_SCIM_TENANTS_SECRET_ARN` is supplied. Initial bootstrap remains an
explicit operator action rather than an unauthenticated runtime path.

**Q: Are limits and tenant state shared across replicas?**

A: With DynamoDB enabled, canonical projects, user configuration, usage/spend,
policies, quota state, webhooks, audit chains, API keys, SCIM, datasource
metadata, query lifecycle/admission, rate limits, and budget reservations have
tenant-aware durable paths. Rate limiting uses atomic fixed-window counters;
budget and query admission use reserve/finalize transactions, and interrupted
query records use fenced reconciliation leases. Authority and admission failures
fail closed.

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
| AgentCore CDK | Governed `chat`/`list_models`/optional `query` actions with private runtime infrastructure |
| Managed-Cognito control-plane CDK | Shared-state tenant administration on private Fargate behind a Cognito HTTPS ALB; no execution routes or Athena/STS authority |
| App Runner `deploy.sh` | Legacy reference path; lacks the canonical, private-network, digest-gate, backup, and readiness controls of the CDK stacks |
| Docker / local | Development, testing, or a separately hardened self-hosted deployment |

`deploy-fargate.sh` requires the immutable image and exact Bedrock ARNs. It
defaults to staging, but supplies the complete production identity parameter set
when `AXON_DEPLOYMENT_MODE=production` and all required `AXON_OIDC_*` values are
present. It also supports the optional SCIM secret, hosted-zone records, and
guarded Fargate recovery-table selection.

**Q: What networking and image controls do the CDK stacks enforce?**

A: Fargate uses CloudFront/WAF, an internal TLS ALB, and private tasks. AgentCore
uses VPC runtime mode with DynamoDB, Bedrock, and optional STS/Athena endpoints.
The shared control plane uses private Fargate tasks behind an HTTPS ALB,
Cognito authentication, approved ingress, and a stable Route 53 alias. The
stacks disable unrestricted security-group egress and accept only private
regional ECR references ending in `@sha256`.

Fargate is restricted to `us-east-1` because its CloudFront WebACL is global.
AgentCore accepts a regional private ECR digest and concrete, wildcard-free
Bedrock model/profile and datasource role ARNs. The control plane requires a
separate verified AMD64 digest and regional ACM certificate.

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

AgentCore `/ready` also does not enumerate datasource roles or validate Athena
workgroups. Workgroup checks run immediately before each query, so query
enablement requires an authenticated execution canary.

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
days. Both backup vaults use governance-mode Vault Lock with a 30-day minimum
and 365-day maximum retention.

`scripts/operations/validate_state_recovery.py` verifies table/PITR/vault
posture and can create a protected temporary PITR restore. The operations
workflow uses a Fargate/AgentCore matrix to audit both targets daily and run
both temporary-table restore exercises monthly with a separate recovery role;
it can retain only the Fargate result for controlled cutover and preserves
90-day evidence artifacts.

Fargate has a guarded restored-table parameter plus
`scripts/operations/fargate_recovery.py` for quiesce, start, status, resume, and
cleanup phases. Its explicit cutover mode keeps CloudFormation's desired task
count at zero until the operator starts validated canaries.
`run_production_validation.py` executes fail-closed RBAC canaries and bodyless
read load without writing credentials to its report. The AgentCore workflow
validates restore only: there is no supported AgentCore application cutover. A
real AWS execution and Fargate cutover/rollback rehearsal remain externally
unverified.

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

**Q: How is Athena role trust bootstrapped safely?**

A: The AgentCore execution role is deterministically named
`axonllm-agentcore-runtime-<region>`. Operators can preconfigure each datasource
role to trust that exact account/region ARN for `sts:AssumeRole`,
`sts:TagSession`, and `sts:SetSourceIdentity`. The stack exports and the
first-adopter deployer prints the full `RuntimeExecutionRoleArn` so it can be
verified after deployment. Runtime IAM and the private STS endpoint restrict
the same actions to exact deployment-approved datasource roles.

**Q: What AgentCore constraints remain?**

A:

- no admin console or Starlette route surface;
- no wired `SessionManager` or AgentCore Memory;
- no bootstrap action on the AgentCore invocation surface; the repository CLI
  can provision its DynamoDB table out of band;
- external OIDC does not deploy the Cognito-authenticated web control plane;
- `/ready` does not validate datasource roles or Athena workgroups;
- restored-table cutover requires the reviewed four-phase AgentCore selector,
  a separate recovery endpoint, full-session quiescence, coordinated
  control-plane shutdown, and authenticated canaries;
- no automatic SNS alarm or security-event topic subscription;
- a synchronous bootstrap worker cannot be forcibly canceled by Python;
- no image publication or deployment step in the evidence and verification
  workflows; publication is a separate protected workflow and no workflow
  deploys a runtime.

The checked-in `.bedrock_agentcore.yaml` is generated local state with public
networking and no production JWT/header contract. `infra/agentcore_stack.py` is
the infrastructure source of truth.

**Q: Is ARM64 release evidence generated?**

A: Yes. `release-security.yml` builds, scans, and SBOMs both images, records
their digests in schema-v3 multi-target SLSA provenance, and KMS-signs both the
provenance and manifest. `deploy-verification.yml` selects either target, binds
the supplied private ECR digest to that target's metadata, scan, SBOM, source
commit, release tag, and CI result, verifies both KMS signatures and the remote
image, and rescans it. `v0.2.4` completed the real tagged
private-ECR/KMS-signature run for both targets. It predates the query and
shared control-plane implementation; a newer tagged run and deployed canaries
are required before those capabilities can rely on release evidence.

### Release Governance

**Q: What evidence is required for a release?**

A: Green CI for the exact commit, a `v*` release ref, source/image scans, SBOMs,
immutable image digests, verified KMS signatures, private ECR publication, a
fresh remote scan, deployment approval, readiness, authorization canaries, alarm
delivery, and recovery evidence. The release workflow creates evidence but does
not publish or deploy. The separate protected publication workflow copies the
verified OCI archives to immutable private ECR without rebuilding; no workflow
deploys a runtime. `v0.2.4` completed the first tagged
private-ECR/KMS-signature flow. A real hardened deployment and AWS restore
exercise remain externally unverified.

**Q: How is release-signing key rotation kept rollback-safe?**

A: `AXON_RELEASE_SIGNING_KEY_ARN` is a repository variable used only by the
tag-producing signer and always contains the current exact key ARN. Before
changing it, create a new retained version alias matching
`alias/axonllm/release-signing-v*` for the new key. Publication and production
read the exact key ARN from the manifest and accept it only when the ARN belongs
to `AXON_AWS_ACCOUNT_ID` and one retained version alias resolves to it.
Historical version aliases are never repointed or deleted, and their keys
remain retained.

**Q: Is the current worktree production-ready?**

A: No. Local focused regressions and implemented controls do not certify an
environment. `v0.2.4` has retained CI and private-ECR/KMS evidence for both
target artifacts, but no hardened runtime or managed identity stack currently
deploys those digests. A real AWS restore exercise, authenticated identity/RBAC
canaries, alarm delivery, load validation, and operational approval still need
retained evidence.

See the [Production Runbook](PRODUCTION_RUNBOOK.md) and
[AgentCore Runbook](AGENTCORE_RUNBOOK.md) for commands and release checks.
