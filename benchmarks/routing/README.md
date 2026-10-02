# Smart-routing benchmark

Compare four ways to classify a prompt's primary task:

| Candidate | Implementation | API cost | Hardware / electricity cost |
|---|---|---|---|
| Regex / heuristic | AxonLLM's production `TaskClassifier`: regex, keywords, and structural rules | $0 | Unmeasured |
| Laya | Local English decision checkpoint | $0 | Unmeasured |
| Strands Decider | Local 2B decision checkpoint with a pinned Qwen backbone | $0 | Unmeasured |
| GPT-4o Mini | `gpt-4o-mini-2024-07-18`, strict JSON output, temperature 0 | Measured from API usage | Unmeasured |

This measures **routing decisions**, not the quality or cost of a downstream
model's answer. The six labels are `coding`, `reasoning`, `creative_writing`,
`summarization`, `math`, and `general`. All model adapters receive the same
prompt and label descriptions. They never receive case IDs, tags, or expected
answers. The production regex classifier is unchanged.

Ten final-test prompts require **reasoning**: causal explanations, competing
hypotheses, weighing evidence, and evaluating arguments. For example:
“A startup's user engagement doubled after a redesign, but revenue stayed flat.
Walk through the competing interpretations before settling on the most credible
one.” Other reasoning cases deliberately include numbers or summarization terms
to test whether a router follows intent rather than a keyword. The report
includes a per-task table so reasoning performance is visible separately.

## Recorded run — October 2, 2026

[Full report](../../docs/benchmarks/smart-routing-2026-10-02.md) ·
[Case-level JSON](../../docs/benchmarks/smart-routing-2026-10-02.json)

60 public synthetic prompts, three repetitions per router, three excluded
warmup requests per router. All 720 measured requests completed without errors.
Predicted labels were identical across repetitions for every candidate.

| Router | Accuracy | Median latency | p95 latency | API cost / 1,000 | Hardware / electricity |
|---|---:|---:|---:|---:|---|
| Regex / Axon heuristic | 86.7% | 0.355 ms | 0.840 ms | $0 | Unmeasured |
| Laya | 80.0% | 37.795 ms | 45.072 ms | $0 | Unmeasured |
| Strands Decider | 98.3% | 164.123 ms | 225.758 ms | $0 | Unmeasured |
| GPT-4o Mini | 93.3% | 772.824 ms | 1,012.677 ms | $0.050908 | Unmeasured |

On the ten reasoning prompts, routing accuracy was 90% for regex, 80% for Laya,
and 100% for both Strands and GPT-4o Mini. These scores measure recognizing
reasoning requests, not producing correct reasoning answers. Strands was the
most accurate router on this set; regex had the lowest latency.

The host was an **Apple M4 Pro with 48 GiB RAM**, using MPS for both local
models. Laya used float32; Strands used bfloat16. Strands ran the reference
PyTorch causal-convolution implementation because the optional optimized
`causal-conv1d` package was absent. These are observed configurations, not
precision-matched or universally optimized speed comparisons. Laya's runtime
warned about clamping its `choice:11+` temperature; this benchmark has six
options, so the affected bucket was not used. Effective temperatures and
installed versions are in the JSON.

The measured GPT-4o Mini requests cost **$0.0091635**, plus **$0.0001386** for
warmup: **$0.0093021 total** for the recorded run. This excludes the earlier
development smoke check. API usage and costs are recorded per request; API
credentials are absent from both artifacts.

## Run all four

Run these commands from the AxonLLM repository root. Local model dependencies
are isolated from the ordinary gateway installation. Python 3.11–3.13 is
supported by this optional runtime; the recorded local run uses Python 3.13.

```bash
uv sync --frozen --project benchmarks/routing/runtime --python 3.13

# Set OPENAI_API_KEY in your shell or secret manager; never commit it.
uv run --frozen --project benchmarks/routing/runtime axon-benchmark-routing \
  --strategy regex --strategy laya --strategy strands --strategy llm \
  --corpus docs/benchmarks/autorouting-final-test-2026-09-16.jsonl \
  --local-device mps \
  --warmup-requests 3 --repetitions 3 --concurrency 1 \
  --llm-base-url https://api.openai.com/v1 \
  --llm-model gpt-4o-mini-2024-07-18 \
  --llm-api-key-env OPENAI_API_KEY \
  --llm-json-schema \
  --llm-input-cost-per-million 0.15 \
  --llm-output-cost-per-million 0.60 \
  --output-json /tmp/smart-routing.json \
  --output-markdown /tmp/smart-routing.md
```

Use `--local-device cpu` on machines without a supported accelerator, or
`--local-device cuda` for NVIDIA GPUs. The default is CPU. On Apple Silicon,
`mps` requests the Metal accelerator. An unavailable device is an error; the
benchmark does not silently substitute CPU. Initial downloads require internet
access and disk space for model weights. Once the pinned files are cached, add
`--local-files-only` to refuse further downloads. GPT-4o Mini still needs API
access.

To run the zero-API-cost comparison, omit `--strategy llm` and all `--llm-*`
arguments. To run just regex without any model installation:

```bash
uv run --frozen axon-benchmark-routing --strategy regex
```

`regex` is an alias for the existing `heuristic` strategy. Reports retain its
name `axon-heuristic`. Existing `llm` and `hybrid` commands continue to work.
Local model strategies are opt-in; ordinary gateway users do not download them.

## Measurement contract

- **Accuracy and macro F1:** all attempted measured cases count, including
  malformed answers, failed calls, and inputs a local model cannot accept.
- **Latency:** wall-clock time includes tokenization and decision decoding for
  local models, and the HTTP round trip for GPT-4o Mini. GPU work is synchronized
  before and after local inference. Main percentiles cover successful requests;
  JSON also includes percentiles over all attempts.
- **Initialization and warmup:** model loading and three warmup prompts are
  reported separately, including warmup API charges. Neither enters measured
  accuracy, latency, or per-1,000-request API cost.
- **Execution:** local models run sequentially and are released between
  candidates. Concurrency other than one is rejected when comparing local
  models. Each strategy sees the same ordered cases and repetitions.
- **Cost:** local regex, Laya, and Strands make no billable API calls: **$0**.
  GPT-4o Mini cost uses reported token usage and explicit price inputs, or a
  compatible proxy's cost header. Missing usage is unknown, never silently $0.
  Hardware/electricity has its own **Unmeasured** column. Warmup charges are
  additional to the measured-request total. Local model token counters are not
  reported as billable API tokens. The hardware/electricity column refers to
  your local machine; provider infrastructure is already included in API prices.
- **Caching:** per-call LLM nonces discourage proxy response-cache reuse.
  They add a small amount of billed input. Strands' internal shared-prefix cache
  is within a request; it is not a cached answer. Compile/fast paths for Laya
  are disabled. Provider-side prompt caching is separate from response caching.
- **Confidence:** heuristic scores, LLM self-reports, and local option
  probabilities have different meanings. Local raw distributions and native
  confidences are preserved separately; none is claimed calibrated for this
  corpus. Do not compare raw thresholds as equivalent probabilities.
- **Input limits:** Laya rejects truncated states or collapsed option spans;
  Strands uses strict context-window checking. A rejection is an error, not an
  answer generated from an undisclosed shortened prompt.
- **Failures:** errors count as incorrect; a completed run with errors returns
  exit status 1 and still writes reports. Missing dependencies, failed model
  loading, or failed warmup stop the run before a full comparison is claimed.

## Reproducibility and limits

Checkpoints and upstream code are pinned in
[`decision_model_routing.py`](../../src/gateway/decision_model_routing.py) and
[`runtime/uv.lock`](runtime/uv.lock).

| Candidate | Checkpoint revision | Upstream code revision |
|---|---|---|
| Laya, `convaiinnovations/laya` | `55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851` | `fa9a2a7070b1789912a49ae24603bbfb1a78b001` |
| Strands, `StrandsAgents/strands-decider-2B-hobson-v19` | `bb282d786bc251fd4e3068de3ada9ddbb38127cd` | `ddd11994bc451ffc78fa30f43037c291fd4d44b6` |

Strands' base is `Qwen/Qwen3.5-2B-Base` at
`b1485b2fa6dfa1287294f269f5fb618e03d52d7c`. This explicitly pins the inference
backbone; it does not independently establish the exact training-time backbone.
The loader redirects a temporary checkpoint configuration to this local
snapshot without changing cached Strands weights.

The report stores checkpoint identities, installed package versions, device,
dtype, effective checkpoint temperatures, configuration, selected-corpus hash,
implementation and runtime-lock hashes, and per-case predictions. Model weights
are not bundled. Laya and Strands have
their own upstream Apache-2.0 licenses; AxonLLM code remains MIT-0.

The existing final-test set contains **60 public synthetic prompts**, ten per
label. It was originally held out from the September heuristic improvements,
but is now public. Treat this comparison as a reproducible demonstration, not
an unseen production benchmark. Three repetitions produce 180 observations,
not 180 independent prompts. Inspect scenario slices and confusion matrices;
there is no universal winning router.

The Laya candidate is specifically the English checkpoint. Multilingual cases
remain in the common test set and are tagged, so this scope limitation remains
visible. Prompt length, language, label ambiguity, machine load, model precision,
network location, and API load can all change the outcome. No local-model
calibration or heuristic tuning should use the final-test labels. Use the
separate development file for adapter checks and any future tuning.

Before production adoption, add representative private traffic, long context,
your own destination-model policy, repeated runs across devices, and downstream
answer-quality/cost checks. A correct task label alone does not prove that the
selected downstream model is best for that request.

## Sources

- [Laya runtime](https://github.com/NandhaKishorM/laya) and
  [checkpoint](https://huggingface.co/convaiinnovations/laya)
- [Strands Decider runtime](https://github.com/strands-labs/strands-decider) and
  [checkpoint](https://huggingface.co/StrandsAgents/strands-decider-2B-hobson-v19)
- [Official GPT-4o Mini documentation](https://developers.openai.com/api/docs/models/gpt-4o-mini)
  and [structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs).
  Checked October 2, 2026: the documented snapshot supports strict structured
  output; standard token price inputs are $0.15/M input and $0.60/M output.
  Recheck pricing before a new run.
