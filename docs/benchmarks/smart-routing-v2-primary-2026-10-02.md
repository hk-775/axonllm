# Auto-routing benchmark

- Corpus: 960 labeled prompts, 1 repetition(s)
- Scope: router decision only; downstream generation is excluded
- Cost: API charges only. Regex and local decision models have $0 API cost; hardware and electricity are excluded.
- Latency: successful requests, after warmup; errors count as incorrect.
- LLM model: `gpt-4o-mini-2024-07-18`

| Strategy | Accuracy | Macro F1 | p50 latency | p95 latency | LLM call rate | API input / output tokens | API cost / 1k | Hardware / electricity cost | Errors |
|---|---:|---:|---:|---:|---:|---:|---:|---|---:|
| axon-heuristic | 49.8% | 0.507 | 0.338 ms | 1.395 ms | 0.0% | 0 / 0 | $0 | Unmeasured | 0 |
| laya | 66.2% | 0.662 | 37.625 ms | 46.527 ms | 0.0% | 0 / 0 | $0 | Unmeasured | 0 |
| strands | 91.4% | 0.914 | 164.176 ms | 207.516 ms | 0.0% | 0 / 0 | $0 | Unmeasured | 0 |
| llm-router | 91.2% | 0.911 | 736.222 ms | 966.043 ms | 100.0% | 278037 / 12416 | $0.051203 | Unmeasured | 0 |

## Compared with AxonLLM heuristic routing

- **laya:** +16.5 percentage points accuracy; +45.1 ms p95 latency; API-only break-even downstream savings $0 per request; cost per additional correct route $0.
- **strands:** +41.6 percentage points accuracy; +206.1 ms p95 latency; API-only break-even downstream savings $0 per request; cost per additional correct route $0.
- **llm-router:** +41.5 percentage points accuracy; +964.6 ms p95 latency; API-only break-even downstream savings $0.000051 per request; cost per additional correct route $0.000124.

## Initialization and warmup (excluded from measured requests)

| Strategy | Initialization | Warmup requests | Warmup API cost |
|---|---:|---:|---:|
| axon-heuristic | 0.0 ms | 3 | $0 |
| laya | 4045.3 ms | 3 | $0 |
| strands | 1627.9 ms | 3 | $0 |
| llm-router | 0.1 ms | 3 | $0.000139 |

## Accuracy by task

| Task | axon-heuristic | laya | strands | llm-router |
|---|---:|---:|---:|---:|
| coding | 63.7% (102/160) | 81.9% (131/160) | 98.8% (158/160) | 91.9% (147/160) |
| reasoning | 21.9% (35/160) | 48.1% (77/160) | 79.4% (127/160) | 96.9% (155/160) |
| creative_writing | 36.2% (58/160) | 90.0% (144/160) | 95.0% (152/160) | 98.1% (157/160) |
| summarization | 63.1% (101/160) | 53.1% (85/160) | 95.6% (153/160) | 98.1% (157/160) |
| math | 17.5% (28/160) | 46.2% (74/160) | 95.6% (153/160) | 93.8% (150/160) |
| general | 96.2% (154/160) | 78.1% (125/160) | 83.8% (134/160) | 68.8% (110/160) |

## Accuracy by scenario

| Scenario | axon-heuristic | laya | strands | llm-router |
|---|---:|---:|---:|---:|
| arts_and_media | 59.4% | 65.6% | 93.8% | 90.6% |
| education_and_history | 51.0% | 55.2% | 95.8% | 82.3% |
| english | 49.8% | 66.2% | 91.4% | 91.2% |
| explicit | 60.8% | 83.8% | 96.7% | 95.0% |
| health_and_wellbeing | 54.2% | 67.7% | 93.8% | 92.7% |
| home_and_travel | 45.8% | 69.8% | 94.8% | 95.8% |
| implicit | 32.9% | 55.8% | 91.7% | 93.8% |
| keyword_collision | 51.7% | 64.2% | 88.3% | 85.8% |
| public_services | 59.4% | 66.7% | 92.7% | 94.8% |
| quoted_distraction | 53.8% | 61.3% | 88.8% | 90.4% |
| retail_and_support | 40.6% | 65.6% | 93.8% | 89.6% |
| science_and_environment | 50.0% | 70.8% | 93.8% | 83.3% |
| software_and_data | 44.8% | 57.3% | 68.8% | 89.6% |
| sports_and_leisure | 45.8% | 68.8% | 91.7% | 94.8% |
| workplace_and_business | 46.9% | 75.0% | 94.8% | 99.0% |
