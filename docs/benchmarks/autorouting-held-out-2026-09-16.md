# Auto-routing benchmark

- Corpus: 60 labeled prompts, 1 repetition(s)
- Scope: router decision only; downstream generation is excluded

| Strategy | Accuracy | Macro F1 | p50 latency | p95 latency | LLM call rate | Input / output tokens | Router cost / 1k | Errors |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| axon-heuristic | 86.7% | 0.875 | 0.307 ms | 0.896 ms | 0.0% | 0 / 0 | $0.000000 | 0 |
| llm-router | 91.7% | 0.926 | 657.668 ms | 843.997 ms | 100.0% | 13961 / 1018 | $0.045082 | 1 |
| hybrid-0.3 | 95.0% | 0.950 | 1.457 ms | 851.265 ms | 28.3% | 3796 / 210 | $0.011590 | 0 |

## Compared with AxonLLM heuristic routing

- **llm-router:** +5.0 percentage points accuracy; +843.1 ms p95 latency; break-even downstream savings $0.000045 per request; cost per additional correct route $0.000902.
- **hybrid-0.3:** +8.3 percentage points accuracy; +850.4 ms p95 latency; break-even downstream savings $0.000012 per request; cost per additional correct route $0.000139.

## Accuracy by scenario

| Scenario | axon-heuristic | llm-router | hybrid-0.3 |
|---|---:|---:|---:|
| clear | 100.0% | 91.7% | 100.0% |
| collision | 83.3% | 83.3% | 83.3% |
| english | 87.5% | 89.6% | 93.8% |
| german | 83.3% | 100.0% | 100.0% |
| implicit | 83.3% | 91.7% | 95.8% |
| multilingual | 83.3% | 100.0% | 100.0% |
| spanish | 83.3% | 100.0% | 100.0% |
