# Changelog

All notable changes to AxonLLM will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Tool calling (function calling) pass-through across every provider.**
  `ChatCompletionRequest` now carries `tools` and `tool_choice`, and each adapter
  translates them into its provider's own dialect in both directions:
  - **OpenAI / Azure** — native shape, passed through.
  - **Anthropic** — `input_schema`, `tool_use` / `tool_result` content blocks,
    `stop_reason: "tool_use"` → `finish_reason: "tool_calls"`.
  - **Bedrock Converse** — `toolConfig..toolSpec`, `toolUse` / `toolResult` blocks,
    `stopReason` mapping.
  - **Google AI / Vertex** — `functionDeclarations`, `functionCall` /
    `functionResponse` parts, via the shared `adapters/gemini_tools.py`.
  - **Cohere** — `parameter_definitions` (JSON Schema unrolled into per-parameter
    type/description/required triples) and top-level `tool_results`.
- 60 tests in `tests/unit/adapters/test_tool_translation.py` covering each dialect,
  the tool-loop round trip, the cache key, and request validation.
- Documentation: tool-calling walkthrough in `README.md`, the per-dialect
  translation table and cross-cutting gotchas in
  `docs/internal/axonllm-architecture-and-features.md` §4, and PRD §6.10 (FR-T1…T7)
  with the tool-call request/response shapes in §9.1. PRD OQ7 ("should we support
  tool use?") is resolved — implemented as per-provider translation rather than
  pass-through, since only OpenAI-style providers accept OpenAI's shape.

- **OpenAI Responses API support, for models that reject Chat Completions.**
  The `-pro` tier (`gpt-5.5-pro`, `gpt-5-pro`) is served only by `/v1/responses`;
  on `/v1/chat/completions` OpenAI answers 400 `This is not a chat model and thus
  not supported in the v1/chat/completions endpoint`. The openai adapter now
  detects the tier and switches both the URL and the payload shape: `input` items
  instead of `messages`, `max_output_tokens` instead of `max_tokens`, flat tool
  specs, and tool traffic as top-level `function_call` / `function_call_output`
  items. Sampling parameters are dropped rather than forwarded — these models
  reject `temperature`/`top_p` with a 400 instead of ignoring them, so passing a
  caller's default through would fail every request and trip the circuit breaker.
  Streaming works too: `response.output_item.done` carries the finished
  `function_call` whole, so unlike the hand-built translators this needs no
  cross-chunk accumulation. Restricted to genuine OpenAI — the OpenAI-*compatible*
  providers sharing the same adapter base and URL builder (Azure, xAI, Groq,
  Together, Fireworks, AI21) expose Chat Completions only, and routing a
  `-pro`-looking id there would 404 a request that otherwise worked.

- **Provider keys can come from a `.env` file in demo mode.** `provider_loader`
  reads API keys from `os.environ` and nothing populated it from a file, so a
  `.env` in the project root was never consulted and every direct-API provider
  was silently skipped — while the gateway still answered through Bedrock, which
  authenticates via AWS credentials on a separate path. The failure therefore
  looked like "OpenAI is broken" rather than "no keys were loaded". Reading the
  file is gated on an explicit `AXON_LOAD_DEMO_DATA=true` (the container `CMD`
  does not set it), and an existing environment variable always wins, so
  platform-injected secrets are never shadowed. Startup logs the variable names
  loaded, never their values.

### Fixed
- **Smart routing ignored cost entirely — `cost_quality_tradeoff` was inert.**
  `_get_model_cost` read pricing off the model registry, which is populated only
  from an inline `pricing:` block in `models.yaml` — and there are none (0 of 48
  provider entries). `config/pricing.yaml` was loaded, but only into
  `CostTracker`, never into routing. So every one of the 45 models costed 0.0,
  the cost term collapsed to `tradeoff × (1 − 0)` — a constant added to every
  candidate — and ranking became pure benchmark order. The documented 70/30
  quality-vs-cost blend silently always picked the highest-benchmark model,
  which is generally the most expensive one: the opposite of the configured
  intent, with no error to notice it by. Routing now resolves pricing from the
  same provider/model_id table `CostTracker` bills from, so the cost used to
  choose a model and the cost actually charged cannot disagree.
- A missing price is now treated as **unknown rather than free**. With 33 of 48
  provider entries unpriced, scoring absent pricing as 0.0 would make an
  unpriced model the cheapest possible candidate — it would win for being
  *unmeasured*, not for being cheap. On the `general` panel that hands selection
  to `claude-haiku` (benchmark 78) over `claude-sonnet` (90). Unpriced
  candidates are scored at the mean of the known costs, excluded from the
  normalization maximum, and flagged `cost_estimated` in the decision trace.
- **`gpt-5.5-pro` was configured to route over Chat Completions**, where it can
  only ever 400 — the model was unusable from the day it was added. Fixed by
  implementing the Responses API path above rather than deleting the config entry,
  since `-pro` is a class of models and the next one added would fail the same way.
- **Tool specs were silently dropped.** `ChatCompletionRequest` had no `tools`
  field, so `_parse_request` never read one: the request succeeded, the model was
  simply never told any tools existed, and it answered confidently that it had no
  such capability. A fluent HTTP 200 with the entire tool-use loop missing, and no
  error anywhere to notice it by.
- The PII path rebuilt the request field-by-field, so any field added later was
  dropped — which is exactly how `tools` went missing. It now uses
  `dataclasses.replace`, so a caller's tools survive a request that happens to
  contain PII (an intermittent failure far harder to find than a total one).
- Smart routing read the *last* message as "the prompt", so mid-tool-loop rounds
  classified a tool result and could be sent to a different model than the round
  that chose the tool. It now walks back to the last real user text.
- The semantic cache key omitted `tools`/`tool_choice`: the same prompt sent with
  a tool list can return a tool call and sent without one returns prose, so a
  cached tool-free reply could be served to a request that needed a tool call.
- Request validation rejected an assistant turn with `tool_calls` and no
  `content`, which is the normal shape of a tool call — it broke every tool loop
  at round two.
- Gemini rejects unknown JSON Schema keys (`additionalProperties`, `$schema`,
  `title`, `default`) rather than ignoring them, so a schema written for OpenAI
  400'd the whole request. Schemas are now filtered recursively.
- **Bedrock Mantle dropped tools on all three of its routes.** It is the one
  provider that bypasses the adapter layer — it hand-builds a payload per Mantle
  API — so the translation above never reached it, and 11 models advertised
  `function_calling` while their tool specs went nowhere. Each route now
  translates in both directions, which is three dialects rather than one:
  `/anthropic/v1/messages` takes `input_schema` and content blocks,
  `/v1/chat/completions` takes the gateway's own OpenAI shape unchanged, and
  `/openai/v1/responses` takes a *flat* tool spec (`name`/`parameters` beside
  `type`, no `function` wrapper) with tool traffic as top-level `function_call` /
  `function_call_output` items rather than as messages. The two non-passthrough
  routes reject OpenAI's shape outright (400 `Unexpected role "tool"` and 400
  `Invalid 'input'`), so the tool loop failed rather than degrading — except on
  Chat Completions, which answered fluently with the tools missing.
- Mantle's Responses route reported `finish_reason: "completed"` even when the
  model called a tool, because that field carries lifecycle status rather than a
  stop reason. A caller driving a tool loop reads that as "nothing left to do",
  so it returned the tool call to the client and never ran it.
- **`POST /v1/chat/completions` dropped tools entirely.** The pipeline had
  translated them per-provider since the work above, but the OpenAI-compatible
  route never read `tools`/`tool_choice` off the request body and `ClientAgent`
  flattened the response down to `content` — which a tool call has none of. So
  tool calling worked for in-process callers and not for the HTTP surface the
  README points OpenAI SDK clients at: another fluent 200 asserting no such tool
  existed. Both directions now carry tools, and `finish_reason` is no longer
  hardcoded to `"stop"`.
- **`finish_reason` leaked raw provider stop reasons.** With the hardcoded
  `"stop"` removed, values like Anthropic's `end_turn`, Gemini's `MAX_TOKENS` and
  Mantle's `completed` reached the client — and typed OpenAI SDK clients
  deserialize that field into an enum, so an unrecognized string is a client-side
  validation error rather than a curiosity. The OpenAI-compatible surface now
  maps every known provider reason onto the four legal values, defaulting to
  `"stop"`, while the internal API keeps reporting what the provider actually
  said.
- **Simulated streaming discarded tool calls.** `simulate_streaming` extracted
  `message.content` and nothing else, so a streamed tool call — which carries no
  text — produced a single empty chunk and vanished. This is the path every
  provider without true SSE takes (boto3 Bedrock, google_ai, and any provider
  whose stream fails to open), so `stream=True` plus `tools` returned an empty
  stream rather than an error. Tool calls now ride the final chunk, whole: their
  `arguments` are a JSON string, and word-splitting them would emit fragments no
  client can parse until reassembled.

## [0.1.0] - 2026-06-22

### Added
- Multi-provider LLM gateway with unified API (Bedrock, Anthropic, OpenAI, Azure, Vertex AI, Cohere)
- 5 routing strategies: round-robin, weighted, least-latency, cost-optimized, smart (intent-aware)
- Ensemble routing: scatter-gather-synthesize across model panels with configurable quorum
- Multi-region hub-and-spoke topology with automatic failover and data residency controls
- Policy hierarchy (org > BU > project > env) with budget, rate limit, and model restrictions
- Quota enforcement with per-user and per-project budget tracking and threshold alerts
- PII redaction with streaming token buffering for split-token detection
- Prompt injection detection with Unicode normalization (homoglyph-resistant)
- Immutable audit trail with SHA-256 hash chain
- Multi-strategy authentication: ALB OIDC JWT, Bearer token, API key
- API key management: issue, rotate, revoke, scope-restricted
- Admin RBAC with scoped access control
- Admin dashboard (9 pages) with token efficiency analytics
- Event dispatcher: webhook, SNS, CloudWatch Logs
- Semantic cache with LRU eviction
- Sliding window rate limiter (per-user and per-project)
- SSE streaming for all providers
- Docker and App Runner deployment support
- 980 tests (unit, integration, e2e, property-based)

### Security
- Race condition fixes in rate limiter, quota enforcer, and audit trail (asyncio.Lock)
- JWT signature verification enforced (removed insecure fallback)
- Admin RBAC resource-scoped authorization
- Bounded caches and buffers to prevent memory exhaustion
- Unicode NFKD normalization in injection detector
- Streaming PII buffer for cross-chunk token detection
- Right-to-left regex replacement to prevent token-on-token false matches
- Cache TTL enforcement in API key service
- Double-check locking for lazy DynamoDB table initialization
- Cryptographic randomness for routing selection (secrets module)
