# Auto-routing benchmark

- Corpus: 60 labeled prompts, 1 repetition(s)
- Scope: router decision only; downstream generation is excluded

| Strategy | Accuracy | Macro F1 | p50 latency | p95 latency | LLM call rate | Input / output tokens | Router cost / 1k | Errors |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| axon-heuristic | 100.0% | 1.000 | 0.350 ms | 0.794 ms | 0.0% | 0 / 0 | $0.000000 | 0 |

## Accuracy by scenario

| Scenario | axon-heuristic |
|---|---:|
| clear | 100.0% |
| collision | 100.0% |
| english | 100.0% |
| french | 100.0% |
| implicit | 100.0% |
| multilingual | 100.0% |
