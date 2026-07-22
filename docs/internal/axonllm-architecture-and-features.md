# AxonLLM — Architecture & Feature Reference

**Audience:** internal (engineering, product, exec).
**Purpose:** one place that explains what each AxonLLM feature is, why it exists,
and the request/data flow through the system. Reflects `src/gateway/` as of
2026-07-22.

> **Note:** unlike Ostiari's `docs/internal/` (git-ignored), this repo's
> `docs/internal/` is **tracked** — keep anything you don't want committed out of
> it, or gitignore the folder first.

---

## 1. The big picture

AxonLLM is an **LLM gateway / routing engine**: one OpenAI-compatible API in
front of many providers, with smart (task-aware) routing, ensemble, health-aware
fallback, cost tracking, guardrails, quotas, caching, and multi-region. It runs
either as a standalone service (Starlette app / Bedrock AgentCore agent) **or
embedded in-process** — which is how Ostiari uses it (imported as `src.gateway`,
routing decisions made without a network hop).

### Component graph

```
   OpenAI / Anthropic SDK clients, Codex, Ostiari (embeds this in-process)
        │  POST /v1/chat/completions  (also /v1/messages, /v1/responses)
        ▼
   ┌───────────────────────────  GatewayAgent  ──────────────────────────────┐
   │  16-step request pipeline (§2): validate → quota → injection/PII →       │
   │  rate limit → access/budget → guardrails → cache → region → ROUTE →      │
   │  response guardrails → PII reinject → audit → cost → session → stream    │
   │                                                                          │
   │  Router (§3): smart_route · ensemble_route · execute_with_fallback       │
   │     ├─ SmartRoutingStrategy  (TaskClassifier + ModelLeaderboard)         │
   │     ├─ EnsembleStrategy      (scatter-gather-synthesize)                 │
   │     └─ HealthTracker + CostTracker                                       │
   │                                                                          │
   │  ProviderFnFactory → AdapterRegistry → 13 provider adapters              │
   └───────────────┬──────────────────────────────────────────┬─────────────┘
                   │ provider call                             │ usage → forward
                   ▼                                           ▼
   Bedrock / OpenAI / Anthropic / Azure / Vertex /       TraceForwarder →
   Gemini / Cohere / Groq / Fireworks / Together /       Ostiari / OTLP sinks
   AI21 / xAI / Mantle
```

### Wiring
`bootstrap.build_gateway_components()` / `build_gateway_agent()` assembles the
whole graph — registry, router, strategies, adapters, security, quota, region,
observability — from `config/*.yaml`. Optional pieces (auth, quota, region, PII,
injection, audit) are `None`-able, so a routing-only embed (Ostiari) skips them.

---

## 2. The request pipeline (`GatewayAgent.handle_chat_completion`)

Every request runs an ordered 16-step pipeline. Each step can short-circuit with
a typed error; most are optional (enabled by the components wired in).

```
 1. Parse request (ChatCompletionRequest)
 2. Extract context (project_id, user_id, scopes, metadata)
 2.5 Request validation (model exists, shape) — skipped for smart/ensemble
 2.7 Policy-hierarchy quota enforcement (rate + budget + token caps)
 2.8 Prompt-injection detection            → block
 2.9 PII redaction (reversible mapping kept for re-injection)
 3.  Rate limit (sliding window)           → 429
 4/5 Project + user model-access checks    → 403 (skipped for smart routing)
 6/7 Project + user budget checks          → 402
 8.  Request guardrails (input)            → block/transform
 9.  Cache check (exact + semantic)        → cache hit short-circuits
 9.5 Region routing (spoke availability + data residency)
 10. ROUTE + EXECUTE  (smart | ensemble | fallback — §3)
 11. Response guardrails (output)
 11.5 PII re-injection into the response
 11.6 Audit trail — record the LLM request (tamper-evident chain)
 12. Cost tracking (UsageRecord) → TraceForwarder (Ostiari / OTLP)
 13. Budget status (streaming)
 14. Session storage
 15/16. Streaming (SSE) or non-streaming return
```

---

## 3. Routing engine (`router.py`)

Three entry methods; the pipeline picks one at step 10.

### 3a. `execute_with_fallback` (default)
For a concrete requested model. Builds a **fallback chain** (`get_fallback_chain`)
— the primary provider-model plus priority-ordered alternates — and tries each in
order, skipping providers the **HealthTracker** has marked unhealthy (circuit
breaking / cooldown). Records health + cost per attempt.

### 3b. `smart_route` (auto model selection)
Triggered by an empty model or a smart-routing flag. Flow:
```
prompt → TaskClassifier → task_type (coding / creative / summarization / …)
       → ModelLeaderboard (per-task quality ranking)
       → SmartRoutingStrategy picks a model by quality vs cost
         (cost_quality_tradeoff, confidence_threshold), scoped to allowed_models
       → execute (with fallback + health)
returns (response, SmartRoutingDecision)   ← decision is observable
```
The classifier is keyword/heuristic (fast, free, approximate).

### 3c. `ensemble_route` (scatter-gather-synthesize)
Triggered by `model == "ensemble"` / `"ensemble:<preset>"` / an ensemble flag.
Flow per `config/ensemble.yaml` preset:
```
scatter  → call every panel model in parallel
gather   → collect PanelMemberResults (drop failures)
synthesize → a judge model merges survivors into one answer via a
             synthesis prompt (consensus / contradiction / ranking criteria)
returns (response, EnsembleDecision)
```
Presets (e.g. `budget` panel + `claude-sonnet` judge; `quality` panel +
`claude-opus` judge); `default_preset` chosen when unnamed.

### Two routing layers (don't conflate)
- **Model selection** (which LLM): smart / ensemble / requested.
- **Provider/backend fallback** (which deployment of the chosen model):
  `execute_with_fallback` + HealthTracker.

---

## 4. Providers & adapters

`AdapterRegistry` + `ProviderFnFactory` + `MultiProviderFactory` normalize 13
providers behind one interface: **bedrock, openai, anthropic, azure, vertex,
google_ai, cohere, groq, fireworks, together, ai21, xai, mantle**. Each adapter
translates the unified `ChatCompletionRequest` to the provider's API and back.
`HttpClient` handles transport (incl. streaming).

### Streaming (true end-to-end)

For a streaming **direct** or **smart-routed** request the gateway opens the
provider SSE stream directly and relays chunks as they arrive — the first token
reaches the client without waiting for the full completion. There is **one**
provider call: the response is not fetched blocking-then-chunked.

- **Model selection without execution:** smart routing resolves the model via
  `select_model()` (no blocking call), then the stream opens on the chosen model.
- **Fallback is pre-first-byte only:** the provider chain is tried while opening
  the stream; once the first byte is sent, a mid-stream failure surfaces as a
  stream error (a provider switch is impossible after the client has bytes). The
  non-streaming path keeps full fallback.
- **End-of-stream accounting:** text and token usage are accumulated as chunks
  flow; cost / audit / trace / OTLP / quota all run once the stream completes —
  identical bookkeeping to the non-streaming path.
- **Usage:** providers are asked to report usage in-stream (OpenAI
  `stream_options.include_usage`; Anthropic `message_start`/`message_delta`
  already carry it). The OpenAI usage chunk arrives *after* the finish_reason
  chunk, so the stream is drained fully rather than stopping on `is_final`. When
  a provider reports nothing, tokens are estimated from the accumulated text via
  tiktoken (flagged approximate).
- **PII re-injection** works across real chunk boundaries via the same buffering
  used elsewhere (a `[EMAIL_1]` token split across two chunks is reassembled).
- **Ensemble** is unchanged: it withholds output until the judge/best-single
  result is ready (it inherently cannot stream mid-panel), then streams the final
  result. `google_ai` uses a distinct SSE shape and falls back to the buffered
  path. Without a `ProviderFnFactory` the gateway keeps the legacy
  select-then-simulate behavior.
- **Embedded in Ostiari:** the embed calls AxonLLM with `stream=False` (Ostiari
  terminates/governs/re-originates streaming at its own layer), so this path is
  standalone-only — the embed is unaffected.

---

## 5. Cost tracking & efficiency

- **CostTracker** — per-request `UsageRecord` (real input/output tokens, model,
  provider, cost from the pricing table); the spine of budgets + analytics.
  Budget checks read **running per-project/per-user spend counters** (O(1)), not
  a sum over the record list — and the counters are authoritative, so trimming
  the in-memory record cap no longer under-counts spend. Rehydrate from
  persisted history via `load_records()`.
- **EfficiencyAnalyzer** (Level 1) — ratio heuristics over UsageRecords (e.g.
  expensive-model-on-cheap-task, output/input ratios) to flag waste.
- **SemanticEfficiencyEngine** — deeper semantic efficiency signals.

---

## 6. Quotas, budgets & rate limiting

- **QuotaEnforcer** — policy-hierarchy enforcement (project → user), combining
  rate limits, USD budgets, and per-request token caps (`enforce_all`,
  `cap_max_tokens`). Step 2.7.
- **SlidingWindowRateLimiter** — per-key RPM window (step 3), local or Redis-backed.
- Project/user **access + budget** checks (steps 4–7) gate model use and spend.
- **Concurrency:** both the rate limiter and quota enforcer shard their state
  behind **per-key locks** (`StripedLock`), so requests for different
  projects/users don't serialize on one global lock. The rate limiter locks both
  the user and project key (in canonical order, deadlock-free) since a check
  mutates both buckets.
- **Retries** use exponential backoff **with jitter** (`RetryConfig.jitter`,
  default 0.5 → delay drawn from `[(1-j), 1]·base·2^attempt`) to avoid
  synchronized retry storms against a failing provider.

---

## 7. Smart-routing intelligence

- **TaskClassifier** — classifies the prompt into a task type.
- **ModelLeaderboard** (`config/leaderboard.yaml`) — per-task quality ranks.
- **FeedbackTracker** — records outcome feedback (`POST /v1/feedback`, thumbs /
  score) to inform routing over time.

---

## 8. Guardrails & security

- **GuardrailEngine** — input (step 8) and output (step 11) guardrails: regex,
  content checks, transforms, block verdicts.
- **PromptInjectionDetector** (`security/injection_detector.py`) —
  `analyze_messages() → DetectionResult(score, should_block, detected_patterns)`.
  Step 2.8. (This is the engine Ostiari's security layer calls.)
- **PIIRedactor** (`security/pii_redactor.py`) — `redact_messages(messages, policy)`
  before the call (step 2.9), reversible mapping re-injected into the response
  (step 11.5). PII types: email, ssn, credit_card, phone, ip_address,
  aws_account_id, medical_record, iban, passport, ipv6. Redacts text inside
  **multimodal/list content** (OpenAI `{"type":"text",...}` and Bedrock
  `{"text":...}` parts), not just plain-string content.
  - **Safe-by-default (env-gated):** `AXON_PII_REDACTION_DEFAULT=on` turns
    redaction on for any request whose resolved policy doesn't explicitly
    configure it (type set via `AXON_PII_REDACT_TYPES`, default all). Unset →
    redaction stays opt-in per policy (unchanged behavior). **The Ostiari embed
    leaves this unset**: Ostiari's `SecurityLayer` redacts at its own layer
    before forwarding, so the embedded agent never re-redacts already-tokenized
    text (a `[EMAIL_1]` token matches no pattern → no-op second pass).
  - **Permanent redaction (`pii_reinject=False`):** strict-regime mode — no
    reversible mapping is retained and originals are NOT re-injected into the
    response (no PII plaintext held in memory). The hierarchy resolver treats
    "reinject off" as the more-private setting: once a parent disables it, a
    child cannot re-enable.
- **AuditTrail** (`security/audit_trail.py`) — records each request (step 11.6).
- **EventDispatcher** — security/webhook event fan-out.

---

## 9. AuthN / AuthZ

- **APIKeyService** — API-key issuance/validation (virtual keys).
- **OIDCService** — OIDC login (OAuth2 Auth Code + PKCE), JWT validation.
- **CedarPolicyService** — Cedar-based authorization policies.
- **PolicyHierarchyResolver** — resolves effective policy (org → project → user),
  feeding the quota enforcer.
- **Middleware** — `auth` (JWT/key), `admin_rbac` (admin surface roles),
  `security` (headers/hardening).

---

## 10. Caching

**CacheManager** — exact-match **and** semantic cache (cosine similarity, TTL).
Step 9; a hit short-circuits the provider call (cost $0). Off unless configured.

---

## 11. Multi-region (hub-and-spoke)

**RegionRouter** (step 9.5) selects the target **spoke** by: (1) **data residency**
(strict mode + user residency constraint), (2) **health** (drop unhealthy/draining
spokes via **SpokeHealthMonitor**), (3) **model availability** on the spoke, then
weighted/failover selection.

The selection is **wired end-to-end** (not just response metadata): the chosen
spoke's `endpoint` overrides the provider `base_url` for that request, and its
`region` rewrites the AWS SigV4 credential region — so Bedrock/Mantle calls use a
boto3 client bound to the spoke's region and HTTP providers hit the spoke's
endpoint. Threaded through the direct, smart-routing, and streaming paths (the
`ProviderFnFactory`/`MultiProviderFactory` `config_for()` + region-bound
provider fns). Ensemble fan-out stays on the default region.

**SpokeHealthMonitor** runs as a background task (started by the app lifespan
when >1 spoke is configured; single-region skips it), checking each spoke's
`health_check_url` and flipping status after N consecutive failures.

**Config:** `config/spokes.yaml` (`AXON_SPOKES_CONFIG`) → `HubConfig` via
`spoke_loader.load_hub_config`; absent/empty/malformed falls back to
`default_single_region`, so single-region deploys need no config and multi-region
is purely additive.

---

## 12. Observability & the Ostiari tie-in

Two complementary paths, chosen automatically by whether AxonLLM is embedded:

- **TraceForwarder** (`observability/trace_forwarder.py`) — maps each `UsageRecord`
  to Ostiari's trace-event shape and forwards it to an **embedding Ostiari** (via
  an in-process sink registered with `register_sink()`, or HTTP to
  `OSTIARI_TRACES_URL`). Ostiari then emits the OTEL span with its governance
  signal (risk tier, decision, session parent grouping). No-op standalone.
- **OTLPSpanExporter** (`observability/otlp_exporter.py`) — the **standalone**
  OTEL path. Opt-in via `OTEL_EXPORTER_OTLP_ENDPOINT`; maps each `UsageRecord` to
  one OTEL span (`gen_ai.request.model`, `gen_ai.usage.*`, plus `axon.*` for
  provider/cost/routing) and ships it over OTLP. Spans go through a
  `BatchSpanProcessor` (background thread) so a down collector never blocks the
  request. Degrades to a no-op if the OTEL SDK isn't installed (`pip install
  'axonllm[otel]'`).
- **No double-export:** the agent calls the native exporter only when the
  TraceForwarder is *not* "Ostiari-detected". Standalone → native span; embedded
  → Ostiari owns the span. Exactly one span per request in either mode. Both use
  the same deterministic id scheme, so a request that traverses both layers
  correlates to one trace.

---

## 13. Admin & serving surfaces

- **Chat routes** (`chat/`) — `/v1/chat/completions`, `/v1/messages`,
  `/v1/responses` (OpenAI Responses API, incl. a cross-provider adapter).
- **Admin routes** (`admin/`) — keys, quotas, policy hierarchy, regions,
  webhooks, audit; behind `admin_rbac`.
- **Entrypoints** — `serve_dashboard.py` (Starlette app), `agentcore_agent.py`
  (Bedrock AgentCore), `cli.py`. All delegate to `bootstrap`.

---

## 14. Config files (`config/`)

| File | Drives |
|---|---|
| `models.yaml` | model registry (names → provider mappings) |
| `providers.yaml` | provider credentials/config |
| `pricing.yaml` | per-model input/output pricing (cost tracking + budgets) |
| `leaderboard.yaml` | per-task quality ranks (smart routing) |
| `ensemble.yaml` | ensemble panels + judge presets |
| `catalog.yaml`, `demo_seed.yaml` | catalog + demo data |

Env-overridable via `AXON_*_CONFIG`; the CLI chdirs to the repo root so relative
`config/` paths resolve (this is why Ostiari's embed transiently chdirs).

---

## 15. How Ostiari embeds AxonLLM (cross-repo)

Ostiari installs AxonLLM (editable, `src.gateway`) and calls
`build_gateway_agent().handle_chat_completion(request_data, ctx)` **in-process** —
no network hop. AxonLLM is the routing authority (smart/ensemble/fallback,
multi-provider); Ostiari layers its behavioral governance (risk scoring, HITL,
per-agent economics, delegation trust) around it and forwards traces back. See
Ostiari's `docs/internal/ostiari-architecture-and-features.md` §6.

---

## 16. Where each feature lives (index)

| Feature | Key files |
|---|---|
| Request pipeline | `agent.py` |
| Router (smart/ensemble/fallback) | `router.py`, `routing.py` |
| Smart routing | `smart_routing.py`, `task_classifier.py`, `model_leaderboard.py`, `feedback_tracker.py` |
| Ensemble | `ensemble.py`, `ensemble_config.py`, `config/ensemble.yaml` |
| Providers | `adapters/`, `provider_fn_factory.py`, `multi_provider_factory.py`, `http_client.py` |
| Cost / efficiency | `cost_tracker.py`, `efficiency_analyzer.py`, `semantic_efficiency.py` |
| Quota / rate | `quota_enforcer.py`, `rate_limiter.py`, `request_validator.py` |
| Guardrails / security | `guardrail_engine.py`, `security/{injection_detector,pii_redactor,audit_trail,event_dispatcher}.py` |
| Auth | `auth/{api_key_service,oidc_service,cedar_policy,policy_hierarchy}.py`, `middleware/` |
| Cache | `cache_manager.py` |
| Multi-region | `multi_region/{region_router,region_config,health_monitor}.py` |
| Observability | `observability/trace_forwarder.py` |
| Registry / models | `model_registry.py`, `models.py`, `provider_config.py` |
| Wiring / serving | `bootstrap.py`, `serve_dashboard.py`, `agentcore_agent.py`, `cli.py`, `config_loader.py` |

---

## 17. One-liner

**AxonLLM** = an embeddable, OpenAI-compatible **LLM routing engine** — smart +
ensemble + fallback across 13 providers, with cost/efficiency, guardrails,
quotas, caching, and multi-region — that runs standalone or in-process as the
routing brain beneath a governance layer (Ostiari).
