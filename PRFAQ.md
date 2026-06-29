# PRFAQ: AxonLLM — The Neural Control Plane for Enterprise LLMs

---

## Press Release

### AxonLLM: The Neural Control Plane for Enterprise LLMs

**Seattle, WA** — Today we announce AxonLLM, the neural control plane for enterprise LLMs — a centralized gateway that gives organizations a single API endpoint to access multiple large language model providers while maintaining full control over costs, access, security, and compliance. AxonLLM eliminates the operational complexity of integrating with multiple LLM providers by providing a unified OpenAI-compatible interface, intelligent request routing, hierarchical policy governance, PII redaction, prompt injection detection, immutable audit trails, multi-region failover, and a web-based admin console — all deployed as a serverless agent on Amazon Bedrock AgentCore.

**The Problem**

Organizations adopting LLMs face a growing challenge: they need access to models from multiple providers (OpenAI, Anthropic, AWS Bedrock, Azure OpenAI, Google Vertex AI, Cohere) to optimize for cost, capability, and availability. Each provider has its own API format, authentication mechanism, pricing model, and rate limits. Engineering teams end up building custom integration code for each provider, duplicating effort across projects. Platform teams lack visibility into who is using what, how much it costs, and whether usage complies with organizational policies. Security teams worry about sensitive data leaking into prompts, prompt injection attacks manipulating model behavior, and the absence of audit trails for compliance. When a provider goes down in one region, there is no automatic failover to another.

**The Solution**

AxonLLM sits between applications and LLM providers as an enterprise control plane. Developers send standard OpenAI-compatible requests to a single endpoint. AxonLLM handles everything else: authenticating requests via OIDC or API keys, resolving governance policies from a hierarchical tree (org → business unit → project → environment), enforcing quotas and budgets derived from those policies, detecting and blocking prompt injection attempts, redacting PII before it reaches the LLM, routing to the optimal provider and region, tracking costs, recording an immutable audit trail, and dispatching security events to external systems. Platform operators get a web-based admin console with nine dashboard pages to manage the full lifecycle.

**Key Capabilities**

- **Unified OpenAI-compatible API** — switch providers without changing application code
- **Intelligent routing** with six strategies: round-robin, weighted, least-latency, cost-optimized, smart (prompt-aware best-model selection), and ensemble (multi-model scatter-gather-synthesize)
- **Multi-region hub-and-spoke** — single-region, active-passive failover, or active-active with weighted distribution and data residency enforcement
- **Policy hierarchy governance** — org → BU → project → environment. Child inherits and can only tighten. Budget, rate limits, allowed models, PII settings all cascade.
- **PII redaction** — per-policy-node, regex-based detection for email, SSN, credit card, phone, IP address, AWS account ID, and medical records. Redacts before the LLM sees the prompt, re-injects original values in the response.
- **Prompt injection detection** — pattern-scored heuristics covering role override, system prompt extraction, delimiter escape, encoded payloads, and boundary injection. Configurable blocking threshold.
- **Immutable audit trail** — SHA-256 hash chain for tamper detection, DynamoDB persistence, every LLM request recorded with security metadata
- **Event dispatcher** — push security events (injection attempts, PII redactions, budget threshold crossings, auth failures) to webhooks, AWS SNS, or CloudWatch Logs
- **Budget threshold alerting** — automatic events at 80%, 90%, and 100% spend
- **Multi-strategy authentication** — ALB OIDC JWT, Bearer tokens, API keys with scoped permissions
- **Admin RBAC** — admin endpoints require `admin` role or `admin:*` scope in ENFORCE mode
- **Quota enforcement** — rate limit RPM, budget limit, max tokens per request, allowed models, allowed providers — all derived from the policy hierarchy
- **Automatic retry** with exponential backoff and multi-provider fallback chains for high availability
- **Comprehensive cost tracking** — prompt tokens, completion tokens, cached tokens, image tokens, reasoning tokens, per-request fees — with configurable budget limits at project and user level
- **Streaming** with PII re-injection — SSE streaming across all providers with token de-redaction in streamed chunks
- **Admin console** — 9 dashboard pages: Overview, Efficiency, Projects, Users, Models, Policies, Hierarchy, Quotas, Regions, API Keys, Audit Trail, Webhooks, Configuration, Health
- **DynamoDB persistence** for state recovery across restarts
- **860+ automated tests** including unit, integration, end-to-end, and property-based tests

**Customer Quote**

"Before AxonLLM, each of our product teams maintained their own LLM integrations. We had no visibility into total spend, no way to enforce access policies, and every provider outage was a fire drill. Security had no audit trail and no guarantee that PII wasn't leaking into model prompts. AxonLLM gave us a single control plane. We cut our integration code by 80%, reduced LLM costs by 30% through intelligent routing, and our platform team can now manage budgets, access policies, and security controls without touching application code. The audit trail alone cleared three compliance blockers."

— VP of Engineering, Enterprise Software Company

**Getting Started**

AxonLLM is deployed as a Python agent on Amazon Bedrock AgentCore Runtime. Configure your model mappings in a YAML file, set up your provider credentials, and launch. The admin console is available immediately at `/admin/dashboard`. Applications point their OpenAI SDK to the AxonLLM endpoint — no other code changes required.

---

## Frequently Asked Questions

### Customer FAQ

**Q: Do I need to change my application code to use AxonLLM?**

A: If your application already uses the OpenAI SDK or OpenAI-compatible API format, you only need to change the base URL to point to AxonLLM. The API accepts and returns OpenAI-compatible payloads. No changes to your request format, response handling, or streaming logic are required.

**Q: Which LLM providers are supported?**

A: AxonLLM ships with provider adapters for OpenAI, Anthropic, AWS Bedrock, Azure OpenAI, Google Vertex AI, and Cohere. Models like GPT-4, Claude, and Gemini Pro are configured as models that can route across one or more of these providers. Adding a new provider means implementing the provider adapter interface — no changes to routing, security, or cost tracking are needed.

**Q: How does routing work when I have the same model available from multiple providers?**

A: You configure a model (e.g., "claude-sonnet") that maps to one or more provider-specific endpoints with a routing strategy and fallback order. Strategies include round-robin, weighted, least-latency, cost-optimized, and smart (prompt-aware). If a provider is unhealthy, it is automatically excluded until it recovers. In multi-region mode, the region router first selects a healthy spoke before the provider router selects the specific endpoint.

**Q: Can AxonLLM consult multiple models and combine their answers?**

A: Yes — ensemble routing implements a scatter-gather-synthesize pattern. You define a preset with a panel of models and a judge model. The gateway dispatches the prompt to all panel models in parallel, gathers their responses, and the judge synthesizes them into one grounded answer. Presets include a quorum, a fallback policy, and an optional per-request cost ceiling. Invoke it by setting the model to `ensemble` or `ensemble:<preset>`.

**Q: How does the policy hierarchy work?**

A: Policies are organized in a tree: organization → business unit → project → environment. Each node can set limits (budget, rate limit RPM, max tokens, allowed models, allowed providers) and security settings (PII redaction types). Child nodes inherit from their parent and can only tighten — never loosen. Budget and rate limit use MIN (tightest wins). Allowed models/providers use INTERSECTION. PII redaction uses OR (once enabled by a parent, children cannot disable it). PII types use UNION (children can add types but not remove them).

**Q: How does PII redaction work?**

A: When PII redaction is enabled for a project (via the policy hierarchy), AxonLLM scans all message content before it reaches the LLM. Detected PII (email addresses, SSNs, credit card numbers, phone numbers, IP addresses, AWS account IDs, medical record numbers) is replaced with indexed tokens like `[EMAIL_1]`, `[SSN_2]`. The LLM processes the redacted prompt. When the response comes back — including in streaming mode — tokens are replaced with the original values before being returned to the caller. An audit record is created documenting what was redacted.

**Q: How does prompt injection detection work?**

A: AxonLLM analyzes all messages for common injection patterns: role override attempts ("ignore previous instructions"), system prompt extraction ("show me your instructions"), delimiter escapes, encoded payloads, and boundary injection. Each pattern has a weight score. If the cumulative score exceeds the configurable blocking threshold (default: HIGH/0.7), the request is rejected with a 400 status. All detections — whether blocked or not — are recorded in the audit trail and dispatched to configured event destinations.

**Q: What happens when a provider goes down?**

A: AxonLLM automatically retries retryable errors (429, 500, 502, 503, 504) with exponential backoff. If retries are exhausted, it falls back to the next provider in the configured fallback chain. In multi-region mode, if an entire spoke becomes unhealthy (consecutive failure threshold exceeded), traffic is automatically routed to healthy spokes. Manual failover is also available via the admin API.

**Q: How do I set spending limits?**

A: Budget limits are set in the policy hierarchy. An organization might set a $50,000 monthly limit, a business unit inherits that and sets $20,000, and a project further limits to $5,000. The resolved limit for any project is the most restrictive in its ancestry chain. When spend reaches 80%, 90%, or 100% of the budget, events are fired to configured destinations (webhook, SNS, CloudWatch). At 100%, further requests are rejected.

**Q: How does the audit trail work?**

A: Every LLM request, security event (injection detected/blocked, PII redaction, auth failure), and administrative action (key issued/revoked/rotated) is recorded in an append-only audit trail. Records form a SHA-256 hash chain — each record's hash includes the previous record's hash, making tampering detectable. The audit trail can be queried via the admin API and its integrity verified with a single API call. Records are persisted to DynamoDB when enabled.

**Q: How does authentication work?**

A: AxonLLM supports three authentication strategies in priority order: (1) ALB OIDC header — for deployments behind an Application Load Balancer with OIDC integration, (2) Bearer token — either an OIDC JWT or an API key prefixed with `axon_`, (3) X-Api-Key header. API keys are scoped to a project and can have restricted scopes. In ENFORCE mode, unauthenticated requests are rejected. In LOG_ONLY mode, they proceed as anonymous.

**Q: Can I restrict who can access admin endpoints?**

A: Yes. The AdminRBACMiddleware checks that the authenticated user has either the `admin` role or a scope matching `admin:*`. In ENFORCE mode, requests without this are rejected with 403. In LOG_ONLY mode, access is allowed but logged.

**Q: Does AxonLLM support streaming?**

A: Yes. When a client sets `stream: true`, AxonLLM returns Server-Sent Events (SSE). If PII redaction is active, tokens are re-injected in each streamed chunk so the caller sees original values throughout the stream. If the target provider supports native streaming, chunks are forwarded as they arrive. If not, AxonLLM simulates streaming.

**Q: How does multi-region work?**

A: AxonLLM uses a hub-and-spoke topology. A hub configuration defines spokes (regions) with their roles (primary, failover, active), health status, weights, supported models, and data residency zones. The region router selects a spoke based on health, data residency constraints, model availability, and weight. Three modes are supported with the same code: single-region (one spoke), active-passive (primary + failover with weight=0), and active-active (N spokes with weights). Spoke health is monitored with configurable check intervals and consecutive-failure thresholds.

**Q: What about data residency requirements?**

A: Each spoke can declare which data residency zones it serves. When data residency strict mode is enabled, requests with a zone constraint are only routed to spokes that serve that zone. If no matching healthy spoke exists, the request is rejected with a 503 rather than routing data to a non-compliant region.

**Q: How do I monitor the system?**

A: The admin console at `/admin/dashboard` provides views for: Overview (spend, requests, health), Efficiency (token waste, prompt quality), Projects, Users, Models, Policies (CRUD), Hierarchy (tree visualization), Quotas (lookup, simulate, reset), Regions (topology, health, failover), API Keys (issue, rotate, revoke), Audit Trail (query, verify), Webhooks (destinations, test), Configuration, and Health.

### Internal FAQ

**Q: Why build this instead of using an existing LLM gateway?**

A: Existing solutions (LiteLLM, Portkey, etc.) are standalone services that require separate infrastructure management and lack enterprise governance features. AxonLLM provides: (1) hierarchical policy governance that mirrors enterprise org structures, (2) integrated PII redaction and injection detection in the request pipeline, (3) immutable audit trails for compliance, (4) multi-region failover with data residency, (5) tight integration with AWS services (Bedrock, DynamoDB, SNS, CloudWatch). By building on AgentCore, we get serverless scaling, managed session persistence, built-in authentication, and observability.

**Q: Why OpenAI-compatible API format?**

A: OpenAI's chat completions format has become the de facto standard. Most LLM client libraries and frameworks already support it. By adopting this format, we minimize migration effort for existing applications and maximize compatibility with the ecosystem.

**Q: What is the adapter pattern and why was it chosen?**

A: Each LLM provider has a `ProviderAdapter` class that implements a fixed interface: `translate_request`, `translate_response`, `translate_stream_chunk`, `list_models`, and `health_check`. This isolates provider-specific logic from the core gateway. Adding a new provider means writing one adapter class — routing, security, cost tracking, audit, caching, guardrails, and the admin dashboard all work automatically.

**Q: How is the system tested?**

A: The project has 864+ automated tests across multiple layers: unit tests for individual components, integration tests verifying service interactions, end-to-end tests that hit the full Starlette app through TestClient (auth → quota → injection → PII → routing → audit), and Hypothesis property-based tests for formal correctness properties. The E2E tests prove the complete security pipeline works when all middleware and services are wired together.

**Q: What is the deployment model?**

A: The gateway is deployed as a Python agent on AgentCore Runtime using `agentcore configure` and `agentcore launch`. It also runs standalone via Docker or plain Python (`python serve_dashboard.py`). Configuration is managed through YAML files for model mappings and provider credentials, and through the admin API/console for policies, keys, and webhooks. DynamoDB persistence is available for state recovery. Auth mode (`LOG_ONLY` or `ENFORCE`) controls whether security is advisory or blocking.

**Q: How does the request pipeline work internally?**

A: The request flows through 15 ordered steps in `GatewayAgent.handle_chat_completion()`:

1. Parse request
2. Extract identity context
3. Request validation
4. **Policy hierarchy resolution** — walk org→BU→project→env tree
5. **Quota enforcement** — check model, provider, budget, RPM, max tokens against resolved policy
6. **Injection detection** — score messages, block if HIGH+, audit + dispatch
7. **PII redaction** — replace sensitive data with tokens, store mapping
8. Rate limiting (sliding window)
9. Project/user model access checks
10. Project/user budget checks
11. Guardrails (content policy)
12. Cache lookup
13. **Region routing** — select healthy spoke (data residency, model availability)
14. **Provider routing** — strategy-based with fallback
15. **Post-response** — response guardrails → PII re-injection → audit trail → cost tracking → budget alerts → event dispatch

Steps 4–7 and 13 are the enterprise governance layer; they are no-ops when services are not configured, allowing the gateway to run in lightweight mode for development.

**Q: How does the multi-provider factory work?**

A: The `MultiProviderFactory` routes Bedrock requests through boto3 (native AWS SDK) and all other providers through an async HTTP client with session pooling. Provider configs are loaded from `config/providers.yaml` with environment variable overrides for API keys. This allows the same model to be served by multiple providers with automatic failover.

**Q: What are the scaling characteristics?**

A: AgentCore Runtime provides serverless scaling — the gateway agent scales automatically based on request volume. In-memory components (rate limiter counters, health tracker state, cache, audit buffer) are per-instance. For multi-instance deployments, DynamoDB provides shared state. The audit trail and event dispatcher are async/fire-and-forget and don't add latency to the request path.

**Q: What is the security posture?**

A: Defense in depth: (1) Auth middleware rejects unauthenticated requests in ENFORCE mode, (2) Admin RBAC restricts admin endpoints to authorized users, (3) Quota enforcement prevents resource abuse, (4) Injection detection blocks manipulation attempts, (5) PII redaction prevents data leakage to models, (6) Audit trail provides non-repudiation with tamper detection, (7) Event dispatcher enables real-time alerting on security events. All security features are policy-driven and can be enabled per org/project without code changes.
