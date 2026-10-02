"""Run the frozen v2 panels with resumable case-level checkpoints and progress."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import time

from benchmarks.routing.corpus import DATA, ROOT, SEED, WORK, digest, read_json, verify_frozen, write_json
from src.gateway.autorouting_benchmark import (
    BenchmarkCase, CaseResult, HeuristicRouter, OpenAICompatibleLLMRouter,
    ROUTER_SYSTEM_PROMPT, RouteDecision, TASK_TYPES, WARMUP_PROMPTS, build_report,
    evaluate_router, render_markdown, summarize_results,
)
from src.gateway.decision_model_routing import LocalDecisionRouter


def panel_cases(panel: str) -> tuple[list[dict], int]:
    """Choose the repeatability panel by family, independently of predictions."""
    rows = [json.loads(line) for line in (DATA / "test.jsonl").read_text().splitlines()]
    rng = random.Random(SEED)
    if panel == "repeatability":
        chosen = set()
        for label in TASK_TYPES:
            families = sorted({r["family_id"] for r in rows if r["expected_task"] == label})
            chosen.update(rng.sample(families, 5))
        rows = [row for row in rows if row["family_id"] in chosen]
    rng.shuffle(rows)
    return rows, 3 if panel == "repeatability" else 1


def verify_router_freeze(manifest: dict) -> None:
    """Refuse inference if a frozen router or routing instruction changed."""
    for name, expected in manifest["locked_router_implementation"].items():
        if hashlib.sha256((ROOT / "src/gateway" / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f"frozen router changed: {name}")
    if digest(ROUTER_SYSTEM_PROMPT) != manifest["routing_system_prompt_sha256"]:
        raise ValueError("frozen routing system prompt changed")


async def run(panel: str, device: str, *, allow_downloads: bool = False) -> dict:
    manifest = verify_frozen()
    verify_router_freeze(manifest)
    rows, repetitions = panel_cases(panel)
    cases = [BenchmarkCase(r["id"], r["prompt"], r["expected_task"], tuple(sorted(r["tags"])))
             for r in rows]
    requested = cases * repetitions
    directory = WORK / "runs" / panel
    configuration = {
        "panel": panel, "corpus_manifest_sha256": digest(manifest),
        "case_order_sha256": digest([c.case_id for c in requested]),
        "device": device, "repetitions": repetitions,
        "local_files_only": not allow_downloads,
        "strategies": ["axon-heuristic", "laya", "strands", "llm-router"],
        "llm_model": "gpt-4o-mini-2024-07-18", "llm_json_schema": True,
        "llm_input_cost_per_million": 0.15, "llm_output_cost_per_million": 0.60,
        "llm_cache_bust": True, "llm_max_output_tokens": 256,
        "llm_timeout_seconds": 60, "concurrency": 1, "warmup_requests": 3, "seed": SEED,
        "runtime_lock_sha256": hashlib.sha256(
            (ROOT / "benchmarks/routing/runtime/uv.lock").read_bytes()).hexdigest(),
    }
    config_file = directory / "configuration.json"
    if config_file.exists() and read_json(config_file) != configuration:
        raise ValueError("checkpoint configuration differs; do not mix measurement runs")
    write_json(config_file, configuration)
    if not os.environ.get("OPENAI_API_KEY"):
        raise ValueError("OPENAI_API_KEY must be set for the four-way run")
    routers = [
        HeuristicRouter(),
        LocalDecisionRouter("laya", device=device, local_files_only=not allow_downloads),
        LocalDecisionRouter("strands", device=device, local_files_only=not allow_downloads),
        OpenAICompatibleLLMRouter(
            base_url="https://api.openai.com/v1", model=configuration["llm_model"],
            api_key=os.environ["OPENAI_API_KEY"], input_cost_per_million=0.15,
            output_cost_per_million=0.60, json_schema=True,
        ),
    ]
    records = {}
    runtime = {}
    for router in routers:
        checkpoint = directory / f"{router.name}.jsonl"
        metadata = directory / f"{router.name}-metadata.json"
        previous = [json.loads(line) for line in checkpoint.read_text().splitlines()] if checkpoint.exists() else []
        if len(previous) > len(requested):
            raise ValueError("checkpoint has too many records")
        results = []
        for expected, saved in zip(requested, previous):
            if (saved["case_id"] != expected.case_id or saved["prompt"] != expected.prompt
                    or saved["expected_task"] != expected.expected_task):
                raise ValueError("checkpoint does not match frozen request sequence")
            results.append(CaseResult(
                saved["case_id"], saved["prompt"], saved["expected_task"], tuple(saved["tags"]),
                RouteDecision(**saved["decision"]),
            ))
        info = read_json(metadata) if metadata.exists() else {"segments": []}
        if len(results) < len(requested):
            print(f"{panel}: preparing {router.name}; {len(results)}/{len(requested)} restored",
                  flush=True)
            started = time.perf_counter()
            try:
                if hasattr(router, "start"):
                    await router.start()
                segment = {
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "first_record": len(results),
                    "initialization_ms": (time.perf_counter() - started) * 1000,
                    "warmup": [],
                }
                info.update(getattr(router, "metadata", {}))
                for prompt in WARMUP_PROMPTS:
                    decision = await router.route(prompt)
                    segment["warmup"].append(asdict(decision))
                    if decision.error:
                        raise RuntimeError(f"{router.name} warmup failed: {decision.error}")
                info["segments"].append(segment)
                info["initialization_ms"] = sum(s["initialization_ms"] for s in info["segments"])
                info["warmup"] = [w for s in info["segments"] for w in s["warmup"]]
                write_json(metadata, info)
                with checkpoint.open("a") as stream:
                    for case in requested[len(results):]:
                        result = (await evaluate_router(router, [case]))[0]
                        stream.write(json.dumps(asdict(result)) + "\n")
                        stream.flush()
                        results.append(result)
                        if len(results) % 100 == 0 or len(results) == len(requested):
                            errors = sum(r.decision.error is not None for r in results)
                            print(f"{panel}: {router.name} {len(results)}/{len(requested)}; "
                                  f"{errors} request errors", flush=True)
                info["segments"][-1]["completed_at"] = datetime.now(timezone.utc).isoformat()
                write_json(metadata, info)
            finally:
                if hasattr(router, "close"):
                    await router.close()
        records[router.name] = results
        runtime[router.name] = info
    report = build_report(
        cases=cases, summaries=[summarize_results(r) for r in records.values()],
        results_by_strategy=records, source="benchmarks/routing/data/v2/test.jsonl",
        repetitions=repetitions,
    )
    report["configuration"] = configuration
    report["runtime"] = runtime
    report["environment"] = {"python": platform.python_version(), "system": platform.system(),
                             "release": platform.release(), "machine": platform.machine()}
    report["corpus_manifest_sha256"] = digest(manifest)
    report["implementation_sha256"] = {
        name: hashlib.sha256((ROOT / "src/gateway" / name).read_bytes()).hexdigest()
        for name in ("task_classifier.py", "autorouting_benchmark.py", "decision_model_routing.py")
    }
    report["runner_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    destination = ROOT / "docs/benchmarks" / f"smart-routing-v2-{panel}-2026-10-02"
    write_json(destination.with_suffix(".json"), report)
    destination.with_suffix(".md").write_text(render_markdown(report))
    print(render_markdown(report))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--panel", choices=("primary", "repeatability"), default="primary")
    parser.add_argument("--device", choices=("cpu", "mps", "cuda"), default="mps")
    parser.add_argument("--allow-downloads", action="store_true",
                        help="allow initial pinned model downloads during initialization")
    args = parser.parse_args()
    report = asyncio.run(run(args.panel, args.device, allow_downloads=args.allow_downloads))
    raise SystemExit(1 if any(s["errors"] for s in report["strategies"]) else 0)


if __name__ == "__main__":
    main()
