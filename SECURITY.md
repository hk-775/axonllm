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
- Injection vulnerabilities (prompt injection bypass, XSS). There is no SQL
  anywhere in the gateway — persistence is DynamoDB only — so SQL injection is
  not an applicable class here.
- PII redaction bypass
- Audit trail tampering
- Credential exposure
- Denial of service via resource exhaustion

### Known, and therefore not a vulnerability report

**Canonical identity is a migration gate and defaults off.** With
`AXON_REQUIRE_CANONICAL_IDENTITY=false`, legacy credential claims can still
supply roles, scopes, tenant, and project authority. The production checklist
reports this as `FAIL`. Production must provision canonical principal records,
enable DynamoDB, run `AXON_AUTH_MODE=ENFORCE`, and set the gate to `true`.

**The current admin control plane is not tenant-scoped.** Canonical tenant RBAC
protects the mapped model-list and inference data plane. Existing `/admin/*`
routes still use legacy `admin` roles and `admin:*` scopes, and the underlying
projects, users, usage, keys, policies, quotas, webhooks, SCIM, and audit records
are not all tenant-qualified. `tenant_admin` intentionally does not imply
current admin-route access. Canonical mode also denies unmapped `/api/*` and
`/v1/*` routes, including the fleet-wide `GET /api/users`. Do not expose one
admin plane to multiple untrusted tenants.

**Project grants are temporary containment, not ownership proof.** Project
records are still globally keyed and do not carry an authoritative tenant owner.
Every tenant role, including `tenant_admin`, therefore needs an explicit
server-held project grant. This blocks arbitrary project selection but does not
make colliding project ids tenant-safe. Use one isolated deployment per tenant
until project lookup and every dependent key are tenant-qualified.

**API-key behavior differs by migration mode.** In legacy mode, key scopes gate
`/admin/*` but do not constrain chat; project model and budget controls are the
effective data-plane boundary. In canonical mode, the key must resolve to a
server-held `service` principal with a project grant and explicit
`model.list`/`inference.invoke` action scopes. Existing key metadata is not
automatically converted into that principal. Keys default to no expiry, and
rotation preserves the old expiry.

**ALB validation is implemented, but the reference stack does not wire it.**
The validator binds ES256 tokens to the configured ALB signer, client, regional
issuer, expiry, and `X-Amzn-Oidc-Identity`. The checked-in Fargate stack does not
configure an ALB authenticate action or the required `AXON_ALB_*` trust values.
It does place an internal TLS ALB behind CloudFront VPC Origin, require WAF/TLS
and approved-egress inputs, and keep tasks private. Do not accept ALB headers
until the authentication action and trust values are complete, and restrict the
ALB from its VPC-wide bootstrap ingress rule to the CloudFront-created
VPC-origin service security group after deployment.

**AgentCore is not a turnkey production deployment.** The adapter is fail closed,
but release deployment still requires a JWT authorizer/header-forwarding
contract, reviewed IaC, private networking, readiness, and tenant-safe memory
design. Initialization is lazy, retained usage and audit state are scanned at
startup, failed project hydration can leave controls absent, distributed
rate/cache/audit semantics are incomplete, and graceful resource shutdown is
not wired. Its dependency export is hash-pinned and includes the runtime, OIDC,
and OIDC HTTP-client packages. See
[ENTERPRISE_HARDENING.md](ENTERPRISE_HARDENING.md) for the exact implemented
boundary and remaining blockers.

Reports that exceed these documented boundaries are still in scope. Examples
include a cross-tenant data-plane read, authorization with an inactive canonical
membership, a revoked or expired key remaining valid beyond documented cache
behavior, or any way to bypass project grants in canonical mode.

## Recognition

We appreciate security researchers who report responsibly. Contributors who report valid vulnerabilities will be credited in the changelog (unless they prefer anonymity).
