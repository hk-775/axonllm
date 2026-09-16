# Auto-routing Benchmark

AxonLLM includes a reproducible benchmark for the routing tradeoff behind
`model: "auto"`:

1. **Axon heuristic** — the production `TaskClassifier`; local keyword,
   structural, and regex signals with no model call.
2. **LLM router** — a small classifier model reached through any
   OpenAI-compatible endpoint, including a LiteLLM proxy.
3. **Hybrid** — use the heuristic first and invoke the LLM only below a
   confidence threshold.

The benchmark measures the routing decision itself. It deliberately excludes
the downstream model response so router latency and token cost are not hidden
inside a much larger generation.

## What is measured

- accuracy and macro F1 against one shared task taxonomy;
- p50, p95, and p99 routing latency;
- LLM escalation rate;
- router input/output tokens and direct router cost;
- accuracy by scenario, including clear, implicit, keyword-collision, and
  multilingual prompts;
- break-even downstream savings per request;
- router cost per additional correct route.

The packaged corpus contains 72 prompts, balanced across:

- `coding`
- `reasoning`
- `creative_writing`
- `summarization`
- `math`
- `general`

It is intentionally harder than ordinary traffic. Its purpose is to expose
where a rule-based router fails, not to claim a universal production accuracy
number.

## Run the zero-cost baseline

```bash
uv run axon-benchmark-routing \
  --strategy heuristic \
  --output-json /tmp/axon-routing-heuristic.json \
  --output-markdown /tmp/axon-routing-heuristic.md
```

The initial AxonLLM baseline on the committed corpus is:

| Metric | Result |
|---|---:|
| Overall accuracy | 54.2% |
| Macro F1 | 0.552 |
| Clear-prompt accuracy | 79.5% |
| Keyword-collision accuracy | 11.1% |
| p95 routing latency | about 0.01 ms |
| Router token cost | $0 |

That result is the reason to test a hybrid rather than assume either regex or
an LLM should handle every request.

## Compare through a LiteLLM or other OpenAI-compatible proxy

Keep credentials in an environment variable; the benchmark never writes or
prints the key.

```bash
export ROUTER_BENCHMARK_KEY="<proxy key>"

uv run axon-benchmark-routing \
  --strategy heuristic \
  --strategy llm \
  --strategy hybrid \
  --hybrid-threshold 0.3 \
  --llm-base-url http://127.0.0.1:4000/v1 \
  --llm-model openai/gpt-4o-mini \
  --llm-api-key-env ROUTER_BENCHMARK_KEY \
  --llm-input-cost-per-million "<current input price>" \
  --llm-output-cost-per-million "<current output price>" \
  --output-json /tmp/axon-routing-comparison.json \
  --output-markdown /tmp/axon-routing-comparison.md
```

If a LiteLLM proxy returns `x-litellm-response-cost`, that observed cost wins.
Otherwise the benchmark calculates cost from response usage and the two
explicit per-million-token prices. Prices are intentionally not hardcoded
because model and provider rates change.

The same command can target AxonLLM itself:

```bash
./scripts/local_demo_backup.sh start
./scripts/local_demo_backup.sh copy-key tenant-acme

# Export the copied key as AXON_BENCHMARK_API_KEY, then:
uv run axon-benchmark-routing \
  --strategy heuristic \
  --strategy llm \
  --strategy hybrid \
  --llm-base-url http://127.0.0.1:8001/v1 \
  --llm-model groq-gpt-oss-20b \
  --llm-api-key-env AXON_BENCHMARK_API_KEY \
  --llm-input-cost-per-million 0.075 \
  --llm-output-cost-per-million 0.30
```

Those example Groq rates match the repository pricing table at the time of the
benchmark. Recheck the provider's current price before using the result for a
financial decision.

## Cache isolation

The benchmark adds a random, explicitly ignored nonce to each LLM classifier
request. This prevents a proxy or gateway response cache from making a later
strategy appear faster or cheaper merely because an earlier strategy already
classified the same corpus prompt.

Use `--allow-router-cache` only when cache performance is itself the subject of
the experiment.

## Useful experiment controls

```bash
# Run only multilingual and collision cases.
uv run axon-benchmark-routing \
  --strategy heuristic \
  --include-tag multilingual \
  --include-tag collision

# Deterministic smaller live sample, balanced across task labels.
uv run axon-benchmark-routing \
  --strategy heuristic \
  --strategy llm \
  --sample-size 12 \
  --seed 775 \
  ...LLM options...

# Increase concurrency only after confirming provider rate limits.
uv run axon-benchmark-routing \
  --concurrency 4 \
  ...other options...
```

Reasoning models may consume tokens before producing their short JSON
classification. The default router completion budget is therefore 256 tokens;
adjust it with `--llm-max-output-tokens`.

## Reading the break-even result

If an LLM router costs `$0.00005` per request, it must save at least that much
in downstream model spend, retry cost, or error handling on the average
request. Better accuracy alone does not make it economical.

`router_cost_per_additional_correct_route_usd` answers a second question: how
much was paid for each routing decision that became correct relative to the
Axon heuristic baseline? If accuracy does not improve, this metric is reported
as unavailable rather than presenting a misleading negative value.

## Corpus format

Supply another JSONL corpus with `--corpus`:

```json
{"id":"case-1","prompt":"Explain why the result follows.","expected_task":"reasoning","tags":["clear","english"]}
```

Every corpus must contain all six labels. Prompts and labels should be reviewed
by people familiar with the target workload; routing accuracy is only as useful
as the ground truth.
