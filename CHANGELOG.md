# Changelog

All notable changes to AxonLLM will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **A startup guide that answers "what do I do after it's running?"** The four
  install paths were already documented, but three of them ended at a gateway
  with no provider keys, no projects and no way in — and the README's only
  configuration advice was a `.env` file that is silently ignored outside demo
  mode. `README.md` now leads with a decision tree and a path table (auth mode
  and time-to-first-call per path), and each path is numbered steps ending in a
  link to the new **Configuring a clean install** section: where provider keys go
  in precedence order (env var → Secrets Manager → `providers.yaml` → `.env`), the
  per-provider variable names, creating a project, and issuing keys.
  `docs/images/dashboard-clean.png` and `dashboard-seeded.png` show the stat band
  of each so "did the demo data load?" is answerable at a glance rather than by
  reading the flag back.

  The AWS path gained the step that was missing entirely: putting keys into the
  `axonllm/api-keys` secret the stack creates, and forcing a new deployment,
  because secrets are read at container start.

- **Documented authentication and authorization end to end**, including four
  behaviours that were true but written down nowhere:
  - **An API key issued with default scopes cannot reach `/admin/*`.**
    `axon issue-key` defaults to `--scopes chat`, and an API-key context carries
    `roles=["service"]`, which `AdminRBACMiddleware` rejects. So the admin API is
    unreachable after switching to `ENFORCE` unless an `admin:*` key was issued
    first — now called out as ordering, with the verified allow/deny matrix.
  - **A clean install has zero Cedar policies, and zero policies means no Cedar
    evaluation at all** — the middleware is constructed with
    `policy_service=None`. "Default deny" is Cedar's rule among the policies that
    exist, not a property of a fresh install, which is the opposite of what a
    reader assumes from the phrase.
  - **`POST /admin/policies` does not take effect until restart.** Policies are
    parsed once at construction, so the endpoint stores a policy that `GET` will
    show and the evaluator will not apply.
  - **SCIM group→role resolution does not feed the auth chain.** `roles_for_user`
    is only read on the SCIM path; roles for authorization come from the JWT or
    SAML assertion, so provisioning a user into an admin group grants nothing
    unless the IdP also puts `admin` in the claim.
  - **`aud` and `iss` on a direct OIDC Bearer token are checked only when
    `AXON_OIDC_AUDIENCE` / `AXON_OIDC_ISSUER` are set.** Signature and `exp` are
    unconditional, but an empty audience means a correctly-signed token minted for
    a *different* application is accepted — the confused-deputy shape. The README
    now says to set both rather than treating them as optional tuning.

  Also documented: the four-step credential precedence in `AuthMiddleware` (as a
  diagram), `ENFORCE` vs `LOG_ONLY` per layer, the OIDC claim mapping table and
  the `python-jose` fail-closed behaviour, SAML's five variables and the fact that
  `/saml/acs` returns JSON rather than minting a session, SCIM's fail-closed 503,
  and a minimal production environment block.

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

- **Production readiness checklist on `/admin/production-checklist`, with a live
  provider model-availability check.** Six checks, each covering a state the
  gateway serves traffic in without complaint — the config is wrong in a way no
  request surfaces:
  - **Pinned model ids still exist at their providers** (new). `config/models.yaml`
    pins a provider-side `model_id`; providers retire, rename and alias those on
    their own schedule, and nothing reconciled the two. The dangerous case is not
    the 404 — that fails over noisily — but the *undocumented alias*: `xai`'s
    `grok-3` answered 200 while resolving to `grok-4.3`, and because an alias
    appears on no price list it also billed **$0.00** while being served by a
    $1.25/$2.50-per-MTok model. The check asks each provider what it currently
    serves and diffs that against the routing table. Verified against the live
    APIs: it flags `grok-3` and a retired OpenAI snapshot, and correctly does
    *not* flag `claude-opus-4-1-20250805`, which Anthropic still lists.
  - **Token pricing covers every provider mapping** — reuses `audit_pricing`, so
    it cannot disagree with the pricing page. FAIL when a model has no priced
    provider at all (bills $0.00 outright, so a budget cap on it can never
    trigger); WARN when partially priced, since the priced provider still bills.
  - **Every routed provider has credentials.** `load_provider_configs` drops
    providers with no key without raising, so a missing credential is invisible
    until a request routes there. FAIL when a model has no usable provider at
    all. Bedrock and Bedrock-Mantle count as credentialled — they authenticate
    through the boto3 chain, not `providers.yaml`, so treating their absence as
    missing would fail this check for most deployments.
  - **API authentication is enforced.** `AXON_AUTH_MODE=LOG_ONLY` serves every
    unauthenticated request while logging that it would have denied it, which
    makes the audit trail read like the control is working.
  - **Demo seed data is not loaded** — WARN when `AXON_LOAD_DEMO_DATA` is merely
    *unset* rather than explicitly false, because `serve_dashboard.py` (the
    container `CMD`) defaults it to `true`, so the same image started the
    ordinary way seeds fabricated spend into a real dashboard.
  - **State survives a restart.** DynamoDB writes are swallowed by design — a
    provider call should not 500 because Dynamo hiccuped — so an unreachable
    table loses every billing record with no request-visible symptom.

  Three properties the checklist is built around. **A check that could not run
  reports UNKNOWN, never PASS** — collapsing those is how an expired credential
  or a blocked egress route renders as a green checklist, so disabling the live
  check leaves the row unknown rather than passing it. **Nothing is enforced**: no
  check can refuse a boot or reject a request, and the page says so, because a
  readiness page that can take down a deployment is one nobody enables. **List
  calls only, never a completion** — listing is free and idempotent, while
  probing an id by generating a token would make loading an admin page a billable
  event; the cost is that alias detection has to be inferred from absence rather
  than observed, and the page reports that ambiguity instead of guessing.

  The availability check spans three authentication styles: bearer-token HTTP
  endpoints, **Bedrock** via boto3, and **Bedrock Mantle** via a SigV4-signed
  `GET /v1/models` — Mantle is OpenAI-shaped but signs with SigV4, so it cannot
  use the bearer-token path. Bedrock reads **two** catalogues, and the second is
  not optional: `models.yaml` pins cross-region inference profiles
  (`us.anthropic.claude-opus-4-6-v1`) for 10 of its 14 mappings, and
  `list_foundation_models` does not return those, so checking it alone would
  report the majority of working Bedrock mappings as retired — a wall of
  confident false findings about the provider carrying the most traffic. A caller
  holding `ListFoundationModels` but not `ListInferenceProfiles` is treated as a
  failed read rather than a short catalogue, for the same reason. This takes
  live coverage from 18 mappings to **43**, leaving nothing in the current
  routing table unchecked; Vertex AI and Azure OpenAI remain unchecked by design,
  since their ids are deployment paths where listing proves nothing.

  Production only. In demo mode the page explains why it is empty rather than
  rendering checks that would all fail correctly and mean nothing, and makes no
  outbound calls at all. Anything unchecked is counted *by name*, so a partial
  check never implies full coverage. A failed provider call yields an error row
  and no findings — a network blip cannot manufacture a wall of bogus "retired
  model" warnings — and an empty model list is treated as a changed response
  shape rather than as "everything is missing". Credentials never reach a
  rendered string: transport failures report the exception type only (httpx
  embeds the request URL, which for Google AI carries the key as a query
  parameter; a botocore error can carry the caller's ARN) and the Google AI key
  is passed via `params` rather than interpolated into the URL. 91 tests in
  `tests/unit/test_production_checklist.py` and
  `tests/unit/test_model_availability.py`.

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

  13 mappings were left unpriced at this point on the reasoning that their
  providers publish no rate for the id being sent. Checking each against the
  provider's live API rather than its pricing page showed that reasoning was
  wrong for five of them — see the entry below.

- **Stale and unroutable provider model ids, found by probing the live APIs.**
  A pricing page is a marketing document and a model list is not a guarantee of
  capacity, so every id `models.yaml` and the adapter `_MODELS` lists advertise
  was tested with a real completion. Coverage rose 73% → 84% as a result, but the
  more useful finding is that "absent from the price list" turned out to predict
  almost nothing about whether an id works:
  - **`grok-3` and `grok-3-mini` answered 200 — and xAI resolves both to
    `grok-4.3`**, which it reports in the response's `model` field. Undocumented
    aliases: absent from `/v1/language-models` and from the price list, so
    unpriceable, so every Grok request billed **$0.00 while being served by a
    model that costs $1.25/$2.50 per MTok**. Nothing failed, which is why it
    survived — a 404 would have been caught the first time anyone tried it. Both
    are replaced by the real tiers, `grok-4.3` and `grok-4.5`, now priced from
    xAI's own API (which reports in hundred-thousandths of a cent per token —
    calibrated against the published page rather than assumed).
  - **`grok-2-vision-1212`** was advertised in `xai_adapter._MODELS` and is
    simply gone: 400 `Model not found`.
  - **Four of Together's five advertised ids need a dedicated endpoint.**
    `DeepSeek-R1`, `Qwen2.5-72B-Instruct-Turbo`, `Llama-4-Maverick-FP8` and
    `Mistral-Small-24B` all return 400 `Unable to access non-serverless model`.
    That is a provisioning error, not a wrong id, so no rename fixes it — and it
    is not fixable by picking a newer snapshot either: R1-0528, V3.1, Qwen2-72B,
    Qwen3-Next-80B, Qwen3.5-397B and QwQ-32B are all equally unavailable. The
    Qwen3.x `-Plus`/`-Max` tier *is* serverless but **streaming-only** (400 `This
    model only supports streaming`), which this gateway's non-streaming path
    cannot use. Both files now list only ids confirmed by a live non-streaming
    completion: Llama-3.3-70B-Turbo, DeepSeek-V4-Pro, Qwen3.5-9B and
    gpt-oss-120b, all priced from Together's own API.
  - **`gemini-3-pro-preview` is still served** — listed by `/v1beta/models` with
    `generateContent` support — despite being off the Gemini price list. So it is
    a working model with no published rate, the one case neither file can fix: the
    id is right and there is no number to put. Kept and left unpriced; the earlier
    claim that 3.1 Pro Preview replaced it was wrong.

  **8 mappings remain unpriced, all for reasons that survive checking:** the five
  `bedrock-mantle` `openai.gpt-5.x` ids (`gpt-5.4`, `-5.6-luna` and `-5.6-terra`
  are published only in `us-gov-east-1`/`us-gov-west-1` at $3.30/$19.80 per MTok;
  `gpt-5.5` and `-5.6-sol` appear under none of the four Bedrock service codes),
  `gemini-3-pro-preview` above, and the two `fireworks` ids — which are the one
  honest unknown here, since there is no `FIREWORKS_API_KEY` in the environment
  and the models endpoint requires one, so they could not be probed either way.
  They are kept rather than removed on that basis: "could not check" is not
  evidence an id is wrong, and deleting a working model is worse than leaving one
  on the drift report. Fireworks stays unpriced for the same reason a guess would
  be tempting — `gpt-oss-120b` is $0.15/$0.60 on both Bedrock and Together, which
  makes a third identical value look inevitable rather than assumed.

### Fixed
- **A Fargate deploy came up seeded with fictional tenants.** `infra/stack.py`
  set five environment variables on the task definition and not
  `AXON_LOAD_DEMO_DATA` — but absent is not neutral for that variable, because
  the container `CMD` is `serve_dashboard.py`, which defaults it to `true`. Every
  enterprise install therefore started with Acme Corp, three fictional users and
  66 fabricated usage records, merged into the same DynamoDB table as real state
  and indistinguishable from it in the UI. Worse than a visible failure: the
  gateway looked like it was already carrying traffic. The stack now sets
  `"false"` explicitly, and `tests/unit/test_infra_stack_env.py` asserts it —
  along with `AXON_AUTH_MODE=ENFORCE`, which has the same property (dropping it
  would open the deployment rather than fall back to the safe value). Turning it
  back on for a deployed demo is now the deliberate edit; README path 4 covers
  it. Note that this stops re-seeding, not deletion: rows a previous run
  persisted survive the flag.

- **Four `UsageRecord` fields never survived a DynamoDB round trip, and one of
  them came back claiming success.** `serialize_usage_record` wrote 14 of the
  dataclass's 18 fields; `cache_creation_tokens`, `latency_ms`, `status` and
  `routing_strategy` were silently absent. Every record restored from DynamoDB
  therefore carried the dataclass default for all four rather than what actually
  happened, which is the same shape as the `dominant_task_type` bug below: the
  field reads as present, nothing raises, and the value is a constant no request
  produced.

  `status` is the one that mattered, because its default is `"success"`. A table
  containing failed requests restored as a set asserting every request had
  succeeded — any error rate computed over restored records read 0%. That is
  worse than missing data: it is confidently wrong in the direction that looks
  healthy. `cache_creation_tokens` is the same story one layer down, since cache
  creation is billed at its own rate and a restored record priced it as ordinary
  prompt tokens.

  All four are now written and read back. The deserializer defaults an *absent*
  field to `""` / `0` rather than to the dataclass default, so a row written
  before this change reports "unknown" instead of asserting a plausible
  measurement — the same rule `task_type` already followed, where `""` (not
  classified) must never be read as `"general"` (classified as general).
  `latency_ms` is cast with an explicit `float()` because
  `_convert_decimals_to_native` narrows a whole-valued `Decimal` to `int`, so a
  latency landing exactly on `1234.0` would otherwise come back typed `int` for
  that row alone.

  The regression test that matters here is derived from
  `UsageRecord.__dataclass_fields__` rather than a hand-written list of names:
  the next field added to the dataclass now fails until it round-trips. Nothing
  connected the field list to the serializer before, which is precisely how four
  additions drifted out without anyone noticing.
- **Every user's `dominant_task_type` reported `general`, because the field was a
  hardcoded literal.** `UserEfficiencyProfile.dominant_task_type` was the string
  `"general"`, never derived from anything: a user who sent nothing but math
  questions looked identical to one who sent nothing but code. The literal was
  only the visible half. `UsageRecord` had no `task_type` field at all, so the
  classification — which *did* run correctly per request, and did drive routing —
  was discarded immediately after use. By profile-build time there was nothing
  left to aggregate, and the constant was standing in for data that had never
  been persisted. The tell was that the *empty*-records path returned `"unknown"`
  while the populated path returned `"general"`: a user with 500 requests looked
  *less* classified than a user with none.

  `UsageRecord` now carries `task_type`, stamped at both construction sites (the
  blocking path and the streaming path's end-of-stream accounting) from the smart
  routing decision when there is one and from the classifier otherwise — the
  fallback matters because most requests never go through smart routing, and
  without it the aggregate would describe only the auto-select minority while
  appearing to cover everyone. Classification is wrapped so it can never fail a
  request that has already been served.

  Throughout, `""` (not classified) is kept distinct from `"general"` (classified
  as general). Rows written before the field existed deserialize to `""`, and the
  mode counts only classified records, falling back to `"unknown"`. Collapsing the
  two would recreate the original bug for historical data — the population most
  likely to be affected by it — which is why it is pinned by tests on both the
  persistence round trip and the aggregate.
- **`mantle_adapter`'s advertised model list, four of six ids of which were not
  served.** Found by pointing the new availability check at Mantle.
  `anthropic.claude-sonnet-4-6`, `anthropic.claude-opus-4-6-v1` and
  `anthropic.claude-haiku-4-5-20251001-v1:0` carried Bedrock-style version
  suffixes Mantle does not use, and `meta.llama4-maverick-17b-instruct-v1:0` has
  no `meta.*` equivalent in the Mantle catalogue at all. Nothing broke, because
  request routing reads `config/models.yaml` rather than any adapter's `_MODELS`
  — which is precisely why the drift went unnoticed: it is an advertising surface
  no request exercises. Replaced with the 11 ids the registry actually routes to,
  all verified served, and pinned in both directions by `TestAdvertisedModels` so
  the list and the routing table cannot diverge again without a test failing.
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
