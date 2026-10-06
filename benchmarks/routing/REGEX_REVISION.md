# Action first regex revision

The original regex classifier scored 478/960 (49.79%) in the published
four-router comparison. This follow-up develops a separate deterministic
candidate. It does not replace that classifier, rewrite its results, or change
the binary economy/capable routing policy used by Practical Eval Lab.

## Changes and scope

`IntentRegexClassifier` prioritizes requested actions over accumulated topic
words. It recognizes implicit summaries, quantitative questions, software
explanations, creative artifacts, and comparisons. It masks straight and curly
quotations, code quotations, and negated clauses; explicit corrections such as
"Actually, just..." can replace an earlier request. Radio scripts and software
scripts have different action rules. The original classifier remains a fallback.

The returned confidence is a rule-precedence score, not a calibrated probability.
This is a six-category English task classifier, not a reasoning engine or a
prompt-injection defense. Nested quotations, indirect or competing requests,
and new domains can still break its lexical assumptions.

## Development and evaluation

The original 240 development prompts contain 60 scenario families. Within each
label, sort its ten family IDs lexically: the first five families (20 prompts)
are used for revision development, and the remaining five for reserved
validation. Across six labels this gives 120 development and 120 validation
prompts, with no family overlap. All wording variants stay together.

Only the 120 development prompts and newly authored behavior tests are used to
revise the rules. Commit the implementation, runner, protocol, source hashes,
and partition IDs before opening validation results. After freezing, evaluate
both candidates once on the 120 reserved validation prompts and on the original
960 test prompts. Do not change rules in response to those outcomes within this
revision.

The original corpus and benchmark results were already public. Reserved
validation means excluded from this revision's rule tuning; it is not a claim
that the examples could never have been encountered. The 960-prompt comparison
is explicitly a **reused-test regression evaluation**, not a fresh confirmatory
test or independent reproduction. A new independently authored dataset is needed
before claiming broader generalization.

Report accuracy, macro F1, per-label and per-wording performance, improvements,
regressions, and every prediction. Include a paired 10,000-resample bootstrap of
whole scenario families within labels, seed 775. Its intervals describe sampling
uncertainty conditional on this corpus; they do not account for tuning or
selection bias. With two candidates there is one pairwise comparison.

Time both local candidates in the same process, randomizing their order per
case, after ten development warmups. Report median and p95 elapsed time as a
local diagnostic; host load is uncontrolled and no hosted-model latency is
remeasured. API cost is $0 for both; hardware/electricity cost is Unmeasured.
Verify that the baseline's recomputed 960 labels match the original recording.

## Reproduce

```sh
uv sync --locked --extra dev
uv run --locked python -m benchmarks.routing.regex_revision train
# Maintainer freezes and commits before the first validation evaluation:
uv run --locked python -m benchmarks.routing.regex_revision freeze
uv run --locked python -m benchmarks.routing.regex_revision evaluate \
  --output /tmp/axon-regex-reproduction
```

The checked-in freeze is immutable for this revision. The evaluator verifies
all frozen file hashes and refuses to overwrite an existing output directory.
The original four-router benchmark remains available at `benchmark.html`.
