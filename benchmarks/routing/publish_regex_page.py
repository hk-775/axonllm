"""Publish the frozen regex revision separately from the original leaderboard."""

from __future__ import annotations

from html import escape
import json
import shutil

from benchmarks.routing.regex_revision import FREEZE, NAMES, ROOT, digest, verify


def table(headers: list[str], rows: list[list[str]]) -> str:
    header = "".join(f"<th>{escape(v)}</th>" for v in headers)
    body = "".join("<tr>" + "".join(f"<td>{escape(v)}</td>" for v in row) + "</tr>"
                   for row in rows)
    return (f'<div class="table-wrap" tabindex="0"><table><thead><tr>{header}</tr>'
            f"</thead><tbody>{body}</tbody></table></div>")


def main() -> None:
    verify()
    source = ROOT / "docs/benchmarks/regex-revision-2026-10-02/results.json"
    result = json.loads(source.read_text())
    if result["freeze_sha256"] != digest(FREEZE):
        raise ValueError("results do not match the frozen candidate")
    rows = []
    for name, panel in result["panels"].items():
        old, new = (panel["strategies"][n] for n in NAMES)
        rows.append([
            name.replace("-", " "), f"{old['correct']}/{old['cases']} ({old['accuracy']:.2%})",
            f"{new['correct']}/{new['cases']} ({new['accuracy']:.2%})",
            str(len(panel["improved"])), str(len(panel["regressed"])),
        ])
    comparison = table(["Partition", "Original", "Revised", "Improved", "Regressed"], rows)
    test = result["panels"]["reused-test"]
    old, new = (test["strategies"][n] for n in NAMES)
    pair = test["uncertainty"]["comparisons"][0]
    low, high = pair["paired_ci95_pp"]
    tasks = table(
        ["Task", "Original", "Revised", "Cases"],
        [[task.replace("_", " "), str(a["correct"]),
          str(new["slices"]["expected_task"][task]["correct"]), str(a["cases"])]
         for task, a in old["slices"]["expected_task"].items()],
    )
    variants = table(
        ["Wording", "Original", "Revised"],
        [[variant.replace("_", " "), f"{a['accuracy']:.2%}",
          f"{new['slices']['variant'][variant]['accuracy']:.2%}"]
         for variant, a in old["slices"]["variant"].items()],
    )
    regressions = []
    for row in test["records"]:
        if row["id"] not in test["regressed"]:
            continue
        changed = row["decisions"][NAMES[1]]
        regressions.append(
            '<details data-regression><summary>'
            f'{escape(row["expected_task"])} → {escape(changed["task_type"])}</summary>'
            f'<p class="case-prompt">{escape(row["prompt"])}</p>'
            f'<p>Matched rules: {escape(", ".join(changed["matched_keywords"]))}</p></details>'
        )
    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AxonLLM — Regex revision evidence</title>
<meta name="description" content="An action-first regex revision with reserved validation, reused-test improvements and regressions, and reproducible raw results.">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%230D1B2A'/%3E%3Cpath d='M8 22L16 10L24 22' stroke='%2300B4D8' stroke-width='3' fill='none'/%3E%3C/svg%3E">
<link rel="stylesheet" href="benchmark.css"></head><body>
<nav aria-label="Main navigation"><div class="container nav-content">
<a class="brand" href="/">Axon<span>LLM</span></a><div class="nav-links">
<a href="benchmark.html">Original benchmark</a><a href="#regressions">Regressions</a>
<a href="https://github.com/hk-775/axonllm">GitHub ↗</a></div></div></nav>
<header class="container hero"><p class="eyebrow">Regex follow-up · October 2, 2026</p>
<h1>Better rules.<br>Visible regressions.</h1>
<p class="lead">An action-first regex candidate improves from {old['accuracy']:.2%} to
{new['accuracy']:.2%} on the original 960 prompts. It fixes {len(test['improved'])} decisions
and breaks {len(test['regressed'])} that the original got right.</p>
<p class="notice"><strong>Reused test, separate experiment.</strong> These rules were developed
after the original results were public. The 960 prompts are a regression check, not a fresh
confirmatory test. The original four-router comparison remains unchanged. Reserved validation
was excluded from this revision's tuning, but came from the same public corpus.</p>
<div class="hero-actions"><a class="button" href="benchmark-regex-results.json" download>
Download all predictions</a><a class="button secondary" href="https://github.com/hk-775/axonllm/blob/main/benchmarks/routing/REGEX_REVISION.md">
Read the protocol ↗</a></div></header>
<main><section class="container" id="results"><h2>Development is not validation.</h2>
<p>The 240 development prompts were split into 120 for tuning and 120 reserved for validation,
keeping all four wordings of a scenario together. The rules were committed in
<code>4f4084c</code> before validation. No rule changes followed these outcomes.</p>
{comparison}
<p class="caption">Perfect development accuracy did not transfer to reserved families.
The lower validation result bounds what these improvements demonstrate.</p>
</section><section class="container"><h2>Where the revision helps.</h2>{tasks}<br>{variants}
<p class="caption">Reused-test accuracy difference: {pair['accuracy_difference_pp']:+.2f}
percentage points; 95% paired family bootstrap interval [{low:+.2f}, {high:+.2f}].
10,000 samples of whole families within labels. This interval excludes tuning and selection bias.</p>
<p>Macro F1: {old['macro_f1']:.3f} → {new['macro_f1']:.3f}. API cost: <strong>$0</strong>
for both local candidates. Hardware/electricity: <strong>Unmeasured</strong>.
Local median time was {old['latency_ms']['p50']:.3f} → {new['latency_ms']['p50']:.3f} ms;
timing is a paired diagnostic under uncontrolled host load.</p></section>
<section class="container" id="regressions"><h2>All {len(regressions)} regressions.</h2>
<p>Lexical rules still confuse technical words used in ordinary senses, mixed tasks, and
nested quotations. Expand a case to inspect the prompt and matched rules.</p>
{''.join(regressions)}</section>
<section class="container"><h2>Try the candidate explicitly.</h2>
<p>The separate <code>IntentRegexClassifier</code> prioritizes the requested action,
handles quotation and negation, and uses the original classifier as a fallback.
Its confidence is a rule score, not a calibrated probability.</p>
<p>Import it from <code>src.gateway.intent_regex_classifier</code> and call
<code>IntentRegexClassifier().classify(prompt)</code>. The gateway default remains
the original classifier. This revision does not change the economy/capable regex
policy in the separate answer-quality experiment.</p>
<p>A new independently authored test set and workload-specific validation are needed before
adopting these rules as a production default.</p></section></main>
<footer class="container"><p>Harleen Kaur · AxonLLM · MIT-0</p>
<a href="benchmark.html">Original four-router evidence</a></footer></body></html>
"""
    for directory in (ROOT / "site", ROOT / "src/gateway/resources/runtime/site"):
        (directory / "benchmark-regex.html").write_text(html)
        shutil.copy2(source, directory / "benchmark-regex-results.json")
    print("Published separate regex revision page and raw results.")


if __name__ == "__main__":
    main()
