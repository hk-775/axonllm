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

### Fixed
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
