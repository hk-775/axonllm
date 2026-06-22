# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in AxonLLM, please report it responsibly.

**Do NOT open a public GitHub issue for security vulnerabilities.**

Instead, please email: **security@axonllm.dev**

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
- Injection vulnerabilities (prompt injection bypass, SQL injection, XSS)
- PII redaction bypass
- Audit trail tampering
- Credential exposure
- Denial of service via resource exhaustion

## Recognition

We appreciate security researchers who report responsibly. Contributors who report valid vulnerabilities will be credited in the changelog (unless they prefer anonymity).
