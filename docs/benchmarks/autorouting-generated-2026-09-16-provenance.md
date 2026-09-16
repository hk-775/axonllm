# Generated auto-routing corpus provenance

Date: 2026-09-16

## Corpus

- 120 unique prompts
- 20 prompts for each of six labels: coding, reasoning, creative writing,
  summarization, math, and general
- 24 clear cases
- 48 implicit-intent cases
- 30 keyword-collision cases
- 18 multilingual cases
- 102 English, 6 French, 6 German, and 6 Spanish prompts

Claude Sonnet generated the candidate corpus. The labels and prompts received
manual review, followed by an independent GPT-4.1 review that returned zero
flags. An earlier candidate corpus was rejected before freezing because its
labels were too ambiguous; it is not included in the benchmark.

## Frozen split

The split was defined before per-case classifier tuning:

- odd-numbered case IDs became the 60-case development set;
- even-numbered case IDs became the 60-case final test set.

Each split contains 10 cases per label. Only development-set failures informed
classifier changes. The classifier was locked before final-test errors were
inspected, and no classifier changes were made afterward.

## SHA-256

```text
39ecb1b767d43854f0c023175b6bcc5e7ccf3b8a0090649f20b3b6292865f860  autorouting-generated-2026-09-16.jsonl
c44aa8cd3b388309af395ca429daa6615ca86a00c9f9297dad9d05d2a7ef6e6d  autorouting-development-2026-09-16.jsonl
48d24b4f58aa3934e327a0af08ed5459323bef01ddc1926da59119021ae39565  autorouting-final-test-2026-09-16.jsonl
```

## Evaluation sequence

1. Run the original heuristic on all 120 prompts before improvements.
2. Freeze the odd/even development and final-test split.
3. Inspect development failures only.
4. Add generalized intent families; do not add case IDs or complete prompt
   strings to the classifier.
5. Reach 100% on development and lock the classifier.
6. Run the final 60 cases and publish every case-level decision.
7. Run LLM-only and confidence-gated hybrid comparisons on the same final set.

The exact development and final reports are stored beside this file.
