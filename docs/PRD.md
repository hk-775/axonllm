# Product Requirements Document: AxonLLM

## The Neural Control Plane for Enterprise LLMs

| Field              | Value                                      |
|--------------------|--------------------------------------------|
| **Product Name**   | AxonLLM                                    |
| **Author**         | Amazon Bedrock Product Management           |
| **Status**         | Draft                                       |
| **Version**        | 1.0                                         |
| **Date**           | 2026-04-27                                  |
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

Deployed as a serverless agent on Amazon Bedrock AgentCore Runtime, AxonLLM eliminates the need for teams to build and maintain custom LLM integration infrastructure while providing the governance controls that enterprises require.

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
| G8 | **Serverless deployment** | The gateway deploys as a managed agent on Amazon Bedrock AgentCore Runtime with zero infrastructure management. |

### 3.2 Non-Goals

| ID | Non-Goal | Rationale |
|----|----------|-----------|
| NG1 | Prompt engineering or prompt management | AxonLLM routes and governs requests; it does not modify or optimize prompts. Prompt management is a separate concern. |
| NG2 | Fine-tuning or model training | The gateway operates on inference endpoints only. Model customization is handled by the underlying providers. |
| NG3 | Replacing provider-native SDKs for all use cases | Applications with deep provider-specific requirements (e.g., fine-tuned model deployments, provider-native tooling) may still use provider SDKs directly. |
| NG4 | End-user authentication | AxonLLM handles service-to-service authentication and authorization. End-user identity management is the responsibility of the consuming application. |
| NG5 | Multi-region active-active deployment | Initial release targets single-region deployment. Multi-region will be evaluated based on customer demand. |

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
| FR-R2 | **Routing strategies** | Six configurable strategies per model: **round-robin** (sequential cycling), **weighted** (proportional distribution by configured weights), **least-latency** (route to fastest provider in sliding window), **cost-optimized** (route to cheapest healthy provider based on token pricing), **smart** (classify the prompt and select the best-performing model across all providers using benchmark leaderboard scores), **ensemble** (scatter-gather-synthesize across a panel of models with a judge — see FR-R7). |
| FR-R3 | **Automatic retry** | Retryable errors (HTTP 429, 500, 502, 503, 504) are retried with exponential backoff (base delay 1s, max 3 retries). Non-retryable errors (400, 401, 403) skip directly to fallback. |
| FR-R4 | **Multi-provider fallback** | When retries are exhausted, the router falls back to the next provider in the configured fallback chain (ordered by `fallback_order`). The caller receives a seamless response. |
| FR-R5 | **Health-aware routing** | Providers experiencing repeated failures are automatically marked unhealthy and excluded from routing for a configurable cooldown period (default 60s). Background health checks restore providers when recovered. |
| FR-R6 | **Provider preference** | Callers may optionally specify a preferred provider. If the preferred provider is available, it is used first; otherwise, the standard fallback chain applies. |
| FR-R7 | **Ensemble routing** | A scatter-gather-synthesize strategy invoked via model `ensemble` or `ensemble:<preset>`. Dispatches the prompt concurrently to a configurable panel (1–10 models), gathers successful responses, and a judge model synthesizes them into one grounded answer. Each preset defines a `panel`, `judge`, `quorum` (minimum survivors required to synthesize, default 1), `fallback_policy` (`best-single` or `error`), optional `cost_ceiling` (per-request USD cap enforced before dispatch), and `ranking_criteria`. Access control validates the full panel + judge; cost is tracked per underlying call; panel latency is bounded by the slowest member (60s per-member timeout) plus the judge. Presets are defined in `config/ensemble.yaml`. |

### 6.2 Provider Adapters

| ID | Requirement | Details |
|----|-------------|---------|
| FR-A1 | **Provider adapter interface** | Each provider implements a `ProviderAdapter` with: `translate_request()`, `translate_response()`, `translate_stream_chunk()`, `list_models()`, `health_check()`. |
| FR-A2 | **Supported providers** | AWS Bedrock (via boto3), Anthropic (HTTP), OpenAI (HTTP), Azure OpenAI (HTTP), Google Vertex AI (HTTP), Cohere (HTTP). |
| FR-A3 | **Dual execution paths** | Bedrock requests use boto3 native SDK (invoke_model for Anthropic models, converse API for others). All non-Bedrock providers use async HTTP with session pooling. |
| FR-A4 | **Request/response normalization** | All provider-specific payloads are translated to/from a unified OpenAI-compatible format (ChatCompletionRequest/Response). Provider differences (field names, message formats, system prompt handling) are abstracted. |
| FR-A5 | **Streaming translation** | Each adapter translates provider-specific SSE events into a unified StreamChunk format. Providers that don't support native streaming receive simulated streaming (word-level chunking of complete responses). |
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
| FR-AC3 | **JWT authentication** | Requests carry JWT tokens validated via AgentCore Identity service. Claims are extracted into a RequestContext (user_id, project_id, roles, scopes). |
| FR-AC4 | **Cedar policy evaluation** | Fine-grained authorization via Cedar policies evaluated against (principal, action, resource) tuples. Policies support ENFORCE and LOG_ONLY modes. |

### 6.5 Content Guardrails

| ID | Requirement | Details |
|----|-------------|---------|
| FR-G1 | **Per-project guardrail rules** | Each project can define guardrail rules with: name, rule_type (keyword_block, regex_match, content_category), pattern, action (block, warn, redact), applies_to (request, response, both). |
| FR-G2 | **Request guardrails** | Rules with `applies_to` = "request" or "both" are evaluated against all message content before the request reaches a provider. Blocking violations return HTTP 400. |
| FR-G3 | **Response guardrails** | Rules with `applies_to` = "response" or "both" are evaluated against response content. Blocking violations replace the response content with a policy violation message. |

### 6.6 Caching

| ID | Requirement | Details |
|----|-------------|---------|
| FR-CA1 | **Response caching** | Per-project configurable response cache. Semantically identical requests within the TTL (default 300s) are served from cache with zero additional provider cost. |
| FR-CA2 | **Deterministic cache keys** | Cache keys are SHA-256 hashes of: model, messages, temperature, max_tokens, top_p, stop, tools, tool_choice, and project_id. Ensures identical requests produce identical keys regardless of field order. The tool list is part of the key because the same prompt sent with tools can return a tool call and sent without them returns prose — a shared key would serve a cached tool-free reply to a request that needs a call. |
| FR-CA3 | **Provider-level prompt caching** | When enabled per project, system prompts are annotated with `cache_control: ephemeral` blocks for Anthropic/Bedrock providers, reducing cost and latency on repeated system prompts. |

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
| FR-AD2 | **Project management** | CRUD operations for projects including budget configuration, member management, model access lists, and guardrail rules. Changes are hot-reloaded without restart. |
| FR-AD3 | **User management** | View users with usage, set individual budgets and model access restrictions. |
| FR-AD4 | **Model management** | View, create, update, and delete model configurations. Changes persist to YAML and optionally to DynamoDB. |
| FR-AD5 | **Usage analytics** | Filterable usage data with breakdowns by time range, provider, model, project, and user. |
| FR-AD6 | **Policy management** | Create and view Cedar authorization policies with ENFORCE/LOG_ONLY modes. |
| FR-AD7 | **Provider health** | Real-time per-provider health status (healthy/unhealthy). |

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
| NFR-A1 | Gateway uptime | 99.9% (matching AgentCore SLA) |
| NFR-A2 | Provider failover time | < 2s (retry + fallback to next provider) |
| NFR-A3 | Health check interval | Configurable (default 30s) |

### 7.3 Scalability

| ID | Requirement | Target |
|----|-------------|--------|
| NFR-S1 | Horizontal scaling | AgentCore Runtime provides serverless auto-scaling based on request volume |
| NFR-S2 | Per-instance in-memory state | Rate limiter counters, health tracker state, and cache are per-instance. Multi-instance deployments can optionally use DynamoDB-backed shared storage. |

### 7.4 Reliability

| ID | Requirement | Target |
|----|-------------|--------|
| NFR-R1 | Retry behavior | Exponential backoff (1s, 2s, 4s) with max 3 retries on transient errors |
| NFR-R2 | Fallback depth | Full fallback chain traversal before returning error to caller |
| NFR-R3 | Graceful degradation | DynamoDB outage does not block request processing (fire-and-forget writes with warning logs) |

### 7.5 Testability

| ID | Requirement | Target |
|----|-------------|--------|
| NFR-T1 | Unit test coverage | 500+ tests covering all components |
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
  +-------------+           |   (14-step orchestration)       |           | (boto3)  |
                            |                                 |           +----------+
  +-------------+           |   1. Parse Request              |
  |  Chat UI    |---------->|   2. Extract Context            |           +----------+
  +-------------+           |   3. Request Validation         |---------->|Anthropic |
                            |   4. Rate Limit Check           |           | (HTTP)   |
  +-------------+           |   5. Project Model Access Check |           +----------+
  |  Dashboard  |---------->|   6. User Model Access Check    |
  +-------------+           |   7. Project Budget Check       |           +----------+
                            |   8. User Budget Check          |---------->| OpenAI   |
                            |   9. Request Guardrails         |           | (HTTP)   |
                            |  10. Cache Check                |           +----------+
                            |  11. Route & Execute            |
                            |  12. Response Guardrails        |           +----------+
                            |  13. Cost Tracking              |---------->| Azure    |
                            |  14. Return Response            |           | (HTTP)   |
                            |                                 |           +----------+
                            |  +----------+  +-----------+    |
                            |  |DynamoDB  |  |Health     |    |           +----------+
                            |  |Persist.  |  |Monitor    |    |---------->| Vertex   |
                            |  +----------+  +-----------+    |           | (HTTP)   |
                            +---------------------------------+           +----------+
```

### 8.2 Component Architecture

| Component | Responsibility | Module |
|-----------|---------------|--------|
| **GatewayAgent** | Orchestrates the 14-step request pipeline | `agent.py` |
| **Router** | Strategy-based provider selection with retry and fallback | `router.py` |
| **Routing Strategies** | Round-robin, weighted, least-latency, cost-optimized selection algorithms | `routing.py` |
| **Smart Routing** | Prompt classification → best-model selection via benchmark leaderboard | `smart_routing.py` |
| **Ensemble Routing** | Scatter-gather-synthesize across a model panel with a judge | `ensemble.py`, `ensemble_config.py` |
| **MultiProviderFactory** | Dispatches Bedrock (boto3) vs. HTTP provider calls | `multi_provider_factory.py` |
| **Provider Adapters** | Per-provider request/response/streaming translation | `adapters/` |
| **HttpClient** | Async HTTP execution with session pooling | `http_client.py` |
| **BedrockProvider** | boto3 invoke_model (Anthropic) and converse API (Nova, DeepSeek) | `bedrock_provider.py` |
| **CostTracker** | Usage recording, cost calculation, budget enforcement | `cost_tracker.py` |
| **RateLimiter** | Sliding window per-user/per-project rate limiting | `rate_limiter.py` |
| **GuardrailEngine** | Request/response content inspection against configurable rules | `guardrail_engine.py` |
| **CacheManager** | In-memory TTL-based response cache | `cache_manager.py` |
| **HealthTracker** | Provider health tracking with cooldown periods and latency recording | `health_tracker.py` |
| **ModelRegistry** | Model configuration loading and validation from YAML | `model_registry.py` |
| **RequestValidator** | Structural, semantic, and token-limit request validation | `request_validator.py` |
| **DynamoPersistence** | DynamoDB read/write for usage records, projects, user configs | `persistence.py` |
| **AuthMiddleware** | JWT validation + Cedar policy evaluation | `middleware/auth.py` |
| **SessionManager** | Conversation persistence via AgentCore Memory | `session_manager.py` |
| **AdminAPI** | Admin dashboard REST API and React SPA | `admin/` |
| **ChatAPI** | Client-facing chat API and web interfaces | `chat/` |
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
  |- demo_seed.yaml     Demo projects, users, budgets, seed data
  |- catalog.yaml       Provider model catalog for admin UI
        |
        v
Dataclass Configurations (runtime)
  |- AppConfig           Server host/port, file paths, feature flags
  |- GatewayConfig       Retry, rate limit, cache, adapter defaults
  |- ProviderConfig      Per-provider auth, URLs, timeouts
  |- VirtualModelConfig  Model-to-provider mappings, routing strategy
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

### 9.3 Web Interfaces

| Page | URL | Purpose |
|------|-----|---------|
| Admin Dashboard | `/admin/dashboard` | Admin console: projects, users, models, budgets, health, policies |
| Chat | `/chat` | Interactive chat with model, provider, and user selection |
| Playground | `/playground` | Clean chat with routing decision visibility |
| Routing Explorer | `/routing` | Prompt-only routing explorer; shows model and provider selection logic |

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

VirtualModelConfig
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
  |- budget_limit: float?
  |- alert_threshold: float?
  |- allowed_models: list[str]?
  |- guardrail_rules: list[GuardrailRule]
  |- cache_enabled: bool
  |- cache_ttl_seconds: int
  |- prompt_caching_enabled: bool
  |- rate_limit_rpm: int?
  |- members: list[str]
  |- created_at: datetime

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
```

### 10.2 DynamoDB Schema

Single-table design using composite keys:

| Entity | PK | SK | Purpose |
|--------|----|----|---------|
| Usage Record | `USAGE#{request_id}` | `USAGE#{timestamp}` | Per-request cost and token tracking |
| Project | `PROJECT#{project_id}` | `PROJECT` | Project configuration and membership |
| User Config | `USER#{user_id}` | `CONFIG` | User budget and model access settings |

---

## 11. Security and Compliance

### 11.1 Authentication

| Layer | Mechanism |
|-------|-----------|
| **API Authentication** | JWT tokens validated via AgentCore Identity service. Bearer token extracted from Authorization header. |
| **Provider Authentication** | Provider-specific: AWS IAM (Bedrock), API keys (Anthropic, OpenAI, Cohere), Azure AD keys (Azure OpenAI), GCP service accounts (Vertex AI). |
| **Credential Management** | API keys sourced from environment variables (highest precedence) or YAML config files. No credentials hardcoded in source. |

### 11.2 Authorization

| Layer | Mechanism |
|-------|-----------|
| **Model Access Control** | Per-project and per-user allowed model lists enforced at gateway level (HTTP 403 rejection). |
| **Cedar Policies** | Fine-grained authorization via Cedar policy language. Policies evaluate (principal, action, resource) tuples. ENFORCE and LOG_ONLY modes. |
| **Budget Enforcement** | Budget limits function as financial authorization gates. Over-budget requests are rejected (HTTP 429). |

### 11.3 Content Safety

| Layer | Mechanism |
|-------|-----------|
| **Request Guardrails** | Content inspection before provider call. Keyword blocking, regex matching, content category filtering. Block/warn/redact actions. |
| **Response Guardrails** | Content inspection after provider response. Blocking violations replace response content with policy message. |

### 11.4 Data Protection

| Concern | Approach |
|---------|----------|
| **Credentials at rest** | Environment variables preferred; YAML config files should be excluded from version control. |
| **Data in transit** | All provider calls use HTTPS. Bedrock uses AWS SDK with SigV4 signing. |
| **Logging** | Structured JSON logs include request metadata (IDs, tokens, cost) but not message content. |

---

## 12. Deployment and Operations

### 12.1 Deployment Options

| Option | Description | Use Case |
|--------|-------------|----------|
| **Amazon Bedrock AgentCore** | `agentcore configure` + `agentcore launch`. Serverless, auto-scaling, managed infrastructure. | Production |
| **AWS App Runner** | Docker container deployed via ECR + App Runner. Semi-managed with container-level configuration. | Production (alternative) |
| **Docker** | `docker build` + `docker run`. Standard containerized deployment. | Staging, on-premises |
| **Local development** | `python serve_dashboard.py`. Uvicorn dev server with hot reload and demo data. | Development |

### 12.2 Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AWS_DEFAULT_REGION` | `us-east-1` | AWS region |
| `AXON_BEDROCK_REGION` | `us-east-1` | Bedrock-specific region |
| `AXON_SERVER_HOST` | `0.0.0.0` | Server bind host |
| `AXON_SERVER_PORT` | `8000` | Server port |
| `AXON_MODELS_CONFIG` | `config/models.yaml` | Model definitions path |
| `AXON_PROVIDERS_CONFIG` | `config/providers.yaml` | Provider configs path |
| `AXON_PRICING_CONFIG` | `config/pricing.yaml` | Token pricing path |
| `AXON_DEMO_SEED_CONFIG` | `config/demo_seed.yaml` | Demo seed data path |
| `AXON_CATALOG_CONFIG` | `config/catalog.yaml` | Provider catalog path |
| `AXON_LOAD_DEMO_DATA` | `false` | Load demo data on startup |
| `LLM_ROUTER_DYNAMODB_ENABLED` | `false` | Enable DynamoDB persistence |
| `ANTHROPIC_API_KEY` | — | Anthropic API key |
| `OPENAI_API_KEY` | — | OpenAI API key |

### 12.3 Runtime Requirements

| Requirement | Specification |
|-------------|---------------|
| **Python** | 3.11+ |
| **Dependencies** | starlette, aiohttp, pyyaml, tiktoken, boto3 |
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
| `startup_summary` | provider_count, model_count, project_count, routing_strategies | Application startup |

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
| Amazon Bedrock AgentCore Runtime | Infrastructure | Primary deployment target unavailable; fall back to App Runner/Docker |
| AWS Bedrock | Provider | Bedrock models unavailable; fallback to direct Anthropic/OpenAI if configured |
| Anthropic API | Provider | Direct Anthropic models unavailable; fallback to Bedrock-hosted Claude |
| OpenAI API | Provider | OpenAI models unavailable; no fallback (provider-exclusive models) |
| DynamoDB | Persistence | State not persisted; in-memory operation continues (graceful degradation) |
| boto3 | SDK | Bedrock provider non-functional |
| tiktoken | Library | Token estimation falls back to character-based approximation |

### 15.2 Risks

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Provider API breaking changes | Medium | High | Adapter pattern isolates changes to single module; monitoring catches format drift |
| In-memory state loss on restart (without DynamoDB) | High | Medium | DynamoDB persistence is available; document as required for production |
| Rate limiter/cache not shared across instances | Medium | Medium | Per-instance state documented; shared DynamoDB backend available for multi-instance |
| Cost tracking accuracy drift from pricing changes | Medium | Low | Pricing config is externalized in YAML; update cadence tracked |
| Single-region deployment limits availability | Low | High | Architecture supports multi-region; future milestone |

---

## 16. Release Milestones

### Phase 1: Foundation (Current)

- [x] Multi-provider routing with 4 strategies
- [x] Provider adapters for Bedrock, Anthropic, OpenAI (working); Azure, Vertex, Cohere (adapter ready)
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
- [x] AgentCore Runtime deployment
- [x] App Runner / Docker deployment
- [x] 500+ unit and property-based tests

### Phase 2: Production Hardening

- [ ] Azure OpenAI, Vertex AI, Cohere adapters fully validated with live providers
- [ ] End-to-end integration test suite in CI/CD
- [ ] Shared rate limiter and cache state via DynamoDB for multi-instance deployments
- [ ] CloudWatch metrics emission and alarms
- [ ] Usage export to S3 for long-term analytics
- [ ] Budget reset schedules (daily, weekly, monthly)

### Phase 3: Enterprise Features

- [ ] Multi-region deployment support
- [ ] SSO integration for admin console
- [ ] Webhook notifications for budget alerts and provider health events
- [ ] Advanced guardrails via Amazon Bedrock Guardrails integration
- [ ] Usage chargeback reporting (CSV/PDF export)
- [ ] API versioning and backward compatibility guarantees
- [ ] Terraform/CDK infrastructure-as-code templates

### Phase 4: Intelligence

- [ ] Adaptive routing (ML-driven provider selection based on historical performance)
- [ ] Semantic caching (embedding-based similarity for cache hits)
- [ ] Automatic model capability matching (route to model best suited for task type)
- [ ] Cost forecasting and anomaly detection
- [ ] Prompt optimization recommendations

---

## 17. Open Questions and Future Considerations

| ID | Question | Context |
|----|----------|---------|
| OQ1 | Should AxonLLM support custom provider plugins via a public adapter SDK? | The adapter pattern is clean and extensible. A public SDK would enable customers to add proprietary or self-hosted model providers. |
| OQ2 | What is the strategy for multi-region active-active deployment? | In-memory state (cache, rate limiter, health tracker) needs coordination. DynamoDB Global Tables could address persistence, but real-time state sync adds complexity. |
| OQ3 | Should budget enforcement support reset schedules? | Current budgets are cumulative with no reset. Monthly/weekly resets would align with typical FinOps cadences. |
| OQ4 | How should AxonLLM handle provider pricing changes? | Pricing YAML is manual. Automated pricing feeds from providers could reduce drift risk. |
| OQ5 | Should the admin console support RBAC? | Currently all admin API operations are available to any authenticated user. Role-based access (admin, viewer, project-scoped) would improve security posture. |
| OQ6 | What is the migration path for teams currently using provider SDKs directly? | OpenAI SDK compatibility minimizes migration effort. Documentation and tooling for gradual adoption should be prioritized. |
| OQ7 | ~~Should AxonLLM support tool use / function calling pass-through?~~ **Resolved: yes, shipped.** | Implemented as bidirectional per-provider translation rather than pass-through (§6.10) — pass-through alone is not possible, since only OpenAI-style providers accept OpenAI's shape. Remaining question: whether to advertise per-model tool support in the registry so smart routing can avoid models that lack it. |

---

## 18. Appendices

### Appendix A: Supported Virtual Models (Current Configuration)

| Virtual Model | Description | Routing Strategy | Providers |
|---------------|-------------|-----------------|-----------|
| `claude-opus` | Claude Opus 4 | least-latency | Bedrock, Anthropic |
| `claude-sonnet` | Claude Sonnet 4 | cost-optimized | Anthropic, Bedrock |
| `claude-haiku` | Claude Haiku 4.5 | round-robin | Anthropic, Bedrock |
| `gpt-4o` | GPT-4o | round-robin | OpenAI |
| `gpt-4o-mini` | GPT-4o Mini | round-robin | OpenAI |
| `o4-mini` | o4 Mini (Reasoning) | weighted | OpenAI |
| `nova-pro` | Amazon Nova Pro | round-robin | Bedrock |
| `nova-lite` | Amazon Nova Lite | round-robin | Bedrock |
| `nova-micro` | Amazon Nova Micro | round-robin | Bedrock |
| `deepseek-r1` | DeepSeek R1 | round-robin | Bedrock |
| `llama-3.3-70b` | Meta Llama 3.3 70B Instruct | round-robin | Bedrock |
| `llama-4-maverick` | Meta Llama 4 Maverick 17B Instruct | round-robin | Bedrock |
| `llama-4-scout` | Meta Llama 4 Scout 17B Instruct | round-robin | Bedrock |
| `mistral-large` | Mistral Large 2 (24.07) | round-robin | Bedrock |
| `pixtral-large` | Mistral Pixtral Large 25.02 (multimodal) | round-robin | Bedrock |
| `gpt-oss-120b` | OpenAI GPT-OSS 120B (open weight) | round-robin | Bedrock |
| `qwen3-235b` | Qwen3 235B A22B (open weight) | round-robin | Bedrock |

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
| 429 | `rate_limit_error` | `rate_limit_exceeded` | User or project rate limit exceeded |
| 429 | `budget_exceeded` | `budget_exceeded` | Project or user budget exhausted |
| 502 | `provider_error` | `all_providers_exhausted` | All providers in fallback chain failed |
| 500 | `server_error` | — | Gateway internal error |

### Appendix C: Project Structure

```
src/gateway/
  |- agent.py                    # GatewayAgent orchestration
  |- router.py                   # Retry, fallback, strategy selection
  |- routing.py                  # 4 base routing strategy implementations
  |- smart_routing.py            # Smart routing (prompt → best model)
  |- ensemble.py                 # Ensemble strategy helpers (synthesis, quorum, ranking)
  |- ensemble_config.py          # Ensemble preset loader (panel + judge)
  |- multi_provider_factory.py   # Bedrock (boto3) vs HTTP dispatch
  |- bedrock_provider.py         # Bedrock-specific boto3 provider
  |- http_client.py              # Async HTTP client with session pooling
  |- provider_fn_factory.py      # Provider callable factory
  |- provider_config.py          # Auth headers and URL construction
  |- provider_loader.py          # YAML + env var config loading
  |- cost_tracker.py             # Usage, cost calculation, budgets
  |- rate_limiter.py             # Sliding window rate limiter
  |- guardrail_engine.py         # Content guardrails
  |- cache_manager.py            # In-memory response cache
  |- health_tracker.py           # Provider health with cooldown
  |- health_check_task.py        # Background health monitoring
  |- request_validator.py        # Request validation
  |- model_registry.py           # YAML model config loader
  |- persistence.py              # DynamoDB persistence layer
  |- streaming.py                # Simulated streaming
  |- session_manager.py          # AgentCore Memory integration
  |- models.py                   # All dataclasses and enums
  |- config.py                   # Centralized configuration
  |- config_loader.py            # YAML + env var parsing
  |- bootstrap.py                # Component wiring
  |- logging.py                  # Structured JSON logging
  |- adapters/                   # Provider-specific translators
  |- admin/                      # Admin dashboard API + SPA
  |- chat/                       # Chat API + web interfaces
  |- middleware/                  # JWT + Cedar auth middleware

config/
  |- models.yaml                 # Model definitions
  |- providers.yaml              # Provider connection configs
  |- pricing.yaml                # Token pricing per provider/model
  |- demo_seed.yaml              # Demo data for development
  |- catalog.yaml                # Provider model catalog

scripts/
  |- demo_client.py              # Demo traffic generator
  |- seed_demo_data.py           # Comprehensive data seeder
  |- run_test_traffic.py         # Integration test scenarios

tests/
  |- unit/                       # Unit tests (500+)
  |- property/                   # Hypothesis property-based tests
```
