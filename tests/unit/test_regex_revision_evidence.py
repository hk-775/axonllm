"""Keep the revision snapshot, family split, and published claims consistent."""

import json

from benchmarks.routing.regex_revision import NAMES, ROOT, partitions, score, verify


def test_revision_retains_frozen_sources_and_disjoint_families():
    verify()
    splits = partitions()
    assert [len(rows) for rows in splits.values()] == [120, 120, 960]
    families = [{r["family_id"] for r in rows} for rows in splits.values()]
    assert not (families[0] & families[1] or families[0] & families[2] or families[1] & families[2])


def test_published_revision_metrics_regrade_every_saved_prediction():
    result = json.loads(
        (ROOT / "docs/benchmarks/regex-revision-2026-10-02/results.json").read_text()
    )
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
