# AxonLLM

[![CI](https://github.com/axonllm/axonllm/actions/workflows/ci.yml/badge.svg)](https://github.com/axonllm/axonllm/actions/workflows/ci.yml)
[![License: MIT-0](https://img.shields.io/badge/License-MIT--0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

**The neural control plane for enterprise LLMs.**

One API, any provider. Smart routing picks the best model for each prompt. Ensemble mode dispatches to multiple models and synthesizes a better answer. Policy-driven security, PII redaction, quota enforcement, and multi-region failover — all in one place.

```bash
git clone https://github.com/axonllm/axonllm.git
cd axonllm
cp config/providers.yaml.example config/providers.yaml
# Add at least one API key (or just use Bedrock with AWS credentials)
docker compose up
# Open http://localhost:8000 — the landing page, with the dashboard one click away
# (or go straight to http://localhost:8000/admin/dashboard)
```

## Why AxonLLM?

| Problem | AxonLLM Solution |
|---------|-----------------|
| Teams call LLM providers directly — no visibility, no control | Single gateway with full observability |
| One model doesn't fit all prompts | Smart routing classifies prompts and picks the optimal model |
| Single-model quality ceiling | Ensemble dispatches to N models + judge synthesizes the best answer |
| LLM costs grow unchecked | Hierarchy-driven budgets (org→BU→project→env) that block before overspend |
| No guardrails on what goes to/from models | PII redaction, prompt injection detection, content filtering |
| Provider outages break everything | Multi-region hub-and-spoke with automatic failover |
| Compliance gaps | Immutable audit trail with SHA-256 hash chain |

## Features

### Routing & Providers
- **Multi-provider routing** — 13 provider adapters: Bedrock, Bedrock Mantle, Anthropic, OpenAI, Azure, Vertex AI, Google AI, Cohere, AI21, Fireworks, Groq, Together, xAI
- **Tool calling (function calling)** — send OpenAI-shaped `tools`/`tool_choice`; each adapter translates into its provider's own dialect (Anthropic `input_schema`, Bedrock `toolSpec`, Gemini `functionDeclarations`, Cohere `parameter_definitions`) and translates the call back. One tool definition works across every provider.
- **5 routing strategies** — round-robin, weighted, least-latency, cost-optimized, smart (intent-aware)
- **Ensemble routing** — scatter-gather-synthesize across a panel of models with configurable quorum
- **Multi-region hub-and-spoke** — single-region, active-passive failover, or active-active with weighted distribution
- **Data residency** — strict mode filters spokes by zone to keep data in-region

### Security & Compliance
- **PII redaction** — per-policy-node, regex-based detection (email, SSN, credit card, phone, IP, AWS account, medical record). Redacts before LLM sees the prompt, re-injects in the response.
- **Prompt injection detection** — pattern-scored heuristics (role override, extraction, delimiter escape, encoded payloads). Configurable blocking threshold.
- **Immutable audit trail** — SHA-256 hash chain, DynamoDB persistence, tamper detection
- **Event dispatcher** — webhook, AWS SNS, CloudWatch Logs. Fire-and-forget on security events.

### Governance & Cost Control
- **Policy hierarchy** — org → business unit → project → environment. Child inherits and can only tighten.
- **Quota enforcement** — rate limit (RPM), budget limit, max tokens, allowed models, allowed providers. All derived from the hierarchy.
- **Budget threshold alerting** — fires events at 80%, 90%, 100% spend via the event dispatcher
- **Per-user and per-project budgets** with automatic blocking

### Identity & Access
- **Multi-strategy auth** — ALB OIDC JWT, Bearer token (OIDC or API key), X-Api-Key header
- **SAML 2.0 SSO** — SP-initiated login + ACS with pure-Python signed-assertion verification (no xmlsec1 system dependency)
- **SCIM 2.0 provisioning** — `/scim/v2/Users` + `/scim/v2/Groups` for IdP-driven joiner/mover/leaver (Okta, Entra ID, …)
- **API key management** — issue, rotate, revoke, scope-restricted keys
- **Admin RBAC** — admin endpoints require `admin` role or `admin:*` scope (ENFORCE mode)

### Observability
- **Admin dashboard** — Sandbox, Overview, Traces, Efficiency, Audit Log, Models, Projects, Users, API Keys, Policies, Quotas, Regions, Webhooks, Health, Configuration, Architecture, Pricing, Readiness
- **Pricing coverage check** — flags models with no configured price, which bill at $0.00 and so silently under-count budgets. Reported at startup and on `/admin/pricing-drift`.
- **Production readiness checklist** — six checks for misconfigurations that serve traffic without raising anything: unpriced models, model ids the provider no longer lists (including aliases that answer 200 while serving a different model), missing credentials, `LOG_ONLY` auth, demo data, unreachable persistence. On `/admin/production-checklist`, production only.
- **Token efficiency analytics** — detect waste, recommend cheaper models, score prompt quality
- **Streaming** — SSE streaming for all providers with PII re-injection

## Supported Providers

| Provider | Auth | Status |
|----------|------|--------|
| AWS Bedrock | AWS credentials (automatic) | Working |
| AWS Bedrock Mantle | AWS credentials (automatic) | Working |
| Anthropic | API key | Working |
| OpenAI | API key | Working |
| Azure OpenAI | API key | Adapter ready |
| Google Vertex AI | GCP service account | Adapter ready |
| Google AI (Gemini) | API key | Adapter ready |
| Cohere | API key | Adapter ready |
| AI21 | API key | Adapter ready |
| Fireworks | API key | Adapter ready |
| Groq | API key | Adapter ready |
| Together | API key | Adapter ready |
| xAI | API key | Adapter ready |

## Quick Start

### Option 1: Docker (recommended)

```bash
cp config/providers.yaml.example config/providers.yaml
# Edit config/providers.yaml with your API keys, or set env vars:
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...

docker compose up
```

### Option 2: Local Python

```bash
pip install -e ".[dev]"
cp config/providers.yaml.example config/providers.yaml
AXON_LOAD_DEMO_DATA=true python serve_dashboard.py
```

Open http://localhost:8000 for the landing page, or
http://localhost:8000/admin/dashboard to go straight to the dashboard.

The dev server (`serve_dashboard.py`) runs in `LOG_ONLY` mode, so local requests
work **without** an API key. Any non-dev deployment defaults to `ENFORCE` (see
[Environment Variables](#environment-variables)) and requires one.

#### Provider keys for a demo

Put your provider keys in a `.env` file in the project root and they are picked
up automatically — but **only** when you set `AXON_LOAD_DEMO_DATA=true`
explicitly, as the command above does:

```bash
# .env (gitignored)
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_AI_API_KEY=...
XAI_API_KEY=xai-...
TOGETHER_API_KEY=...
FIREWORKS_API_KEY=fw_...
```

The name for each provider is in `provider_loader.py`; a provider whose key is
absent is dropped from the routing table rather than failing at request time, so
a missing name looks like "that model has no providers" instead of "no key".

This is a demo convenience, not a production config mechanism. In a real deploy
secrets come from the platform (ECS task definition, Secrets Manager, App Runner
env), and a file that shadowed those would be near-impossible to debug. Two rules
keep that from happening:

- Without an explicit `AXON_LOAD_DEMO_DATA=true`, the file is never read — and
  the container `CMD` doesn't set it.
- **An existing environment variable always wins.** The file only fills in names
  that aren't already set, so injected secrets are never overridden.

Startup logs the variable *names* it loaded, never their values. Set
`AXON_DEV_ENV_FILE` to read a different path.

### Get an API key

Under `ENFORCE`, every request needs an `axon_` key. Mint the first one from the
CLI — this works in-process and does **not** require an existing admin credential
(so there's no chicken-and-egg):

```bash
axon issue-key --project my-project --name my-first-key
# → axon_xxxxxxxx…   (shown once — store it)
```

For the key to be recognized by a running server, persistence must be enabled and
pointed at the same table the server uses (`LLM_ROUTER_DYNAMODB_ENABLED=true`,
`AXON_DYNAMODB_TABLE=…`); the CLI warns if it isn't. Pass the key as
`Authorization: Bearer <key>` or `X-Api-Key: <key>`, or export `AXON_API_KEY` for
the `axon chat` / `axon models` commands.

### Try it

```bash
# Simple chat (drop the -H line when hitting the LOG_ONLY dev server)
curl -X POST http://localhost:8000/api/chat \
  -H 'Content-Type: application/json' \
  -H 'X-Api-Key: axon_your_key_here' \
  -d '{"model": "claude-sonnet", "messages": [{"role": "user", "content": "Hello"}]}'

# Ensemble — same prompt to multiple models, judge synthesizes
curl -X POST http://localhost:8000/api/chat \
  -H 'Content-Type: application/json' \
  -H 'X-Api-Key: axon_your_key_here' \
  -d '{"model": "ensemble:quality", "messages": [{"role": "user", "content": "Explain CRDTs"}]}'

# Check quota state
curl http://localhost:8000/admin/quotas/proj:my-project \
  -H 'X-Api-Key: axon_admin_key'

# Simulate a request against quota enforcement
curl -X POST http://localhost:8000/admin/quotas/simulate \
  -H 'Content-Type: application/json' \
  -H 'X-Api-Key: axon_admin_key' \
  -d '{"project_id": "proj:ml", "model": "claude-opus", "estimated_cost": 0.05}'
```

### OpenAI-compatible endpoint — drop-in `base_url` swap

AxonLLM exposes an OpenAI-compatible surface at `/v1`, so existing code that uses
the OpenAI SDK can point at the gateway by changing only the `base_url` and the
API key — no request/response reshaping. Routing, quotas, guardrails, and cost
attribution all still apply.

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",   # AxonLLM instead of api.openai.com
    api_key="axon_your_key_here",          # an AxonLLM API key (axon_...)
)

# Non-streaming
resp = client.chat.completions.create(
    model="claude-sonnet",
    messages=[{"role": "user", "content": "Hello"}],
)
print(resp.choices[0].message.content)

# Streaming
for chunk in client.chat.completions.create(
    model="claude-sonnet",
    messages=[{"role": "user", "content": "Count to 3"}],
    stream=True,
):
    print(chunk.choices[0].delta.content or "", end="")
```

Raw HTTP:

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer axon_your_key_here' \
  -d '{"model": "claude-sonnet", "messages": [{"role": "user", "content": "Hello"}]}'

curl http://localhost:8000/v1/models \
  -H 'Authorization: Bearer axon_your_key_here'
```

Attribution (user/project for quotas and cost) is taken from the authenticated
API key, not the request body. Supported: `model`, `messages`, `temperature`,
`max_tokens`, `stream`, `tools`, `tool_choice`. Ensemble/smart-routing model names
(e.g. `ensemble:quality`) work here too.

### Tool calling — one definition, every provider

Define tools once in OpenAI's shape. Each adapter translates them into its
provider's dialect on the way out and translates the model's call back into
`tool_calls` on the way in, so the same loop works whether the request lands on
Bedrock, Bedrock Mantle, Anthropic, Gemini, or Cohere.

```python
tools = [{
    "type": "function",
    "function": {
        "name": "db_query",
        "description": "Run a read-only SQL query",
        "parameters": {
            "type": "object",
            "properties": {"sql": {"type": "string"}},
            "required": ["sql"],
        },
    },
}]

messages = [{"role": "user", "content": "How many rows are in the orders table?"}]
resp = client.chat.completions.create(model="claude-sonnet", messages=messages, tools=tools)

call = resp.choices[0].message.tool_calls[0]        # finish_reason == "tool_calls"
args = json.loads(call.function.arguments)          # {"sql": "SELECT COUNT(*) FROM orders"}

messages += [
    {"role": "assistant", "tool_calls": [call.model_dump()]},
    {"role": "tool", "tool_call_id": call.id, "content": "42"},
]
resp = client.chat.completions.create(model="claude-sonnet", messages=messages, tools=tools)
# → "There are 42 rows in the orders table."
```

Notes that matter in practice:

- **`arguments` is a JSON string** in OpenAI's shape (and an object in every other
  dialect). AxonLLM re-encodes at each boundary; a model that emits malformed JSON
  yields `{}` rather than failing the request, so your tool reports the bad call.
- **Keep schemas to plain JSON Schema.** Gemini *rejects* unknown keys
  (`additionalProperties`, `$schema`, `title`, `default`) rather than ignoring
  them, so AxonLLM strips them recursively — but a schema that leans on them means
  something different than you wrote.
- **Not every model supports tools.** Routing honors your `model`; smart routing
  picks by task, not by tool support, so pin a model when a call requires them.
- Cohere's v1 chat has no `tool_choice` equivalent — a non-`auto` value comes back
  as a response warning rather than being silently ignored.
- **Bedrock Mantle serves three APIs**, chosen by model, and each has its own tool
  dialect — including one where the tool spec is *flat* (`name` beside `type`, no
  `function` wrapper). AxonLLM picks the route and the dialect for you, so the loop
  above is unchanged; it matters only if you read the provider's raw payloads.
- **`stream=True` works with tools**, but a tool call arrives as one complete
  `delta.tool_calls` rather than incrementally: the `arguments` are a JSON string,
  and splitting them across chunks would emit fragments no client can parse until
  reassembled. Accumulate deltas as usual and you get the same call either way.
  Providers reached over their native SSE (OpenAI, Azure) stream text
  incrementally as before; the rest buffer, which is unchanged from streaming
  without tools.

## Web Interfaces

| Page | URL | Purpose |
|------|-----|---------|
| Dashboard | `/admin/dashboard` | Admin console — governance, security, management |
| Chat | `/chat` | Chat with model + provider + user selection |
| Playground | `/playground` | Router picks provider, shows routing decision |
| Routing Explorer | `/routing` | Smart routing or ensemble — classify prompt, explain decision |
| Pricing Coverage | `/admin/pricing-drift` | Which models have no price, and what that costs you |
| Production Readiness | `/admin/production-checklist` | What is misconfigured in ways no request would reveal (production only) |

## Architecture

```
Request → Auth (OIDC/API Key) → Quota Enforcement (policy hierarchy)
  → Injection Detection → PII Redaction → Rate Limit → Access Check
  → Budget Check → Guardrails → Cache Check → Region Route
  → Provider Route (strategy) → Response Guardrails → PII Re-injection
  → Audit Trail → Cost Track → Event Dispatch → Response
```

### Request Pipeline Steps

1. **Auth** — validate credentials, establish identity context
2. **Quota enforcement** — resolve policy hierarchy, check model/provider/budget/RPM/tokens
3. **Injection detection** — score messages, block HIGH+ threats, audit + dispatch
4. **PII redaction** — replace sensitive data with tokens before LLM sees it
5. **Rate limiting** — sliding window per-user and per-project
6. **Access checks** — project and user model restrictions
7. **Budget check** — project and user spend limits
8. **Guardrails** — content policy evaluation
9. **Cache** — exact-match response cache (SHA-256 of model + messages + params), then an optional semantic match on the reworded question. Written back after guardrails and PII re-injection, so a hit cannot bypass either. A hit is labelled on the way out: `x_cached: true` plus `x_cache_type` of `exact` or `semantic` (absent on a provider call)
10. **Region routing** — select spoke based on health, data residency, model availability
11. **Provider routing** — strategy-based model selection + fallback
12. **Response guardrails** — output filtering
13. **PII re-injection** — restore original values in response
14. **Audit trail** — immutable record with hash chain
15. **Cost tracking** — record usage, check budget thresholds, fire alerts

## Admin API Reference

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/admin/usage` | GET | Aggregated usage (filters: `start_time`, `end_time`, `provider`, `model`, `project_id`, `user_id`) |
| `/admin/usage/export` | GET | Export usage for chargeback. `format=csv` (default, file attachment) or `json`; `level=records` (per-request, default) or `breakdown` (aggregated). Same filters as `/admin/usage`. |
| `/admin/quotas/{project_id}` | GET | Current quota state for a project |
| `/admin/quotas/{project_id}/reset` | POST | Reset spend counter |
| `/admin/quotas/simulate` | POST | Test if a request would be allowed |
| `/admin/keys` | GET/POST | List or issue API keys |
| `/admin/keys/{key_id}/rotate` | POST | Rotate an API key |
| `/admin/keys/{key_id}/revoke` | POST | Revoke an API key |
| `/admin/policies` | GET/POST | List or create policy nodes |
| `/admin/policies/{node_id}` | GET/PUT/DELETE | Manage a policy node |
| `/admin/policies/resolve/{project_id}` | GET | Resolve effective policy |
| `/admin/audit/records` | GET | Query audit records |
| `/admin/audit/verify` | POST | Verify hash chain integrity |
| `/admin/audit/stats` | GET | Audit statistics |
| `/admin/webhooks` | GET/POST | List or add event destinations |
| `/admin/webhooks/{name}` | DELETE | Remove a destination |
| `/admin/webhooks/{name}/test` | POST | Send test event |
| `/admin/semantic-cache` | GET | Semantic cache stats: entries, hits, misses, and how many candidates the literal guard rejected |
| `/admin/semantic-cache` | DELETE | Invalidate entries — one project with `?project_id=`, all of them without |
| `/admin/regions` | GET | Current topology |
| `/admin/regions/health` | GET | Spoke health status |
| `/admin/regions/health/check` | POST | Trigger health check |
| `/admin/regions/failover` | POST | Force failover |
| `/admin/regions/{region}/status` | PUT | Set spoke status |

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | — | Anthropic API key |
| `OPENAI_API_KEY` | — | OpenAI API key |
| `AWS_DEFAULT_REGION` | `us-east-1` | AWS region for Bedrock |
| `AXON_LOAD_DEMO_DATA` | `false` | Load demo projects/users on startup; also enables reading provider keys from `.env` |
| `AXON_DEV_ENV_FILE` | `.env` | Path to the demo env file (only read when `AXON_LOAD_DEMO_DATA=true`) |
| `AXON_NO_BROWSER` | `false` | Stop `serve_dashboard.py` opening the pricing-coverage page when models are unpriced (already skipped when stdout is not a tty) |
| `LLM_ROUTER_DYNAMODB_ENABLED` | `false` | Enable DynamoDB persistence |
| `AXON_DYNAMODB_TABLE` | `axonllm-state` | DynamoDB table name (must match the provisioned table) |
| `AXON_SERVER_PORT` | `8000` | Server port |
| `AXON_AUTH_MODE` | `ENFORCE` | Auth enforcement: `ENFORCE` (default, fail-closed) or `LOG_ONLY` (local dev) |
| `AXON_OIDC_ISSUER` | — | OIDC token issuer URL |
| `AXON_OIDC_AUDIENCE` | — | OIDC expected audience |
| `AXON_SCIM_TOKEN` | — | Bearer secret the IdP uses for SCIM provisioning; SCIM is disabled until set |
| `AXON_SAML_SP_ENTITY_ID` | — | SP entity id (SAML audience) |
| `AXON_SAML_ACS_URL` | — | Assertion Consumer Service URL (this gateway's `/saml/acs`) |
| `AXON_SAML_IDP_SSO_URL` | — | IdP SSO redirect endpoint |
| `AXON_SAML_IDP_CERT` / `AXON_SAML_IDP_CERT_FILE` | — | IdP signing certificate (PEM inline or file path); SAML disabled until set |
| `AXON_SEMANTIC_CACHE` | `false` | Build the embedder for semantic caching. A project also needs `semantic_cache_enabled` — both must say yes |
| `AXON_SEMANTIC_CACHE_REGION` | `AXON_BEDROCK_REGION` | Region for the embedding calls |
| `AXON_SEMANTIC_CACHE_MODEL` | `amazon.titan-embed-text-v2:0` | Bedrock embedding model id |
| `AXON_SEMANTIC_CACHE_THRESHOLD` | `0.95` | Cosine similarity a stored prompt must clear to be served. Must be in `(0, 1]`; an unparseable or out-of-range value falls back to the default rather than to `0`, which would match everything |
| `OSTIARI_TRACES_URL` | — | When set, forward request traces to this Ostiari ingest URL (e.g. `http://control-plane:8000/api/traces/ingest`) |
| `OSTIARI_GATEWAY_ID` | `axonllm` | Gateway identifier reported in Ostiari's Live Traces |
| `OSTIARI_INGEST_KEY` | — | Shared secret sent as `X-Ingest-Key` when Ostiari's ingest endpoint requires auth |
| `OSTIARI_TRACES_TIMEOUT` | `3.0` | Per-request timeout (seconds) for trace forwarding |

### Models

Define models in `config/models.yaml`:

```yaml
models:
  - name: claude-sonnet
    routing_strategy: least-latency
    providers:
      - provider: bedrock
        model_id: us.anthropic.claude-sonnet-4-20250514-v1:0
        fallback_order: 0
      - provider: anthropic
        model_id: claude-sonnet-4-20250514
        fallback_order: 1
```

#### Pricing drives smart routing

Smart routing ranks candidates on `(1 - cost_quality_tradeoff) × benchmark +
cost_quality_tradeoff × cheapness`, so **the cost half only works for models that
have a price**. Prices live in `config/pricing.yaml`, keyed by provider and then
by the *provider-side* `model_id` — the same lookup used to bill the request, so
the cost used to choose a model matches the cost actually charged:

```yaml
providers:
  bedrock:
    us.amazon.nova-pro-v1:0:      # must match model_id in models.yaml
      prompt_token_cost: 0.0008   # per 1,000 tokens
      completion_token_cost: 0.0032
```

A model with no entry is treated as **unknown-cost, not free** — it is scored at
the mean of the priced candidates and flagged `cost_estimated` in the decision
trace. Scoring it as 0.0 would make it the cheapest candidate and let it win for
being unmeasured rather than for being cheap. Add a price for any model you want
genuinely ranked on cost. An inline `pricing:` block on a provider entry in
`models.yaml` overrides the table for that mapping.

OpenAI's `-pro` tier (`gpt-5.5-pro`, `gpt-5-pro`) is served only by the Responses
API, and answers 400 `This is not a chat model` on Chat Completions. Configure it
like any other model — the `openai` adapter recognizes the tier and switches
endpoint and payload shape itself. Two consequences worth knowing:

- **`temperature` and `top_p` are dropped**, not forwarded. These models reject
  them with a 400 rather than ignoring them, so a request carrying either would
  fail outright.
- **Only `provider: openai` gets this.** The OpenAI-compatible providers (xAI,
  Groq, Together, Fireworks, AI21, Azure) have no `/v1/responses` route, so a
  `-pro`-suffixed `model_id` there stays on Chat Completions and will fail if the
  provider does not genuinely serve it.

#### Pricing coverage

Per-token rates live in `config/pricing.yaml`, keyed by provider and then by the
**provider-side `model_id`** — the id sent to the provider, not the gateway's
model name:

```yaml
providers:
  bedrock:
    us.anthropic.claude-sonnet-4-20250514-v1:0:
      prompt_token_cost: 0.003       # per 1,000 tokens
      completion_token_cost: 0.015
```

`models.yaml` and `pricing.yaml` are edited independently and nothing tied them
together, so a model added to one and not the other has two silent consequences:

- **It bills at $0.00.** An unknown provider/model pair costs zero, so the usage
  record carries no cost and project spend does not move — budget blocks and
  quota alerts under-count rather than erring safe.
- **Smart routing scores it on an estimate.** With no rate to read, the cost half
  of `cost_quality_tradeoff` substitutes the average of the known prices and
  flags the decision `cost_estimated`.

Neither raises anything, so the gateway reports the gap instead: the startup
banner names the count and links to **`/admin/pricing-drift`**, which lists every
unpriced mapping, every pricing entry no model reads (usually the other half of a
renamed model id), and a paste-ready YAML fragment for the missing ones. Rates in
the fragment are left at `0.0` deliberately — a guessed price bills silently,
where a missing one shows up on the page.

The banner is gated on unpriced mappings only, so it clears once every model has
a rate; leftover entries are listed but not escalated, since they charge nobody
anything.

`serve_dashboard.py` also opens the page in a browser when it finds a gap, since
a line in the startup scroll is exactly the kind of warning that gets missed.
That only happens on an interactive terminal — the same file is the container
`CMD`, so a piped or containerized run prints the banner and nothing else. Set
`AXON_NO_BROWSER=true` to suppress it locally too.

#### Production readiness checklist

Pricing coverage is one instance of a general problem: the gateway serves traffic
happily while its configuration is wrong, because none of these states raise
anything. **`/admin/production-checklist`** answers the question a log cannot —
not "is anything broken right now" but "is this deployment ready to carry real
traffic":

| Check | Fails when | Why it is invisible otherwise |
|---|---|---|
| Pinned model ids exist at their providers | An id is not in the provider's own model list | A retired id fails over; an **alias answers 200 while serving a different model**, and bills $0.00 |
| Token pricing covers every mapping | A model has no priced provider | Bills $0.00, so a budget cap on it can never trigger |
| Every routed provider has credentials | A model has no usable provider | Providers without keys are dropped silently at startup |
| API authentication is enforced | `AXON_AUTH_MODE=LOG_ONLY` | Requests are served *and* logged as denied |
| Demo seed data is not loaded | `AXON_LOAD_DEMO_DATA` is unset or true | `serve_dashboard.py` — the container `CMD` — defaults it to `true` |
| State survives a restart | DynamoDB is disabled or unreachable | Writes are swallowed by design, so billing data vanishes silently |

The model-id check is the one that goes out to the network. It asks each
configured provider what it currently serves and diffs that against
`models.yaml` — one **list** call per provider, never a completion: listing is
free, generating a token is not, and loading an admin page should not be a
billable event. The tradeoff is that a retired id and an honoured-but-undocumented
alias look identical from a list, so the page reports both as *unlisted* and says
which two things that could mean, rather than guessing.

Three things worth knowing before you rely on it:

- **A check that could not run reports UNKNOWN, not PASS.** Turning the live check
  off with `AXON_CHECK_MODEL_AVAILABILITY=false` (for an egress-filtered
  deployment) leaves that row unknown. Collapsing "could not verify" into "fine"
  is how an expired credential renders as a green checklist.
- **Nothing is enforced.** No check can refuse a boot or reject a request. An
  operator who has read a warning and decided to ship is making a call the
  gateway is not positioned to overrule, and a readiness page that can take down
  a deployment is one nobody enables. Failures print a startup banner; warnings
  stay quiet, since a banner on every healthy boot is one nobody reads.
- **It is hidden in demo mode.** A demo deliberately runs with no credentials,
  `LOG_ONLY` auth and seeded data — exactly the configuration this checklist
  exists to fail. Rendering it there would show a wall of red that is correct for
  a demo and teaches operators to ignore the page, so the page explains itself
  instead and makes no outbound calls.

Coverage spans three authentication styles, not just bearer tokens: API-key
providers over HTTP, **Bedrock** through boto3, and **Bedrock Mantle** through a
SigV4-signed `GET /v1/models`. Bedrock reads two catalogues rather than one —
`models.yaml` pins cross-region inference profiles (`us.anthropic.…`) for most of
its mappings, and `list_foundation_models` does not return those, so checking it
alone would report the majority of working Bedrock mappings as retired.

Vertex AI and Azure OpenAI stay unchecked, because their model ids are deployment
and publisher paths where listing proves nothing about whether a mapping
resolves. Anything unchecked is counted as *unchecked by name* rather than
quietly omitted, so a partial check never reads as full coverage.

### Ensemble Presets

Define ensemble presets in `config/ensemble.yaml`:

```yaml
presets:
  quality:
    panel:
      - claude-opus
      - gpt-4o
      - claude-sonnet
    judge: claude-opus
    quorum: 2
    fallback_policy: best-single
```

### Policy Hierarchy

```
org:acme (rate_limit_rpm=1000, budget=$50k, allowed_models=[claude-opus, claude-sonnet])
  └── bu:engineering (budget=$20k)
        └── proj:ml-team (budget=$5k, rate_limit_rpm=200)
              └── env:prod (rate_limit_rpm=100)
```

Child nodes inherit from parents. Rules: budget uses MIN (tightest wins), rate limit uses MIN, allowed models uses INTERSECTION, PII redaction uses OR (once enabled, can't disable), PII types uses UNION (children add stricter types).

## Testing

```bash
pip install -e ".[dev]"
pytest tests/ -x -q
```

1457 tests including unit, integration, end-to-end, and Hypothesis property-based tests.

## Deployment

### ECS Fargate (recommended)

```bash
./deploy-fargate.sh us-east-1
```

This deploys AxonLLM as a Fargate service (via CDK) with:
- ALB with sticky sessions and 5-minute idle timeout (for SSE streaming)
- Auto-scaling (2-10 tasks) on CPU and request count
- DynamoDB tables for persistence (audit trail, API keys, policies)
- Secrets Manager for provider API keys
- IAM role with Bedrock invoke permissions
- CloudWatch Container Insights

Prerequisites:
- AWS CLI configured with appropriate permissions
- Docker running
- Node.js installed (for CDK CLI)
- First-time: `cd infra && pip install -r requirements.txt && cdk bootstrap`

### AWS App Runner (simpler, less control)

```bash
./deploy.sh us-east-1
```

Simpler setup but no ALB, no sticky sessions, limited scaling control.

### Docker (self-hosted)

```bash
docker build -t axonllm .
docker run -p 8000:8000 \
  -e ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY \
  -e AWS_DEFAULT_REGION=us-east-1 \
  -e AXON_AUTH_MODE=ENFORCE \
  axonllm
```

### Production Checklist

| Setting | Recommendation |
|---------|---------------|
| `AXON_AUTH_MODE` | Set to `ENFORCE` (default is `LOG_ONLY` for easy local dev) |
| API keys | Use env vars, never commit to `providers.yaml` |
| DynamoDB | Enable with `LLM_ROUTER_DYNAMODB_ENABLED=true` for persistent audit trail |
| OIDC | Configure `AXON_OIDC_ISSUER` and `AXON_OIDC_AUDIENCE` for SSO |
| TLS | Terminate TLS at ALB/CloudFront, not at the gateway |
| Budgets | Set org-level budget limits before granting project access |

## Embedding in Ostiari (trace forwarding)

When AxonLLM runs embedded inside [Ostiari](https://github.com/…), each completed
request is forwarded as a trace event into Ostiari's Live Traces view. Forwarding
activates automatically when Ostiari is *detected* — no code change to standalone
deployments (with neither of the following, it's a no-op).

**Two delivery paths (either or both):**

1. **HTTP** — set `OSTIARI_TRACES_URL` to Ostiari's control-plane ingest endpoint:

   ```bash
   OSTIARI_TRACES_URL=http://control-plane:8000/api/traces/ingest \
   OSTIARI_GATEWAY_ID=axon-prod-1 \
   python serve_dashboard.py
   ```

2. **In-process** — when co-located in the same process, the embedding host registers
   a sink; AxonLLM calls it directly (no network hop, no dependency on the `ostiari`
   package):

   ```python
   from src.gateway.observability.trace_forwarder import register_sink
   register_sink(lambda event: ostiari_storage.save_trace_event(event))
   ```

Each forwarded event carries the model, provider, token counts, cost, latency, and
user/project in Ostiari's trace shape. AxonLLM is a routing/cost layer, not a risk
scorer, so it sends neutral risk fields (`tier="allow"`, `score=0`) and leaves risk
scoring to Ostiari. Forwarding is best-effort: a slow or unavailable Ostiari never
slows or fails a request.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT-0 — See [LICENSE](LICENSE).
