# Routing corpus v2 — data card

**Purpose:** a reproducible, controlled comparison of short English task-routing
decisions. It is not a production-traffic sample or an evaluation of the answers
produced by a downstream model.

## Composition

The frozen corpus has 1,200 prompts: six labels × ten domains × five scenario
families × four variants. Each label has 200 prompts overall, 40 in development,
and 160 in test. Each domain has 120 overall, 24 in development, and 96 in test.

Labels: coding, reasoning, creative writing, summarization, math, and general.
Domains: software/data, workplace/business, home/travel, science/environment,
education/history, public services, arts/media, retail/support, sports/leisure,
and health/wellbeing.

The four requested variants are explicit requests, implicit intent, keyword
collisions, and quoted distractions. Variant tags describe authoring
instructions; the independent audit validates task labels, not an objective
difficulty level. Prompts must contain 10–160 whitespace-delimited words.
Source passages and code snippets are synthetic and supplied within prompts.

One family per label/domain goes to development, chosen with seed 775. All four
variants stay together. This gives 240 development prompts in 60 families and
960 test prompts in 240 families. The repeatability panel samples five test
families per label, totaling 120 prompts; it is not a new set of independent
examples.

## Authorship and annotation

GPT-4.1 (`gpt-4.1-2025-04-14`) authored the candidates. Claude Sonnet 4.6
(`claude-sonnet-4-6`) independently assigned task labels and ambiguity flags.
Reviewers received only opaque transport IDs and prompts, with the shared label
policy. They did not receive gold labels, rationales, domains, or family IDs.
Initial review batches mixed tasks and domains.

The first pass flagged **240 prompts** for label disagreement, ambiguity, or
length acceptance problems. The authoring model revised disputed cases before
candidate inference. Changed prompts were reviewed again; exact unchanged
prompts could reuse their prior review. Transport-ID errors were rechecked,
never guessed or matched by position. Audit and revision artifacts retain this
history.

These are **AI-authored, model-reviewed labels**. No human annotation, human
inter-annotator agreement, or independent expert review is claimed. Consensus
filtering can favor easier cases. Sharing a provider between the generator and
GPT-4o Mini creates a possible model-family bias, even with a separate-provider
reviewer. Readers should inspect labels and disputed examples before using the
scores to make consequential deployment decisions.

## Leakage and duplicates

Model adapters receive the prompt and shared routing taxonomy only. Case IDs,
expected labels, audit reasons, and tags remain in the scorer. No router fitting,
prompt optimization, or threshold tuning uses this new test set.

Exact normalized duplicates are rejected. Cross-family and legacy-corpus
overlap is checked using four-word-shingle Jaccard similarity with threshold
0.65. The legacy comparison includes the packaged corpus and the earlier
120-prompt generated corpus. This is a lexical check, not proof of semantic
independence or absence from training data. Related variants deliberately remain
within a family.

## Files and integrity

All paths below are under `data/v2/`:

| File | Contents |
|---|---|
| `development.jsonl` | 240 prompts, family IDs, labels, tags, and author rationales |
| `test.jsonl` | 960 prompts with the same schema |
| `label-review.json` | Final blind-review labels, ambiguity flags, reasons and request hashes |
| `pre-freeze-revisions.json` | Original disputed prompts and audit feedback from every revision round |
| `duplicate-audit.json` | Duplicate-check method, threshold and findings |
| `authoring-usage.json` | Usage reported by retained generation/revision/review responses |
| `manifest.json` | Freeze timestamp, file hashes, split counts and locked router identities |

The usage file is provenance, not a billing statement. Interrupted authoring
requests may not have a retained usage response. Authoring/audit calls are
separate from the API costs reported for the four routers.

`python -m benchmarks.routing.corpus validate` verifies the frozen hashes,
balanced classes, complete families, split separation, and final label-review
agreement. Validation makes no network calls. Do not overwrite a frozen version
to fix a label after seeing model results; publish a versioned correction.

## Interpretation and reuse

Use scenario families as the unit for uncertainty analysis. The published
bootstrap resamples families within class and pairs those resamples across
routers. Related variants and repeated calls are not independent observations.
Common authoring patterns and domain-level dependencies can remain beyond the
family grouping, and the synthetic design is not representative of real users.

This set can substantiate claims about measured behavior on these specified
short English tasks. It does not substantiate universal superiority, production
reliability, multilingual coverage, long-context routing, calibrated confidence,
or general reasoning competence. Add representative traffic and independent human
label review for those claims.

Dataset text, annotations, and project code use the repository's **MIT-0**
license. The separately downloaded model weights and upstream runtimes retain
their own licenses; none are redistributed in this corpus.
