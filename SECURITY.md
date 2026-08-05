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
| 0.1.x   | Yes       |

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

**API key scopes are not enforced on the data plane.** Scopes are carried on the
request context and checked by admin RBAC for `/admin/*`, but nothing consults
them on `/v1/*`: a key issued `["models:read"]` — or `[]` — can still call
`/v1/chat/completions` and spend money. What actually bounds a key is its
project's `allowed_models` and budget. Keys also default to no expiry, and
rotation carries the old expiry through, so revocation is the only thing that
reliably stops one. This is documented rather than fixed, it is reported by
`/admin/production-checklist` as a WARN, and it is stated here because it sits
squarely inside "authorization bypass" above. A report that a low-scope key can
reach the data plane is expected behaviour; a report that a *revoked* or
*expired* key can, or that project `allowed_models`/budget can be bypassed, is a
vulnerability.

## Recognition

We appreciate security researchers who report responsibly. Contributors who report valid vulnerabilities will be credited in the changelog (unless they prefer anonymity).
