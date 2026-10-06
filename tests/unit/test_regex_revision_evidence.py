"""Keep the revision snapshot, family split, and published claims consistent."""

import json

from benchmarks.routing.corpus import verify_frozen
from benchmarks.routing.regex_revision import (
    FREEZE,
    NAMES,
    ROOT,
    digest,
    models,
    partitions,
    score,
)

EVIDENCE = ROOT / "docs/benchmarks/regex-revision-2026-10-02"


def test_revision_retains_frozen_sources_and_disjoint_families():
    snapshot = json.loads(FREEZE.read_text())
    # Production dependencies can receive security updates; retain and verify
    # the exact historical lock separately, without installing it in CI.
    for name, expected in snapshot["files"].items():
        path = EVIDENCE / "uv.lock" if name == "uv.lock" else ROOT / name
        assert digest(path) == expected, f"frozen file changed: {name}"
    verify_frozen()
    splits = partitions()
    assert snapshot["partitions"] == {
        name: [row["id"] for row in rows] for name, rows in splits.items()
    }
    assert [len(rows) for rows in splits.values()] == [120, 120, 960]
    families = [{r["family_id"] for r in rows} for rows in splits.values()]
    assert not (families[0] & families[1] or families[0] & families[2] or families[1] & families[2])


def test_published_revision_metrics_regrade_every_saved_prediction():
    result = json.loads((EVIDENCE / "results.json").read_text())
    assert result["freeze_sha256"] == digest(FREEZE)
    assert result["freeze"] == json.loads(FREEZE.read_text())
    for name, rows in partitions().items():
        panel = result["panels"][name]
        assert [r["id"] for r in panel["records"]] == [r["id"] for r in rows]
        for strategy in NAMES:
            predictions = {r["id"]: r["decisions"][strategy]["task_type"] for r in panel["records"]}
            metrics = score(rows, predictions)
            assert all(panel["strategies"][strategy][key] == value for key, value in metrics.items())
        transitions = [
            (r["id"], *(r["decisions"][n]["task_type"] == r["expected_task"] for n in NAMES))
            for r in panel["records"]
        ]
        assert panel["improved"] == [cid for cid, old, new in transitions if new and not old]
        assert panel["regressed"] == [cid for cid, old, new in transitions if old and not new]


def test_current_environment_preserves_every_recorded_regex_decision():
    result = json.loads((EVIDENCE / "results.json").read_text())
    candidates = models()
    for split, rows in partitions().items():
        recorded = {
            row["id"]: row["decisions"]
            for row in result["panels"][split]["records"]
        }
        for row in rows:
            for name, classifier in candidates.items():
                assert (
                    classifier.classify(row["prompt"]).task_type
                    == recorded[row["id"]][name]["task_type"]
                ), (split, row["id"], name)
