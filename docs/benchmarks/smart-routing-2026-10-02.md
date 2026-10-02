# Auto-routing benchmark

- Corpus: 60 labeled prompts, 3 repetition(s)
- Scope: router decision only; downstream generation is excluded
- Cost: API charges only. Regex and local decision models have $0 API cost; hardware and electricity are excluded.
- Latency: successful requests, after warmup; errors count as incorrect.
- LLM model: `gpt-4o-mini-2024-07-18`

| Strategy | Accuracy | Macro F1 | p50 latency | p95 latency | LLM call rate | API input / output tokens | API cost / 1k | Hardware / electricity cost | Errors |
|---|---:|---:|---:|---:|---:|---:|---:|---|---:|
| axon-heuristic | 86.7% | 0.875 | 0.355 ms | 0.840 ms | 0.0% | 0 / 0 | $0 | Unmeasured | 0 |
| laya | 80.0% | 0.779 | 37.795 ms | 45.072 ms | 0.0% | 0 / 0 | $0 | Unmeasured | 0 |
| strands | 98.3% | 0.983 | 164.123 ms | 225.758 ms | 0.0% | 0 / 0 | $0 | Unmeasured | 0 |
| llm-router | 93.3% | 0.936 | 772.824 ms | 1012.677 ms | 100.0% | 51802 / 2322 | $0.050908 | Unmeasured | 0 |

## Compared with AxonLLM heuristic routing

- **laya:** -6.7 percentage points accuracy; +44.2 ms p95 latency; API-only break-even downstream savings $0 per request; cost per additional correct route n/a.
- **strands:** +11.7 percentage points accuracy; +224.9 ms p95 latency; API-only break-even downstream savings $0 per request; cost per additional correct route $0.
- **llm-router:** +6.7 percentage points accuracy; +1011.8 ms p95 latency; API-only break-even downstream savings $0.000051 per request; cost per additional correct route $0.000764.

## Initialization and warmup (excluded from measured requests)

| Strategy | Initialization | Warmup requests | Warmup API cost |
|---|---:|---:|---:|
| axon-heuristic | 0.0 ms | 3 | $0 |
| laya | 3955.1 ms | 3 | $0 |
| strands | 1563.5 ms | 3 | $0 |
| llm-router | 0.1 ms | 3 | $0.000139 |

## Accuracy by task

| Task | axon-heuristic | laya | strands | llm-router |
|---|---:|---:|---:|---:|
| coding | 70.0% (21/30) | 80.0% (24/30) | 100.0% (30/30) | 90.0% (27/30) |
| reasoning | 90.0% (27/30) | 80.0% (24/30) | 100.0% (30/30) | 100.0% (30/30) |
| creative_writing | 90.0% (27/30) | 90.0% (27/30) | 90.0% (27/30) | 100.0% (30/30) |
| summarization | 90.0% (27/30) | 30.0% (9/30) | 100.0% (30/30) | 100.0% (30/30) |
| math | 80.0% (24/30) | 100.0% (30/30) | 100.0% (30/30) | 90.0% (27/30) |
| general | 100.0% (30/30) | 100.0% (30/30) | 100.0% (30/30) | 80.0% (24/30) |

## Accuracy by scenario

| Scenario | axon-heuristic | laya | strands | llm-router |
|---|---:|---:|---:|---:|
| clear | 100.0% | 75.0% | 100.0% | 100.0% |
| collision | 83.3% | 66.7% | 100.0% | 83.3% |
| english | 87.5% | 81.2% | 97.9% | 91.7% |
| german | 83.3% | 83.3% | 100.0% | 100.0% |
| implicit | 83.3% | 91.7% | 95.8% | 91.7% |
| multilingual | 83.3% | 75.0% | 100.0% | 100.0% |
| spanish | 83.3% | 66.7% | 100.0% | 100.0% |
