"""Build canonical static benchmark artifacts from verified measured evidence."""

from __future__ import annotations

from html import escape
import hashlib
import json
from pathlib import Path
import shutil

from benchmarks.routing.corpus import DATA, ROOT, digest, read_json, verify_frozen, write_json
from src.gateway.autorouting_benchmark import TASK_TYPES

NAMES = {"axon-heuristic": "Regex / heuristic", "laya": "Laya",
         "strands": "Strands Decider", "llm-router": "GPT-4o Mini"}
SOURCE = Path(__file__).parent


def price(value: float | None) -> str:
    return "Unknown" if value is None else "$0" if value == 0 else f"${value:.6f}"


def main() -> None:
    manifest = verify_frozen()
    evidence = read_json(ROOT / "docs/benchmarks/smart-routing-v2-evidence-2026-10-02.json")
    primary = read_json(ROOT / "docs/benchmarks/smart-routing-v2-primary-2026-10-02.json")
    for key, panel in (("primary_report", "primary"), ("repeatability_report", "repeatability")):
        report_path = ROOT / "docs/benchmarks" / f"smart-routing-v2-{panel}-2026-10-02.json"
        if hashlib.sha256(report_path.read_bytes()).hexdigest() != evidence["artifact_sha256"][key]:
            raise ValueError("raw report changed after uncertainty analysis")
    if evidence["corpus_manifest_sha256"] != digest(manifest) or not evidence["repeatability"]:
        raise ValueError("complete verified evidence, including repeatability, is required")
    if {s["strategy"] for s in evidence["strategies"]} != set(NAMES):
        raise ValueError("all four candidate results are required")
    site = ROOT / "site"
    previous = read_json(site / "benchmark-results.json")
    if previous["schema"] == "axonllm.autorouting-benchmark-summary/v1":
        archive = site / "benchmark-2026-09-16.html"
        if not archive.exists():
            old_html = (site / "benchmark.html").read_text()
            archive.write_text(old_html.replace("benchmark.css", "benchmark-2026-09-16.css")
                               .replace("benchmark-results.json", "benchmark-results-2026-09-16.json"))
            shutil.copy2(site / "benchmark.css", site / "benchmark-2026-09-16.css")
            shutil.copy2(site / "benchmark-results.json", site / "benchmark-results-2026-09-16.json")

    cards, result_rows, pairs, tasks, repeatability = [], [], [], [], []
    for summary in evidence["strategies"]:
        name = NAMES[summary["strategy"]]
        low, high = summary["accuracy_ci95"]
        accuracy = summary["accuracy"]
        cost = price(summary["router_cost_per_1k_requests_usd"])
        median, p95 = summary["latency_ms"]["p50"], summary["latency_ms"]["p95"]
        cards.append(
            f'<article class="result-card" data-benchmark-result data-strategy="{summary["strategy"]}" '
            f'data-accuracy="{accuracy}"><h3>{escape(name)}</h3><div class="score">{accuracy:.2%}</div>'
            f'<p class="interval">{low:.1%}–{high:.1%} · 95% family interval</p>'
            f'<div class="accuracy-track" data-accuracy-bar role="img" aria-label="{escape(name)} accuracy {accuracy:.2%}">'
            f'<div class="accuracy-fill" style="width:{accuracy * 100:.3f}%"></div></div>'
            f'<p class="mini">Median latency: {median:.2f} ms</p><p class="mini">API cost / 1,000: {cost}</p></article>'
        )
        result_rows.append(
            f"<tr><td>{escape(name)}</td><td>{accuracy:.2%} / {low:.1%}–{high:.1%}</td>"
            f"<td>{summary['macro_f1']:.3f}</td><td>{median:.2f} / {p95:.2f} ms</td>"
            f"<td>{cost}</td><td>Unmeasured</td><td>{summary['errors']}</td></tr>"
        )
    for pair in evidence["paired_comparisons"]:
        low, high = pair["familywise_ci95_pp"]
        pairs.append(
            f"<tr><td>{escape(NAMES[pair['candidate']])} vs {escape(NAMES[pair['baseline']])}</td>"
            f"<td>{pair['accuracy_difference_pp']:+.2f} pp</td><td>{low:+.2f} to {high:+.2f} pp</td>"
            f"<td>{'Yes' if pair['separated_after_six_comparisons'] else 'No'}</td></tr>"
        )
    for label in TASK_TYPES:
        cells = "".join(f"<td>{s['by_task'][label]['accuracy']:.1%}</td>" for s in evidence["strategies"])
        tasks.append(f"<tr><td>{escape(label.replace('_', ' '))}</td>{cells}</tr>")
    for summary in evidence["repeatability"]["strategies"]:
        repeatability.append(
            f"<li>{escape(NAMES[summary['strategy']])}: "
            f"{summary['cases_with_prediction_changes']}/120 prompts changed predicted label.</li>"
        )
    replacements = {
        "CARDS": "".join(cards), "RESULT_ROWS": "".join(result_rows), "PAIRS": "".join(pairs),
        "TASKS": "".join(tasks), "REPEATABILITY": "".join(repeatability),
        "TASK_OPTIONS": "".join(f'<option value="{label}">{escape(label.replace("_", " "))}</option>'
                                for label in TASK_TYPES),
        "HARDWARE": escape(evidence["methodology"]["hardware"]),
    }
    html = (SOURCE / "page.html").read_text()
    for key, value in replacements.items():
        html = html.replace(f"@@{key}@@", value)
    if "@@" in html:
        raise ValueError("unresolved page template marker")
    (site / "benchmark.html").write_text(html)
    shutil.copy2(SOURCE / "page.css", site / "benchmark.css")
    shutil.copy2(SOURCE / "page.js", site / "benchmark.js")
    write_json(site / "benchmark-results.json", evidence)
    by_strategy = {
        strategy: {record["id"]: record["decision"]["task_type"] for record in records}
        for strategy, records in primary["records"].items()
    }
    cases = []
    for line in (DATA / "test.jsonl").read_text().splitlines():
        row = json.loads(line)
        cases.append({
            **{k: row[k] for k in ("id", "family_id", "prompt", "expected_task", "domain", "variant")},
            "predictions": {strategy: records[row["id"]] for strategy, records in by_strategy.items()},
        })
    write_json(site / "benchmark-cases.json", {"corpus_manifest_sha256": digest(manifest), "cases": cases})
    packaged = ROOT / "src/gateway/resources/runtime/site"
    for path in site.glob("benchmark*"):
        if path.is_file():
            shutil.copy2(path, packaged / path.name)
    print("Built canonical benchmark page, case explorer, archives, and matching packaged assets.")


if __name__ == "__main__":
    main()
