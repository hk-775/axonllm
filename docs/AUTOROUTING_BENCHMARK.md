# Auto-routing Benchmark

AxonLLM includes a reproducible benchmark for the routing tradeoff behind
`model: "auto"`:

1. **Axon heuristic** — the production `TaskClassifier`; local keyword,
   structural, and regex signals with no model call.
2. **LLM router** — a classifier model reached through an OpenAI-compatible
   endpoint, including AxonLLM or a LiteLLM proxy.
3. **Hybrid** — use the heuristic first and invoke the LLM only below a
   confidence threshold.

The benchmark measures the routing decision itself. It excludes downstream
answer generation so router latency and token cost are not hidden inside a
larger completion.

## What is measured

- accuracy and macro F1 against one shared task taxonomy;
- p50, p95, and p99 routing latency;
- LLM escalation rate;
- router input/output tokens and direct router cost;
- accuracy by scenario, including clear, implicit, keyword-collision, and
  multilingual prompts;
- break-even downstream savings per request;
- router cost per additional correct route;
- malformed or failed router responses, counted as incorrect.

## Published held-out snapshot — 2026-09-16

The public report is available at
[hk-775.github.io/axonllm/benchmark.html](https://hk-775.github.io/axonllm/benchmark.html).
The exact case-level outputs are committed as
[JSON](benchmarks/autorouting-held-out-2026-09-16.json) and
[Markdown](benchmarks/autorouting-held-out-2026-09-16.md).

| Strategy | Accuracy | Macro F1 | p50 latency | p95 latency | LLM call rate | Router cost / 1K | Errors |
|---|---:|---:|---:|---:|---:|---:|---:|
| Improved Axon heuristic | 86.7% | 0.875 | 0.307 ms | 0.896 ms | 0.0% | $0 | 0 |
| GPT-4o Mini router | 91.7% | 0.926 | 657.668 ms | 843.997 ms | 100.0% | $0.045082 | 1 |
| Hybrid, threshold 0.3 | 95.0% | 0.950 | 1.457 ms | 851.265 ms | 28.3% | $0.011590 | 0 |

The hybrid gained 8.3 percentage points over the local heuristic while making
17 LLM calls for 60 requests. Its measured direct router cost was about
`$0.00001159` per request. That means the downstream route must save at least
that amount on average before the hybrid is cheaper end to end.

The LLM-only run had one malformed classification response after consuming its
256-token completion budget. The benchmark counts that response as an error
and an incorrect route.

## Corpus, split, and tuning boundary

The fresh corpus contains 120 prompts, 20 for each of:

- `coding`
- `reasoning`
- `creative_writing`
- `summarization`
- `math`
- `general`

It includes 24 clear, 48 implicit, 30 keyword-collision, and 18 multilingual
cases. Claude Sonnet generated the candidates. They received manual review and
a separate GPT-4.1 label review that flagged zero cases.

Before per-case tuning, the corpus was deterministically split:

- odd-numbered case IDs: 60-prompt development set;
- even-numbered case IDs: 60-prompt final test set.

Only development-set misses informed classifier changes. The heuristic moved
from 35.0% to 100.0% on development. The rules were then locked before final
per-case inspection and scored 86.7% on the final test set. No classifier
changes were made after inspecting final-test errors.

The original rules had scored 38.3% on that same final half before the
development work, so the held-out improvement was 48.3 percentage points.

See the committed
[provenance and hashes](benchmarks/autorouting-generated-2026-09-16-provenance.md),
[development baseline](benchmarks/autorouting-development-baseline-2026-09-16.json),
and
[locked development result](benchmarks/autorouting-development-final-2026-09-16.json).

## Live-run controls

The held-out comparison used:

- `gpt-4o-mini` through AxonLLM's local OpenAI-compatible endpoint;
- one sequential repetition;
- a hybrid confidence threshold of `0.3`;
- cache-isolating nonces;
- explicit benchmark inputs of `$0.15` per million input tokens and `$0.60`
  per million output tokens;
- no downstream answer generation.

A preceding Groq recovery probe returned upstream `502` responses for five of
six requests. That provider-contaminated run was excluded from the published
classification comparison; GPT-4o Mini completed the held-out run without
upstream HTTP failures. Provider failures are operational evidence, not a fair
measure of classifier quality.

Prices are benchmark inputs, not a promise of current provider pricing.
Recheck the active provider price before making a financial decision.

## Reproduce the held-out heuristic

```bash
uv run --locked axon-benchmark-routing \
  --corpus docs/benchmarks/autorouting-final-test-2026-09-16.jsonl \
  --strategy heuristic \
  --output-json /tmp/axon-routing-held-out-heuristic.json \
  --output-markdown /tmp/axon-routing-held-out-heuristic.md
```

## Reproduce the live comparison

Start the seeded demo and copy a tenant key:

```bash
./scripts/local_demo_backup.sh start
./scripts/local_demo_backup.sh copy-key tenant-acme
```

Export the copied value as `AXON_BENCHMARK_API_KEY`, then run:

```bash
uv run --locked axon-benchmark-routing \
  --corpus docs/benchmarks/autorouting-final-test-2026-09-16.jsonl \
  --strategy heuristic \
  --strategy llm \
  --strategy hybrid \
  --hybrid-threshold 0.3 \
  --concurrency 1 \
  --llm-base-url http://127.0.0.1:8001/v1 \
  --llm-model gpt-4o-mini \
  --llm-api-key-env AXON_BENCHMARK_API_KEY \
  --llm-input-cost-per-million 0.15 \
  --llm-output-cost-per-million 0.60 \
  --output-json /tmp/axon-routing-held-out.json \
  --output-markdown /tmp/axon-routing-held-out.md
```

The same command can target a LiteLLM proxy or another OpenAI-compatible
endpoint. Keep credentials in an environment variable; the benchmark never
writes or prints the key.

If a LiteLLM proxy returns `x-litellm-response-cost`, that observed cost wins.
Otherwise the benchmark calculates cost from response usage and the two
explicit per-million-token inputs.

## Cache isolation

The benchmark adds a random, explicitly ignored nonce to each LLM classifier
request. This prevents a proxy or gateway response cache from making a later
strategy appear faster or cheaper because an earlier strategy already
classified the same prompt.

Use `--allow-router-cache` only when cache performance is itself the subject of
the experiment.

## Useful controls

```bash
# Run only multilingual or collision cases.
uv run --locked axon-benchmark-routing \
  --corpus docs/benchmarks/autorouting-final-test-2026-09-16.jsonl \
  --strategy heuristic \
  --include-tag multilingual \
  --include-tag collision

# Deterministic balanced live sample.
uv run --locked axon-benchmark-routing \
  --corpus docs/benchmarks/autorouting-final-test-2026-09-16.jsonl \
  --strategy heuristic \
  --strategy llm \
  --sample-size 12 \
  --seed 775 \
  ...LLM options...

# Increase concurrency only after confirming provider limits.
uv run --locked axon-benchmark-routing \
  --concurrency 4 \
  ...other options...
```

Reasoning models may consume tokens before producing the short JSON
classification. The default router completion budget is 256 tokens; adjust it
with `--llm-max-output-tokens`.

## Historical 72-prompt regression snapshot

The packaged 72-prompt corpus remains useful as a regression suite. The
heuristic improved from 54.2% to 98.6% after its failures were reviewed, so
that 98.6% is explicitly a tuned development score, not held-out production
evidence. Its exact historical outputs remain available as
[JSON](benchmarks/autorouting-2026-09-16.json) and
[Markdown](benchmarks/autorouting-2026-09-16.md).

## Reading the break-even result

If a router costs `$0.00005` per request, it must save at least that much in
downstream model spend, retry cost, or error handling on the average request.
Higher classification accuracy alone does not make it economical.

`router_cost_per_additional_correct_route_usd` answers a second question: how
much was paid for each routing decision that became correct relative to the
Axon heuristic baseline? If accuracy does not improve, the metric is reported
as unavailable.

## Corpus format

Supply another JSONL corpus with `--corpus`:

```json
{"id":"case-1","prompt":"Explain why the result follows.","expected_task":"reasoning","tags":["clear","english"]}
```

Every corpus must contain all six labels. Prompts and labels should be reviewed
by people familiar with the target workload; routing accuracy is only as useful
as the ground truth.
