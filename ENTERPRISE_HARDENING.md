# AxonLLM Enterprise Hardening Runbook

This runbook describes `hardening/enterprise-tenant-foundation` as of
2026-08-07. It separates controls present in the code from work still required
before a shared, multi-tenant production release.

## Release Status

Implemented:

- Canonical principals backed by strongly consistent DynamoDB reads.
- Default-deny tenant role/action policy, project grants, cross-tenant 404
  concealment, and data-plane enforcement for model listing and inference.
- Hardened direct and ALB OIDC verification and a fail-closed AgentCore adapter.
- Bounded chat ingress, PII-sensitive cache bypass, current-policy checks on
  cache hits, append-only audit-chain containment, secret-aware Docker context,
  and a non-root immutable image.
- A Fargate reference stack with an internal TLS ALB behind CloudFront VPC
  Origin, WAF, private tasks, and explicitly approved HTTPS egress.
- Hash-locked application/AgentCore dependencies and CI gates for lint, tests,
  secret/dependency/IaC scanning, CDK synthesis, and container build/scan.

Not complete:

- Authoritative tenant-qualified project ownership and tenant-scoped
  persistence for all customer data and `/admin/*` routes.
- An automated principal migration/provisioning path or transactional SCIM and
  API-key lifecycle integration.
- Production AgentCore IaC, JWT authorizer/header forwarding, private
  networking, memory isolation, and a real dependency readiness probe.
- The remaining release blockers listed at the end of this document.

The current branch is a tenant authorization foundation. It is not a claim that
the admin control plane is safe for multiple untrusted tenants.

## Canonical Identity

Set all of the following for production:

```bash
AXON_AUTH_MODE=ENFORCE
AXON_REQUIRE_CANONICAL_IDENTITY=true
AXON_LOAD_DEMO_DATA=false

LLM_ROUTER_DYNAMODB_ENABLED=true
AXON_DYNAMODB_TABLE=axonllm-state
AWS_DEFAULT_REGION=us-east-1

AXON_OIDC_ISSUER=https://idp.example.com/oauth2/default
AXON_OIDC_AUDIENCE=api://axonllm
```

For HTTP traffic authenticated by an ALB, also set:

```bash
AXON_ALB_SIGNER_ARN=arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/axon-prod/...
AXON_ALB_CLIENT_ID=<listener-auth-action-client-id>
AXON_ALB_ISSUER=https://public-keys.auth.elb.us-east-1.amazonaws.com
```

`AWS_DEFAULT_REGION` must match the ALB signer region. These values are not
needed by the direct OIDC or AgentCore paths.

Provider credentials and `AXON_BEDROCK_REGION` are deployment-specific.
`AXON_CHECK_MODEL_AVAILABILITY=true` enables the readiness checklist's live
provider catalogue check.

Canonical identity defaults to `false` only to permit migration. The production
checklist reports that state as `FAIL`. When the flag is true, startup refuses:

- any `AXON_AUTH_MODE` other than `ENFORCE`; or
- disabled DynamoDB persistence.

Authentication first verifies a credential and extracts identity hints. The
canonical resolver then looks up server-held authority and replaces all
credential-provided roles, scopes, principal id, and authorization version.
Token roles and scopes are never authoritative in canonical mode.

Each principal record must contain:

| Field | Requirement |
|---|---|
| `issuer`, `subject` | Exact verified credential identity |
| `tenant_id`, `principal_id` | Non-empty server-owned identifiers |
| `roles` | One or more canonical `TenantRole` values |
| `auth_method` | `oidc_jwt`, `api_key`, or the supported source method |
| `membership_status` | `active` to authorize |
| `project_ids` | Explicit grants for every tenant role, including tenant admins, until authoritative project ownership exists |
| `scopes` | Server-held action scopes, primarily for `service` principals |
| `authorization_version` | Positive integer; increment by one on each update |

OIDC records use the IdP issuer and `sub`. API-key records use issuer
`urn:axonllm:api-key`, subject equal to the key id, role `service`, and an
explicit project grant and action scopes. Existing API-key and SCIM records do
not create canonical principal rows automatically.

DynamoDB reads are strongly consistent. Missing, malformed, inactive, ambiguous,
or identity-mismatched rows deny access. Writes support optimistic
`authorization_version` conditions, but no public administration route currently
manages these rows. Populate them through a controlled migration tool or trusted
one-off process, and retain a reviewed manifest and rollback snapshot.

## Tenant RBAC

Every decision requires an active membership. Unknown actions default deny.

| Role | Tenant actions |
|---|---|
| `tenant_member` | `model.list`, `inference.invoke`, `query.select`, `tenant.config.read`, `policy.read`, `quota.read` |
| `tenant_auditor` | All member actions plus membership, API-key, webhook, usage, and audit reads; usage and audit exports |
| `tenant_admin` | All auditor actions plus tenant config, membership, API-key, policy, quota, and webhook writes |
| `service` | Only explicitly stored action scopes, and only for granted projects |
| `platform_admin` | Platform actions only; tenant access requires break glass |

The kernel requires every tenant role to hold an explicit grant when the
resource carries a project id, including `tenant_admin`: the gateway cannot
safely infer ownership from a globally keyed project id. A service scope may be
an exact action, a namespace wildcard such as `inference.*`, or `*`; these are
server-held scopes, not trusted token claims.

`query.select` expresses read-only query intent. `query.mutate` is denied for
every role, including platform administrators and services. No SQL/query HTTP
surface is currently wired to these actions, so this is a policy contract, not
yet an end-user query feature.

Cross-tenant resources and ungranted projects return 404 to conceal existence.
Ordinary same-tenant role denials return 403.

### Current Enforcement Boundary

Tenant RBAC is enforced on:

- `GET /api/models`
- `GET /v1/models`
- `POST /api/chat`
- `POST /api/chat/stream`
- `POST /v1/chat/completions`
- AgentCore `list_models` and `chat`

Other routes remain under their existing authorizers. In particular,
`/admin/*` still uses legacy `admin` roles and `admin:*` scopes and operates over
data that is not comprehensively tenant-qualified. A canonical `tenant_admin`
does not unlock those routes. Do not grant legacy global admin scopes to tenant
principals in a shared deployment.

Canonical mode also default-denies any unmapped `/api/*` or `/v1/*` endpoint.
That intentionally makes `GET /api/users` unavailable: its current result is a
fleet-wide user aggregate with no tenant-qualified ownership filter.

The standalone OIDC validator requires `iss`, `aud`, `exp`, and `sub`, but does
not require the custom tenant or project claims. A unique tenant membership can
resolve without a tenant hint. In canonical mode, mapped HTTP data-plane routes
reject a missing project before dispatch with `400 project_context_required`;
there is no default-project fallback on that path. AgentCore requires both
tenant and project claims. Legacy migration mode does not provide this canonical
RBAC floor and must not be used for shared tenancy.

Projects, users, usage, API keys, caches, quotas, policies, webhooks, SCIM
objects, and audit data must all be tenant-qualified and filtered before
tenant-admin/member read and write behavior can be exposed through the admin
API. Until then, use a separately isolated operator control plane or one
deployment per tenant.

The explicit project grant is containment, not authoritative ownership. The
middleware currently constructs a project resource using the resolved
principal's tenant because `Project` has no tenant owner and project records are
keyed only by project id. A grant whose id collides with another tenant's project
could therefore select globally keyed state. Production requires a
tenant-qualified project lookup before authorization and compound
tenant/project keys throughout the data plane. Tests must cover identical
project ids in different tenants.

## Break Glass

The authorization kernel permits a `platform_admin` to enter a different tenant
only when the caller supplies a non-blank reason. The decision is marked
`break_glass`; it still cannot grant `query.mutate`.

No HTTP or AgentCore ingress currently accepts a break-glass reason, and no
complete approval, expiry, alerting, or audit workflow is wired. Break glass is
therefore a kernel primitive, not an operational feature. Keep platform
administrators out of tenant paths until an authenticated, time-bounded,
ticket-linked, append-only audited workflow exists.

## Direct OIDC

Direct Bearer-token validation is fail closed:

- Both issuer and audience must be configured.
- Only `RS256` and `ES256` are accepted.
- `iss`, `aud`, `exp`, and non-empty string `sub` are required.
- `kid` must match exactly one JWK.
- JWK key type and optional `alg`, `use`, and `key_ops` must permit verification.
- Audience strings and arrays are supported by the JWT verifier.
- Mapped string claims are type checked; roles must be a string or string list,
  and OAuth `scope` must be a string.
- JWKS cache lifetime is bounded to one hour. Once stale, cached keys are
  discarded; discovery or JWKS refresh failure denies the token.
- Issuer and JWKS URLs must use HTTPS, discovery must return the exact configured
  issuer, and the JWKS URI must remain on the same origin. Redirects, proxy
  environment variables, compressed/oversized responses, and malformed or
  duplicate JSON are rejected.
- An unknown signing `kid` causes one single-flight JWKS refresh for normal key
  rotation. Unknown-key refreshes are limited to one per 30 seconds per process
  so attacker-controlled key ids cannot create unbounded issuer traffic.
- Missing `python-jose[cryptography]` denies every JWT rather than decoding it
  without signature verification.

ALB OIDC uses a separate ES256 regional-key path. It requires
`AXON_ALB_SIGNER_ARN`, `AXON_ALB_CLIENT_ID`, and `AXON_ALB_ISSUER`, validates
protected signer/client/issuer/expiry metadata before key retrieval, verifies
the signature, and requires signed `sub` to equal `X-Amzn-Oidc-Identity`. Key
retrieval and caching are bounded. Duplicate, incomplete, malformed, or invalid
ALB headers fail closed without trying another credential.

The checked-in Fargate stack does not create the ALB authentication action or
set these trust values. Deployment wiring, task ingress restricted to the ALB,
and an authenticated canary remain release prerequisites.

## Fargate Reference Stack

The stack is deliberately restricted to `us-east-1` because its CloudFront WAF
has global scope. Deployment requires five values with no source or template
defaults:

| Parameter | Requirement |
|---|---|
| `ViewerDomainName` | Public CloudFront alternate name |
| `ViewerCertificateArn` | `us-east-1` ACM certificate covering the viewer name |
| `OriginDomainName` | Private name presented to the internal ALB |
| `OriginCertificateArn` | `us-east-1` ACM certificate covering the origin name |
| `ApprovedHttpsPrefixListId` | Customer-managed allowlist for every task HTTPS destination |

`deploy-fargate.sh` reads the corresponding `AXON_*` environment variables,
passes them as CloudFormation parameters, and refuses another region. The
prefix list must cover image/runtime AWS dependencies, OIDC endpoints, and every
configured LLM provider without a `0.0.0.0/0` entry. The stack outputs
`CloudFrontDistributionDomain` and `InternalALBDomain` for DNS wiring.

This improves transport and network defaults but does not make the deployment
multi-tenant ready. It still lacks the ALB authentication action, canonical
principal provisioning, tenant-qualified ownership, WAF/access logging and
tuning, multi-AZ NAT, and a tested readiness gate. After CloudFront creates its
VPC-origin service security group, restrict ALB ingress to that group instead of
the VPC-wide bootstrap rule.

## AgentCore Runtime

The AgentCore adapter supports three actions:

| Action | Authentication | Behavior |
|---|---|---|
| `chat` | Required | Strict chat validation, canonical RBAC, native async streaming |
| `list_models` | Required | Models for the signed and granted project |
| `health` | Not required | Liveness only: `ready: false`, dependencies unchecked |

Identity is accepted only from `context.request_headers`, using exactly one of:

```text
Authorization: Bearer <OIDC JWT>
X-Amzn-Bedrock-AgentCore-Runtime-Custom-Identity-Token: Bearer <OIDC JWT>
```

Supplying both, duplicate case variants, a malformed Bearer value, or no trusted
runtime header fails with 401. Payload fields such as `user_id`, `project_id`,
`tenant_id`, `roles`, and `scopes` are rejected. The adapter independently
re-verifies the JWT and requires signed tenant and project claims, then resolves
canonical authority; token roles and scopes are discarded.

Before deployment:

1. Provision an AgentCore JWT authorizer for the exact issuer/audience and
   allowlist forwarding of `Authorization`; or place a trusted signed facade in
   front and use only the custom identity-token header.
2. Set canonical identity, DynamoDB, OIDC, clean-start, region, and provider
   configuration. Provision every principal before enabling traffic.
3. Install the checked-in hash-pinned `requirements.txt`, generated from
   `uv.lock`; it includes `bedrock-agentcore`, `python-jose`, and cryptography.
   It also includes `httpx`, which direct OIDC discovery requires. Keep the CI
   lock-consistency check green and do not resolve dependencies dynamically
   during deployment.
4. Give the execution role least-privilege access to the state table, required
   Bedrock models, runtime secrets, logs/traces, and only the AWS services in use.
5. Replace local generated AgentCore configuration with reviewed IaC. Do not
   commit `.bedrock_agentcore*`, staged archives, account ids, role ARNs, or
   deployment buckets.
6. Test token expiry, wrong issuer/audience, wrong tenant/project, deprovisioned
   membership, JWKS outage, DynamoDB outage, concurrent cold start, and streaming.

AgentCore does not expose the Starlette admin console or
`/admin/production-checklist`. Run control-plane readiness separately and use
authenticated `list_models` plus a low-cost completion as the AgentCore canary.
The `health` action must never be used as a readiness signal.

Runtime initialization is lazy on the first authenticated action. It loads
projects, users, usage, feedback, policies, destinations, topology, and audit
state; usage and audit paths scan retained history. A project-load failure can
currently return an empty mapping, after which missing project controls can fail
open. Rate-limit windows, cache state, and audit-chain serialization also remain
process-local, and the AgentCore runtime does not close provider HTTP sessions or
flush OTLP resources on shutdown. These are release blockers, not tuning items.

### Private Networking

No production AgentCore network topology is supplied by this branch. A release
deployment must define private subnets, security groups, DNS, and egress
explicitly. Prefer VPC endpoints for DynamoDB, Bedrock Runtime, Secrets Manager,
CloudWatch, and other used AWS services. Direct OIDC discovery/JWKS and non-AWS
providers require controlled HTTPS egress through an inspected NAT/proxy path.
Restrict inbound invocation with the runtime authorizer and IAM resource policy;
network placement does not replace authentication.

### Memory Isolation

The AgentCore entrypoint does not currently wire `SessionManager`, does not use
`context.session_id`, and does not read or write AgentCore Memory. A configured
memory resource therefore does not mean conversation memory is active.

Before enabling memory, derive every namespace from canonical tenant, principal,
and project ids plus an opaque server-controlled session id. Never use a
payload-supplied tenant or a globally reusable session id. Define per-tenant
retention/deletion, encryption, access policies, export controls, and adversarial
cross-tenant tests. Separate memory resources per tenant where the isolation
requirement warrants it.

## Container Constraints

The Docker image:

- resolves dependencies from `uv.lock` with `--frozen`;
- installs the OIDC, SAML, and OTEL extras;
- runs as UID/GID `10001` with no login shell or writable home;
- makes `/app` root-owned and non-writable; and
- excludes env files, provider config, private keys, AWS/AgentCore state, CDK
  output, caches, tests, and local build artifacts from the build context.

Run it with a read-only root filesystem, a bounded `noexec,nosuid` tmpfs for
`/tmp`, dropped Linux capabilities, `no-new-privileges`, runtime secret mounts,
and platform-managed liveness/readiness probes. File-backed configuration writes
will fail in the immutable image; use tenant-safe durable state or build a new
image. CI builds, inspects, and scans the image and synthesizes/scans CDK assets.
A release still needs a successful CI run for the exact commit, an
SBOM/signature or equivalent provenance, and a deployment smoke test.

## Rollout And Readiness

1. Inventory issuers, subjects, tenants, projects, roles, service keys, and
   expected action scopes. Resolve duplicate subjects and ambiguous memberships.
2. Back up the state table and write canonical principal rows with
   `authorization_version=1`. Reconcile every existing API key to a `service`
   principal.
3. Deploy an isolated staging environment with `ENFORCE`, DynamoDB, canonical
   identity, and demo data off. There is no shadow/dual-resolution mode.
4. Test each role positively and negatively, including cross-tenant 404,
   ungranted project 404, inactive membership 403, service scope denial, and
   universal query-mutation denial. Verify missing-project canonical HTTP
   requests return `400 project_context_required`.
5. Run `/admin/production-checklist`. Resolve every `FAIL`; investigate every
   `UNKNOWN`; explicitly accept or remediate each `WARN`.
6. Exercise direct OIDC and AgentCore canaries, provider fallback, streaming,
   audit verification, budget limits, revocation, and restart recovery.
7. Roll out by tenant or a small traffic percentage while monitoring 401, 403,
   404, 429, 503, JWKS refresh, DynamoDB errors, dropped writes, and audit-chain
   health.

Disabling canonical identity is a security downgrade, not a routine rollback:
legacy credential claims become authority again. Prefer rolling back application
code while retaining canonical records and `ENFORCE`. Never recover production
access by switching to `LOG_ONLY`.

The production checklist currently covers eight areas: auth enforcement,
canonical identity, demo data, provider credentials, pricing, model ids,
persistence, and API-key posture. It reports rather than enforces, is hidden in
demo mode, and its API-key scope warning still describes legacy behavior without
proving that canonical service-principal scopes match key metadata.

## Remaining Release Blockers

- Tenant-qualified persistence and filtered admin reads/writes for projects,
  users, usage, keys, caches, quotas, policies, webhooks, SCIM, and audit.
- Authoritative project ownership lookup before authorization, including
  isolation tests for the same project id in two tenants.
- Redacted member views and tenant-admin configuration routes.
- Transactional API-key issue/revoke and SCIM deprovisioning tied to canonical
  principal authorization versions.
- Fargate ALB authentication-action/trust-value wiring, post-create restriction
  to the CloudFront VPC-origin service security group, DNS, and access logging.
- Distributed rate-limit and budget semantics plus cross-replica cache and
  revocation behavior verified under load.
- Durable audit/accounting reservation semantics for failures after the first
  streamed byte; terminal errors are sanitized and emitted before `[DONE]`, but
  already delivered provider content cannot be recalled.
- Fail-closed control-state hydration, bounded startup reads, eager AgentCore
  initialization, graceful HTTP/OTLP shutdown, and dependency readiness.
- AgentCore IaC, JWT authorizer/header forwarding, private networking,
  distributed controls, and memory isolation.
- Secret rotation and audit of any previously staged CDK/AgentCore build assets.
- Release SBOM/signing/provenance and successful CI evidence for the exact
  artifact; backup/restore drills, load tests, and multi-replica failure tests.
