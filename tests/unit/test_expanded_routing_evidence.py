"""Dataset separation, blind annotation, and family-level uncertainty contracts."""

from __future__ import annotations

from collections import Counter
import json

import pytest

from benchmarks.routing import corpus
from benchmarks.routing.analyze import family_bootstrap, verify_primary, verify_summaries
from src.gateway.autorouting_benchmark import (
    BenchmarkCase, CaseResult, RouteDecision, TASK_TYPES, build_report, summarize_results,
)


def cases(families_per_label=5, domains=None):
    rows = []
    for label in TASK_TYPES:
        for domain in domains or corpus.DOMAINS:
            for slot in range(families_per_label):
                fid = corpus.family_id(label, domain, slot)
                for variant in corpus.VARIANTS:
                    cid = corpus.digest([fid, variant])[:20]
                    rows.append({
                        "id": cid, "family_id": fid, "family_slot": slot,
                        "expected_task": label, "domain": domain, "variant": variant,
                        "prompt": f"Here is a deliberately unique synthetic test prompt with "
                                  f"sufficient words for fixture {cid}.",
                        "concept": "test fixture", "rationale": "fixture only",
                        "tags": ["english", domain, variant],
                    })
    return rows


def test_family_split_is_balanced_and_never_leaks_variants():
    rows = cases()
    assert corpus.validate_cases(rows)["families"] == 300
    dev, test = corpus.split_cases(rows)
    assert len(dev) == 240 and len(test) == 960
    assert not {r["family_id"] for r in dev} & {r["family_id"] for r in test}
    assert Counter(r["expected_task"] for r in test) == {label: 160 for label in TASK_TYPES}
    assert Counter(r["domain"] for r in test) == {domain: 96 for domain in corpus.DOMAINS}
    assert corpus.split_cases(list(reversed(rows))) == (dev, test)


def test_published_corpus_retains_frozen_hashes_and_blind_label_agreement():
    manifest = corpus.verify_frozen()
    assert manifest["development"] == {"cases": 240, "families": 60}
    assert manifest["test"] == {"cases": 960, "families": 240}
    duplication = corpus.read_json(corpus.DATA / "duplicate-audit.json")
    assert duplication["findings"] == []
    assert duplication["legacy_cases_checked"] == 192


def test_normalized_duplicates_are_rejected():
    rows = cases()
    rows[1]["prompt"] = rows[0]["prompt"].upper() + "!"
    with pytest.raises(ValueError, match="duplicate normalized"):
        corpus.validate_cases(rows)


def test_ambiguity_and_length_violations_require_revision():
    rows = cases(1, [corpus.DOMAINS[0]])[:4]
    reviews = [{"id": row["id"], "label": row["expected_task"], "ambiguous": False,
                "reason": "fixture"} for row in rows]
    reviews[0]["ambiguous"] = True
    rows[1]["prompt"] = "Too short."
    assert len(corpus.audit_failures(rows, reviews)) == 2


@pytest.mark.asyncio
async def test_independent_auditor_never_receives_gold_or_family_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(corpus, "WORK", tmp_path)
    rows = cases(1, [corpus.DOMAINS[0]])
    by_prompt = {row["prompt"]: row for row in rows}

    class Reviewer:
        async def call(self, provider, system, user, **_kwargs):
            assert provider == "anthropic"
            request = json.loads(user)
            assert all(set(item) == {"id", "prompt"} for item in request)
            return {"model": corpus.REVIEWER, "request_sha256": "fixture",
                    "output": {"reviews": [
                {"id": item["id"], "label": by_prompt[item["prompt"]]["expected_task"],
                 "ambiguous": False, "reason": "test"}
                for item in request
            ]}}

    reviews = await corpus.audit(Reviewer(), rows)
    assert len(reviews) == len(rows)
    assert corpus.audit_failures(rows, reviews) == []


@pytest.mark.asyncio
async def test_revision_requires_every_case_and_preserves_ids_across_mixed_labels(tmp_path, monkeypatch):
    monkeypatch.setattr(corpus, "WORK", tmp_path)
    rows = cases(1, [corpus.DOMAINS[0]])
    failures = [{"case": row, "review": {"reason": "Ambiguous requested action."}} for row in rows]

    class Writer:
        async def call(self, provider, system, user, *, response_schema, **_kwargs):
            assert provider == "openai"
            request = json.loads(user.split("\n", 1)[1])
            assert len({item["target_task"] for item in request}) == 1
            assert set(response_schema["required"]) == {item["id"] for item in request}
            return {"output": {
                item["id"]: {"prompt": item["prompt_to_replace"] + " Revised.",
                             "rationale": "One primary action."} for item in request
            }}

    await corpus.repair(Writer(), failures, 0)
    replacements = [
        item for path in (tmp_path / "repairs").glob("*.json")
        for item in json.loads(path.read_text())["output"]["replacements"]
    ]
    assert {item["id"] for item in replacements} == {row["id"] for row in rows}
    assert all(item["prompt"].endswith("Revised.") for item in replacements)


def test_bootstrap_is_paired_and_does_not_treat_variant_copies_as_independent():
    rows = cases(4, [corpus.DOMAINS[0]])
    predictions = {
        "perfect": {r["id"]: r["expected_task"] for r in rows},
        "partial": {
            r["id"]: r["expected_task"] if r["family_slot"] % 2 == 0 else "__error__"
            for r in rows
        },
    }
    original = family_bootstrap(rows, predictions, resamples=200)
    doubled_rows = rows + [{**r, "id": r["id"] + "-copy"} for r in rows]
    doubled_predictions = {
        name: {**values, **{cid + "-copy": value for cid, value in values.items()}}
        for name, values in predictions.items()
    }
    doubled = family_bootstrap(doubled_rows, doubled_predictions, resamples=200)
    assert original["families"] == doubled["families"] == 24
    assert original["estimates"] == doubled["estimates"]
    assert original["comparisons"] == doubled["comparisons"]
    assert original["estimates"]["perfect"]["accuracy_ci95"] == [1.0, 1.0]
    assert original["comparisons"][0]["accuracy_difference_pp"] == -50.0


def test_bootstrap_rejects_missing_predictions_and_mixed_family_labels():
    rows = cases(1, [corpus.DOMAINS[0]])
    with pytest.raises(ValueError, match="same cases"):
        family_bootstrap(rows, {"incomplete": {}}, resamples=100)
    predictions = {"one": {r["id"]: r["expected_task"] for r in rows}}
    rows[4]["family_id"] = rows[0]["family_id"]
    with pytest.raises(ValueError, match="span different label"):
        family_bootstrap(rows, predictions, resamples=100)


def test_primary_analysis_rejects_repetitions_and_modified_labels():
    rows = cases(1, [corpus.DOMAINS[0]])
    manifest = {"fixture": True}
    records = [{"id": r["id"], "prompt": r["prompt"], "expected_task": r["expected_task"],
                "decision": {"task_type": r["expected_task"]}} for r in rows]
    report = {"corpus_manifest_sha256": corpus.digest(manifest), "records": {"model": records}}
    assert len(verify_primary(report, rows, manifest)["model"]) == len(rows)
    report["records"]["model"] = records * 3
    with pytest.raises(ValueError, match="one prediction"):
        verify_primary(report, rows, manifest)
    report["records"]["model"] = records
    records[0]["expected_task"] = "wrong"
    with pytest.raises(ValueError, match="differs from frozen"):
        verify_primary(report, rows, manifest)


def test_summary_cannot_claim_better_scores_or_lower_cost_than_raw_predictions():
    case = BenchmarkCase("one", "Explain this code snippet.", "coding", ())
    decision = RouteDecision(strategy="example", task_type="general", confidence=0.8,
                             latency_ms=123.0, cost_usd=0.01)
    result = CaseResult(case.case_id, case.prompt, case.expected_task, case.tags, decision)
    summary = summarize_results([result])
    report = build_report(cases=[case], summaries=[summary],
                          results_by_strategy={"example": [result]}, source="fixture", repetitions=1)
    verify_summaries(report)
    summary["router_cost_usd"] += 1e-16
    verify_summaries(report)  # harmless cross-version floating-point rounding
    summary["accuracy"] = 1.0
    with pytest.raises(ValueError, match="differs from raw"):
        verify_summaries(report)
    summary["accuracy"] = 0.0
    summary["router_cost_per_1k_requests_usd"] = 0.0
    with pytest.raises(ValueError, match="differs from raw"):
        verify_summaries(report)
