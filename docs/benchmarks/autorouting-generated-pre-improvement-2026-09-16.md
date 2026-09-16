# Auto-routing benchmark

- Corpus: 120 labeled prompts, 1 repetition(s)
- Scope: router decision only; downstream generation is excluded

| Strategy | Accuracy | Macro F1 | p50 latency | p95 latency | LLM call rate | Input / output tokens | Router cost / 1k | Errors |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| axon-heuristic | 36.7% | 0.364 | 0.137 ms | 0.233 ms | 0.0% | 0 / 0 | $0.000000 | 0 |
| llm-router | 98.3% | 0.987 | 2634.709 ms | 6126.539 ms | 100.0% | 35450 / 11909 | $0.051929 | 1 |
| hybrid-0.3 | 95.0% | 0.955 | 2690.288 ms | 6054.009 ms | 77.5% | 27377 / 9560 | $0.041011 | 1 |

## Compared with AxonLLM heuristic routing

- **llm-router:** +61.7 percentage points accuracy; +6126.3 ms p95 latency; break-even downstream savings $0.000052 per request; cost per additional correct route $0.000084.
- **hybrid-0.3:** +58.3 percentage points accuracy; +6053.8 ms p95 latency; break-even downstream savings $0.000041 per request; cost per additional correct route $0.000070.

## Accuracy by scenario

| Scenario | axon-heuristic | llm-router | hybrid-0.3 |
|---|---:|---:|---:|
| clear | 62.5% | 100.0% | 95.8% |
| collision | 40.0% | 100.0% | 90.0% |
| english | 37.3% | 98.0% | 94.1% |
| french | 33.3% | 100.0% | 100.0% |
| german | 16.7% | 100.0% | 100.0% |
| implicit | 22.9% | 95.8% | 95.8% |
| multilingual | 33.3% | 100.0% | 100.0% |
| spanish | 50.0% | 100.0% | 100.0% |
