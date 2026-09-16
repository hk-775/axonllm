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

## Published snapshot — 2026-09-16

The public report is available at
[hk-775.github.io/axonllm/benchmark.html](https://hk-775.github.io/axonllm/benchmark.html).
The exact case-level outputs are committed as
[JSON](benchmarks/autorouting-2026-09-16.json) and
[Markdown](benchmarks/autorouting-2026-09-16.md).

| Strategy | Accuracy | Macro F1 | p50 latency | p95 latency | LLM call rate | Router cost / 1K |
|---|---:|---:|---:|---:|---:|---:|
| Improved Axon heuristic | 98.6% | 0.986 | 0.081 ms | 0.112 ms | 0.0% | $0 |
| LLM router | 94.4% | 0.945 | 1,345.748 ms | 5,132.984 ms | 100.0% | $0.044897 |
| Hybrid, threshold 0.3 | 97.2% | 0.972 | 0.212 ms | 2,973.273 ms | 16.7% | $0.007574 |

The heuristic result is a **development regression score**, not held-out
production evidence. The classifier was improved after reviewing failures in
this 72-prompt corpus, raising its score from the original 54.2% baseline to
98.6%. A deployment decision should use a separately frozen, human-reviewed
sample from the target workload.

The live comparison used `groq-gpt-oss-20b` through AxonLLM's local
OpenAI-compatible endpoint, one sequential repetition, a hybrid confidence
threshold of `0.3`, cache-isolating nonces, and explicit benchmark inputs of
`$0.075` per million input tokens and `$0.30` per million output tokens.
Downstream answer generation was excluded.

## Run the zero-cost baseline

```bash
uv run --locked axon-benchmark-routing \
  --strategy heuristic \
  --output-json /tmp/axon-routing-heuristic.json \
  --output-markdown /tmp/axon-routing-heuristic.md
```

The initial pre-improvement AxonLLM baseline on the committed corpus was:

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

uv run --locked axon-benchmark-routing \
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
uv run --locked axon-benchmark-routing \
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
uv run --locked axon-benchmark-routing \
  --strategy heuristic \
  --include-tag multilingual \
  --include-tag collision

# Deterministic smaller live sample, balanced across task labels.
uv run --locked axon-benchmark-routing \
  --strategy heuristic \
  --strategy llm \
  --sample-size 12 \
  --seed 775 \
  ...LLM options...

# Increase concurrency only after confirming provider rate limits.
uv run --locked axon-benchmark-routing \
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
