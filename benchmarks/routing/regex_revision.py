"""Develop and evaluate a separate regex candidate without rewriting v2 evidence."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import random
import statistics
import time

from benchmarks.routing.analyze import family_bootstrap
from benchmarks.routing.corpus import DATA, ROOT, verify_frozen
from src.gateway.autorouting_benchmark import TASK_TYPES, _percentile
from src.gateway.intent_regex_classifier import IntentRegexClassifier
from src.gateway.task_classifier import TaskClassifier

FREEZE = ROOT / "benchmarks/routing/regex-revision-freeze.json"
FILES = (
    "src/gateway/intent_regex_classifier.py",
    "src/gateway/task_classifier.py",
    "benchmarks/routing/regex_revision.py",
    "benchmarks/routing/analyze.py",
    "benchmarks/routing/REGEX_REVISION.md",
    "tests/unit/test_intent_regex_classifier.py",
    "benchmarks/routing/data/v2/development.jsonl",
    "benchmarks/routing/data/v2/test.jsonl",
    "docs/benchmarks/smart-routing-v2-primary-2026-10-02.json",
    "uv.lock",
)
NAMES = ("original-regex", "action-first-regex")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def partitions() -> dict[str, list[dict]]:
    """Split development by whole family; keep the already published test intact."""
    rows = [json.loads(s) for s in (DATA / "development.jsonl").read_text().splitlines()]
    training = set()
    for label in TASK_TYPES:
        training.update(sorted({r["family_id"] for r in rows
                                if r["expected_task"] == label})[:5])
    return {
        "development": [r for r in rows if r["family_id"] in training],
        "reserved-validation": [r for r in rows if r["family_id"] not in training],
        "reused-test": [json.loads(s) for s in (DATA / "test.jsonl").read_text().splitlines()],
    }


def models() -> dict:
    return dict(zip(NAMES, (TaskClassifier(), IntentRegexClassifier())))


def score(rows: list[dict], predictions: dict[str, str]) -> dict:
    """Compute route metrics directly from case-level labels."""
    matrix = {label: Counter() for label in TASK_TYPES}
    for row in rows:
        matrix[row["expected_task"]][predictions[row["id"]]] += 1
    correct = sum(matrix[label][label] for label in TASK_TYPES)
    f1 = []
    for label in TASK_TYPES:
        actual = sum(matrix[label].values())
        predicted = sum(matrix[other][label] for other in TASK_TYPES)
        f1.append(2 * matrix[label][label] / (actual + predicted)
                  if actual + predicted else 0)
    slices = {}
    for dimension in ("expected_task", "variant"):
        slices[dimension] = {}
        for value in sorted({r[dimension] for r in rows}):
            subset = [r for r in rows if r[dimension] == value]
            count = sum(predictions[r["id"]] == r["expected_task"] for r in subset)
            slices[dimension][value] = {"correct": count, "cases": len(subset),
                                       "accuracy": count / len(subset)}
    return {"correct": correct, "cases": len(rows), "accuracy": correct / len(rows),
            "macro_f1": statistics.mean(f1), "slices": slices,
            "confusion_matrix": matrix}


def train() -> dict:
    """Inspect only the development half; never display validation failures."""
    rows = partitions()["development"]
    result = {}
    for name, model in models().items():
        predictions = {r["id"]: model.classify(r["prompt"]).task_type for r in rows}
        result[name] = score(rows, predictions)
    return result


def freeze() -> dict:
    """Create the pre-evaluation snapshot exactly once."""
    verify_frozen()
    result = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "protocol": "REGEX_REVISION.md",
        "files": {name: digest(ROOT / name) for name in FILES},
        "partitions": {name: [r["id"] for r in rows]
                       for name, rows in partitions().items()},
        "development_metrics": train(),
        "notice": "Freeze before reserved validation; original test is reused, not fresh.",
    }
    with FREEZE.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    return result


def verify() -> dict:
    """Reject modified code, data, protocol, or partitions."""
    result = json.loads(FREEZE.read_text())
    for name, expected in result["files"].items():
        if digest(ROOT / name) != expected:
            raise ValueError(f"frozen file changed: {name}")
    if result["partitions"] != {name: [r["id"] for r in rows]
                                for name, rows in partitions().items()}:
        raise ValueError("partition membership changed")
    verify_frozen()
    return result


def render(result: dict) -> str:
    lines = [
        "# Action first regex results", "",
        "**Follow-up revision; original four-router results remain unchanged.**",
        "Rules were frozen before reserved validation. The original 960-prompt test",
        "is reused for regression analysis; these are not fresh confirmatory results.", "",
        "| Partition | Original | Revised | Improved cases | Regressions |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for name, panel in result["panels"].items():
        old, new = (panel["strategies"][n] for n in NAMES)
        lines.append(f"| {name} | {old['correct']}/{old['cases']} "
                     f"({old['accuracy']:.2%}) | {new['correct']}/{new['cases']} "
                     f"({new['accuracy']:.2%}) | {len(panel['improved'])} | "
                     f"{len(panel['regressed'])} |")
    panel = result["panels"]["reused-test"]
    lines.extend(["", "## Reused test details", "",
                  "| Task | Original correct | Revised correct | Cases |",
                  "| --- | ---: | ---: | ---: |"])
    for label in TASK_TYPES:
        old, new = (panel["strategies"][n]["slices"]["expected_task"][label] for n in NAMES)
        lines.append(f"| {label} | {old['correct']} | {new['correct']} | {old['cases']} |")
    pair = panel["uncertainty"]["comparisons"][0]
    lo, hi = pair["paired_ci95_pp"]
    lines.extend([
        "", f"Accuracy change: **{pair['accuracy_difference_pp']:+.2f} percentage points**;",
        f"paired family bootstrap 95% interval [{lo:+.2f}, {hi:+.2f}] points.",
        "This interval does not include selection bias from reusing a published corpus.",
        "", "| Local timing diagnostic | Median ms | p95 ms | API cost | Hardware/electricity |",
        "| --- | ---: | ---: | ---: | --- |",
    ])
    for name in NAMES:
        timing = panel["strategies"][name]["latency_ms"]
        lines.append(f"| {name} | {timing['p50']:.4f} | {timing['p95']:.4f} | $0 | Unmeasured |")
    lines.extend([
        "", "Timing uses paired local calls with randomized order under uncontrolled host",
        "load. It is not a new four-model latency benchmark.",
        "", "## Inspect and reproduce", "",
        "The JSON recording contains every prediction, rule trace, timing, improvement,",
        "and regression. `REGEX_REVISION.md` describes the development split and limitations.",
        "Use `IntentRegexClassifier` explicitly to try this candidate; the gateway default",
        "and original published classifier are unchanged.", "",
    ])
    return "\n".join(lines)


def evaluate(output: Path) -> dict:
    snapshot = verify()
    output.mkdir(parents=True, exist_ok=False)
    candidates = models()
    splits = partitions()
    for row in splits["development"][:10]:
        for model in candidates.values():
            model.classify(row["prompt"])
    rng = random.Random(775)
    result = {"created_at": datetime.now(timezone.utc).isoformat(),
              "freeze_sha256": digest(FREEZE), "freeze": snapshot,
              "environment": {"python": platform.python_version(),
                              "system": platform.system(), "machine": platform.machine()},
              "api_cost_usd": 0, "hardware_electricity_cost_usd": None, "panels": {}}
    for split, rows in splits.items():
        predictions = {name: {} for name in NAMES}
        timings = {name: [] for name in NAMES}
        records, improved, regressed = [], [], []
        for row in rows:
            record = {k: row[k] for k in ("id", "family_id", "prompt", "expected_task", "variant")}
            record["decisions"] = {}
            order = list(NAMES)
            rng.shuffle(order)
            for name in order:
                started = time.perf_counter_ns()
                decision = candidates[name].classify(row["prompt"])
                elapsed = (time.perf_counter_ns() - started) / 1_000_000
                predictions[name][row["id"]] = decision.task_type
                timings[name].append(elapsed)
                record["decisions"][name] = {**asdict(decision), "latency_ms": elapsed}
            old, new = (predictions[name][row["id"]] == row["expected_task"] for name in NAMES)
            if new and not old:
                improved.append(row["id"])
            if old and not new:
                regressed.append(row["id"])
            records.append(record)
        if split == "reused-test":
            original = json.loads((ROOT / FILES[8]).read_text())["records"]["axon-heuristic"]
            recorded = {r["id"]: r["decision"]["task_type"] for r in original}
            if predictions[NAMES[0]] != recorded:
                raise ValueError("baseline labels differ from published original")
        result["panels"][split] = {
            "strategies": {
                name: {**score(rows, predictions[name]),
                       "latency_ms": {"p50": statistics.median(timings[name]),
                                      "p95": _percentile(timings[name], .95)}}
                for name in NAMES
            },
            "improved": improved, "regressed": regressed, "records": records,
            "uncertainty": family_bootstrap(rows, predictions, resamples=10000, seed=775),
        }
    (output / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    (output / "README.md").write_text(render(result))
    print(render(result))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("train", "freeze", "evaluate"))
    parser.add_argument("--output", type=Path,
                        default=ROOT / "docs/benchmarks/regex-revision-2026-10-02")
    args = parser.parse_args()
    if args.command == "evaluate":
        evaluate(args.output)
    elif args.command == "freeze":
        freeze()
        print(FREEZE)
    else:
        print(json.dumps(train(), indent=2))


if __name__ == "__main__":
    main()
