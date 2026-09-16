# Auto-routing benchmark

- Corpus: 72 labeled prompts, 1 repetition(s)
- Scope: router decision only; downstream generation is excluded

| Strategy | Accuracy | Macro F1 | p50 latency | p95 latency | LLM call rate | Input / output tokens | Router cost / 1k | Errors |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| axon-heuristic | 98.6% | 0.986 | 0.081 ms | 0.112 ms | 0.0% | 0 / 0 | $0.000000 | 0 |
| llm-router | 94.4% | 0.945 | 1345.748 ms | 5132.984 ms | 100.0% | 19801 / 5825 | $0.044897 | 0 |
| hybrid-0.3 | 97.2% | 0.972 | 0.212 ms | 2973.273 ms | 16.7% | 3275 / 999 | $0.007574 | 0 |

## Compared with AxonLLM heuristic routing

- **llm-router:** -4.2 percentage points accuracy; +5132.9 ms p95 latency; break-even downstream savings $0.000045 per request; cost per additional correct route n/a.
- **hybrid-0.3:** -1.4 percentage points accuracy; +2973.2 ms p95 latency; break-even downstream savings $0.000008 per request; cost per additional correct route n/a.

## Accuracy by scenario

| Scenario | axon-heuristic | llm-router | hybrid-0.3 |
|---|---:|---:|---:|
| ambiguous | 83.3% | 66.7% | 83.3% |
| clear | 100.0% | 100.0% | 100.0% |
| code-block | 100.0% | 100.0% | 100.0% |
| collision | 100.0% | 88.9% | 88.9% |
| english | 100.0% | 95.7% | 97.8% |
| ethical | 100.0% | 100.0% | 100.0% |
| implicit | 100.0% | 94.1% | 100.0% |
| infrastructure | 100.0% | 100.0% | 100.0% |
| logic | 100.0% | 100.0% | 100.0% |
| marketing | 100.0% | 100.0% | 100.0% |
| multilingual | 100.0% | 100.0% | 100.0% |
| notation | 75.0% | 75.0% | 75.0% |
| operations | 100.0% | 100.0% | 100.0% |
| planning | 100.0% | 100.0% | 100.0% |
| proof | 100.0% | 100.0% | 100.0% |
| short | 100.0% | 100.0% | 100.0% |
| software | 100.0% | 0.0% | 100.0% |
| statistics | 100.0% | 100.0% | 100.0% |
| translation | 100.0% | 100.0% | 100.0% |
| travel | 100.0% | 100.0% | 100.0% |
