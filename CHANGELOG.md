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

- **Pricing coverage report, at startup and on `/admin/pricing-drift`.**
  `config/models.yaml` and `config/pricing.yaml` are edited independently and
  nothing checked one against the other, so a model added without a price failed
  twice in silence: `CostTracker.calculate_cost` returns 0.0 for an unknown
  provider/model pair — so the usage record carries no cost, project spend does
  not move, and budget blocks and quota alerts under-count rather than erring
  safe — while smart routing, having no rate to read, scores the model on the
  average of the known prices. Neither path raises, which is why this is a page
  rather than a log line: when the page was written **33 of 48 provider mappings
  had no price at all**, across seven providers with no section in the file (see
  the entry below for what filling those in found). The page
  lists every unpriced mapping, every pricing entry no model reads (usually the
  other half of a renamed model id), and a paste-ready YAML fragment for the
  gaps — with rates left at `0.0`, since a guessed price bills silently where a
  missing one is visible. Rename hints match on the model *family* rather than
  string similarity: `mistral-large-2402` → `-2407` is a version bump worth
  reusing a rate for, but `claude-haiku-4-5` → `claude-opus-4` is a different
  tier at roughly 15× the price, and suggesting it would overcharge and look
  deliberate. The audit performs the same provider + provider-side-`model_id`
  lookup the biller does, so a mapping it calls priced is exactly one
  `CostTracker` can find. Gated on unpriced mappings only, so the banner clears
  once every model has a rate. The dev server also opens the page when it finds a
  gap, but only on an interactive terminal — `serve_dashboard.py` is the
  container `CMD`, so a piped or containerized run prints the banner and nothing
  else; `AXON_NO_BROWSER=true` suppresses it locally too.

- **Real published rates for 20 of the 33 unpriced mappings** — coverage 31% →
  73%, and five providers that had no section in `config/pricing.yaml` at all
  (`ai21`, `bedrock-mantle`, `google_ai`, `groq`, plus new entries under
  `openai`, `anthropic` and `bedrock`) now bill at a real rate instead of $0.00.
  Every figure is from the provider's own price list, cited by URL and fetch date
  in the file header: OpenAI, Anthropic and Google AI pricing pages, `groq.com`,
  `ai21.com`, and the AWS Price List API for Bedrock and Bedrock-Mantle.
  Three judgement calls are documented inline rather than buried:
  - **Regional vs global Bedrock endpoints.** Claude 4.5 and later carry a 10%
    premium on regional endpoints, and the `us.` prefix in `models.yaml` *is* a
    regional inference profile — so those entries take the regional rate
    ($1.10/$5.50 per MTok for Haiku 4.5, not the $1.00/$5.00 global figure).
  - **Mantle SKUs are priced from the Bedrock rate.** Of the 66 model/direction
    pairs where us-east-1 lists both a `…-mantle-…-standard` SKU and a plain
    on-demand one, all 66 agree to the cent — checked rather than assumed, since
    that equivalence is what licenses six of the new entries.
  - **Sub-threshold context tiers.** Where a provider charges more above a
    context length (`gemini-3.1-pro-preview` doubles above 200k, `gpt-5.5-pro`
    above 272k), the lower rate is used and the premium noted. `CostTracker`
    prices a request from token counts alone and has no context tier to switch
    on, so encoding the higher rate would overcharge every ordinary request.

  **13 mappings are left deliberately unpriced**, because the provider publishes
  no rate for the id being sent — these are stale *model ids* rather than missing
  prices, and the fix belongs in `models.yaml`:
  `together` (`deepseek-ai/DeepSeek-R1`, and the `Llama-3.3-70B-Instruct-Turbo`
  and `Qwen2.5-72B-Instruct-Turbo` ids — the page now lists DeepSeek V4 Pro,
  a base "Llama 3.3 70B" and Qwen3.x, with no Turbo tier priced separately),
  `xai` (`grok-3`, `grok-3-mini`; the list covers only grok-4.x), `fireworks`
  (no per-model serverless table at all), `google_ai`
  (`gemini-3-pro-preview`, replaced by 3.1), and the five `bedrock-mantle`
  `openai.gpt-5.x` ids (three appear only in GovCloud at premium rates, two in no
  region). Filling these with a near-miss id's rate would bill confidently wrong
  for a model that may not even resolve, and would clear the drift page's warning
  while making the underlying problem invisible. Leaving them listed keeps both
  visible.

### Fixed
- **Claude Opus 4.8 was billed at 3× the published rate.** The `anthropic`
  entry read `0.015/0.075` per 1K tokens — the retired Opus 4.1 rate — while
  Anthropic prices Opus 4.5 and later at $5/$25 per MTok, a third of the earlier
  generation. So every Opus 4.8 request overcharged: a 1M-in/100K-out call
  recorded $22.50 against a real cost of $7.50. This is the *opposite* failure to
  an unpriced model and invisible for the same reason — nothing cross-checks a
  rate that parses, and the drift page can only report a price that is missing,
  not one that is wrong. Budget blocks and quota alerts fired early against
  inflated spend, which reads as a working system rather than a broken one.
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
- A missing price is now treated as **unknown rather than free**. With most
  provider entries unpriced at the time, scoring absent pricing as 0.0 would make an
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
