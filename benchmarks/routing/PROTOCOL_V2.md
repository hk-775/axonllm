# Expanded smart-routing protocol — fixed before scoring

Date: October 2, 2026. Authoring split seed: 775.

This experiment measures task-category routing, including recognizing requests
that need reasoning. It does not evaluate downstream reasoning answers.

## Dataset plan

Generate 1,200 English prompts: six classes × ten domains × five substantive
scenario families × four variants. Variants are explicit requests, implicit
intent, keyword collisions, and quoted distractions. Prompts include their own
source content and must fit a short-request use case (10–160 words).

Use GPT-4.1 (`gpt-4.1-2025-04-14`) for authoring and Claude Sonnet 4.6
(`claude-sonnet-4-6`) for a blind label audit. The auditor receives opaque IDs and
prompts, never intended labels, family IDs, tags, or author rationales.
Review batches mix labels and domains. Revise disputed/ambiguous prompts before
freezing; retain the full dispute log. Neither authors nor auditors see candidate
predictions. Agreement is an annotation check, not proof of objectively correct
labels or human review. Filtering for agreement can favor easier/consensus cases.
The generator shares a provider with one candidate; cross-provider review does
not eliminate model-family bias.

Keep each four-variant family wholly in one split. Choose one of five families
per class/domain for development: 240 prompts in 60 families. The remaining
960 prompts in 240 families form the test set. Reject exact normalized duplicates
and audit four-word-shingle Jaccard overlap ≥0.65 across families and against the
earlier packaged/generated corpora. This lexical check cannot prove semantic
novelty or absence from model training.

Freeze data, audit files, and hashes before any candidate sees a new test prompt.
Record checkpoint pins and hashes of the production classifier, local adapters,
and routing system prompt. Do not optimize prompts, thresholds, temperatures,
precision, or regex rules against new test results. The development split is
provided for future work; the fixed routers require no new fitting.

## Runs and costs

Primary run: all four candidates, all 960 test prompts, one pass per candidate,
concurrency one, fixed shuffled order shared by all candidates, three excluded
warmups. Local Laya English and Strands use the same pinned checkpoints and MPS
device as the pilot. GPT-4o Mini stays `gpt-4o-mini-2024-07-18`, temperature zero,
strict JSON schema and cache-isolating nonces. The order is regex, Laya, Strands,
GPT-4o Mini. Models load sequentially and are released between candidates.

Secondary repeatability panel: deterministically sample five test families per
class (120 prompts), then run three repetitions. Report it separately; do not add
repeat observations to the primary accuracy denominator.

Score errors as incorrect. Report accuracy, macro F1, per-class and per-domain
accuracy, challenge-variant accuracy, confusion matrices, p50/p95 latency,
initialization, API usage and API cost. Successful-request latency and
all-attempt latency remain distinguishable. Regex and local models have $0 API
cost. Hardware and electricity have a separate **Unmeasured** column. Generation,
label-audit and warmup usage are separate from router cost.

## Statistical analysis

The inferential unit is a scenario family, not each correlated variant and not
each repeated call. Use 10,000 seeded, class-stratified bootstrap resamples of the
40 test families per class, carrying all four variants together. Resample the
same family indices for every router (paired comparison).

Report descriptive 95% percentile intervals for each candidate's accuracy and
macro F1. For all six pairwise accuracy differences, also report Bonferroni
family-wise 95% intervals (individual confidence level 99.1667%). Do not call a
pair separated if that adjusted interval includes zero. Rank estimates on this
corpus without treating a small point-estimate lead as universal superiority.
Intervals describe variation under this synthetic family sampling scheme;
they do not establish representativeness of real users or deployment traffic.

## Publication boundaries

Publish the dataset, final and disputed annotations, protocol, frozen manifest,
model/runtime identities, raw predictions, analysis code, and aggregate report.
Label all pages **model-reviewed synthetic benchmark**. Report unfavorable
results and failures without tuning them away. Preserve the original 60-prompt
pilot separately. The larger set provides stronger evidence about this specified
short English routing task, not general agent reasoning, multilingual ability,
long-context routing, calibration, or production outcome quality.

AWS architecture decision: this change adds local benchmarking and direct
external model APIs; it creates no AWS resources or application topology changes.
AxonLLM's existing editable AWS services reference and rendered diagram remain
in `docs/aws-services-architecture.drawio` and
`docs/images/aws-services-architecture.png`. GitHub Pages publishes static
artifacts only; it cannot run models or access API credentials.
