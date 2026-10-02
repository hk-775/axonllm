# Expanded smart-routing evidence

Model-reviewed synthetic benchmark: 960 test prompts in 240 scenario families; 240 development prompts kept separate. Routing decisions only.

| Router | Accuracy (95% family interval) | Macro F1 | Median | p95 | API cost / 1,000 | Hardware/electricity | Errors |
|---|---:|---:|---:|---:|---:|---|---:|
| axon-heuristic | 49.79% (46.9%–52.7%) | 0.507 | 0.34 ms | 1.40 ms | $0 | Unmeasured | 0 |
| laya | 66.25% (63.3%–69.2%) | 0.662 | 37.63 ms | 46.53 ms | $0 | Unmeasured | 0 |
| strands | 91.35% (89.2%–93.3%) | 0.914 | 164.18 ms | 207.52 ms | $0 | Unmeasured | 0 |
| llm-router | 91.25% (88.9%–93.5%) | 0.911 | 736.22 ms | 966.04 ms | $0.051203 | Unmeasured | 0 |

## Paired accuracy differences

Intervals account for all six comparisons (Bonferroni family-wise 95%). Positive values favor the candidate. An interval crossing zero is inconclusive.

| Candidate vs baseline | Difference | Adjusted interval | Separated |
|---|---:|---:|---|
| laya vs axon-heuristic | +16.46 pp | +11.35 to +21.67 pp | Yes |
| strands vs axon-heuristic | +41.56 pp | +37.08 to +46.15 pp | Yes |
| llm-router vs axon-heuristic | +41.46 pp | +36.56 to +46.35 pp | Yes |
| strands vs laya | +25.10 pp | +20.83 to +29.27 pp | Yes |
| llm-router vs laya | +25.00 pp | +20.52 to +29.48 pp | Yes |
| llm-router vs strands | -0.10 pp | -4.06 to +3.65 pp | No |

## Reasoning-task routing

- axon-heuristic: 21.9% (35/160), family interval 16.2%–27.5%.
- laya: 48.1% (77/160), family interval 40.6%–55.6%.
- strands: 79.4% (127/160), family interval 71.9%–86.2%.
- llm-router: 96.9% (155/160), family interval 94.4%–99.4%.

## Separate repeatability panel

- axon-heuristic: 0/120 prompts changed predicted label across three repetitions.
- laya: 0/120 prompts changed predicted label across three repetitions.
- strands: 0/120 prompts changed predicted label across three repetitions.
- llm-router: 0/120 prompts changed predicted label across three repetitions.

## Limits

- Synthetic, model-reviewed labels; no human annotation claim.
- Variants are correlated; intervals resample whole scenario families.
- Consensus filtering and generator/reviewer model-family bias remain.
- No production traffic, long-context or multilingual generalization claim.
- Laya float32 and Strands bfloat16; Strands reference causal-convolution kernel.

These intervals describe this synthetic family sampling scheme, not real-world representativeness.
