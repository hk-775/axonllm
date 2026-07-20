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
# Open http://localhost:8000/admin/dashboard
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
- **API key management** — issue, rotate, revoke, scope-restricted keys
- **Admin RBAC** — admin endpoints require `admin` role or `admin:*` scope (ENFORCE mode)

### Observability
- **Admin dashboard** — Sandbox, Overview, Traces, Efficiency, Audit Log, Models, Projects, Users, API Keys, Policies, Quotas, Regions, Webhooks, Health, Configuration, Architecture
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

Open http://localhost:8000/admin/dashboard.

The dev server (`serve_dashboard.py`) runs in `LOG_ONLY` mode, so local requests
work **without** an API key. Any non-dev deployment defaults to `ENFORCE` (see
[Environment Variables](#environment-variables)) and requires one.

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
`max_tokens`, `stream`. Ensemble/smart-routing model names (e.g. `ensemble:quality`)
work here too.

## Web Interfaces

| Page | URL | Purpose |
|------|-----|---------|
| Dashboard | `/admin/dashboard` | Admin console — governance, security, management |
| Chat | `/chat` | Chat with model + provider + user selection |
| Playground | `/playground` | Router picks provider, shows routing decision |
| Routing Explorer | `/routing` | Smart routing or ensemble — classify prompt, explain decision |

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
9. **Cache** — exact-match response cache (SHA-256 of model + messages + params)
10. **Region routing** — select spoke based on health, data residency, model availability
11. **Provider routing** — strategy-based model selection + fallback
12. **Response guardrails** — output filtering
13. **PII re-injection** — restore original values in response
14. **Audit trail** — immutable record with hash chain
15. **Cost tracking** — record usage, check budget thresholds, fire alerts

## Admin API Reference

| Endpoint | Method | Description |
|----------|--------|-------------|
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
| `AXON_LOAD_DEMO_DATA` | `false` | Load demo projects/users on startup |
| `LLM_ROUTER_DYNAMODB_ENABLED` | `false` | Enable DynamoDB persistence |
| `AXON_SERVER_PORT` | `8000` | Server port |
| `AXON_AUTH_MODE` | `LOG_ONLY` | Auth enforcement: `LOG_ONLY` or `ENFORCE` |
| `AXON_OIDC_ISSUER` | — | OIDC token issuer URL |
| `AXON_OIDC_AUDIENCE` | — | OIDC expected audience |

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

980 tests including unit, integration, end-to-end, and Hypothesis property-based tests.

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

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT-0 — See [LICENSE](LICENSE).
