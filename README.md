# AxonLLM

**The neural control plane for enterprise LLMs.**

One API, any provider. Smart routing picks the best model for each prompt. Ensemble mode dispatches to multiple models and synthesizes a better answer. Cost tracking, budgets, access control, and guardrails — all in one place.

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
| LLM costs grow unchecked | Per-user, per-project budgets that block before overspend |
| No guardrails on what goes to/from models | Configurable input/output content filtering |
| Provider outages break everything | Automatic retry + failover across providers |

## Features

- **Multi-provider routing** — Bedrock, Anthropic, OpenAI, Azure, Vertex AI, Cohere
- **5 routing strategies** — round-robin, weighted, least-latency, cost-optimized, smart (intent-aware)
- **Ensemble routing** — scatter-gather-synthesize across a panel of models with configurable quorum
- **Cost tracking** — per-request, per-user, per-project with budget enforcement
- **Access control** — restrict which models each user or project can access
- **Prompt caching** — provider-level cache (Anthropic/Bedrock) for repeated system prompts
- **Rate limiting** — sliding window per-user and per-project
- **Guardrails** — keyword, regex, and category-based content filtering
- **Token efficiency analytics** — detect waste, recommend cheaper models, score prompt quality
- **Streaming** — SSE streaming for all providers
- **DynamoDB persistence** — optional, for production state durability
- **Admin dashboard** — manage projects, users, models, budgets, health
- **Chat + Playground UIs** — test models with routing visibility

## Supported Providers

| Provider | Auth | Status |
|----------|------|--------|
| AWS Bedrock | AWS credentials (automatic) | Working |
| Anthropic | API key | Working |
| OpenAI | API key | Working |
| Azure OpenAI | API key | Adapter ready |
| Google Vertex AI | GCP service account | Adapter ready |
| Cohere | API key | Adapter ready |

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

### Try it

```bash
# Simple chat
curl -X POST http://localhost:8000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"model": "claude-sonnet", "messages": [{"role": "user", "content": "Hello"}]}'

# Ensemble — same prompt to multiple models, judge synthesizes
curl -X POST http://localhost:8000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"model": "ensemble:quality", "messages": [{"role": "user", "content": "Explain CRDTs"}]}'

# Streaming
curl -X POST http://localhost:8000/api/chat/stream \
  -H 'Content-Type: application/json' \
  -d '{"model": "claude-sonnet", "messages": [{"role": "user", "content": "Write a haiku"}]}'
```

## Web Interfaces

| Page | URL | Purpose |
|------|-----|---------|
| Dashboard | `/admin/dashboard` | Admin console — projects, users, models, budgets, health |
| Chat | `/chat` | Chat with model + provider + user selection |
| Playground | `/playground` | Router picks provider, shows routing decision |
| Routing Explorer | `/routing` | Smart routing or ensemble — classify prompt, explain decision |

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

### Models

Define models in `config/models.yaml`:

```yaml
virtual_models:
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

## Architecture

```
Request → Validate → Rate Limit → Access Check → Budget Check → Guardrails
  → Cache Check → Route (strategy) → Provider → Response Guardrails
  → Cost Track → Response
```

The gateway supports retry with exponential backoff and automatic failover to healthy providers.

## Testing

```bash
pip install -e ".[dev]"
pytest tests/ -x -q
```

730+ tests including unit tests and Hypothesis property-based tests.

## Deployment

### AWS App Runner

```bash
./deploy.sh us-east-1
```

### Docker

```bash
docker build -t axonllm .
docker run -p 8000:8000 \
  -e ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY \
  -e AWS_DEFAULT_REGION=us-east-1 \
  axonllm
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT-0 — See [LICENSE](LICENSE).
