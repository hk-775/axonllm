# Auto-routing benchmark

- Corpus: 60 labeled prompts, 1 repetition(s)
- Scope: router decision only; downstream generation is excluded

| Strategy | Accuracy | Macro F1 | p50 latency | p95 latency | LLM call rate | Input / output tokens | Router cost / 1k | Errors |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| axon-heuristic | 35.0% | 0.334 | 0.131 ms | 0.218 ms | 0.0% | 0 / 0 | $0.000000 | 0 |

## Accuracy by scenario

| Scenario | axon-heuristic |
|---|---:|
| clear | 66.7% |
| collision | 38.9% |
| english | 35.2% |
| french | 33.3% |
| implicit | 16.7% |
| multilingual | 33.3% |
