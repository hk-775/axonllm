"""Paired scenario-family bootstrap analysis for the frozen routing benchmark."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from itertools import combinations
import hashlib
import json
import math
from pathlib import Path
import random

from benchmarks.routing.corpus import DATA, ROOT, SEED, digest, read_json, verify_frozen, write_json
from benchmarks.routing.run_expanded import panel_cases
from src.gateway.autorouting_benchmark import (
    CaseResult, RouteDecision, TASK_TYPES, _percentile, summarize_results,
)


def family_bootstrap(
    rows: list[dict], predictions: dict[str, dict[str, str]], *,
    resamples: int = 10000, seed: int = SEED,
) -> dict:
    """Resample whole families within each class, paired across all candidates."""
    if resamples < 100:
        raise ValueError("at least 100 bootstrap resamples are required")
    ids = {row["id"] for row in rows}
    if any(set(p) != ids for p in predictions.values()):
        raise ValueError("candidate predictions must cover exactly the same cases")
    by_label = defaultdict(lambda: defaultdict(list))
    for row in rows:
        by_label[row["expected_task"]][row["family_id"]].append(row)
    if set(by_label) != set(TASK_TYPES):
        raise ValueError("all six labels are required for stratified analysis")
    family_labels = defaultdict(set)
    for row in rows:
        family_labels[row["family_id"]].add(row["expected_task"])
    if any(len(labels) != 1 for labels in family_labels.values()):
        raise ValueError("a family cannot span different label strata")
    sizes = {len(family) for families in by_label.values() for family in families.values()}
    if len(sizes) != 1:
        raise ValueError("this design requires equal-sized scenario families")
    names = list(predictions)
    indices = {label: i for i, label in enumerate(TASK_TYPES)}
    vectors = []
    for label in TASK_TYPES:
        families = []
        for _, family in sorted(by_label[label].items()):
            families.append([
                list(Counter(indices.get(predictions[name][r["id"]], 6) for r in family).items())
                for name in names
            ])
        vectors.append(families)
    samples = {name: {"accuracy": [], "macro_f1": [], "by_task": {t: [] for t in TASK_TYPES}}
               for name in names}
    rng = random.Random(seed)
    for _ in range(resamples):
        matrices = [[[0] * 7 for _ in TASK_TYPES] for _ in names]
        for label_index, families in enumerate(vectors):
            weights = Counter(rng.choices(range(len(families)), k=len(families)))
            for index, weight in weights.items():
                for strategy_index, sparse_counts in enumerate(families[index]):
                    row = matrices[strategy_index][label_index]
                    for predicted, count in sparse_counts:
                        row[predicted] += count * weight
        for name, matrix in zip(names, matrices):
            true_positives = [matrix[i][i] for i in range(6)]
            samples[name]["accuracy"].append(sum(true_positives) / len(rows))
            f1 = []
            for i, label in enumerate(TASK_TYPES):
                actual = sum(matrix[i])
                predicted = sum(row[i] for row in matrix)
                f1.append(2 * true_positives[i] / (actual + predicted) if actual + predicted else 0)
                samples[name]["by_task"][label].append(true_positives[i] / actual)
            samples[name]["macro_f1"].append(sum(f1) / 6)

    def interval(values: list[float], alpha: float = 0.05) -> list[float]:
        return [_percentile(values, alpha / 2), _percentile(values, 1 - alpha / 2)]

    estimates = {}
    for name in names:
        estimates[name] = {
            "accuracy_ci95": interval(samples[name]["accuracy"]),
            "macro_f1_ci95": interval(samples[name]["macro_f1"]),
            "by_task_ci95": {label: interval(values)
                             for label, values in samples[name]["by_task"].items()},
        }
    pairs = []
    pair_count = math.comb(len(names), 2)
    for first, second in combinations(names, 2):
        differences = [100 * (b - a) for a, b in
                       zip(samples[first]["accuracy"], samples[second]["accuracy"])]
        adjusted = interval(differences, 0.05 / pair_count)
        observed = 100 * sum(
            (predictions[second][r["id"]] == r["expected_task"])
            - (predictions[first][r["id"]] == r["expected_task"]) for r in rows
        ) / len(rows)
        pairs.append({
            "baseline": first, "candidate": second, "accuracy_difference_pp": observed,
            "paired_ci95_pp": interval(differences),
            "familywise_ci95_pp": adjusted,
            "separated_after_six_comparisons": adjusted[0] > 0 or adjusted[1] < 0,
        })
    return {
        "method": "class-stratified paired scenario-family percentile bootstrap",
        "resamples": resamples, "seed": seed,
        "families": len(family_labels), "cases": len(rows),
        "familywise_method": "Bonferroni across all pairwise accuracy comparisons",
        "estimates": estimates, "comparisons": pairs,
    }


def verify_primary(report: dict, rows: list[dict], manifest: dict) -> dict[str, dict[str, str]]:
    if report["corpus_manifest_sha256"] != digest(manifest):
        raise ValueError("report belongs to a different frozen corpus")
    by_id = {row["id"]: row for row in rows}
    predictions = {}
    for strategy, records in report["records"].items():
        if len(records) != len(rows) or {r["id"] for r in records} != set(by_id):
            raise ValueError("primary analysis requires one prediction per unique test case")
        for record in records:
            expected = by_id[record["id"]]
            if record["prompt"] != expected["prompt"] or record["expected_task"] != expected["expected_task"]:
                raise ValueError("record text or label differs from frozen corpus")
        predictions[strategy] = {r["id"]: r["decision"]["task_type"] for r in records}
    return predictions


def verify_summaries(report: dict) -> None:
    """Recompute published scores, latencies, errors, and cost from raw decisions."""
    def equivalent(left, right) -> bool:
        # Python versions can differ in floating-point summation. Permit only
        # rounding noise, far below one case or a billed token.
        if isinstance(left, float) and isinstance(right, (int, float)):
            return math.isfinite(left) and math.isfinite(right) and math.isclose(
                left, right, rel_tol=1e-12, abs_tol=1e-12,
            )
        if isinstance(left, dict) and isinstance(right, dict):
            return left.keys() == right.keys() and all(equivalent(value, right[key])
                                                       for key, value in left.items())
        if isinstance(left, list) and isinstance(right, list):
            return len(left) == len(right) and all(equivalent(a, b) for a, b in zip(left, right))
        return left == right

    summaries = {summary["strategy"]: summary for summary in report["strategies"]}
    if len(summaries) != len(report["strategies"]) or set(summaries) != set(report["records"]):
        raise ValueError("summary candidates do not match raw records")
    for strategy, records in report["records"].items():
        if any(record["decision"]["strategy"] != strategy for record in records):
            raise ValueError("raw record strategy differs from its group")
        results = [
            CaseResult(record["id"], record["prompt"], record["expected_task"],
                       tuple(record["tags"]), RouteDecision(**record["decision"]))
            for record in records
        ]
        if not equivalent(summarize_results(results), summaries[strategy]):
            raise ValueError("published summary differs from raw decisions")


def analyze(
    primary_path: Path, repeat_path: Path | None = None, *,
    hardware_description: str = "Hardware details not recorded; see runtime device and environment.",
) -> dict:
    manifest = verify_frozen()
    rows = [json.loads(line) for line in (DATA / "test.jsonl").read_text().splitlines()]
    report = read_json(primary_path)
    verify_summaries(report)
    predictions = verify_primary(report, rows, manifest)
    if set(predictions) != {"axon-heuristic", "laya", "strands", "llm-router"}:
        raise ValueError("publication analysis requires all four candidates")
    uncertainty = family_bootstrap(rows, predictions)
    strategies = [
        {**summary, **uncertainty["estimates"][summary["strategy"]]}
        for summary in report["strategies"]
    ]
    result = {
        "schema": "axonllm.routing-evidence/v2",
        "evaluation_status": "model-reviewed synthetic benchmark",
        "generated_at": report["generated_at"],
        "corpus_manifest_sha256": digest(manifest),
        "artifact_sha256": {
            "primary_report": hashlib.sha256(primary_path.read_bytes()).hexdigest(),
            "analysis_code": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "protocol": hashlib.sha256((ROOT / "benchmarks/routing/PROTOCOL_V2.md").read_bytes()).hexdigest(),
        },
        "methodology": {
            "unique_cases": len(rows), "development_cases": manifest["development"]["cases"],
            "families": uncertainty["families"], "domains": 10, "variants_per_family": 4,
            "language": "English", "repetitions": 1, "concurrency": 1,
            "generator": manifest["generator"], "reviewer": manifest["reviewer"],
            "bootstrap": {k: v for k, v in uncertainty.items() if k not in {"estimates", "comparisons"}},
            "scope": "short-prompt task routing; downstream answer quality excluded",
            "hardware": hardware_description,
            "hardware_description_source": "operator-supplied description; not inferred from model scores",
            "limitations": [
                "Synthetic, model-reviewed labels; no human annotation claim.",
                "Variants are correlated; intervals resample whole scenario families.",
                "Consensus filtering and generator/reviewer model-family bias remain.",
                "No production traffic, long-context or multilingual generalization claim.",
                "Laya float32 and Strands bfloat16; Strands reference causal-convolution kernel.",
            ],
        },
        "strategies": strategies,
        "paired_comparisons": uncertainty["comparisons"],
        "runtime": report["runtime"], "configuration": report["configuration"],
        "environment": report["environment"],
        "repeatability": None,
    }
    if repeat_path:
        result["artifact_sha256"]["repeatability_report"] = hashlib.sha256(repeat_path.read_bytes()).hexdigest()
        repeat = read_json(repeat_path)
        verify_summaries(repeat)
        if repeat["corpus_manifest_sha256"] != digest(manifest):
            raise ValueError("repeatability panel uses a different corpus")
        selected, repetitions = panel_cases("repeatability")
        expected_sequence = selected * repetitions
        if set(repeat["records"]) != set(predictions):
            raise ValueError("repeatability panel must contain the same four candidates")
        results = []
        for strategy, records in repeat["records"].items():
            if len(records) != len(expected_sequence) or any(
                (record["id"], record["prompt"], record["expected_task"])
                != (expected["id"], expected["prompt"], expected["expected_task"])
                for record, expected in zip(records, expected_sequence)
            ):
                raise ValueError("repeatability records differ from the frozen panel sequence")
            grouped = defaultdict(list)
            for record in records:
                grouped[record["id"]].append(record["decision"]["task_type"])
            if len(grouped) != 120 or any(len(values) != 3 for values in grouped.values()):
                raise ValueError("repeatability panel must contain 120 cases times three")
            results.append({
                "strategy": strategy, "unique_cases": len(grouped),
                "cases_with_prediction_changes": sum(len(set(v)) > 1 for v in grouped.values()),
            })
        result["repeatability"] = {
            "unique_cases": 120, "families": 30, "repetitions": 3,
            "strategies": results, "summaries": repeat["strategies"],
        }
    return result


def markdown(result: dict) -> str:
    lines = [
        "# Expanded smart-routing evidence",
        "",
        "Model-reviewed synthetic benchmark: 960 test prompts in 240 scenario families; "
        "240 development prompts kept separate. Routing decisions only.",
        "",
        "| Router | Accuracy (95% family interval) | Macro F1 | Median | p95 | API cost / 1,000 | Hardware/electricity | Errors |",
        "|---|---:|---:|---:|---:|---:|---|---:|",
    ]
    for summary in result["strategies"]:
        low, high = summary["accuracy_ci95"]
        cost = summary["router_cost_per_1k_requests_usd"]
        price = "Unknown" if cost is None else "$0" if cost == 0 else f"${cost:.6f}"
        lines.append(
            f"| {summary['strategy']} | {summary['accuracy']:.2%} ({low:.1%}–{high:.1%}) | "
            f"{summary['macro_f1']:.3f} | {summary['latency_ms']['p50']:.2f} ms | "
            f"{summary['latency_ms']['p95']:.2f} ms | {price} | Unmeasured | {summary['errors']} |"
        )
    lines.extend([
        "", "## Paired accuracy differences", "",
        "Intervals account for all six comparisons (Bonferroni family-wise 95%). "
        "Positive values favor the candidate. An interval crossing zero is inconclusive.",
        "", "| Candidate vs baseline | Difference | Adjusted interval | Separated |",
        "|---|---:|---:|---|",
    ])
    for pair in result["paired_comparisons"]:
        low, high = pair["familywise_ci95_pp"]
        lines.append(
            f"| {pair['candidate']} vs {pair['baseline']} | {pair['accuracy_difference_pp']:+.2f} pp | "
            f"{low:+.2f} to {high:+.2f} pp | {'Yes' if pair['separated_after_six_comparisons'] else 'No'} |"
        )
    lines.extend(["", "## Reasoning-task routing", ""])
    for summary in result["strategies"]:
        task = summary["by_task"]["reasoning"]
        low, high = summary["by_task_ci95"]["reasoning"]
        lines.append(f"- {summary['strategy']}: {task['accuracy']:.1%} "
                     f"({task['correct']}/{task['requests']}), family interval {low:.1%}–{high:.1%}.")
    if result["repeatability"]:
        lines.extend(["", "## Separate repeatability panel", ""])
        for summary in result["repeatability"]["strategies"]:
            lines.append(f"- {summary['strategy']}: {summary['cases_with_prediction_changes']}/120 "
                         "prompts changed predicted label across three repetitions.")
    lines.extend(["", "## Limits", ""])
    lines.extend("- " + line for line in result["methodology"]["limitations"])
    lines.append("\nThese intervals describe this synthetic family sampling scheme, not real-world representativeness.\n")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--primary", type=Path, default=ROOT / "docs/benchmarks/smart-routing-v2-primary-2026-10-02.json")
    parser.add_argument("--repeatability", type=Path)
    parser.add_argument("--hardware-description", default=(
        "Hardware details not recorded; see runtime device and environment."
    ), help="describe the actual measured host; do not copy another run's hardware")
    args = parser.parse_args()
    result = analyze(args.primary, args.repeatability, hardware_description=args.hardware_description)
    destination = ROOT / "docs/benchmarks/smart-routing-v2-evidence-2026-10-02"
    write_json(destination.with_suffix(".json"), result)
    destination.with_suffix(".md").write_text(markdown(result))
    print(markdown(result))


if __name__ == "__main__":
    main()
