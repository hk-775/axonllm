# Auto-routing benchmark

- Corpus: 120 labeled prompts, 3 repetition(s)
- Scope: router decision only; downstream generation is excluded
- Cost: API charges only. Regex and local decision models have $0 API cost; hardware and electricity are excluded.
- Latency: successful requests, after warmup; errors count as incorrect.
- LLM model: `gpt-4o-mini-2024-07-18`

| Strategy | Accuracy | Macro F1 | p50 latency | p95 latency | LLM call rate | API input / output tokens | API cost / 1k | Hardware / electricity cost | Errors |
|---|---:|---:|---:|---:|---:|---:|---:|---|---:|
| axon-heuristic | 56.7% | 0.556 | 0.305 ms | 1.436 ms | 0.0% | 0 / 0 | $0 | Unmeasured | 0 |
| laya | 75.8% | 0.762 | 38.280 ms | 48.143 ms | 0.0% | 0 / 0 | $0 | Unmeasured | 0 |
| strands | 89.2% | 0.889 | 164.527 ms | 214.931 ms | 0.0% | 0 / 0 | $0 | Unmeasured | 0 |
| llm-router | 97.5% | 0.975 | 575.562 ms | 910.457 ms | 100.0% | 104432 / 4632 | $0.051233 | Unmeasured | 0 |

## Compared with AxonLLM heuristic routing

- **laya:** +19.2 percentage points accuracy; +46.7 ms p95 latency; API-only break-even downstream savings $0 per request; cost per additional correct route $0.
- **strands:** +32.5 percentage points accuracy; +213.5 ms p95 latency; API-only break-even downstream savings $0 per request; cost per additional correct route $0.
- **llm-router:** +40.8 percentage points accuracy; +909.0 ms p95 latency; API-only break-even downstream savings $0.000051 per request; cost per additional correct route $0.000125.

## Initialization and warmup (excluded from measured requests)

| Strategy | Initialization | Warmup requests | Warmup API cost |
|---|---:|---:|---:|
| axon-heuristic | 0.0 ms | 3 | $0 |
| laya | 241.8 ms | 3 | $0 |
| strands | 1369.3 ms | 3 | $0 |
| llm-router | 0.1 ms | 3 | $0.000139 |

## Accuracy by task

| Task | axon-heuristic | laya | strands | llm-router |
|---|---:|---:|---:|---:|
| coding | 90.0% (54/60) | 95.0% (57/60) | 100.0% (60/60) | 90.0% (54/60) |
| reasoning | 25.0% (15/60) | 60.0% (36/60) | 70.0% (42/60) | 95.0% (57/60) |
| creative_writing | 45.0% (27/60) | 95.0% (57/60) | 95.0% (57/60) | 100.0% (60/60) |
| summarization | 70.0% (42/60) | 65.0% (39/60) | 100.0% (60/60) | 100.0% (60/60) |
| math | 10.0% (6/60) | 65.0% (39/60) | 100.0% (60/60) | 100.0% (60/60) |
| general | 100.0% (60/60) | 75.0% (45/60) | 70.0% (42/60) | 100.0% (60/60) |

## Accuracy by scenario

| Scenario | axon-heuristic | laya | strands | llm-router |
|---|---:|---:|---:|---:|
| arts_and_media | 37.5% | 62.5% | 83.3% | 95.8% |
| education_and_history | 50.0% | 50.0% | 75.0% | 100.0% |
| english | 56.7% | 75.8% | 89.2% | 97.5% |
| explicit | 70.0% | 90.0% | 93.3% | 100.0% |
| health_and_wellbeing | 66.7% | 100.0% | 91.7% | 91.7% |
| home_and_travel | 50.0% | 66.7% | 100.0% | 100.0% |
| implicit | 46.7% | 53.3% | 90.0% | 100.0% |
| keyword_collision | 56.7% | 80.0% | 90.0% | 93.3% |
| public_services | 75.0% | 83.3% | 91.7% | 100.0% |
| quoted_distraction | 53.3% | 80.0% | 83.3% | 96.7% |
| retail_and_support | 66.7% | 75.0% | 100.0% | 91.7% |
| science_and_environment | 37.5% | 100.0% | 100.0% | 100.0% |
| software_and_data | 100.0% | 25.0% | 0.0% | 100.0% |
| sports_and_leisure | 65.0% | 80.0% | 95.0% | 100.0% |
| workplace_and_business | 50.0% | 83.3% | 91.7% | 100.0% |
