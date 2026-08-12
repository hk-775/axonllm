# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in AxonLLM, please report it responsibly.

**Do NOT open a public GitHub issue for security vulnerabilities.**

Instead, use GitHub's private vulnerability reporting: the **Security** tab →
**Report a vulnerability**. That keeps the report private to the maintainers
until a fix ships, and it needs no third-party service.

This project deliberately publishes no security email address. An earlier version
of this file listed `security@axonllm.dev`, a domain with no DNS record, so mail
to it bounced silently — worse than naming no address at all, because a reporter
believes they have disclosed when nobody has received anything. Private
vulnerability reporting has no such failure mode: it is delivered in GitHub, so
it cannot be misaddressed, and you can see your own report.

Include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

## Response Timeline

- **Acknowledgment**: within 48 hours
- **Initial assessment**: within 5 business days
- **Fix timeline**: depends on severity, typically within 30 days for critical issues

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.2.x   | Yes       |
| 0.1.x   | No        |

## Scope

The following are in scope:
- Authentication/authorization bypass
- Injection vulnerabilities, including prompt-injection bypass, XSS, and any
  way to escape the read-only Athena SQL policy. Gateway persistence is
  DynamoDB, but the optional query plane parses and executes bounded Athena
  `SELECT` statements against explicitly bound tenant datasources.
- PII redaction bypass
- Audit trail tampering
- Credential exposure
- Denial of service via resource exhaustion

### Documented security boundaries

**Legacy authority is development-only migration behavior.** The
`development` profile can run without canonical identity and can honor legacy
credential roles and `admin:*` scopes. The `production` profile refuses startup
unless authentication is `ENFORCE`, canonical identity is required, and
DynamoDB persistence is enabled. A way to start a production-profile runtime
without those controls is in scope.

**Project ownership and grants are separate checks.** Canonical HTTP and
AgentCore ingress strongly resolve
`PK=TENANT#{tenant_id}, SK=PROJECT#{project_id}` before RBAC. A missing row is
concealed as 404 and an unavailable or malformed store fails closed with 503.
The resolved project is propagated through model listing, chat, policy, usage,
and cache processing. Every tenant role, including `tenant_admin`, still needs
an explicit server-held project grant for project-scoped data-plane actions.

**Canonical control-plane access is tenant qualified.** Tenant members and
auditors can read but cannot mutate. Tenant administrators can update
tenant-owned projects, membership, keys, policies, quotas, webhooks, SCIM state,
and configuration. Service principals have no control-plane access. Platform
administrators need an attributed break-glass reason and explicit
`X-Axon-Target-Tenant` selector before tenant dispatch. Legacy `admin` and
`admin:*` compatibility applies only outside canonical production operation.

**Some API surfaces are intentionally absent.** Canonical mode denies unmapped
`/api/*` and `/v1/*` routes. `GET /api/users` remains unavailable because its
legacy aggregate has no safe tenant selector. When exact Athena role bindings
are configured, `query.select` gates both `POST /v1/query` and the AgentCore
`query` action through the same datasource authority, AST policy, admission,
lifecycle, and audit service. `query.mutate` always denies.

**Interrupted query state is reconciled, not trusted.** A fenced periodic worker
claims expired accepted/running records, cancels or observes known Athena
executions to terminal state, atomically reconciles admission accounting, and
replays pending terminal audit writes. A running record whose datasource or
exact deployment binding cannot be re-established is deferred without releasing
its accounting state or inventing a terminal result.

**Canonical key lifecycle has a separate audit transaction.** Canonical issue,
rotation, and revocation transactionally update tenant-qualified key and service
principal state. Keys default to 90 days and cannot exceed 365 days. Credential
mutation and hash-chain audit append are separate DynamoDB transactions. Failed
issue or rotation audits trigger credential containment. Revocation can succeed
while its audit append returns 503, so operators must reconcile that response
against durable key state.

**Break glass has no standalone tenant registry.** Middleware validates target
syntax, canonical platform authority, reason, and request consistency, then
emits the pre-dispatch audit. Each handler performs the authoritative
tenant-qualified resource lookup. The absence of a separate tenant registry is
a documented design constraint; bypassing those handler lookups is in scope.

**AgentCore memory is not enabled.** The checked-in AgentCore stack provides JWT
authorization, private networking, encrypted durable state, readiness, logging,
tracing, backups, alarms, and an immutable image gate. The runtime does not
currently persist conversation memory. A future memory implementation must
namespace state by canonical tenant, principal, project, and an opaque
server-controlled session identifier.

**Repository controls are not deployment certification.** `v0.2.4` completed
the private-ECR/KMS release flow for both image targets, but a hardened runtime
deployment, target-account restore exercise, and application cutover rehearsal
remain external release evidence. See
[ENTERPRISE_HARDENING.md](ENTERPRISE_HARDENING.md),
[the production runbook](docs/PRODUCTION_RUNBOOK.md), and
[the AgentCore runbook](docs/AGENTCORE_RUNBOOK.md).

Reports that exceed these documented boundaries are still in scope. Examples
include a cross-tenant data-plane read, authorization with an inactive canonical
membership, a revoked or expired key remaining valid beyond documented cache
behavior, or any way to bypass project grants in canonical mode.

## Recognition

We appreciate security researchers who report responsibly. Contributors who report valid vulnerabilities will be credited in the changelog (unless they prefer anonymity).
