# PRFAQ: AxonLLM — The Neural Control Plane for Enterprise LLMs

---

## Press Release

### AxonLLM: The Neural Control Plane for Enterprise LLMs

**Seattle, WA** — Today we announce AxonLLM, a centralized gateway that gives organizations a single API endpoint to access multiple large language model providers while maintaining full control over routing, costs, access, compliance, and content safety. AxonLLM eliminates the operational complexity of integrating with multiple LLM providers by providing a unified OpenAI-compatible interface, intelligent request routing with automatic failover, per-project and per-user budget management, configurable content guardrails, and a real-time web-based admin console — all deployed as a serverless agent on Amazon Bedrock AgentCore Runtime.

**The Problem**

Organizations adopting LLMs face a growing operational burden: they need access to models from multiple providers — AWS Bedrock, Anthropic, OpenAI, Azure OpenAI, Google Vertex AI, and Cohere — to optimize for cost, capability, and availability. Each provider has its own API format, authentication mechanism, pricing model, and failure behavior. Engineering teams end up building and maintaining custom integration code for each provider, duplicating effort across projects. Platform teams lack visibility into who is using what, how much it costs, and whether usage complies with organizational policies. When a provider goes down, applications break with no automated recovery. When costs spike, there is no circuit breaker. When sensitive content reaches a model, there is no safety net.

**The Solution**

AxonLLM sits between applications and LLM providers as a 14-step request pipeline. Developers send standard OpenAI-compatible requests to a single endpoint. AxonLLM handles everything else: validating requests, enforcing rate limits, checking model access permissions, enforcing budgets, applying content guardrails, checking the response cache, routing to the optimal provider based on configurable strategies, retrying on transient failures, falling back to alternative providers, tracking costs with full token-level attribution, and returning a unified response. Platform operators get a web-based admin console to manage projects, set budgets at both the project and user level, configure access policies and guardrail rules, monitor usage in real time, and view per-provider health status.

**Key Capabilities**

- **Unified OpenAI-compatible API** that works with any supported LLM provider — switch providers without changing application code
- **Intelligent routing** with four configurable strategies: round-robin, weighted, least-latency, and cost-optimized — each model can use a different strategy
- **Automatic retry and failover** with exponential backoff on transient errors (429, 500, 502, 503, 504) and multi-provider fallback chains for high availability. Non-retryable errors (400, 401, 403) skip directly to the next provider.
- **Comprehensive cost tracking** with full token-level attribution including cached tokens, cache creation tokens, image/vision tokens, reasoning tokens (o1/o3/o4 models), and flat per-request fees — with configurable budget limits and alerts at both the project and user level
- **Per-user and per-project model access control** enforced at the gateway level before any request reaches a provider, with effective access computed as the intersection of project and user access lists
- **Configurable content guardrails** per project with keyword blocking, regex matching, and content category filtering — applied to requests, responses, or both, with block/warn/redact actions
- **Provider-level prompt caching** for Anthropic and Bedrock providers, annotating system prompts with `cache_control: ephemeral` blocks to reduce cost and latency on repeated system prompts
- **Streaming support (SSE)** across all providers, including real SSE pass-through for native streaming and simulated word-level streaming for providers that don't support it
- **Six provider adapters** with a clean adapter pattern: AWS Bedrock (via boto3 with invoke_model and converse APIs), Anthropic, OpenAI, Azure OpenAI, Google Vertex AI, and Cohere — each translating to/from a unified request/response format
- **Admin console** with real-time dashboards for usage monitoring, cost analytics, project management, model configuration, Cedar policy management, and provider health status — all changes hot-reloaded without restart
- **Chat, Playground, and Routing Explorer** web interfaces for interactive model testing, routing decision visibility, and routing logic exploration
- **DynamoDB persistence** for state recovery across restarts — usage records, project configurations, and user settings persist to a single DynamoDB table with on-demand billing
- **Sliding window rate limiting** per user (60 RPM default) and per project (600 RPM default) with rate limit headers on every response
- **Request validation** including message structure, role validation, model existence, and token limit checking via tiktoken
- **Cedar authorization policies** for fine-grained access control with ENFORCE and LOG_ONLY modes
- **Structured JSON logging** with request-level observability including trace IDs, latency, retry counts, and fallback provider history
- **500+ tests** including unit tests and Hypothesis property-based tests covering routing fairness, cost accuracy, cache determinism, retry behavior, and configuration round-trip consistency

**Customer Quotes**

"Before AxonLLM, each of our product teams maintained their own LLM integrations. We had no visibility into total spend, no way to enforce access policies, and every provider outage was a fire drill. AxonLLM gave us a single control plane. We cut our integration code by 80%, reduced LLM costs by 30% through cost-optimized routing, and our platform team can now manage budgets, model access, and content guardrails without touching application code."

— VP of Engineering, Enterprise Software Company

"We run 15 different projects consuming LLMs. With AxonLLM, each project has its own budget, its own guardrail rules, and its own model access list. When one team hit their budget limit, the gateway blocked further requests instead of running up a surprise bill. That level of financial governance is exactly what our CFO was asking for."

— Director of Platform Engineering, Financial Services

**Getting Started**

AxonLLM is deployed as a Python agent on Amazon Bedrock AgentCore Runtime. Configure your model mappings in `config/models.yaml`, set up your provider credentials via environment variables or `config/providers.yaml`, and launch. The admin console is available immediately at `/admin/dashboard`. Applications point their OpenAI SDK to the AxonLLM endpoint — no other code changes required.

```bash
# Local development
AXON_LOAD_DEMO_DATA=true python serve_dashboard.py

# AgentCore deployment
agentcore configure --entrypoint agentcore_agent.py --name axon_llm --region us-east-1 --non-interactive
agentcore launch

# App Runner deployment
./deploy.sh us-east-1
```

---

## Frequently Asked Questions

### Customer FAQ

**Q: Do I need to change my application code to use AxonLLM?**

A: If your application already uses the OpenAI SDK or any OpenAI-compatible API format, you only need to change the base URL to point to AxonLLM. The API accepts and returns OpenAI-compatible payloads. No changes to your request format, response handling, or streaming logic are required. AxonLLM responds with the same JSON structure your application already expects, with the addition of a `provider` field indicating which backend served the request.

**Q: Which LLM providers are supported?**

A: AxonLLM ships with six provider adapters: AWS Bedrock (via boto3), Anthropic (direct HTTP), OpenAI (direct HTTP), Azure OpenAI, Google Vertex AI, and Cohere. Bedrock, Anthropic, and OpenAI are fully validated with live providers. Azure OpenAI, Vertex AI, and Cohere adapters are built and ready for validation. Each adapter implements a standard interface (`translate_request`, `translate_response`, `translate_stream_chunk`), so adding a new provider means implementing one adapter class — routing, cost tracking, caching, guardrails, and the admin dashboard all work automatically.

**Q: Which models are available out of the box?**

A: The default configuration includes a broad catalog spanning proprietary and open-weight models: Claude Opus 4, Claude Sonnet 4, Claude Haiku 4.5, GPT-4o, GPT-4o Mini, o4 Mini (Reasoning), Amazon Nova Pro, Amazon Nova Lite, Amazon Nova Micro, DeepSeek R1, Meta Llama 3.3 70B, Meta Llama 4 Maverick, Meta Llama 4 Scout, Mistral Large 2, Mistral Pixtral Large, OpenAI GPT-OSS 120B, Qwen3 235B, and the Gemini 2.x family. Many of the open-weight models (Llama, Mistral, DeepSeek, GPT-OSS, Qwen) are served through AWS Bedrock. Claude models can route to both Bedrock and Anthropic direct, with the router choosing based on the configured strategy. You can add, modify, or remove models through the admin console or by editing `config/models.yaml`.

**Q: How does routing work when I have the same model available from multiple providers?**

A: You configure a model (e.g., `claude-opus`) that maps to one or more provider-specific endpoints. Each model has a routing strategy:

- **Round-robin**: distributes requests evenly across healthy providers in sequence
- **Weighted**: distributes proportionally to configured weights (e.g., 70/30 split)
- **Least-latency**: routes to the provider with the lowest average latency in a sliding time window
- **Cost-optimized**: routes to the cheapest healthy provider based on per-token pricing

If a provider is unhealthy (marked after repeated failures with a configurable cooldown), it is automatically excluded from routing until it recovers. You can also optionally specify a preferred provider per request to override the strategy.

**Q: What happens when a provider goes down?**

A: AxonLLM provides three layers of resilience:

1. **Retry**: Transient errors (HTTP 429, 500, 502, 503, 504) are retried with exponential backoff (1s, 2s, 4s) up to 3 times. Non-retryable errors (400, 401, 403) are not retried.
2. **Fallback**: When retries are exhausted, the router moves to the next provider in the configured fallback chain (ordered by `fallback_order`).
3. **Health tracking**: Providers that exhaust all retries are marked unhealthy for a configurable cooldown period (default 60 seconds). A background health check task proactively monitors provider health at regular intervals and restores providers when they recover.

Your application sees a seamless response — it doesn't need to know which provider served it or how many fallback attempts occurred. The response includes a `provider` field and the structured logs capture retry counts and fallback providers tried.

**Q: How does cost tracking work?**

A: Every request is tracked with full token-level attribution. The cost engine goes beyond basic input/output token pricing — it accounts for:

- **Standard tokens**: prompt (input) and completion (output) tokens at per-model rates
- **Cached tokens**: discounted rate for tokens served from provider-level prompt cache
- **Cache creation tokens**: cost of writing new entries to the provider cache
- **Image/vision tokens**: for multimodal model requests
- **Reasoning tokens**: for models like o1, o3, and o4 that use internal reasoning
- **Per-request fees**: flat fee per API call where applicable

All pricing is configurable per provider/model in `config/pricing.yaml`. Costs are calculated per 1,000 tokens. When providers don't return token counts, tiktoken is used for estimation with cl100k_base encoding as fallback.

**Q: Can I set spending limits?**

A: Yes. Budgets are configurable at two levels:

- **Project-level**: Set a `budget_limit` and `alert_threshold` for each project. When spend reaches the alert threshold, a notification is emitted. When spend exceeds the hard limit, further requests are rejected with HTTP 429 and error code `budget_exceeded`.
- **User-level**: Set individual budget limits and alert thresholds per user, enforced in addition to project limits.

Both budget checks happen before the request reaches any provider, so you never pay for a request that exceeds your budget. Budgets can be viewed and edited through the admin console or the Admin API.

**Q: How does access control work?**

A: AxonLLM enforces model access at two levels, both checked before any provider call:

- **Project model access**: Administrators configure which models each project can use. Requests to unauthorized models return HTTP 403 with code `model_not_allowed`.
- **User model access**: Individual users can have their own allowed model lists. When both project and user lists are set, the effective access is the **intersection** — the user can only access models allowed by both.

For fine-grained authorization, AxonLLM supports Cedar policies evaluated against (principal, action, resource) tuples, with ENFORCE and LOG_ONLY modes. JWT tokens are validated via the AgentCore Identity service.

**Q: Does AxonLLM support streaming?**

A: Yes. When a client sets `stream: true` (via `/api/chat/stream`), AxonLLM returns Server-Sent Events (SSE). For providers that support native streaming (Anthropic, OpenAI), SSE chunks are forwarded from the provider in real time. For providers that don't support streaming natively, AxonLLM simulates it by breaking the complete response into word-level chunks that preserve whitespace faithfully. All streams end with a `[DONE]` marker. Errors during streaming are surfaced as SSE error events.

**Q: Can I cache responses to reduce costs?**

A: Yes, at two levels:

1. **Response caching** (gateway-level): Configurable per project. When enabled, semantically identical requests within the TTL (default 300 seconds) are served from an in-memory cache. Cache keys are SHA-256 hashes of the model, messages, parameters, and project ID, ensuring deterministic matching. Cached responses include zero additional provider cost.

2. **Prompt caching** (provider-level): When enabled per project, system prompts sent to Anthropic and Bedrock providers are annotated with `cache_control: ephemeral` blocks. This leverages the provider's native prompt caching to reduce cost on repeated system prompts.

**Q: What guardrails are available?**

A: Each project can configure guardrail rules that inspect requests, responses, or both. Three rule types are supported:

- **Keyword blocking**: blocks messages containing specific words or phrases (case-insensitive)
- **Regex matching**: blocks messages matching a regular expression pattern
- **Content category filtering**: category-based keyword matching for content classification

Each rule specifies an action: **block** (reject the request or replace the response), **warn** (log but allow), or **redact** (remove matching content). Blocking violations on requests return HTTP 400 with code `guardrail_violation`. Blocking violations on responses replace the content with a policy message and add a warning header.

**Q: How do I monitor the system?**

A: Three layers of monitoring:

1. **Admin console** (`/admin/dashboard`): Real-time dashboards showing total requests, costs, active projects, active users, cache hit rates, per-project budget utilization, and per-provider health status. All CRUD operations are available — create projects, manage budgets, configure model access, define guardrail rules, and create Cedar policies.

2. **Interactive web interfaces**: The Chat page (`/chat`) lets you interact with models directly with provider/user selection. The Playground (`/playground`) shows routing decisions and usage metrics. The Routing Explorer (`/routing`) reveals model and provider selection logic with detailed explanations of why each routing decision was made.

3. **Structured logs**: Every request emits a JSON log with request_id, project_id, user_id, model, provider, latency_ms, status_code, token counts, cost, trace_id, streaming/cached flags, retry count, and fallback providers tried.

**Q: Can I manage everything through the admin console, or do I need to edit config files?**

A: The admin console supports full management of projects, users, budgets, model access, model configurations, guardrail rules, and Cedar policies — all hot-reloaded without restart. Initial provider credentials and model definitions are set via YAML config files or environment variables, but once the system is running, day-to-day operations are fully manageable through the web console and Admin API.

**Q: How do I deploy AxonLLM?**

A: Four deployment options:

- **Amazon Bedrock AgentCore**: `agentcore configure` and `agentcore launch` for serverless, auto-scaling production deployment
- **AWS App Runner**: Docker container deployed via ECR for semi-managed production hosting (via `./deploy.sh`)
- **Docker**: Standard containerized deployment for any environment
- **Local development**: `python serve_dashboard.py` for development with optional demo data

All options use the same configuration (YAML files + environment variables) and expose the same API endpoints.

---

### Internal FAQ

**Q: Why build this instead of using an existing LLM gateway?**

A: Existing solutions (LiteLLM, Portkey, etc.) are standalone services requiring separate infrastructure, monitoring, and scaling management. By building on AgentCore Runtime, AxonLLM gets serverless scaling, managed session persistence, built-in identity/authentication, and Cedar-based authorization out of the box. The tight integration with AgentCore services — Identity for JWT validation, Policy for Cedar evaluation, Memory for session persistence — significantly reduces operational overhead compared to self-hosting a third-party gateway. Additionally, the native Bedrock integration via boto3 (using both invoke_model and converse APIs) provides first-class support for Amazon's own model ecosystem.

**Q: Why OpenAI-compatible API format?**

A: OpenAI's chat completions format has become the de facto industry standard. Most LLM client libraries and frameworks already support it. By adopting this format, we minimize migration effort for existing applications, maximize compatibility with the ecosystem, and allow applications to transparently switch between providers without code changes.

**Q: What is the adapter pattern and why was it chosen?**

A: Each LLM provider has a `ProviderAdapter` class implementing a fixed interface: `translate_request`, `translate_response`, `translate_stream_chunk`, `list_models`, and `health_check`. This isolates all provider-specific logic from the core gateway. Two shared base classes reduce duplication: `OpenAIStyleAdapter` (for OpenAI and Azure OpenAI, which share the same format) and `AnthropicStyleAdapter` (for Anthropic and Bedrock, which share Anthropic's message format). Vertex AI and Cohere each have standalone adapters due to their unique API formats. Adding a new provider means writing one adapter class — routing, cost tracking, caching, guardrails, rate limiting, validation, and the admin dashboard all work automatically.

**Q: How does the dual execution path work for Bedrock vs. HTTP providers?**

A: The `MultiProviderFactory` acts as the top-level dispatch. When the target provider is "bedrock," the request is handled by `bedrock_provider.py` using boto3 — Anthropic models on Bedrock use `invoke_model` while other models (Amazon Nova, DeepSeek) use the `converse` API. Both paths run in a thread pool via `asyncio.to_thread` to avoid blocking the event loop. All non-Bedrock providers (Anthropic direct, OpenAI, Azure, Vertex, Cohere) go through `HttpClient`, an async HTTP client with per-provider session pooling via aiohttp.

**Q: How is the system bootstrapped?**

A: `bootstrap.py` is the centralized wiring hub. `build_gateway_components()` constructs all components in dependency order: pricing config, persistence layer, core components (health tracker, model registry, router, rate limiter, guardrail engine, cache manager), multi-provider factory, request validator, and finally the gateway agent. Demo seed data is loaded before persisted state (DynamoDB), allowing persisted data to override. Two builder functions are available: `build_starlette_app()` for HTTP-enabled deployment and `build_gateway_agent()` for agent-only deployment (AgentCore).

**Q: How is the system tested?**

A: Three testing layers:

1. **Unit tests** (500+): Component-level isolation with mocks. Every module has dedicated test coverage — router retry/fallback logic, cost calculation accuracy, cache key determinism, rate limiter fairness, guardrail evaluation, request validation, adapter translation, config loading, persistence serialization.

2. **Property-based tests** (Hypothesis): Formal correctness properties verified with randomized inputs. Key properties include: retryable errors trigger correct attempt counts with exponential backoff, non-retryable errors skip to fallback immediately, weighted routing distributes proportionally to weights (verified with 5000-request tolerance tests), least-latency selects fastest provider, cost-optimized selects cheapest, cache keys are deterministic, YAML round-trip consistency.

3. **Integration test scripts**: `run_test_traffic.py` exercises 6 end-to-end scenarios against a running instance — normal chat, multi-turn conversation, rate limiting (rapid-fire until 429), budget enforcement, model access control (403 on restricted model), and guardrail violations (400 on blocked content).

**Q: What is the deployment model?**

A: The gateway supports four deployment models:

- **AgentCore Runtime** (production): `agentcore configure --entrypoint agentcore_agent.py` wraps the gateway agent for serverless execution. The `agentcore_agent.py` entrypoint handles three actions: `chat`, `list_models`, and `health`.
- **App Runner** (production alternative): `deploy.sh` builds a Docker image, pushes to ECR, and creates/updates an App Runner service with port 8000 exposed.
- **Docker** (staging/on-premises): Standard `Dockerfile` based on python:3.12-slim with all dependencies installed.
- **Local dev**: `serve_dashboard.py` runs a Uvicorn server with optional demo data seeding via `AXON_LOAD_DEMO_DATA=true`.

All modes share identical configuration (YAML + env vars) and API surface.

**Q: How does the configuration system work?**

A: Configuration flows through four layers:

1. **Environment variables** (highest precedence): `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `AXON_*` settings, `LLM_ROUTER_DYNAMODB_ENABLED`, `AWS_DEFAULT_REGION`.
2. **YAML files**: `models.yaml` (model definitions), `providers.yaml` (provider auth/URLs), `pricing.yaml` (per-token pricing), `demo_seed.yaml` (demo data), `catalog.yaml` (provider model catalog for admin UI).
3. **Dataclass defaults**: `GatewayConfig` aggregates sub-configs for retry (3 retries, 1s base delay), rate limiting (60 RPM user, 600 RPM project), cache (300s TTL), adapter defaults (4096 max tokens), and valid provider names.
4. **Runtime state**: Hot-reloaded via admin console — project budgets, model access lists, guardrail rules, user configs, and model definitions can all change without restart.

Config loading is defensive: missing files return empty defaults, malformed entries are skipped with warnings, and partial loading ensures one bad entry doesn't break the entire config.

**Q: What are the scaling characteristics?**

A: AgentCore Runtime provides serverless scaling — the gateway agent scales automatically based on request volume. In-memory components (rate limiter counters, health tracker state, cache) are per-instance. For multi-instance deployments, DynamoDB persistence stores usage records, project configurations, and user settings for state recovery. The rate limiter and cache are not yet shared across instances — this is a documented limitation targeted for Phase 2. The async architecture (Starlette + aiohttp + asyncio) supports thousands of concurrent connections per instance with non-blocking I/O.

**Q: How does DynamoDB persistence work?**

A: The `DynamoPersistence` layer uses a single-table design with composite keys (PK/SK pattern). Three entity types are stored: usage records (`USAGE#{request_id}` / `USAGE#{timestamp}`), projects (`PROJECT#{project_id}` / `PROJECT`), and user configs (`USER#{user_id}` / `CONFIG`). The table uses PAY_PER_REQUEST billing to match the serverless model. Persistence is optional — controlled by `LLM_ROUTER_DYNAMODB_ENABLED=true`. The table is auto-created on first startup. All DynamoDB writes are fire-and-forget with warning logs on failure, ensuring persistence outages never block request processing.

**Q: What is the cost calculation formula?**

A: For each request, cost is calculated as:

```
((prompt_tokens - cached_tokens - cache_creation_tokens) / 1000 * prompt_rate)
+ (completion_tokens / 1000 * completion_rate)
+ (cached_tokens / 1000 * cached_rate)
+ (cache_creation_tokens / 1000 * creation_rate)
+ (image_tokens / 1000 * image_rate)
+ (reasoning_tokens / 1000 * reasoning_rate)
+ per_request_flat_fee
```

Cached and creation tokens are subtracted from prompt tokens to avoid double-billing. If a specific token type's rate is not configured, it falls back to the prompt token rate. Returns 0.0 if no pricing is configured for the provider/model combination.

**Q: What is the request pipeline in detail?**

A: The GatewayAgent orchestrates a 14-step pipeline for every request:

1. **Parse request** — Convert raw dict to typed `ChatCompletionRequest`
2. **Extract context** — Extract user_id, project_id, roles, scopes from JWT claims
3. **Request validation** — Structural (message format), semantic (role values), model existence, token limits
4. **Rate limit check** — Sliding window check for both user and project
5. **Project model access check** — Is this model in the project's allowed list?
6. **User model access check** — Is this model in the user's allowed list?
7. **Project budget check** — Has the project exceeded its budget limit?
8. **User budget check** — Has the user exceeded their individual limit?
9. **Request guardrails** — Evaluate request content against project guardrail rules
10. **Cache check** — Serve from cache if enabled and key matches within TTL
11. **Route and execute** — Strategy-based provider selection with retry and fallback
12. **Response guardrails** — Evaluate response content against project guardrail rules
13. **Cost tracking** — Calculate cost and record usage (in-memory + optional DynamoDB)
14. **Return response** — Non-streaming dict or SSE async generator

Every step that rejects a request returns immediately with the appropriate HTTP status code and error type, avoiding unnecessary work downstream.
