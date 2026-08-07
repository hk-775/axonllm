# Product Requirements Document: AxonLLM

## The Neural Control Plane for Enterprise LLMs

| Field              | Value                                      |
|--------------------|--------------------------------------------|
| **Product Name**   | AxonLLM                                    |
| **Author**         | Amazon Bedrock Product Management           |
| **Status**         | Draft                                       |
| **Version**        | 1.1                                         |
| **Date**           | 2026-08-05 (reconciled against the code)    |
| **Target Launch**  | TBD                                         |
| **Classification** | Internal / Confidential                     |

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Problem Statement](#2-problem-statement)
3. [Goals and Non-Goals](#3-goals-and-non-goals)
4. [Customer Segments](#4-customer-segments)
5. [User Personas and Use Cases](#5-user-personas-and-use-cases)
6. [Functional Requirements](#6-functional-requirements)
7. [Non-Functional Requirements](#7-non-functional-requirements)
8. [System Architecture](#8-system-architecture)
9. [API Specification](#9-api-specification)
10. [Data Model](#10-data-model)
11. [Security and Compliance](#11-security-and-compliance)
12. [Deployment and Operations](#12-deployment-and-operations)
13. [Observability and Monitoring](#13-observability-and-monitoring)
14. [Success Metrics and KPIs](#14-success-metrics-and-kpis)
15. [Dependencies and Risks](#15-dependencies-and-risks)
16. [Release Milestones](#16-release-milestones)
17. [Open Questions and Future Considerations](#17-open-questions-and-future-considerations)
18. [Appendices](#18-appendices)

---

## 1. Executive Summary

Organizations adopting large language models face compounding operational complexity: each new provider introduces a distinct API format, authentication mechanism, pricing model, and failure mode. Engineering teams duplicate integration code across projects, platform teams lack visibility into who is using what and at what cost, and provider outages cause cascading application failures with no automated recovery.

AxonLLM is a unified gateway that sits between applications and LLM providers, giving platform teams a single control plane for intelligent request routing, cost tracking, budget enforcement, access control, content guardrails, and observability. Applications send requests to AxonLLM using a standard OpenAI-compatible API. AxonLLM handles everything else: translating requests to the target provider's format, routing to the optimal provider based on configurable strategies, retrying on failures, falling back to alternative providers, tracking costs per request/user/project, enforcing budgets, applying content guardrails, and surfacing real-time usage analytics through a web-based admin console.

AxonLLM's full HTTP and admin surface runs as a Starlette application. A separate Amazon Bedrock AgentCore adapter currently provides a hardening preview for chat, model listing, and liveness; it is not a production-complete deployment.

---

## 2. Problem Statement

### 2.1 Current State

Enterprise organizations increasingly depend on LLMs from multiple providers (AWS Bedrock, Anthropic, OpenAI, Azure OpenAI, Google Vertex AI, Cohere) to optimize for cost, capability, latency, and availability. Each provider presents its own:

- **API format** requiring custom integration code per provider
- **Authentication mechanism** (AWS IAM, API keys, Azure AD, GCP service accounts)
- **Pricing model** with varying token granularity (input/output, cached, reasoning, image, per-request fees)
- **Rate limits** and throttling behavior
- **Failure modes** and error response formats

### 2.2 Pain Points

| Pain Point | Impact |
|---|---|
| **Integration fragmentation** | Each product team maintains its own LLM integrations, duplicating effort and creating inconsistent implementations across the organization |
| **No cost visibility** | No centralized view of total LLM spend. Finance and platform teams cannot attribute costs to teams, projects, or individuals |
| **No spend controls** | When usage spikes, there is no circuit breaker. A single runaway process can exhaust the organization's API quota or budget |
| **No access governance** | No mechanism to restrict which models each team or user can access. Sensitive workloads may inadvertently use non-approved providers |
| **No resilience** | When a provider experiences an outage, applications break. Recovery requires manual intervention and code changes |
| **No content safety net** | No centralized guardrails to prevent harmful content from reaching models or being returned to users |

### 2.3 Desired Outcome

A single gateway endpoint that applications target instead of individual providers, giving platform teams centralized control over routing, cost, access, safety, and observability without requiring changes to application code.

---

## 3. Goals and Non-Goals

### 3.1 Goals

| ID | Goal | Success Criteria |
|----|------|-----------------|
| G1 | **Unified API** | Applications use a single OpenAI-compatible endpoint to access any supported LLM provider. Provider switch requires zero application code changes. |
| G2 | **Intelligent routing** | Requests are routed to the optimal provider based on configurable strategies (round-robin, weighted, least-latency, cost-optimized, smart, and ensemble) with automatic failover. |
| G3 | **Cost management** | Every request is tracked with full token-level cost attribution (including cached, reasoning, image, and per-request fees) at the project and user level, with configurable budget limits and alerts. |
| G4 | **Access control** | Per-project and per-user model access restrictions are enforced at the gateway level before requests reach any provider. |
| G5 | **Content safety** | Configurable guardrail rules (keyword blocking, regex matching, content category filtering) inspect requests and responses, with block/warn/redact actions. |
| G6 | **High availability** | Automatic retry with exponential backoff on transient failures, multi-provider fallback chains, and health-aware routing ensure application continuity during provider outages. |
| G7 | **Operational visibility** | A web-based admin console provides real-time dashboards for usage monitoring, cost analytics, project management, and provider health status. |
| G8 | **Managed AgentCore deployment** | Graduate the current three-action AgentCore hardening preview to a production deployment with reviewed infrastructure, identity, networking, readiness, and distributed-state controls. |

### 3.2 Non-Goals

| ID | Non-Goal | Rationale |
|----|----------|-----------|
| NG1 | Prompt engineering or prompt management | AxonLLM routes and governs requests; it does not modify or optimize prompts. Prompt management is a separate concern. |
| NG2 | Fine-tuning or model training | The gateway operates on inference endpoints only. Model customization is handled by the underlying providers. |
| NG3 | Replacing provider-native SDKs for all use cases | Applications with deep provider-specific requirements (e.g., fine-tuned model deployments, provider-native tooling) may still use provider SDKs directly. |
| NG4 | End-user authentication | AxonLLM handles service-to-service authentication and authorization. End-user identity management is the responsibility of the consuming application. |
| NG5 | ~~Multi-region active-active deployment~~ **No longer a non-goal — shipped.** | `multi_region/` implements hub-and-spoke in single-region, active-passive and active-active weighted modes, with spoke health monitoring, failover and data-residency filtering. What remains out of scope is *shared runtime state* across regions: the cache, rate limiter, quota counters and health tracker are per-task, so a multi-region deployment enforces its limits per instance. |

---

## 4. Customer Segments

### 4.1 Primary: Platform Engineering Teams

Teams responsible for providing shared infrastructure and developer tooling across the organization. They need centralized governance over LLM usage without becoming a bottleneck for individual product teams.

**Key needs:** Cost attribution, budget enforcement, access control, provider management, usage analytics.

### 4.2 Secondary: Application Development Teams

Teams building LLM-powered features who need reliable, low-friction access to models without managing provider integrations, credentials, or failover logic.

**Key needs:** Simple API, provider abstraction, streaming support, automatic failover.

### 4.3 Tertiary: Finance and Security/Compliance

Stakeholders who need visibility into LLM spend and assurance that usage complies with organizational policies.

**Key needs:** Cost reporting, access audit trails, content guardrails, policy enforcement.

---

## 5. User Personas and Use Cases

### 5.1 Personas

#### Platform Administrator (Primary)

- Manages LLM provider credentials and model configurations
- Sets up projects with budget limits, access policies, and guardrail rules
- Monitors usage dashboards and responds to budget alerts
- Onboards new teams by creating projects and assigning model access

#### Application Developer (Secondary)

- Integrates applications with the AxonLLM API
- Selects models without needing to know which provider serves them
- Relies on automatic failover for application resilience
- Uses the playground and routing explorer to understand routing behavior

#### Security/Compliance Officer (Tertiary)

- Reviews Cedar authorization policies
- Configures content guardrails for sensitive projects
- Audits usage logs for policy compliance
- Sets model access restrictions based on data classification requirements

### 5.2 Use Cases

| ID | Use Case | Persona | Priority |
|----|----------|---------|----------|
| UC1 | **Route a chat completion request to the optimal provider** | Developer | P0 |
| UC2 | **Enforce project-level budget limits and alert on threshold** | Admin | P0 |
| UC3 | **Restrict which models a project or user can access** | Admin | P0 |
| UC4 | **Automatically fail over when a provider is unavailable** | Developer | P0 |
| UC5 | **View real-time cost and usage analytics per project/user/model** | Admin | P0 |
| UC6 | **Block requests containing prohibited content via guardrails** | Security | P0 |
| UC7 | **Stream chat completions via SSE for interactive applications** | Developer | P0 |
| UC8 | **Manage projects, users, models, and policies via admin console** | Admin | P1 |
| UC9 | **Cache repeated prompts to reduce cost and latency** | Admin | P1 |
| UC10 | **Rate limit users/projects to prevent abuse** | Admin | P1 |
| UC11 | **Leverage prompt caching at the provider level (Anthropic/Bedrock)** | Developer | P1 |
| UC12 | **Explore routing decisions interactively via routing explorer** | Developer | P2 |
| UC13 | **Define Cedar authorization policies for fine-grained access control** | Security | P2 |
| UC14 | **Persist state to DynamoDB for recovery across restarts** | Admin | P1 |

---

## 6. Functional Requirements

### 6.1 Multi-Provider Routing

| ID | Requirement | Details |
|----|-------------|---------|
| FR-R1 | **Model abstraction** | Models are defined as models that map to one or more provider-specific endpoints. Callers reference model names (e.g., `claude-opus`) without knowing the underlying provider. |
| FR-R2 | **Routing strategies** | Five configurable strategies per model, plus a sixth mode that is not a per-provider selection at all: **round-robin** (sequential cycling, with an independent cursor per provider set so two models cannot advance each other's), **weighted** (proportional distribution by configured weights), **least-latency** (route to fastest provider in sliding window), **cost-optimized** (route to cheapest healthy provider based on token pricing), **smart** (classify the prompt and select the best-performing model across all providers using benchmark leaderboard scores), **ensemble** (scatter-gather-synthesize across a panel of models with a judge — see FR-R7). The first four are registered in `Router._strategies`; smart is registered when a smart strategy is supplied; ensemble is a separate scatter-gather code path rather than a member of the strategy map. |
| FR-R3 | **Automatic retry** | Retryable errors (HTTP 429, 500, 502, 503, 504) are retried with backoff (base delay 1s, max 3 retries). Delays are **jittered**, not fixed: `RetryConfig.jitter = 0.5` draws each delay from `[0.5·full, full]` (equal jitter), so a fleet that fails together does not retry in lockstep. The deterministic 1/2/4s sequence occurs only at `jitter=0`. Non-retryable errors (400, 401, 403) skip directly to fallback. |
| FR-R4 | **Multi-provider fallback** | When retries are exhausted, the router falls back to the next provider in the configured fallback chain (ordered by `fallback_order`). The caller receives a seamless response. |
| FR-R5 | **Health-aware routing** | Providers experiencing repeated failures are automatically marked unhealthy and excluded from routing for a configurable cooldown period (default 60s). Background health checks restore providers when recovered. |
| FR-R6 | **Provider preference** | Callers may optionally specify a preferred provider. If the preferred provider is available, it is used first; otherwise, the standard fallback chain applies. |
| FR-R7 | **Ensemble routing** | A scatter-gather-synthesize strategy invoked via model `ensemble` or `ensemble:<preset>`. Dispatches the prompt concurrently to a configurable panel (1–10 models), gathers successful responses, and a judge model synthesizes them into one grounded answer. Each preset defines a `panel`, `judge`, `quorum` (minimum survivors required to synthesize, default 1), `fallback_policy` (`best-single` or `error`), optional `cost_ceiling` (per-request USD cap enforced before dispatch), and `ranking_criteria`. Access control validates the full panel + judge; cost is tracked per underlying call; panel latency is bounded by the slowest member (60s per-member timeout) plus the judge. Presets are defined in `config/ensemble.yaml`. |

### 6.2 Provider Adapters

| ID | Requirement | Details |
|----|-------------|---------|
| FR-A1 | **Provider adapter interface** | Each provider implements a `ProviderAdapter` with: `translate_request()`, `translate_response()`, `translate_stream_chunk()`, `list_models()`, `health_check()`. Only the first three are `@abstractmethod`; `list_models()` and `health_check()` have default implementations, so a new adapter can omit them. |
| FR-A2 | **Supported providers** | Thirteen: AWS Bedrock (via boto3), Bedrock Mantle, Anthropic (HTTP), OpenAI (HTTP), Azure OpenAI (HTTP), Google Vertex AI (HTTP), Google AI (HTTP), Cohere (HTTP), xAI, Groq, Together, Fireworks, AI21. `VALID_PROVIDERS` in `config.py` is the authoritative list. |
| FR-A3 | **Dual execution paths** | Bedrock requests use boto3 native SDK (invoke_model for Anthropic models, converse API for others). All non-Bedrock providers use async HTTP with session pooling. |
| FR-A4 | **Request/response normalization** | All provider-specific payloads are translated to/from a unified OpenAI-compatible format (ChatCompletionRequest/Response). Provider differences (field names, message formats, system prompt handling) are abstracted. |
| FR-A5 | **Streaming translation** | Each adapter translates provider-specific SSE events into a unified StreamChunk format. True streaming is the primary path — `HttpClient.execute_streaming` reads the provider's own SSE and translates chunk by chunk. Simulated streaming (word-level chunking of a complete response) is the *fallback*, for providers or routes without a native stream. |
| FR-A6 | **Tool-call translation** | Each adapter translates OpenAI-shaped `tools`/`tool_choice` into its provider's dialect and translates the model's tool call back into `tool_calls` — see 6.10. |

### 6.3 Cost Tracking and Budget Management

| ID | Requirement | Details |
|----|-------------|---------|
| FR-C1 | **Per-request cost tracking** | Every request records: request_id, project_id, user_id, provider, model, prompt_tokens, completion_tokens, total_tokens, cost, timestamp, cached_tokens, cache_creation_tokens, image_tokens, reasoning_tokens. |
| FR-C2 | **Comprehensive cost calculation** | Cost engine accounts for: standard input/output tokens, cached input tokens (discounted rate), cache creation tokens, image/vision tokens, reasoning tokens (o1/o3/o4 models), and flat per-request fees. Pricing is configurable per provider/model in YAML. |
| FR-C3 | **Project-level budgets** | Each project has a configurable `budget_limit` and `alert_threshold`. When spend reaches the alert threshold, a notification is emitted. When spend exceeds the budget limit, further requests are rejected with HTTP 429. |
| FR-C4 | **User-level budgets** | Individual users have independent budget limits and alert thresholds, enforced in addition to project-level budgets. |
| FR-C5 | **Usage aggregation** | Usage data is queryable with filters: time range, provider, model, project_id, user_id. Reports include totals and breakdowns by provider, model, project, and user. |
| FR-C6 | **Token estimation** | When providers don't return token counts, tiktoken is used for estimation (cl100k_base encoding fallback). |

### 6.4 Access Control

| ID | Requirement | Details |
|----|-------------|---------|
| FR-AC1 | **Project model access lists** | Each project has a configurable list of allowed models. Requests to models not in the list are rejected with HTTP 403 before reaching any provider. |
| FR-AC2 | **User model access lists** | Individual users have configurable allowed model lists. The effective allowed set is the intersection of project and user lists when both are set. |
| FR-AC3 | **JWT authentication** | Requests carry OIDC JWTs, verified in `auth/oidc_service.py` against the issuer's JWKS using `python-jose`. Claims are extracted into a RequestContext (user_id, project_id, roles, scopes). Verification is **fail-closed**: if `python-jose` is absent, decoding is refused rather than falling back to an unverified read of the token. |
| FR-AC4 | **Cedar policy evaluation** | Fine-grained authorization via Cedar policies evaluated against (principal, action, resource) tuples. Policies support ENFORCE and LOG_ONLY modes. |
| FR-AC5 | **API keys** | `auth/api_key_service.py` issues project-scoped `axon_`-prefixed keys, stored as SHA-256 hashes; the plaintext is returned once and never persisted. Tenant-qualified production issuance conditionally creates the primary row, global hash lookup, and tenant/project edge in one DynamoDB transaction. Revocation atomically changes key state and advances a per-tenant epoch used to evict replica caches. Supports validate and non-atomic revoke-then-create rotation, with a 300s validation cache and five-second epoch polling. With persistence disabled, keys live in an in-memory store for local development. |
| FR-AC6 | **Scope enforcement depends on identity mode** | In legacy mode, request-context scopes gate `/admin/*`; `/v1/*` and `/api/chat` do not consult them, so project model and budget controls are the effective data-plane boundary. In canonical mode, a key must resolve to a server-held `service` principal and its stored action scopes gate mapped data-plane actions. Key metadata is not automatically converted into that principal. Keys default to **no expiry**, and rotation carries the old `expires_at` through. Reported by `/admin/production-checklist` and documented in `SECURITY.md`. |
| FR-AC7 | **Enterprise identity** | SAML 2.0 SSO (`auth/saml_service.py`, assertion signature verification via `signxml`) and SCIM 2.0 user/group provisioning (`auth/scim_service.py`) are exposed at `/saml/*` and `/scim/v2/*`. |
| FR-AC8 | **Canonical tenant identity** | With `AXON_REQUIRE_CANONICAL_IDENTITY=true`, verified credential hints resolve through strongly consistent DynamoDB reads to an active server-held principal. Credential roles, scopes, project grants, principal id, and authorization version are replaced rather than merged. Missing, inactive, ambiguous, or malformed authority fails closed. |
| FR-AC9 | **Authoritative project ownership** | Canonical HTTP and AgentCore requests strongly resolve `PK=TENANT#{tenant_id}`, `SK=PROJECT#{project_id}` before RBAC. Missing ownership is concealed as 404; an unavailable or malformed store returns 503. The exact resolved project is propagated through model listing and inference so colliding project ids cannot select another tenant's configuration. |

### 6.4.1 Policy Hierarchy and Quotas

| ID | Requirement | Details |
|----|-------------|---------|
| FR-PH1 | **Four-level hierarchy** | `auth/policy_hierarchy.py` resolves effective policy across `org > business_unit > project > environment`, walking leaf to root. |
| FR-PH2 | **Child tightens only** | The invariant is that a child can never exceed its parent. Numeric limits resolve to `min(parent, child)`; model and provider lists resolve to the intersection. A business unit cannot grant itself more budget than its org allows. |
| FR-PH3 | **Resolution caching** | Resolved policies are cached for 300s, so the hierarchy walk is not repeated per request. |
| FR-PH4 | **Quota enforcement** | `quota_enforcer.py` turns a `ResolvedPolicy` into request-time checks: `rate_limit_rpm` (its own sliding window keyed by project), `budget_limit` against projected spend, `max_tokens_per_request` as a cap on the requested `max_tokens`, and `allowed_models` / `allowed_providers` as rejections. |
| FR-PH5 | **Budget alert thresholds** | Alerts fire at 80%, 90% and 100% of the hierarchy budget (`BUDGET_ALERT_THRESHOLDS`), distinct from the per-project `alert_threshold` in FR-C3. |
| FR-PH6 | **Striped locking** | Quota and rate-limit counters are guarded by `striped_lock.py` — per-key locks rather than one global lock, so requests for different projects are not serialized behind each other on the hot path. |
| FR-PH7 | **Node ids are addressing, not labels** | `get_ancestry` is entered by *project id* and constructs an environment key as `f"{project_id}:{environment}"`. So a project node's `node_id` must equal the project id and an environment node's must be `{project_id}:{env}`; the `org:`/`bu:` prefixes in the seeded tree are convention only, since nothing enters the walk at those levels. A project id with no matching node yields an empty ancestry and therefore a `ResolvedPolicy` with every field `None` — and `GET /admin/quotas/{project_id}` returns that as **200 with null limits**, indistinguishable from a project deliberately left unlimited. A misnamed node reads as an absent policy, not as an error. |

### 6.5 Content Guardrails

| ID | Requirement | Details |
|----|-------------|---------|
| FR-G1 | **Per-project guardrail rules** | Each project can define guardrail rules with: name, rule_type (keyword_block, regex_match, content_category), pattern, action (block, warn, redact), applies_to (request, response, both). Rules are a field on the project, so they are edited through `PUT /admin/projects/{id}` rather than a guardrail endpoint of their own, and take effect on the next request without a restart (FR-AD2). `GuardrailEngine` is stateless — the rule list is a call argument, not constructor state. |
| FR-G2 | **Request guardrails** | Rules with `applies_to` = "request" or "both" are evaluated against all message content before the request reaches a provider. Blocking violations return HTTP 400. |
| FR-G3 | **Response guardrails** | Rules with `applies_to` = "response" or "both" are evaluated against response content. Blocking violations replace the response content with a policy violation message. |

### 6.6 Caching

| ID | Requirement | Details |
|----|-------------|---------|
| FR-CA1 | **Response caching** | Per-project configurable response cache, isolated by tenant and project. Semantically identical requests within the TTL (default 300s) are served from cache with zero additional provider cost. |
| FR-CA2 | **Deterministic cache keys** | Cache keys are SHA-256 hashes of: model, messages, temperature, max_tokens, top_p, stop, tools, tool_choice, tenant_id, and project_id. Ensures identical requests produce identical keys regardless of field order while the same project id in another tenant always misses. The tool list is part of the key because the same prompt sent with tools can return a tool call and sent without them returns prose. |
| FR-CA3 | **Provider-level prompt caching** | When enabled per project, system prompts are annotated with `cache_control: ephemeral` blocks for Anthropic/Bedrock providers, reducing cost and latency on repeated system prompts. |
| FR-CA4 | **Semantic cache** | A second lookup, tried only after the exact key in FR-CA2 misses: embed the prompt, compare by cosine similarity against recent entries in the same `(tenant_id, project_id)` bucket, and serve the stored answer above a threshold. This catches "what is the refund policy?" against "what's our refund policy?" without allowing a same-named project in another tenant to participate. Per-project via `semantic_cache_enabled`. |
| FR-CA5 | **Embedding backend** | `embeddings.py` defines a one-method protocol (`embed(text) -> vector`) with Bedrock Titan as the only backend today. A protocol rather than a direct call because it keeps the cache's tests off the network; Titan rather than sentence-transformers because `boto3` is already a dependency and the gateway already talks to Bedrock, whereas sentence-transformers pulls torch into an otherwise slim image. |
| FR-CA6 | **Similarity threshold** | Default 0.90 cosine, overridable per project via `semantic_cache_threshold`. Chosen against a labelled corpus, for distance from the highest-scoring pair of genuinely *different* questions (0.7476) rather than from a round number. |
| FR-CA7 | **False-hit guards** | Cosine similarity alone is not sufficient — two prompts differing only in a negation or a unit score above 0.95. Two guards run before a hit is served: a **literal-token check** (numbers, identifiers and quoted strings must match, so "retry 3 times" does not answer "retry 5 times"), and a **polar-axis check** over seven opposition axes plus a set of one-sided blocks, so a prompt and its inverse never share an answer. On the calibration corpus these took must-block from 24/26 to 26/26 while must-allow went from 6/19 to 17/19. |

### 6.7 Rate Limiting

| ID | Requirement | Details |
|----|-------------|---------|
| FR-RL1 | **Sliding window rate limiter** | Per-user (default 60 RPM) and per-project (default 600 RPM) rate limits using a sliding window algorithm. The more restrictive limit applies when both are active. |
| FR-RL2 | **Rate limit headers** | All responses include `X-RateLimit-Limit`, `X-RateLimit-Remaining`, and `X-RateLimit-Reset` headers. Rejected requests include a `Retry-After` header. |

### 6.8 Request Validation

| ID | Requirement | Details |
|----|-------------|---------|
| FR-V1 | **Structural validation** | Messages must be dicts with `role` and `content` fields. Roles must be one of: system, user, assistant, tool. An assistant turn carrying `tool_calls` may omit `content` — that is the normal shape of a tool call. |
| FR-V2 | **Model validation** | Requested model must exist in the model registry. Unknown models return HTTP 404. |
| FR-V3 | **Token limit validation** | Estimated prompt token count (via tiktoken) is checked against the model's `max_context_tokens` when configured. |

### 6.9 Admin Console

| ID | Requirement | Details |
|----|-------------|---------|
| FR-AD1 | **Dashboard overview** | Real-time stats: total requests, total cost, active projects, active users, cache hit rate, provider health. |
| FR-AD2 | **Project management** | CRUD operations for projects including budget configuration, member management, model access lists, cache and semantic-cache settings, and guardrail rules — all via `PUT /admin/projects/{id}`. Changes are hot-reloaded without restart: the admin API and the request pipeline hold the *same* `projects` dict, and the pipeline reads `project.guardrail_rules` fresh per request, so a rule added mid-flight blocks the very next call. |
| FR-AD3 | **User management** | View users with usage, set individual budgets and model access restrictions. |
| FR-AD4 | **Model management** | View, create, update, and delete model configurations. Changes persist to YAML and optionally to DynamoDB. |
| FR-AD5 | **Usage analytics** | Filterable usage data with breakdowns by time range, provider, model, project, and user, plus CSV export. |
| FR-AD6 | **Policy management** | Create and view Cedar authorization policies with ENFORCE/LOG_ONLY modes, and manage the four-level policy hierarchy and its quotas. |
| FR-AD7 | **Provider health** | Real-time per-provider health status (healthy/unhealthy). |
| FR-AD8 | **API key management** | Issue, list, revoke and rotate keys. The plaintext appears once, in the issue response. |
| FR-AD9 | **Audit and PII views** | Browse audit records, verify the hash chain, and inspect PII redaction activity. |
| FR-AD10 | **Production readiness checklist** | `/admin/production-checklist` runs eight checks over the live configuration and reports PASS/WARN/FAIL, including canonical identity and the API-key caveat in FR-AC6. |
| FR-AD11 | **Drift detection** | Catalogue drift compares `config/catalog.yaml` against `models.yaml`; pricing drift reports models configured without pricing (currently 6 of 49). |
| FR-AD12 | **Runtime configuration surface** | What is *not* runtime-editable is provider credentials. Projects, users, models, Cedar policies, the policy hierarchy, quotas, regions, webhooks and guardrail rules all take effect without a restart. |

### 6.10 Tool Calling (Function Calling)

| ID | Requirement | Details |
|----|-------------|---------|
| FR-T1 | **Unified tool definition** | Callers send OpenAI-shaped `tools` and `tool_choice`. One definition works across every provider; no per-provider tool schema is required of the caller. |
| FR-T2 | **Bidirectional dialect translation** | Each adapter translates the tool spec on the way out and the model's tool call back into OpenAI `tool_calls` on the way in. Five dialects: OpenAI-style (`tools[].function.parameters` / `tool_calls[]` / `role:"tool"`), Anthropic-style (`input_schema` / `tool_use` / `tool_result`), Bedrock Converse (`toolConfig..toolSpec` / `toolUse` / `toolResult`), Gemini (`functionDeclarations` / `functionCall` / `functionResponse`), Cohere (`parameter_definitions` / top-level `tool_results`). |
| FR-T3 | **Normalized completion signal** | A tool call always surfaces as `finish_reason: "tool_calls"` regardless of the provider's own signal (Anthropic/Bedrock `stop_reason: "tool_use"`; Gemini leaves `finishReason` at `STOP` and signals only via the part itself). |
| FR-T4 | **Arguments encoding** | OpenAI carries tool arguments as a JSON string, every other dialect as an object; the value is re-encoded at each boundary. Malformed model output yields `{}` rather than failing the request, so the tool reports the bad call. |
| FR-T5 | **Schema compatibility** | Gemini rejects unknown JSON Schema keys (`additionalProperties`, `$schema`, `title`, `default`) rather than ignoring them, so schemas are filtered recursively before dispatch. |
| FR-T6 | **Unsupported-parameter reporting** | Where a provider has no equivalent for a requested parameter (e.g. Cohere v1 chat has no `tool_choice`), the response carries a warning rather than dropping the instruction silently. |
| FR-T7 | **Multi-round tool loops** | A full loop is supported: tool spec → tool call → tool result → final answer. Governance applies to every round (cost, quota, audit, guardrails), and smart routing classifies the last real user text rather than the intervening tool result, so all rounds of one loop route consistently. |

### 6.11 Persistence

| ID | Requirement | Details |
|----|-------------|---------|
| FR-P1 | **DynamoDB persistence** | Optional persistence layer using a single DynamoDB table with composite keys (PK/SK pattern). Stores usage records, project configurations, and user configurations. Enabled via `LLM_ROUTER_DYNAMODB_ENABLED=true`. |
| FR-P2 | **State recovery** | On startup, persisted projects, user configs, and usage records are loaded from DynamoDB to restore full state. |
| FR-P3 | **PAY_PER_REQUEST billing** | DynamoDB table uses on-demand billing to match the serverless deployment model. |

### 6.12 Token Efficiency and Right-Sizing

Budgets in §6.3 answer "did we spend too much". These answer "did we need to".
Both run over the `UsageRecord` data already collected, so nothing extra is
stored and no request is slowed.

| ID | Requirement | Details |
|----|-------------|---------|
| FR-E1 | **Level 1 ratio heuristics** | `efficiency_analyzer.py` computes per-user and per-project metrics from `UsageRecord` history and grades them `excellent` → `wasteful`. Seven tunable thresholds: completion/prompt ratio floor (0.05), cache utilization floor (0.10), expensive-model ratio ceiling (0.80), duplicate-request rate ceiling (0.15), token velocity ceiling (50,000), average prompt token ceiling (4,000), and a peer deviation factor (2.0×). |
| FR-E2 | **Peer comparison** | A user is scored against the other users on the same project, so "high spend" is judged relative to comparable work rather than against an absolute number that suits no team. |
| FR-E3 | **Level 2 prompt analysis** | `semantic_efficiency.py` decomposes a prompt into system / history / user tokens and reports compression opportunity, system-prompt ratio, history token count, redundancy indicators, and an assessed complexity — all before the request reaches a provider. |
| FR-E4 | **Level 3 right-sizing** | Models are mapped to a seven-tier ladder (nova-micro → claude-opus) with cost multipliers. When the classified complexity needs a tier more than one below the tier actually used, a `ModelRecommendation` is emitted with the target model, estimated percentage saving, and a `minimal`/`moderate` quality-impact rating. **Advisory only** — nothing reroutes a request on this basis. |
| FR-E5 | **Output utilization** | Inferred from the shape of the completion-token distribution, because `UsageRecord` does not store the requested `max_tokens` — `OutputAnalysis.max_tokens_set` is consequently always `False`, which is a known limitation rather than a reading of the request. Two signals: a short-response ratio (under 50 tokens, suggesting an over-large model for the work) and a truncation ratio (clustered near the observed maximum, suggesting a cap that is too low). Each produces a recommendation above 70% and 30% respectively. |
| FR-E6 | **Reporting surface** | Exposed at `GET /admin/efficiency`, `/admin/projects/{id}/efficiency` and `/admin/users/{id}/efficiency`, and on the dashboard's Observe → Efficiency page. |

---

## 7. Non-Functional Requirements

### 7.1 Performance

| ID | Requirement | Target |
|----|-------------|--------|
| NFR-P1 | Gateway overhead latency (excluding provider call) | < 50ms p99 |
| NFR-P2 | Cache hit response time | < 10ms p99 |
| NFR-P3 | Concurrent request handling | Async event loop supports thousands of concurrent connections per instance |
| NFR-P4 | Streaming first-byte latency | Pass-through from provider (no buffering) |

### 7.2 Availability

| ID | Requirement | Target |
|----|-------------|--------|
| NFR-A1 | Gateway uptime | 99.9% production target; not yet established for the AgentCore preview |
| NFR-A2 | Provider failover time | < 2s (retry + fallback to next provider) |
| NFR-A3 | Health check interval | Configurable (default 30s) |

### 7.3 Scalability

| ID | Requirement | Target |
|----|-------------|--------|
| NFR-S1 | Horizontal scaling | Runtime scaling must be validated only after readiness and distributed-control blockers are closed |
| NFR-S2 | Per-instance and persisted state | Rate limits, health state, response caches, and parts of audit/accounting coordination remain per-instance. Projects and API keys have tenant-qualified canonical storage; other persisted customer records are not comprehensively tenant-qualified |

### 7.4 Reliability

| ID | Requirement | Target |
|----|-------------|--------|
| NFR-R1 | Retry behavior | Jittered backoff (base 1s, doubling, each delay drawn from `[0.5·full, full]`) with max 3 retries on transient errors |
| NFR-R2 | Fallback depth | Full fallback chain traversal before returning error to caller |
| NFR-R3 | Mode-aware failure behavior | Non-authoritative telemetry writes may degrade with warning logs. Canonical principal/project reads, API-key lifecycle transactions, and AgentCore startup/readiness fail closed on DynamoDB outage |

### 7.5 Testability

| ID | Requirement | Target |
|----|-------------|--------|
| NFR-T1 | Automated test coverage | 2,844 passing tests across unit, integration, end-to-end, and property-based suites |
| NFR-T2 | Property-based tests | Hypothesis-driven validation of formal correctness properties (routing fairness, cost accuracy, cache determinism, retry behavior) |
| NFR-T3 | Integration test scenarios | Automated test traffic covering: normal chat, multi-turn, rate limiting, budget enforcement, model access control, guardrail violations |

---

## 8. System Architecture

### 8.1 High-Level Architecture

```
                                    AxonLLM Gateway
                            +---------------------------------+
  +-------------+           |                                 |           +----------+
  |  Your App   |---------->|   Request Pipeline              |---------->| Bedrock  |
  +-------------+           |                                 |           | (boto3)  |
                            |   1.   Parse Request            |           +----------+
  +-------------+           |   2.   Extract Context          |
  |  Chat UI    |---------->|   2.5  Request Validation       |           +----------+
  +-------------+           |   2.7  Quota (policy hierarchy) |---------->|Anthropic |
                            |   2.8  Injection Detection      |           | (HTTP)   |
  +-------------+           |   2.9  PII Redaction            |           +----------+
  |  Dashboard  |---------->|   3.   Rate Limit Check         |
  +-------------+           |   4-5. Model Access Checks      |           +----------+
                            |   6-7. Budget Checks            |---------->| OpenAI   |
                            |   8.   Request Guardrails       |           | (HTTP)   |
                            |   9.   Cache (exact, semantic)  |           +----------+
                            |   9.5  Region / Residency       |
                            |  10.   Route & Execute          |           +----------+
                            |  11.   Response Guardrails      |---------->| Azure    |
                            |  11.5  PII Re-injection         |           | (HTTP)   |
                            |  11.6  Audit Trail              |           +----------+
                            |  12.   Cost Tracking            |
                            |  13-14 Budget / Session         |           +----------+
                            |  15.   Streaming Return         |---------->| 9 more   |
                            |  15.5  Cache Write              |           |providers |
                            |  16.   Non-streaming Return     |           +----------+
                            |                                 |
                            |  +----------+  +-----------+    |
                            |  |DynamoDB  |  |Health     |    |           +----------+
                            |  |Persist.  |  |Monitor    |    |---------->| Vertex   |
                            |  +----------+  +-----------+    |           | (HTTP)   |
                            +---------------------------------+           +----------+
```

### 8.2 Component Architecture

| Component | Responsibility | Module |
|-----------|---------------|--------|
| **GatewayAgent** | Orchestrates the request pipeline (see 8.1 for the step numbering) | `agent.py` |
| **Router** | Strategy-based provider selection with retry and fallback | `router.py` |
| **Routing Strategies** | Round-robin, weighted, least-latency, cost-optimized selection algorithms — the four base strategies | `routing.py` |
| **Smart Routing** | Prompt classification → best-model selection via benchmark leaderboard | `smart_routing.py` |
| **TaskClassifier** | Prompt → task type, the input smart routing selects on | `task_classifier.py` |
| **ModelLeaderboard** | Benchmark rankings loaded from `config/leaderboard.yaml` | `model_leaderboard.py` |
| **FeedbackTracker** | Records per-request outcome feedback against routing decisions | `feedback_tracker.py` |
| **Ensemble Routing** | Scatter-gather-synthesize across a model panel with a judge | `ensemble.py`, `ensemble_config.py` |
| **MultiProviderFactory** | Dispatches Bedrock (boto3) vs. HTTP provider calls | `multi_provider_factory.py` |
| **Provider Adapters** | Per-provider request/response/streaming translation | `adapters/` |
| **HttpClient** | Async HTTP execution with session pooling | `http_client.py` |
| **BedrockProvider** | boto3 invoke_model (Anthropic) and converse API (Nova, DeepSeek) | `bedrock_provider.py` |
| **MantleProvider** | Bedrock Mantle via SigV4, dispatching to whichever of three inference APIs serves the requested model | `mantle_provider.py` |
| **CostTracker** | Usage recording, cost calculation, budget enforcement | `cost_tracker.py` |
| **QuotaEnforcer** | Applies a `ResolvedPolicy` at request time — RPM, budget, token cap, model and provider allow-lists | `quota_enforcer.py` |
| **PolicyHierarchyResolver** | Leaf-to-root walk of org/BU/project/environment, child-tightens-only | `auth/policy_hierarchy.py` |
| **RateLimiter** | Sliding window per-user/per-project rate limiting | `rate_limiter.py` |
| **StripedLock** | Per-key async locks so limit state does not serialize the hot path | `striped_lock.py` |
| **GuardrailEngine** | Request/response content inspection against configurable rules | `guardrail_engine.py` |
| **PIIRedactor / PIINER** | Regex redaction with reversible mapping, plus a Comprehend layer for shapeless PII | `security/pii_redactor.py`, `security/pii_ner.py` |
| **InjectionDetector** | Heuristic prompt-injection scoring to a five-level threat rating | `security/injection_detector.py` |
| **AuditTrail** | Append-only compliance records with a SHA-256 hash chain | `security/audit_trail.py` |
| **EventDispatcher** | Fire-and-forget security-event fan-out to webhook, CloudWatch or SNS | `security/event_dispatcher.py` |
| **CacheManager** | In-memory TTL-based exact-match response cache | `cache_manager.py` |
| **SemanticCache** | Embedding-similarity second-chance lookup with literal and polar-axis guards | `semantic_cache.py`, `embeddings.py` |
| **EfficiencyAnalyzer** | Level 1 token-waste heuristics over `UsageRecord` data | `efficiency_analyzer.py` |
| **SemanticEfficiency** | Levels 2-3 — ML waste detection and model right-sizing | `semantic_efficiency.py` |
| **HealthTracker** | Provider health tracking with cooldown periods and latency recording | `health_tracker.py` |
| **ModelRegistry** | Model configuration loading and validation from YAML | `model_registry.py` |
| **RequestValidator** | Structural, semantic, and token-limit request validation | `request_validator.py` |
| **DynamoPersistence** | DynamoDB single-table read/write across 14 entity types | `persistence.py` |
| **AuthMiddleware** | API key or OIDC JWT validation + Cedar policy evaluation | `middleware/auth.py` |
| **AdminRBAC** | Role and scope gate on `/admin/*` | `middleware/admin_rbac.py` |
| **APIKeyService** | Issue, validate, revoke, rotate `axon_` keys; hashes only at rest | `auth/api_key_service.py` |
| **SAML / SCIM / OIDC** | Enterprise SSO and user provisioning | `auth/saml_service.py`, `auth/scim_service.py`, `auth/oidc_service.py` |
| **RegionRouter** | Hub-and-spoke region selection with health, failover and residency filtering | `multi_region/` |
| **TraceForwarder** | Emits per-request traces to Ostiari; OTLP export alongside | `observability/` |
| **SessionManager (inactive)** | AgentCore Memory abstraction exists but is not wired into bootstrap or the AgentCore entrypoint | `session_manager.py` |
| **AdminAPI** | Admin REST API (69 method+path routes) and the server-rendered dashboard | `admin/` |
| **ChatAPI** | Client-facing chat API, OpenAI-compatible API, and web interfaces | `chat/` |
| **CLI** | `axon` entry point — `demo`, `serve`, `issue-key`, `chat`, `models` | `cli.py` |
| **Bootstrap** | Centralized dependency injection and component wiring | `bootstrap.py` |

### 8.3 Provider Adapter Pattern

All providers implement a uniform `ProviderAdapter` interface:

```
ProviderAdapter (ABC)
  |-- translate_request(ChatCompletionRequest) -> dict
  |-- translate_response(dict) -> ChatCompletionResponse
  |-- translate_stream_chunk(dict) -> StreamChunk
  |-- list_models() -> list[ModelInfo]
  |-- health_check() -> bool
      |
      |-- OpenAIStyleAdapter (shared base for OpenAI-format providers)
      |     |-- OpenAIAdapter
      |     |-- AzureOpenAIAdapter
      |
      |-- AnthropicStyleAdapter (shared base for Anthropic-format providers)
      |     |-- AnthropicAdapter
      |     |-- BedrockAdapter
      |
      |-- VertexAIAdapter (standalone, unique API format)
      |-- CohereAdapter (standalone, unique API format)
```

### 8.4 Configuration Hierarchy

```
Environment Variables (highest precedence)
        |
        v
YAML Files (config/)
  |- models.yaml        Model definitions with provider mappings
  |- providers.yaml     Provider connection configs and API keys
  |- pricing.yaml       Per-provider, per-model token pricing
  |- catalog.yaml       Provider model catalog, compared against models.yaml
  |                     for drift by admin/catalog_drift.py
  |- ensemble.yaml      Ensemble presets — panel membership and judge model
  |- leaderboard.yaml   Benchmark rankings consumed by model_leaderboard.py
  |- spokes.yaml        Multi-region spoke topology (ships as .example)
  |- demo_seed.yaml     Demo projects, users, budgets, seed data
        |
        v
Dataclass Configurations (runtime)
  |- AppConfig           Server host/port, file paths, feature flags
  |- GatewayConfig       Retry, rate limit, cache, adapter defaults
  |- ProviderConfig      Per-provider auth, URLs, timeouts
  |- ModelConfig         Model-to-provider mappings, routing strategy
```

---

## 9. API Specification

### 9.1 Chat API

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `GET` | `/api/models` | List available models (filtered by project/user access) | Optional |
| `GET` | `/api/users` | List known users | Optional |
| `POST` | `/api/chat` | Non-streaming chat completion | Required |
| `POST` | `/api/chat/stream` | Streaming chat completion (SSE) | Required |
| `POST` | `/v1/chat/completions` | **OpenAI-compatible** chat completion, streaming or not. This is the migration path referenced by OQ6: point an existing OpenAI SDK at this base URL and the application needs no code change | Required |
| `GET` | `/v1/models` | OpenAI-compatible model list | Required |

On both `/api/chat` and `/v1/chat/completions`, a `user_id` or `project_id` in the
request *body* is deliberately **ignored**. Identity comes from the authenticated
context only — otherwise any caller could bill another tenant's project by naming
it. Under `LOG_ONLY` an anonymous request resolves to no user and no project, so
policy lookups fall through to defaults rather than to some other tenant's
settings.

#### POST /api/chat — Request

```json
{
  "model": "claude-opus",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Explain quantum computing."}
  ],
  "temperature": 0.7,
  "max_tokens": 1024,
  "top_p": 1.0,
  "stop": ["\n\n"],
  "stream": false,
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "db_query",
        "description": "Run a read-only SQL query",
        "parameters": {
          "type": "object",
          "properties": {"sql": {"type": "string"}},
          "required": ["sql"]
        }
      }
    }
  ],
  "tool_choice": "auto"
}
```

`tools` and `tool_choice` are optional and OpenAI-shaped; the target provider's
dialect is AxonLLM's concern, not the caller's (§6.10).

#### POST /api/chat — Response

```json
{
  "id": "chatcmpl-abc123",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Quantum computing leverages..."
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 42,
    "completion_tokens": 256,
    "total_tokens": 298
  },
  "model": "claude-opus",
  "provider": "bedrock"
}
```

#### POST /api/chat — Response (tool call)

When the model calls a tool, the assistant message carries `tool_calls` and
`finish_reason` is `tool_calls` — normalized across providers regardless of the
signal the provider itself used:

```json
{
  "id": "chatcmpl-abc123",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": null,
        "tool_calls": [
          {
            "id": "toolu_01A",
            "type": "function",
            "function": {"name": "db_query", "arguments": "{\"sql\": \"SELECT COUNT(*) FROM orders\"}"}
          }
        ]
      },
      "finish_reason": "tool_calls"
    }
  ],
  "usage": {"prompt_tokens": 574, "completion_tokens": 59, "total_tokens": 633},
  "model": "claude-opus",
  "provider": "bedrock"
}
```

The caller runs the tool and sends the result back as a `role: "tool"` message to
continue the loop. `arguments` is a JSON **string** (OpenAI's convention), not an
object.

#### POST /api/chat/stream — SSE Response

```
data: {"id":"chatcmpl-abc123","choices":[{"delta":{"content":"Quantum "}}],"model":"claude-opus","is_final":false}

data: {"id":"chatcmpl-abc123","choices":[{"delta":{"content":"computing "}}],"model":"claude-opus","is_final":false}

data: [DONE]
```

### 9.2 Admin API

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/admin/overview` | Dashboard stats (requests, cost, active projects/users, cache hit rate) |
| `GET` | `/admin/projects` | List all projects |
| `POST` | `/admin/projects` | Create project |
| `GET` | `/admin/projects/{id}` | Get project details |
| `PUT` | `/admin/projects/{id}` | Update project |
| `POST` | `/admin/projects/{id}/members` | Add project member |
| `DELETE` | `/admin/projects/{id}/members/{user_id}` | Remove project member |
| `GET` | `/admin/projects/{id}/models` | List project allowed models |
| `POST` | `/admin/projects/{id}/models` | Add model to project access list |
| `DELETE` | `/admin/projects/{id}/models/{model_name}` | Remove model from project access list |
| `GET` | `/admin/usage` | Usage records (filterable) |
| `GET` | `/admin/users` | List users with usage |
| `GET` | `/admin/users/{id}` | User detail |
| `PUT` | `/admin/users/{id}/budget` | Set user budget |
| `PUT` | `/admin/users/{id}/allowed-models` | Set user model access |
| `GET` | `/admin/models` | List models |
| `POST` | `/admin/models` | Create model |
| `PUT` | `/admin/models/{name}` | Update model |
| `DELETE` | `/admin/models/{name}` | Delete model |
| `GET` | `/admin/catalog` | Provider model catalog |
| `GET` | `/admin/policies` | List Cedar policies |
| `POST` | `/admin/policies` | Create Cedar policy |
| `GET` | `/admin/health` | Provider health status |
| `GET` | `/admin/usage/export` | Chargeback export — `format=csv\|json`, `level=records\|breakdown`, 14 columns |
| `GET` | `/admin/traces` | Recorded request traces |
| `GET` | `/admin/efficiency` | Token efficiency analytics |
| `GET` | `/admin/projects/{id}/efficiency` | Per-project efficiency |
| `GET` | `/admin/users/{id}/efficiency` | Per-user efficiency |
| `GET` | `/admin/architecture` | Interactive architecture page |
| `GET` | `/admin/pricing-drift` | Pricing coverage report |
| `GET` | `/admin/catalog-drift` | Drift between `models.yaml`, `catalog.yaml` and observed traffic |
| `GET` | `/admin/production-checklist` | Eight readiness checks |
| `GET` `DELETE` | `/admin/semantic-cache` | Semantic cache stats; invalidate |

**API keys**

| Method | Path | Description |
|--------|------|-------------|
| `GET` `POST` | `/admin/projects/{id}/keys` | List / issue keys for a project |
| `POST` | `/admin/keys/{key_id}/rotate` | Rotate — note it carries the old `expires_at` through |
| `DELETE` | `/admin/keys/{key_id}` | Revoke. The only mechanism that reliably stops a key |

**Audit and PII**

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/admin/audit/records` | Hash-chain entries |
| `GET` | `/admin/audit/verify` | Recompute the chain and report tampering |
| `GET` | `/admin/audit/export` | Export the chain |
| `GET` | `/admin/audit/stats` | Aggregate counts |
| `GET` | `/admin/audit/security` | Security events (injection, guardrail, PII) |
| `POST` | `/admin/pii/preview` | Before/after redaction on supplied text. Entity detection requires an explicit `ner: true`, because it bills |

**Policy hierarchy**

| Method | Path | Description |
|--------|------|-------------|
| `GET` `POST` | `/admin/policies/hierarchy` | List / create nodes |
| `GET` `PUT` | `/admin/policies/hierarchy/{node_id}` | Read / update a node |
| `GET` | `/admin/policies/effective/{project_id}` | The resolved policy after inheritance |

**Quotas**

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/admin/quotas/{project_id}` | Current spend and limits |
| `POST` | `/admin/quotas/{project_id}/reset` | Manual reset (there is no scheduler) |
| `POST` | `/admin/quotas/simulate` | Dry-run a request against the quota rules |

**Regions**

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/admin/regions` | Topology |
| `PUT` | `/admin/regions/config` | Set mode (single / active-passive / active-active) |
| `POST` `PUT` `DELETE` | `/admin/regions/spokes[/{region}]` | Manage spokes |
| `PUT` | `/admin/regions/{region}/status` | Drain or restore a region |
| `GET` `POST` | `/admin/regions/health[/check]` | Spoke health; force a check |
| `POST` | `/admin/regions/failover` | Trigger failover |
| `POST` | `/admin/regions/route` | Resolve which region a request would take |

**Webhooks**

| Method | Path | Description |
|--------|------|-------------|
| `GET` `POST` | `/admin/webhooks` | List / upsert destinations (webhook, SNS, CloudWatch Logs) |
| `DELETE` | `/admin/webhooks/{name}` | Remove a destination |
| `POST` | `/admin/webhooks/{name}/test` | Send a test event |
| `GET` | `/admin/webhooks/stats` | Delivery stats |

### 9.3 Web Interfaces

| Page | URL | Purpose |
|------|-----|---------|
| Admin Dashboard | `/admin/dashboard` | Admin console — a single-page app with the nav tree below |
| Chat | `/chat` | Interactive chat with model, provider, and user selection |
| Playground | `/playground` | Clean chat with routing decision visibility |
| Routing Explorer | `/routing` | Prompt-only routing explorer; shows model and provider selection logic |
| Health | `/health` | Unauthenticated liveness probe, for the ALB target group |
| SAML | `/saml/login`, `/saml/acs`, `/saml/metadata` | SP-initiated login, assertion consumer, SP metadata |
| SCIM | `/scim/v2/Users`, `/scim/v2/Groups` | User and group provisioning (own bearer-token auth) |

The dashboard's nav tree, in the order it renders:

| Group | Destinations |
|-------|--------------|
| — | Sandbox |
| **Observe** | Overview, Traces, Efficiency, Audit Log |
| **Configure** | Models, Projects, Users, API Keys |
| **Govern** | Policies, Hierarchy, Quotas, Regions, Webhooks |
| **System** | Health, Configuration, Architecture, Pricing, Catalogue, Readiness |

---

## 10. Data Model

### 10.1 Core Entities

```
ChatCompletionRequest
  |- messages: list[dict]          # [{role, content}]
  |- model: str                    # Model name
  |- temperature: float?
  |- max_tokens: int?
  |- top_p: float?
  |- stop: list[str]?
  |- stream: bool
  |- system: str?
  |- tools: list[dict]?            # OpenAI-shaped tool specs (§6.10)
  |- tool_choice: str | dict?

ChatCompletionResponse
  |- id: str
  |- choices: list[dict]           # [{index, message, finish_reason}]
  |- usage: TokenUsage
  |- model: str
  |- provider: str
  |- warnings: list[str]

TokenUsage
  |- prompt_tokens: int
  |- completion_tokens: int
  |- total_tokens: int
  |- cached_tokens: int
  |- cache_creation_tokens: int

ModelConfig
  |- name: str                     # Model name
  |- description: str
  |- providers: list[ProviderModelMapping]
  |- routing_strategy: RoutingStrategy
  |- capabilities: list[str]?
  |- max_context_tokens: int?

ProviderModelMapping
  |- provider: str                 # Provider name
  |- model_id: str                 # Provider-specific model identifier
  |- weight: float                 # For weighted routing
  |- fallback_order: int           # Fallback priority
  |- pricing: TokenPricing?

TokenPricing
  |- prompt_token_cost: float      # Per 1K tokens
  |- completion_token_cost: float  # Per 1K tokens
  |- cached_token_cost: float?     # Discounted rate for cached tokens
  |- cache_creation_token_cost: float?
  |- image_token_cost: float?
  |- reasoning_token_cost: float?
  |- per_request_cost: float       # Flat fee per API call

Project
  |- project_id: str
  |- name: str
  |- tenant_id: str?                 # Required on canonical multi-tenant paths
  |- budget_limit: float?
  |- alert_threshold: float?
  |- allowed_models: list[str]?
  |- guardrail_rules: list[GuardrailRule]
  |- cache_enabled: bool
  |- cache_ttl_seconds: int            # 300
  |- semantic_cache_enabled: bool      # Second-chance reworded lookup (§6.6)
  |- semantic_cache_threshold: float?  # Cosine floor; defaults to 0.90
  |- log_level: str                    # "INFO"
  |- log_destination: str?
  |- prompt_caching_enabled: bool
  |- ltm_enabled: bool                 # Reserved; no active AgentCore Memory wiring
  |- retention_period_hours: int       # 24
  |- rate_limit_rpm: int?
  |- members: list[str]
  |- created_at: datetime

UserConfig
  |- (per-user budget, alert threshold and allowed_models — the user-level
  |   counterpart to Project, intersected with it per FR-AC2)

PolicyNode                             # One level of the §6.4.1 hierarchy
  |- node_id: str
  |- node_type: str                    # org | business_unit | project | environment
  |- parent_id: str?
  |- display_name: str
  |- limits: dict                      # Whatever this level constrains
  |- created_at: datetime

ResolvedPolicy                         # Output of the leaf-to-root walk
  |- rate_limit_rpm: int?
  |- budget_limit: float?
  |- allowed_models: list[str]?
  |- allowed_providers: list[str]?
  |- max_tokens_per_request: int?
  |- pii_redaction_enabled: bool
  |- pii_redact_types: list[str]?
  |- pii_reinject: bool                # True — put originals back in the response
  |- pii_ner_enabled: bool             # Comprehend layer for shapeless PII
  |- pii_ner_types: list[str]?

APIKey                                 # Plaintext is never stored; hash only
  |- key_id: str
  |- key_hash: str                     # SHA-256 of the axon_ key
  |- project_id: str
  |- tenant_id: str?                   # Tenant-qualified production namespace
  |- name: str
  |- scopes: list[str]                 # Control plane only — see FR-AC6
  |- created_by: str
  |- created_at: datetime
  |- expires_at: datetime?             # None by default, and rotation carries it through
  |- revoked: bool
  |- revoked_at: datetime?
  |- last_used_at: datetime?

UsageRecord
  |- request_id: str
  |- project_id: str
  |- user_id: str
  |- provider: str
  |- model: str
  |- prompt_tokens: int
  |- completion_tokens: int
  |- total_tokens: int
  |- cost: float
  |- timestamp: datetime
  |- cached_tokens: int
  |- cache_creation_tokens: int
  |- image_tokens: int
  |- reasoning_tokens: int
  |- latency_ms: float
  |- status: str                       # "success" by default; failures are recorded too
  |- routing_strategy: str             # Which strategy chose this provider
  |- task_type: str                    # From task_classifier.py, for right-sizing analysis
  |- provider_request_id: str          # The provider's own ID, for cross-referencing
```

The last five `UsageRecord` fields are what makes the efficiency analysis in
`efficiency_analyzer.py` and `semantic_efficiency.py` possible without a second
store: latency, outcome, chosen strategy and classified task type all travel on
the same record as the cost.

### 10.2 DynamoDB Schema

Single-table design using composite keys:

| Entity | PK | SK | Purpose |
|--------|----|----|---------|
| Usage Record | `USAGE#{request_id}` | `USAGE#{timestamp}` | Per-request cost and token tracking |
| Project (canonical) | `TENANT#{tenant_id}` | `PROJECT#{project_id}` | Tenant-owned project configuration used for authorization |
| Project (legacy) | `PROJECT#{project_id}` | `PROJECT` | Migration-compatible unqualified project configuration |
| User Config | `USER#{user_id}` | `CONFIG` | User budget and model access settings |
| Feedback | `FEEDBACK#{request_id}` | `FEEDBACK#{timestamp}` | Per-request thumbs up/down |
| API Key (tenant) | `TENANT#{tenant_id}#APIKEY#{key_id}` | `METADATA` | Tenant-qualified hashed key record with scopes, expiry, revocation |
| API Key project edge | `TENANT#{tenant_id}#PROJECT#{project_id}` | `APIKEY#{key_id}` | Tenant/project key listing without a scan |
| API Key (legacy) | `APIKEY#{key_id}` | `APIKEY` | Migration-compatible unqualified key row |
| API Key project edge (legacy) | `PROJECT#{project_id}` | `APIKEY#{key_id}` | Migration-compatible unqualified project listing |
| API Key lookup | `APIKEY_HASH#{hash}` | `LOOKUP` | Global secret-hash reverse index pointing to the tenant-qualified primary row |
| Audit Record | `AUDIT#{project_id}` | `AUDIT#{timestamp}#{record_id}` | Hash-chain entries, ordered so the chain can be replayed and verified |
| Policy Node | `POLICY_NODE#{node_id}` | `CONFIG` | One node of the org → BU → project → env hierarchy |
| Policy Edge | `POLICY_NODE#{parent_id}` | `CHILD#{node_id}` | Parent-to-child edge, so a subtree loads without a scan |
| Cedar Policy | `CEDAR_POLICY#{policy_id}` | `CONFIG` | Cedar policy source |
| SCIM User | `SCIM#USER#{id}` | `SCIM_USER` | SCIM-provisioned user |
| SCIM Group | `SCIM#GROUP#{id}` | `SCIM_GROUP` | SCIM-provisioned group |
| Event Destinations | `EVENT_DESTINATIONS` | `CONFIG` | The whole destination set in one item |
| Region Topology | `REGION_TOPOLOGY` | `CONFIG` | The whole spoke topology in one item |
| Revocation Epoch (tenant) | `TENANT#{tenant_id}` | `AUTHZ#EPOCH` | Counter advanced atomically with revocation and polled by each instance |
| Revocation Epoch (legacy) | `REVOCATION` | `EPOCH` | Migration-compatible global counter for unqualified keys |
| Spend Counter | `SPEND#{scope}#{id}` | `TOTAL` | Fleet-wide spend, so a budget limit is a limit across every instance rather than per instance |

`EVENT_DESTINATIONS` and `REGION_TOPOLOGY` are deliberately single items holding
an entire set rather than a row per member: a row per destination cannot express a
*deletion* without a read-diff-write, which is how a repeated
`POST /admin/webhooks` came to double-deliver every event before it was fixed.

Revocation epochs and spend counters exist because per-process state answers
differently depending on which task the load balancer picked. Epoch updates use
atomic `ADD` inside the revocation transaction; spend updates also use `ADD` and
return the post-update total, so an instance learns the fleet figure from a
write it was already making instead of paying for a read on the request path.
`{scope}` is `quota` for the enforcing counter and `project`/`user` for the
reported ones. They use separate keys because both are charged on the same
request and one key would double every cost.

Telemetry and several configuration writes are fire-and-forget with a warning
log on failure so a completed provider call is not converted into a 500.
Authority is different: canonical principal/project reads fail closed,
API-key issue/revoke transactions surface failure, and AgentCore refuses startup
or readiness when required DynamoDB state is unavailable. The checklist still
surfaces dropped non-authoritative writes and unreachable persistence.

---

## 11. Security and Compliance

### 11.1 Authentication

| Layer | Mechanism |
|-------|-----------|
| **API Authentication** | Two accepted credentials on the same header: an `axon_`-prefixed API key, or an OIDC JWT verified against the issuer's JWKS. `AXON_AUTH_MODE` decides whether a missing credential is refused (`ENFORCE`) or served anonymously and logged (`LOG_ONLY`). |
| **Enterprise SSO** | SAML 2.0 (`/saml/*`) with signed-assertion verification, and SCIM 2.0 (`/scim/v2/*`) for user and group provisioning. |
| **Provider Authentication** | Provider-specific: AWS IAM/SigV4 (Bedrock, Bedrock Mantle), API keys (Anthropic, OpenAI, xAI, Together, Fireworks, Groq, Cohere, AI21, Google AI), Azure AD keys (Azure OpenAI), GCP service accounts (Vertex AI). |
| **Credential Management** | API keys sourced from environment variables (highest precedence) or YAML config files. A local `.env` is read only when `AXON_LOAD_DEMO_DATA` is set, so a production process never picks up a developer's keys. No credentials hardcoded in source. |

### 11.2 Authorization

| Layer | Mechanism |
|-------|-----------|
| **Model Access Control** | Per-project and per-user allowed model lists enforced at gateway level (HTTP 403 rejection). |
| **Canonical Tenant RBAC** | Strongly consistent principal and tenant/project resolution precedes default-deny action authorization. Cross-tenant or ungranted resources are concealed as 404; an unavailable ownership store returns 503. |
| **Policy Hierarchy** | `org > business_unit > project > environment`, resolved leaf-to-root with child-tightens-only semantics (see §6.4.1). Numeric limits take the minimum, lists take the intersection. |
| **Cedar Policies** | Fine-grained authorization via Cedar policy language. Policies evaluate (principal, action, resource) tuples. ENFORCE and LOG_ONLY modes. |
| **Admin RBAC** | `middleware/admin_rbac.py` gates `/admin/*` on the caller's roles and API-key scopes. |
| **API key scopes** | Legacy key scopes are control-plane only. Canonical mode replaces them with server-held service-principal action scopes for mapped data-plane actions; see FR-AC6. |
| **Budget Enforcement** | Budget limits function as financial authorization gates. Over-budget requests are rejected (HTTP 429). |

### 11.3 Content Safety

| Layer | Mechanism |
|-------|-----------|
| **Request Guardrails** | Content inspection before provider call. Keyword blocking, regex matching, content category filtering. Block/warn/redact actions. |
| **Response Guardrails** | Content inspection after provider response. Blocking violations replace response content with policy message. |
| **PII redaction** | `security/pii_redactor.py` replaces detected PII with indexed tokens (`[EMAIL_1]`, `[SSN_2]`) before the prompt leaves the gateway, and keeps a reversible mapping so originals are re-injected into the response for the caller. Ten regex types: email, ssn, credit_card, phone, ip_address, ipv6, aws_account_id, medical_record, iban, passport. Configured per org/BU/project through the resolved policy; `AXON_PII_REDACTION_DEFAULT` flips an entire deployment to redact-by-default for policies that say nothing. |
| **Named-entity PII** | `security/pii_ner.py` adds a second detector for PII that has no *shape* — names, addresses, ages — which no regex can match. Backed by Amazon Comprehend, chosen over spaCy/Presidio because `boto3` is already in the image while `en_core_web_sm` adds ~148MB and 1.35s of start-up, and because it tags `Jenkins`, `Django` and `UserService` as PERSON/ORG. The two detectors are a **union**, not a replacement: Comprehend missed `10.0.0.7`, which the `ip_address` pattern catches trivially. |
| **Prompt injection detection** | `security/injection_detector.py` scores prompts across role-override attempts, system-prompt extraction, delimiter escape, and base64-encoded payloads, returning a five-level `ThreatLevel` (none → critical) after Unicode normalization. |
| **Audit trail** | `security/audit_trail.py` records every request/response pair append-only: who asked, the redacted prompt and the response, security events, and the policy state at the time. Each record carries a SHA-256 hash of its predecessor, so any retroactive edit breaks the chain and is detectable. |
| **Security event fan-out** | `security/event_dispatcher.py` pushes security events to webhooks (Slack, PagerDuty, custom), CloudWatch Logs, or SNS. Fire-and-forget: a failing destination is logged and never blocks the request. |

### 11.4 Data Protection

| Concern | Approach |
|---------|----------|
| **Credentials at rest** | Environment variables preferred; YAML config files should be excluded from version control. API keys are persisted as SHA-256 hashes only — the plaintext exists once, in the issue response. |
| **Data in transit** | All provider calls use HTTPS. Bedrock and Bedrock Mantle use AWS SDK / SigV4 signing. |
| **Logging** | Structured JSON logs include request metadata (IDs, tokens, cost) but not message content. |
| **Prompt content leaving the tenant** | PII redaction runs before dispatch, so the provider sees tokens rather than the original values; the audit trail stores the redacted form as well. |
| **Data residency** | `multi_region/` can filter candidate regions by residency requirement, so a request tagged for one jurisdiction is not routed to a spoke outside it. |

---

## 12. Deployment and Operations

### 12.1 Deployment Options

| Option | Description | Use Case |
|--------|-------------|----------|
| **ECS Fargate via CDK** | `./deploy-fargate.sh <region>`. The stack provisions CloudFront/WAF, an internal TLS ALB, private Fargate tasks, DynamoDB/IAM, retained ALB/CloudFront access logs, rollback-enabled deployments, and an ALB `/ready` gate for DynamoDB reachability. ALB authentication and tenant-scoped admin state remain release blockers. | Reference / isolated staging |
| **Amazon Bedrock AgentCore** | `agentcore configure` + `agentcore launch` against `agentcore_agent.py`; hardening preview with `chat`, `list_models`, `health` liveness, and a distinct dependency-aware `GET /ready`. | Evaluation |
| **AWS App Runner** | Docker container deployed via ECR + App Runner (`deploy.sh`). Semi-managed with container-level configuration; it does not close the shared-tenancy blockers. | Reference / isolated staging |
| **Docker / Compose** | `docker build` + `docker run`, or `docker compose up` using `docker-compose.yml` (gateway + DynamoDB Local). | Staging, on-premises |
| **Local development** | `uv run python serve_dashboard.py`. Uvicorn dev server with demo data seeded and `AXON_AUTH_MODE=LOG_ONLY`. | Development |
| **CLI** | `uv run axon serve` (or `uv run axon demo`) after `uv sync`. Same app, argparse front end. | Development, scripted runs |

The AgentCore preview does not mount the Starlette application or expose its
HTTP/admin console. The repository has no checked-in production AgentCore IaC,
JWT authorizer, resource policy, or private-network topology, and it does not
wire `SessionManager` or AgentCore Memory. Initialization now runs explicitly in
the SDK lifespan, request admission has a deadline, and OIDC/JWKS and DynamoDB
are verified before traffic. A synchronous bootstrap worker cannot be forcibly
cancelled if it hangs, so production needs process-level startup containment.
Shutdown closes provider HTTP and OTLP resources. `health` remains liveness and
`GET /ready` is the dependency probe. Distributed controls and non-project
tenant-qualified persistence remain production blockers.

### 12.1.1 CLI

`pyproject.toml` exposes one console script, `axon` → `src.gateway.cli:main`, with
five subcommands. `uv sync` installs it into `.venv/bin`, which is not on `PATH`,
so invoke it as `uv run axon <subcommand>` (or activate the venv first):

| Command | Purpose |
|---------|---------|
| `axon demo` | Start the server and generate real traffic against it, for a live demo |
| `axon serve` | Start the gateway server |
| `axon issue-key` | Mint an API key in-process — the way to bootstrap a key under `ENFORCE`, where the admin API itself needs one |
| `axon chat` | Send a single chat message from the terminal |
| `axon models` | List available models |

### 12.2 Environment Variables

**Core**

| Variable | Default | Description |
|----------|---------|-------------|
| `AXON_AUTH_MODE` | `ENFORCE` | `ENFORCE` requires an `axon_` key or JWT on every request; `LOG_ONLY` serves anonymous requests and only logs what it would have denied. **The single most consequential variable here** — `serve_dashboard.py` sets `LOG_ONLY`, so a local gateway is open by default and a Fargate one is not. |
| `AXON_LOAD_DEMO_DATA` | `false` in code, **`true` in `serve_dashboard.py`** | Seeds `config/demo_seed.yaml`. Also the gate on reading `.env` — two behaviours on one flag. |
| `AWS_DEFAULT_REGION` | `us-east-1` | AWS region |
| `AXON_BEDROCK_REGION` | `us-east-1` | Bedrock-specific region |
| `AXON_SERVER_HOST` | `0.0.0.0` | Server bind host |
| `AXON_SERVER_PORT` | `8000` | Server port |
| `AXON_API_KEY` | — | Bootstrap admin key, for issuing the first real key |
| `AXON_NO_BROWSER` | — | Suppress the browser launch on startup |
| `AXON_DEV_ENV_FILE` | `.env` | Alternate dotenv path (demo mode only) |

**Config file paths**

| Variable | Default | Description |
|----------|---------|-------------|
| `AXON_MODELS_CONFIG` | `config/models.yaml` | Model definitions |
| `AXON_PROVIDERS_CONFIG` | `config/providers.yaml` | Provider auth and base URLs |
| `AXON_PRICING_CONFIG` | `config/pricing.yaml` | Token pricing |
| `AXON_DEMO_SEED_CONFIG` | `config/demo_seed.yaml` | Demo seed data |
| `AXON_CATALOG_CONFIG` | `config/catalog.yaml` | Provider catalogue for the admin UI |
| `AXON_ENSEMBLE_CONFIG` | `config/ensemble.yaml` | Ensemble presets |
| `AXON_SPOKES_CONFIG` | `config/spokes.yaml` | Multi-region spoke topology |

**Persistence**

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_ROUTER_DYNAMODB_ENABLED` | `false` | Enable DynamoDB persistence |
| `AXON_DYNAMODB_TABLE` | `axonllm-state` | Table name |

**Security and identity**

| Variable | Default | Description |
|----------|---------|-------------|
| `AXON_PII_REDACTION_DEFAULT` | — | Default PII redaction for projects that do not set it |
| `AXON_PII_REDACT_TYPES` | — | Restrict which of the ten regex types apply |
| `AXON_PII_NER_DEFAULT` | — | Default Comprehend entity detection (billed per call) |
| `AXON_PII_NER_TYPES` | — | Restrict entity types (default: name, address, age) |
| `AXON_OIDC_ISSUER` | — | OIDC issuer, for JWKS discovery |
| `AXON_OIDC_AUDIENCE` | — | Expected `aud` claim |
| `AXON_SAML_IDP_ENTITY_ID` | — | SAML IdP entity id |
| `AXON_SAML_IDP_SSO_URL` | — | SAML IdP SSO endpoint |
| `AXON_SAML_IDP_CERT` / `_CERT_FILE` | — | IdP signing certificate, inline or by path |
| `AXON_SAML_SP_ENTITY_ID` | — | This gateway's SP entity id |
| `AXON_SAML_ACS_URL` | — | Assertion consumer service URL |
| `AXON_SCIM_TOKEN` | — | Bearer token for `/scim/v2/*` |

**Caching**

| Variable | Default | Description |
|----------|---------|-------------|
| `AXON_SEMANTIC_CACHE` | `false` | Master switch; a project flag alone cannot enable it |
| `AXON_SEMANTIC_CACHE_MODEL` | — | Embedding model id (Bedrock Titan) |
| `AXON_SEMANTIC_CACHE_REGION` | `AXON_BEDROCK_REGION` | Region for embedding calls |
| `AXON_SEMANTIC_CACHE_THRESHOLD` | `0.90` | Cosine threshold; out-of-range or unparseable values log and fall back to the default |

**Observability**

| Variable | Default | Description |
|----------|---------|-------------|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | — | OTLP span export target |
| `OSTIARI_TRACES_URL` | — | External control-plane trace sink |
| `OSTIARI_INGEST_KEY` | — | Auth for the above |
| `OSTIARI_GATEWAY_ID` | `axonllm` | Identifies this gateway in forwarded traces |
| `OSTIARI_TRACES_TIMEOUT` | `3.0` | Per-forward timeout, seconds. Best-effort: a slow sink never slows a request |

**Provider credentials** — `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `AZURE_OPENAI_API_KEY`,
`GOOGLE_API_KEY`, `COHERE_API_KEY`, `XAI_API_KEY`, `GROQ_API_KEY`,
`TOGETHER_API_KEY`, `FIREWORKS_API_KEY`, `AI21_API_KEY`, and the standard AWS
chain for Bedrock. A provider with no credential is dropped from the routing
table at startup without raising, so a missing key is invisible until a request
routes there — which is what the readiness checklist's credential check exists to
catch.

**Also read:** `AXON_CHECK_MODEL_AVAILABILITY` (enables the readiness
checklist's live provider probe; leaving it off renders that row UNKNOWN rather
than PASS).

### 12.3 Runtime Requirements

| Requirement | Specification |
|-------------|---------------|
| **Python** | 3.11+ |
| **Dependencies** | starlette, aiohttp, pyyaml, tiktoken, boto3, `uvicorn[standard]` |
| **Optional extras** | `otel` (opentelemetry-exporter-otlp-proto-http), `saml` (signxml), `oidc` (python-jose[cryptography]), `dev`. `python-jose` is **fail-closed**: without it, JWT signature verification refuses to decode rather than trusting an unverified token, so an OIDC deployment that omits the extra rejects every token. |
| **Install** | `uv sync`, resolving from the committed `uv.lock`. `requirements.txt` exists only for the AgentCore image build, which reads it instead of `pyproject.toml`. |
| **AWS Credentials** | Required for Bedrock provider (automatic via IAM role or `aws configure`) |
| **DynamoDB** | Optional; table auto-created on first startup when enabled |

---

## 13. Observability and Monitoring

### 13.1 Structured Logging

All events are emitted as structured JSON with the following event types:

| Event | Fields | Trigger |
|-------|--------|---------|
| `request_completed` | request_id, project_id, user_id, model, provider, latency_ms, status_code, tokens, cost, trace_id, is_streaming, is_cached, retry_count, fallback_providers_tried | Every completed request |
| `provider_failure` | provider, error_type, status_code, retry_attempt, message, timestamp | Provider error during execution |
| `startup` | provider_count, model_count, project_count, routing_strategies | Application startup — **but note this event never fires.** `log_startup_summary` has no call site anywhere in `src/`, `scripts/` or `serve_dashboard.py`, so the emitter exists and nothing invokes it. (`startup_summary` is the method name; `startup` is the `event` value it would write.) |

### 13.2 Admin Dashboard Metrics

| Metric | Description |
|--------|-------------|
| Total Requests | Aggregate request count across all projects |
| Total Cost | Aggregate spend across all providers |
| Active Projects | Projects with recent usage |
| Active Users | Users with recent usage |
| Cache Hit Rate | Percentage of requests served from cache |
| Provider Health | Per-provider healthy/unhealthy status |
| Budget Utilization | Per-project spend vs. budget limit |
| Per-user Breakdown | Individual user cost and request counts |
| Per-model Breakdown | Per-model cost and request distribution |
| Per-provider Breakdown | Per-provider cost and request distribution |

### 13.3 Health Monitoring

- Background health check task polls provider endpoints at configurable intervals
- Unhealthy providers are excluded from routing with configurable cooldown
- Provider health status is visible in admin dashboard and `/admin/health` endpoint
- Latency is recorded per-request for the least-latency routing strategy
- In a multi-region deployment, `multi_region/health_monitor.py` tracks spoke
  health separately and drives failover between regions

### 13.4 Trace Export

Two exporters, arranged so exactly one span is produced per request in either
deployment mode.

| Path | Module | Behaviour |
|------|--------|-----------|
| **OTLP (standalone)** | `observability/otlp_exporter.py` | Maps each `UsageRecord` to one OpenTelemetry span, using GenAI semantic conventions (`gen_ai.*`) where they exist and `axon.*` for what OTEL has no standard for — provider, cost, routing strategy. Opt-in: a no-op unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set, and degrades to disabled rather than failing if the `otel` extra is not installed. |
| **Ostiari forwarding (embedded)** | `observability/trace_forwarder.py` | Forwards each completed request to an embedding Ostiari, over an HTTP sink (`OSTIARI_TRACES_URL`) or an in-process callback registered via `register_sink()`, or both. |

Two design rules matter to anyone reading the traces:

**No double export.** When embedded, Ostiari's own OTLP exporter emits the span
*with* the governance signal (risk tier, decision, session grouping), so
AxonLLM's exporter suppresses itself. Standalone, it emits directly to the
customer's backend. One span per request either way.

**AxonLLM does not score risk.** It is a routing and cost layer, so it forwards
neutral risk fields (`tier="allow"`, `score=0`) and puts its real signal — tokens,
cost, latency, provider — in the params and metadata. Risk scoring belongs to
Ostiari.

Forwarding is best-effort and must never affect the request path: every failure
is swallowed with a log, so a slow or broken Ostiari cannot slow or fail a chat
call. With no URL and no registered sink, the forwarder is inert.

---

## 14. Success Metrics and KPIs

### 14.1 Adoption Metrics

| Metric | Target | Measurement |
|--------|--------|-------------|
| Teams onboarded | 10+ within 6 months of launch | Count of active projects |
| Daily active users | 50+ | Distinct user_ids per day |
| Daily request volume | 10,000+ | Total requests per day |

### 14.2 Operational Metrics

| Metric | Target | Measurement |
|--------|--------|-------------|
| Gateway overhead latency (p99) | < 50ms | Time from request receipt to provider call initiation |
| Provider failover success rate | > 99% | Percentage of failed primary calls recovered by fallback |
| Cache hit rate (for cache-enabled projects) | > 20% | Cached responses / total requests for cache-enabled projects |

### 14.3 Business Impact Metrics

| Metric | Target | Measurement |
|--------|--------|-------------|
| LLM cost reduction via routing optimization | 15-30% | Comparison of cost-optimized routing vs. single-provider baseline |
| Integration code reduction | 70-80% | Lines of provider-specific code eliminated from application teams |
| Mean time to provider failover | < 5s | Time from provider failure to successful fallback response |
| Budget overrun incidents | Zero | Requests blocked by budget enforcement vs. uncontrolled spend |

---

## 15. Dependencies and Risks

### 15.1 Dependencies

| Dependency | Type | Impact if Unavailable |
|------------|------|----------------------|
| Amazon Bedrock AgentCore Runtime | Optional preview infrastructure | AgentCore evaluation is unavailable; Starlette deployment modes are unaffected |
| AWS Bedrock | Provider | Bedrock models unavailable; fallback to direct Anthropic/OpenAI if configured |
| Anthropic API | Provider | Direct Anthropic models unavailable; fallback to Bedrock-hosted Claude |
| OpenAI API | Provider | OpenAI models unavailable; no fallback (provider-exclusive models) |
| DynamoDB | Persistence and canonical authority | Legacy/local operation can continue in memory, but canonical identity/project authorization, durable API-key lifecycle, and AgentCore readiness fail closed |
| boto3 | SDK | Bedrock provider non-functional |
| tiktoken | Library | Token estimation falls back to character-based approximation |

### 15.2 Risks

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Provider API breaking changes | Medium | High | Adapter pattern isolates changes to single module; monitoring catches format drift |
| In-memory state loss on restart (without DynamoDB) | High | Medium | DynamoDB persistence is available; document as required for production |
| Rate limiter/response cache not shared across instances | Medium | Medium | Tenant isolation is enforced inside each process; deployment-wide rate semantics and cache consistency still require a shared backend or conservative per-replica limits |
| Tenant-global admin data | High | High | Keep the legacy admin plane isolated until users, usage, quotas, policies, webhooks, SCIM, and audit records are tenant-qualified and filtered |
| Cost tracking accuracy drift from pricing changes | Medium | Low | Pricing config is externalized in YAML; update cadence tracked |
| Single-region deployment limits availability | Low | High | Architecture supports multi-region; future milestone |

---

## 16. Release Milestones

### Phase 1: Foundation (Current)

- [x] Multi-provider routing with 5 strategies, plus ensemble scatter-gather
- [x] 13 provider adapters. Verified live: Bedrock, Bedrock Mantle, Anthropic, OpenAI, xAI, Together, Fireworks. Built but untested for want of a credential: Azure, Vertex, Google AI, Cohere, Groq, AI21
- [x] Automatic retry with exponential backoff and fallback chains
- [x] Cost tracking with comprehensive token-level attribution
- [x] Project and user budget enforcement
- [x] Per-project and per-user model access control
- [x] Content guardrails (keyword, regex, category)
- [x] Sliding window rate limiting
- [x] Response caching with TTL
- [x] Provider-level prompt caching (Anthropic/Bedrock)
- [x] Tool calling (function calling) with per-provider dialect translation
- [x] Admin dashboard with real-time analytics
- [x] Chat, Playground, and Routing Explorer web interfaces
- [x] DynamoDB persistence layer
- [x] AgentCore action adapter hardening preview
- [x] App Runner / Docker deployment
- [x] ECS Fargate via CDK (`infra/stack.py`, `./deploy-fargate.sh`)
- [x] PII redaction with re-injection, plus optional Comprehend entity detection
- [x] Prompt injection detection
- [x] Immutable SHA-256 hash-chain audit trail
- [x] Policy hierarchy (org → BU → project → environment) with quota enforcement
- [x] API key issue / rotate / revoke, and admin RBAC
- [x] Canonical principal resolution and authoritative tenant/project lookup for
      mapped HTTP and AgentCore data-plane actions
- [x] Tenant-qualified exact/semantic cache namespaces and API-key persistence;
      atomic key issue and revoke
- [x] 2,844 unit, integration, end-to-end, and property-based tests

### Phase 2: Production Hardening

- [ ] Production AgentCore IaC, JWT authorizer, resource policy, private
      networking, and deployment wiring for the implemented `/ready` contract
- [x] AgentCore explicit initialization with an admission deadline,
      OIDC/DynamoDB dependency readiness, authoritative project resolution, and
      graceful provider/OTLP shutdown
- [ ] Process-level containment or an async/cancellable replacement for a
      synchronous AgentCore bootstrap worker that can outlive its timeout
- [ ] Active tenant-isolated AgentCore Memory integration; `SessionManager`
      exists but is not wired into the runtime
- [ ] Azure OpenAI, Vertex AI, Cohere adapters fully validated with live providers
- [x] End-to-end integration test suite in CI/CD — `.github/workflows/ci.yml` runs
      the full suite on 3.11 and 3.12 with lint and a `uv lock --check`, including
      `tests/unit/test_e2e_starlette.py`
- [ ] Shared rate limiter and cache state via DynamoDB for multi-instance
      deployments — still in-process dicts in `rate_limiter.py` and
      `quota_enforcer.py`, so limits are per-task and a 2-task Fargate service
      enforces roughly double the configured RPM
- [ ] Tenant-qualified admin authorization and persistence for users, usage,
      quotas, policies, webhooks, SCIM, and audit; canonical principal lifecycle
      integration for SCIM and API keys; atomic key rotation
- [ ] CloudWatch metrics emission and alarms — *partial*: CloudWatch **Logs**
      emission ships via the event dispatcher, and OTLP span export plus a trace
      forwarder ship a full tracing path. Metrics and alarms proper are not built
- [ ] Usage export to S3 for long-term analytics
- [ ] Budget reset schedules (daily, weekly, monthly) — *partial*: manual reset
      ships as `POST /admin/quotas/{project_id}/reset`; there is no scheduler

### Phase 3: Enterprise Features

- [x] Multi-region deployment support — hub-and-spoke in single-region,
      active-passive and active-active weighted modes, with spoke health
      monitoring, failover, data-residency zone filtering and 9 admin endpoints
      (`src/gateway/multi_region/`)
- [x] SSO integration for admin console — SAML 2.0 with signed-assertion
      verification, SCIM 2.0 user/group provisioning, and direct OIDC/JWKS
- [x] Webhook notifications for budget alerts and provider health events —
      `EventDispatcher` fans out to webhook, SNS and CloudWatch Logs; budget
      alerts fire at 80/90/100%
- [ ] Advanced guardrails via Amazon Bedrock Guardrails integration
- [x] Usage chargeback reporting — CSV/JSON ships as
      `GET /admin/usage/export?format=csv|json&level=records|breakdown`, with 14
      chargeback columns. PDF is not built
- [ ] API versioning and backward compatibility guarantees
- [x] CDK infrastructure-as-code templates — `infra/stack.py` (CloudFront + ALB +
      ECS Fargate + DynamoDB + Secrets Manager). Terraform is not built

### Phase 4: Intelligence

- [ ] Adaptive routing (ML-driven provider selection based on historical
      performance) — *the write half only*. `feedback_tracker.py` records an
      outcome for every smart-routing decision and `bootstrap.py` reloads that
      history at startup, but nothing reads it back to influence a selection:
      `smart_routing.py` calls `_record_feedback` and never queries the tracker.
      The data needed to close the loop is being collected; the loop is not closed
- [x] Semantic caching — Bedrock Titan embeddings, 0.90 cosine threshold with a
      literal-token guard and a seven-axis polar guard, per-project opt-in, with
      stats and invalidate endpoints
- [x] Automatic model capability matching — this is the smart routing strategy in
      FR-R2, shipped: `task_classifier.py` classifies the prompt and
      `model_leaderboard.py` supplies the quality scores it weighs against cost
- [ ] Cost forecasting and anomaly detection
- [ ] Prompt optimization recommendations — *partial*. `semantic_efficiency.py`
      already returns compression opportunity, system-prompt ratio, history token
      count and redundancy indicators per prompt, surfaced at
      `/admin/efficiency`, `/admin/projects/{id}/efficiency` and
      `/admin/users/{id}/efficiency`. What is missing is *acting* on them —
      nothing rewrites or trims a prompt
- [x] Token efficiency and model right-sizing — not on the original roadmap but
      built: Level 1 ratio heuristics over `UsageRecord` data
      (`efficiency_analyzer.py`) with per-user profiles, waste alerts and
      project-peer comparison, plus Levels 2-3 tier-based right-sizing
      recommendations with estimated savings and a quality-impact rating
      (`semantic_efficiency.py`)

---

## 17. Open Questions and Future Considerations

| ID | Question | Context |
|----|----------|---------|
| OQ1 | Should AxonLLM support custom provider plugins via a public adapter SDK? | The adapter pattern is clean and extensible. A public SDK would enable customers to add proprietary or self-hosted model providers. |
| OQ2 | What is the strategy for multi-region active-active deployment? | **Partly resolved**: `multi_region/` ships active-active weighted routing, spoke health monitoring, failover and residency filtering. The open half is unchanged and is the harder one — in-memory state (cache, rate limiter, quota, health tracker) is still per-task, so limits are enforced per instance rather than per deployment. DynamoDB Global Tables could address persistence; real-time state sync adds complexity. |
| OQ3 | Should budget enforcement support *scheduled* resets? | Manual reset ships (`POST /admin/quotas/{project_id}/reset`), so the question is now specifically about a scheduler: budgets are otherwise cumulative, and monthly/weekly resets would align with typical FinOps cadences. |
| OQ4 | How should AxonLLM handle provider pricing changes? | Pricing YAML is manual. Automated pricing feeds from providers could reduce drift risk. |
| OQ5 | ~~Should the admin console support RBAC?~~ **Resolved: yes, shipped.** | `AdminRBACMiddleware` gates every `/admin/*` path on the `admin` role or an `admin:*` / `admin:<resource>` scope, with ENFORCE and LOG_ONLY modes. Remaining question: scopes are enforced on the admin plane only — nothing consults them on `/v1/*`, so a key issued `["models:read"]` can still spend money. Whether to enforce scopes on the data plane, or to keep `allowed_models` and budget as the only data-plane bound, is open. |
| OQ6 | What is the migration path for teams currently using provider SDKs directly? | OpenAI SDK compatibility minimizes migration effort. Documentation and tooling for gradual adoption should be prioritized. |
| OQ7 | ~~Should AxonLLM support tool use / function calling pass-through?~~ **Resolved: yes, shipped.** | Implemented as bidirectional per-provider translation rather than pass-through (§6.10) — pass-through alone is not possible, since only OpenAI-style providers accept OpenAI's shape. Remaining question: whether to advertise per-model tool support in the registry so smart routing can avoid models that lack it. |

---

## 18. Appendices

### Appendix A: Configured Models (Current Configuration)

Generated from `config/models.yaml` — 46 models. Regenerate rather than hand-edit,
since a table maintained by hand is what left this appendix 17 rows long and one
routing strategy wrong.

| Model | Description | Routing Strategy | Providers |
|-------|-------------|-----------------|-----------|
| `claude-opus` | Claude Opus 4 | least-latency | bedrock, anthropic |
| `claude-sonnet` | Claude Sonnet 4 | cost-optimized | anthropic, bedrock |
| `claude-haiku` | Claude Haiku 4.5 | round-robin | anthropic, bedrock |
| `gpt-5.5` | GPT-5.5 | round-robin | bedrock-mantle |
| `gpt-5.5-pro` | GPT-5.5 Pro (Responses API) | round-robin | openai |
| `gpt-5.4` | GPT-5.4 | round-robin | bedrock-mantle |
| `gpt-oss-120b-mantle` | GPT-OSS 120B (Mantle) | round-robin | bedrock-mantle |
| `deepseek-v3-mantle` | DeepSeek V3.1 (Mantle) | round-robin | bedrock-mantle |
| `qwen3-32b-mantle` | Qwen3 32B (Mantle) | round-robin | bedrock-mantle |
| `gpt-5.6-sol` | GPT-5.6 Sol (Mantle) | round-robin | bedrock-mantle |
| `gpt-5.6-terra` | GPT-5.6 Terra (Mantle) | round-robin | bedrock-mantle |
| `gpt-5.6-luna` | GPT-5.6 Luna (Mantle) | round-robin | bedrock-mantle |
| `claude-sonnet-5` | Claude Sonnet 5 (Mantle) | round-robin | bedrock-mantle |
| `claude-opus-4-8` | Claude Opus 4.8 (Mantle) | round-robin | bedrock-mantle |
| `claude-haiku-4-5-mantle` | Claude Haiku 4.5 (Mantle) | round-robin | bedrock-mantle |
| `gpt-4.1` | GPT-4.1 | round-robin | openai |
| `gpt-4o` | GPT-4o | least-latency | openai |
| `gpt-4o-mini` | GPT-4o Mini | round-robin | openai |
| `o4-mini` | o4 Mini (Reasoning) | weighted | openai |
| `o3` | o3 (Reasoning) | round-robin | openai |
| `nova-pro` | Amazon Nova Pro | round-robin | bedrock |
| `nova-lite` | Amazon Nova Lite | round-robin | bedrock |
| `nova-micro` | Amazon Nova Micro | round-robin | bedrock |
| `deepseek-r1` | DeepSeek R1 | round-robin | bedrock |
| `llama-3.3-70b` | Meta Llama 3.3 70B Instruct | round-robin | bedrock |
| `llama-4-maverick` | Meta Llama 4 Maverick 17B Instruct | round-robin | bedrock |
| `llama-4-scout` | Meta Llama 4 Scout 17B Instruct | round-robin | bedrock |
| `mistral-large` | Mistral Large (24.02) | round-robin | bedrock |
| `pixtral-large` | Mistral Pixtral Large 25.02 (multimodal) | round-robin | bedrock |
| `gpt-oss-120b` | OpenAI GPT-OSS 120B (open weight) | round-robin | bedrock |
| `qwen3-235b` | Qwen3 VL 235B A22B (multimodal, open weight) | round-robin | bedrock |
| `gemini-3.5-flash` | Gemini 3.5 Flash | round-robin | google_ai |
| `gemini-3.1-pro` | Gemini 3.1 Pro | round-robin | google_ai |
| `gemini-3-pro` | Gemini 3 Pro | round-robin | google_ai |
| `grok-4.3` | Grok 4.3 | round-robin | xai |
| `grok-4.5` | Grok 4.5 | round-robin | xai |
| `groq-llama-3.3-70b` | Llama 3.3 70B (Groq) | round-robin | groq |
| `groq-llama-3.1-8b` | Llama 3.1 8B Instant (Groq) | round-robin | groq |
| `together-llama-3.3-70b` | Llama 3.3 70B Turbo (Together) | round-robin | together |
| `together-deepseek-v4` | DeepSeek V4 Pro (Together) | round-robin | together |
| `together-qwen-3.5-9b` | Qwen 3.5 9B (Together) | round-robin | together |
| `together-gpt-oss-120b` | GPT-OSS 120B (Together) | round-robin | together |
| `fireworks-deepseek-v4` | DeepSeek V4 Pro (Fireworks) | round-robin | fireworks |
| `fireworks-gpt-oss-120b` | GPT-OSS 120B (Fireworks) | round-robin | fireworks |
| `jamba-large` | AI21 Jamba 1.6 Large | round-robin | ai21 |
| `jamba-mini` | AI21 Jamba 1.6 Mini | round-robin | ai21 |

### Appendix B: Error Codes

| HTTP Status | Error Type | Code | Trigger |
|-------------|-----------|------|---------|
| 400 | `invalid_request` | `invalid_message_format` | Malformed message structure |
| 400 | `invalid_request` | `invalid_role` | Unrecognized message role |
| 400 | `invalid_request` | `token_limit_exceeded` | Prompt exceeds model context window |
| 400 | `content_policy_violation` | `guardrail_violation` | Request blocked by guardrail rule |
| 401 | `authentication_error` | — | Invalid or missing JWT token |
| 403 | `forbidden` | `model_not_allowed` | Model not in project or user access list |
| 403 | `authorization_error` | — | Cedar policy denied |
| 404 | `not_found` | `model_not_found` | Model does not exist |
| 403 | `authorization_error` | `admin_access_denied` | Admin RBAC refused an `/admin/*` call — the caller's roles and key scopes did not cover the resource (`middleware/admin_rbac.py`) |
| 404 | `not_found` | `model_not_found` | Model does not exist |
| 429 | `rate_limit_error` | `rate_limit_exceeded` | User or project rate limit exceeded |
| 429 | `budget_exceeded` | `budget_exceeded` | Project or user budget exhausted |
| 429 | `quota_exceeded` | `quota_rate_limit_rpm`, `quota_budget_limit`, `quota_max_tokens_per_request`, `quota_allowed_models`, `quota_allowed_providers` | A limit from the resolved policy hierarchy was hit. The code is `quota_` plus the `limit_type` from the `QuotaDecision`, so it names exactly which of the five bound (`quota_enforcer.py`, `agent.py:276`) |
| 502 | `provider_error` | `all_providers_exhausted` | All providers in fallback chain failed |
| 500 | `server_error` | — | Gateway internal error |

Streaming responses cannot change status code once the first chunk has been
sent, so a mid-stream failure arrives as an error *event* in the SSE body rather
than as an HTTP status:

| Error type | Trigger |
|-----------|---------|
| `stream_error` | An exception raised after streaming began. Emitted as `{"data": {"error": {...}}}` on the open stream (`agent.py`) |
| `ensemble_error` | An ensemble run failed while streaming — panel dispatch or judge synthesis raised |

`401 authentication_error` is emitted by `middleware/auth.py` only under
`AXON_AUTH_MODE=ENFORCE`. Under `LOG_ONLY` the same request is instead given an
anonymous `RequestContext` and served, with the would-be denial logged. The same
split applies to policy denials: `403 authorization_error` under `ENFORCE`,
logged-and-allowed under `LOG_ONLY`. SAML callback failures
(`auth/saml_routes.py`) also surface as `authentication_error`.

### Appendix C: Project Structure

Enumerated from the tree rather than remembered, because the earlier version of
this appendix had drifted to about two-thirds of the modules that exist.

```
src/gateway/                     # 40 top-level modules + 8 packages
  |- agent.py                    # GatewayAgent orchestration — the 21-step pipeline in §8.1
  |- router.py                   # Retry with jittered backoff, fallback, strategy selection
  |- routing.py                  # The 4 base routing strategies
  |- smart_routing.py            # Smart routing (prompt → best model)
  |- task_classifier.py          # Prompt → task type, feeds smart routing
  |- feedback_tracker.py         # Outcome feedback for smart-routing decisions
  |- model_leaderboard.py        # Benchmark rankings from config/leaderboard.yaml
  |- ensemble.py                 # Ensemble helpers (synthesis, quorum, ranking)
  |- ensemble_config.py          # Ensemble preset loader (panel + judge)
  |- multi_provider_factory.py   # Bedrock (boto3) vs HTTP dispatch
  |- bedrock_provider.py         # Bedrock-specific boto3 provider
  |- mantle_provider.py          # Bedrock Mantle via SigV4 (3 inference APIs by model)
  |- http_client.py              # Async HTTP client with session pooling
  |- provider_fn_factory.py      # Provider callable factory
  |- provider_config.py          # Auth headers and URL construction
  |- provider_loader.py          # YAML + env var config loading
  |- dev_env.py                  # Reads .env for demos only, gated on AXON_LOAD_DEMO_DATA
  |- cost_tracker.py             # Usage, cost calculation, budgets
  |- quota_enforcer.py           # Turns a ResolvedPolicy into request-time limits
  |- rate_limiter.py             # Sliding window rate limiter
  |- striped_lock.py             # Per-key async locks, so one lock doesn't serialize the hot path
  |- guardrail_engine.py         # Content guardrails
  |- cache_manager.py            # Exact-match in-memory response cache
  |- semantic_cache.py           # Reworded-question cache (embeddings + polar-axis guard)
  |- embeddings.py               # Embedding backend protocol (Bedrock Titan today)
  |- efficiency_analyzer.py      # Level 1 token efficiency — ratio heuristics on UsageRecord
  |- semantic_efficiency.py      # Levels 2-3 — ML waste detection and model right-sizing
  |- health_tracker.py           # Provider health with cooldown
  |- health_check_task.py        # Background health monitoring
  |- request_validator.py        # Request validation
  |- model_registry.py           # YAML model config loader
  |- persistence.py              # DynamoDB single-table persistence (14 entity types)
  |- streaming.py                # Streaming helpers; simulated streaming is the fallback
  |- session_manager.py          # Inactive AgentCore Memory abstraction
  |- models.py                   # All dataclasses and enums
  |- config.py                   # Centralized configuration
  |- config_loader.py            # YAML + env var parsing
  |- bootstrap.py                # Component wiring
  |- logging.py                  # Structured JSON logging
  |- cli.py                      # `axon` console script (§12.1.1)
  |
  |- adapters/                   # 13 provider translators + shared style bases
  |    base.py, registry.py, anthropic_style.py, openai_style.py,
  |    openai_responses.py, gemini_tools.py, and one adapter each for
  |    ai21, anthropic, azure, bedrock, cohere, fireworks, google_ai,
  |    groq, mantle, openai, together, vertex, xai
  |- admin/                      # Admin API (69 method+path routes) + server-rendered dashboard
  |    routes.py, key_routes.py, policy_routes.py, quota_routes.py,
  |    region_routes.py, webhook_routes.py, audit_routes.py,
  |    production_checklist.py, catalog_drift.py, pricing_drift.py,
  |    model_availability.py, page_style.py
  |- auth/                       # Identity and authorization
  |    api_key_service.py, cedar_policy.py, oidc_service.py,
  |    policy_hierarchy.py, saml_service.py, saml_routes.py,
  |    scim_service.py, scim_routes.py
  |- chat/                       # Chat API, OpenAI-compatible API, web UI
  |    routes.py, openai_routes.py, client_agent.py
  |- middleware/                 # auth.py (JWT/API key), admin_rbac.py, security.py
  |- multi_region/               # region_config.py, region_router.py,
  |                              # health_monitor.py, spoke_loader.py
  |- observability/              # trace_forwarder.py (→ Ostiari), otlp_exporter.py
  |- security/                   # pii_redactor.py, pii_ner.py, injection_detector.py,
  |                              # audit_trail.py, event_dispatcher.py

config/
  |- models.yaml                 # Model definitions
  |- providers.yaml              # Provider connection configs (+ .example)
  |- pricing.yaml                # Token pricing per provider/model
  |- catalog.yaml                # Provider model catalog, for drift detection
  |- ensemble.yaml               # Ensemble presets (panel + judge)
  |- leaderboard.yaml            # Benchmark rankings
  |- spokes.yaml.example         # Multi-region spoke topology template
  |- demo_seed.yaml              # Demo data for development

infra/                           # AWS CDK app for the ECS Fargate stack
  |- app.py, stack.py, cdk.json

scripts/
  |- demo_client.py              # Demo traffic generator
  |- seed_demo_data.py           # Comprehensive data seeder
  |- run_test_traffic.py         # Integration test scenarios
  |- demo.sh                     # One-shot local demo
  |- build_architecture_assets.sh, build_narration_audio.sh
  |- demo/                       # Demo film pipeline: record.py, synthesize.py,
  |                              # make_captions.py, encode.py, paths.py, narration.json

tests/
  |- unit/                       # 88 files
  |- property/                   # 19 files, Hypothesis property-based
  |- conftest.py                 # Shared fixtures for the 2,844-test suite
```
