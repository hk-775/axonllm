# Action first regex results

**Follow-up revision; original four-router results remain unchanged.**
Rules were frozen before reserved validation. The original 960-prompt test
is reused for regression analysis; these are not fresh confirmatory results.

| Partition | Original | Revised | Improved cases | Regressions |
| --- | ---: | ---: | ---: | ---: |
| development | 55/120 (45.83%) | 120/120 (100.00%) | 65 | 0 |
| reserved-validation | 55/120 (45.83%) | 74/120 (61.67%) | 19 | 0 |
| reused-test | 478/960 (49.79%) | 625/960 (65.10%) | 162 | 15 |

## Reused test details

| Task | Original correct | Revised correct | Cases |
| --- | ---: | ---: | ---: |
| coding | 102 | 116 | 160 |
| reasoning | 35 | 90 | 160 |
| creative_writing | 58 | 74 | 160 |
| summarization | 101 | 119 | 160 |
| math | 28 | 75 | 160 |
| general | 154 | 151 | 160 |

Accuracy change: **+15.31 percentage points**;
paired family bootstrap 95% interval [+12.71, +17.92] points.
This interval does not include selection bias from reusing a published corpus.

| Local timing diagnostic | Median ms | p95 ms | API cost | Hardware/electricity |
| --- | ---: | ---: | ---: | --- |
| original-regex | 0.3330 | 1.3731 | $0 | Unmeasured |
| action-first-regex | 0.1619 | 0.6658 | $0 | Unmeasured |

Timing uses paired local calls with randomized order under uncontrolled host
load. It is not a new four-model latency benchmark.

## Inspect and reproduce

The JSON recording contains every prediction, rule trace, timing, improvement,
and regression. `REGEX_REVISION.md` describes the development split and limitations.
Use `IntentRegexClassifier` explicitly to try this candidate; the gateway default
and original published classifier are unchanged.
