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

---

## 5. Cost tracking & efficiency

- **CostTracker** — per-request `UsageRecord` (real input/output tokens, model,
  provider, cost from the pricing table); the spine of budgets + analytics.
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
spokes via **SpokeHealthMonitor**), (3) **model availability** on the spoke.
Config in `multi_region/region_config.py` (hub + spokes). `default_single_region`
for the common one-region case.

---

## 12. Observability & the Ostiari tie-in

- **TraceForwarder** (`observability/trace_forwarder.py`) — maps each `UsageRecord`
  to a trace event and forwards it. Notably it can forward to **Ostiari**
  (`_ostiari_url`) — so AxonLLM usage shows up in Ostiari's trace pipeline — and
  to registered **Sinks** (OTLP / Langfuse / LangSmith / Arize style).
- Standard OTEL spans + Prometheus metrics on the standalone server.

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
