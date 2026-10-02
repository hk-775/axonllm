"""Routing contracts and measurement controls without downloading model weights."""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

from src.gateway import autorouting_benchmark as benchmark
from src.gateway.decision_model_routing import (
    DecisionModelError,
    LocalDecisionRouter,
    parse_decision,
    routing_request,
)


def answer(choice="coding"):
    probabilities = {label: 0.02 for label in benchmark.TASK_TYPES}
    probabilities[choice] = 0.90
    return {"answers": {"task_type": {
        "type": "choice", "choice": choice,
        "probabilities": probabilities, "confidence": 0.88,
    }}}


def test_model_input_contains_only_prompt_and_shared_taxonomy():
    request = routing_request("The word coding here is just quoted text.")
    assert set(request) == {"state", "questions"}
    assert request["questions"]["task_type"]["criteria"] == benchmark.ROUTING_CRITERIA
    assert request["questions"]["task_type"]["instructions"] == benchmark.ROUTING_INSTRUCTIONS
    assert all(description in benchmark.ROUTER_SYSTEM_PROMPT
               for description in benchmark.ROUTING_CRITERIA.values())


@pytest.mark.parametrize("mutation", [
    lambda a: a["probabilities"].pop("general"),
    lambda a: a["probabilities"].update(coding=float("nan")),
    lambda a: a["probabilities"].update(coding=0.3),
    lambda a: a.update(choice="math"),
    lambda a: a.update(confidence=float("inf")),
])
def test_rejects_incomplete_invalid_or_inconsistent_choices(mutation):
    document = answer()
    mutation(document["answers"]["task_type"])
    with pytest.raises(DecisionModelError):
        parse_decision(document)


@pytest.mark.asyncio
async def test_local_prediction_synchronizes_and_keeps_confidence_semantics_separate():
    router = LocalDecisionRouter("laya")
    events = []
    router._synchronize = lambda: events.append("sync")

    def predict(request):
        events.append(request["state"])
        return answer()

    router._predict = predict
    result = await router.route("prompt")
    assert events == ["sync", "prompt", "sync"]
    assert result.confidence == 0.90
    assert result.native_confidence == 0.88
    assert result.probabilities["coding"] == 0.90
    assert result.cost_usd == 0
    assert result.llm_calls == 0
    assert result.latency_ms > 0
    assert result.error is None


@pytest.mark.asyncio
async def test_local_failure_has_zero_api_cost_without_leaking_exception():
    router = LocalDecisionRouter("strands")

    def fail(_request):
        raise ValueError("secret-or-private-model-output")

    router._predict = fail
    result = await router.route("prompt")
    assert result.error == "ValueError"
    assert result.cost_usd == 0
    summary = benchmark.summarize_results([
        benchmark.CaseResult("case", "prompt", "coding", (), result),
    ])
    assert summary["accuracy"] == 0
    assert summary["errors"] == 1
    assert summary["by_task"]["coding"] == {"requests": 1, "correct": 0, "accuracy": 0.0}
    assert summary["by_task"]["reasoning"]["accuracy"] is None
    assert summary["router_cost_usd"] == 0
    assert summary["hardware_electricity_cost_usd"] is None
    assert summary["hardware_electricity_cost_status"] == "unmeasured"


def test_unknown_api_cost_is_never_reported_as_free():
    result = benchmark.RouteDecision("test", "general", 1.0, 1.0, cost_usd=None)
    summary = benchmark.summarize_results([
        benchmark.CaseResult("case", "prompt", "general", (), result),
    ])
    assert summary["router_cost_usd"] is None
    assert summary["cost_coverage"] == 0


@pytest.mark.asyncio
async def test_cli_excludes_warmup_and_releases_models_sequentially(monkeypatch):
    events = []

    async def start(self):
        events.append(("start", self.name))

    async def route(self, prompt):
        events.append(("route", self.name, prompt))
        return benchmark.RouteDecision(self.name, "coding", 0.9, 2.0)

    async def close(self):
        events.append(("close", self.name))

    monkeypatch.setattr(LocalDecisionRouter, "start", start)
    monkeypatch.setattr(LocalDecisionRouter, "route", route)
    monkeypatch.setattr(LocalDecisionRouter, "close", close)
    args = benchmark.build_parser().parse_args([
        "--strategy", "laya", "--strategy", "strands", "--sample-size", "6",
        "--warmup-requests", "2", "--repetitions", "2",
    ])
    report = await benchmark.run_benchmark(args)
    assert [s["requests"] for s in report["strategies"]] == [12, 12]
    assert len(report["runtime"]["laya"]["warmup"]) == 2
    assert events.index(("close", "laya")) < events.index(("start", "strands"))
    assert len([e for e in events if e[:2] == ("route", "laya")]) == 14
    assert "Hardware / electricity cost" in benchmark.render_markdown(report)
    assert "Unmeasured" in benchmark.render_markdown(report)


@pytest.mark.asyncio
async def test_warmup_failure_still_releases_model(monkeypatch):
    events = []

    async def start(self):
        self._predict = lambda _: {}

    async def close(self):
        events.append("closed")

    monkeypatch.setattr(LocalDecisionRouter, "start", start)
    monkeypatch.setattr(LocalDecisionRouter, "close", close)
    args = benchmark.build_parser().parse_args(["--strategy", "laya", "--sample-size", "6"])
    with pytest.raises(RuntimeError, match="warmup failed"):
        await benchmark.run_benchmark(args)
    assert events == ["closed"]


@pytest.mark.asyncio
async def test_local_concurrency_rejected_before_loading():
    args = benchmark.build_parser().parse_args(["--strategy", "laya", "--concurrency", "2"])
    with pytest.raises(ValueError, match="concurrency 1"):
        await benchmark.run_benchmark(args)


def test_cli_baseline_needs_no_local_model_packages(tmp_path):
    # This suite itself runs in the normal gateway environment, without Torch.
    output = tmp_path / "new" / "report.json"
    assert benchmark.main([
        "--strategy", "regex", "--sample-size", "6", "--output-json", str(output),
    ]) == 0
    document = json.loads(output.read_text())
    assert document["strategies"][0]["strategy"] == "axon-heuristic"
    assert document["strategies"][0]["router_cost_usd"] == 0
    assert "torch" not in sys.modules


@pytest.mark.asyncio
async def test_llm_schema_and_missing_usage_accounting(aiohttp_server):
    from aiohttp import web

    observed = []

    async def handle(request):
        observed.append(await request.json())
        return web.json_response({
            "choices": [{"message": {"content": '{"task_type":"coding","confidence":0.9}'}}],
        })

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handle)
    server = await aiohttp_server(app)
    async with benchmark.OpenAICompatibleLLMRouter(
        base_url=str(server.make_url("/v1")), model="gpt-4o-mini-2024-07-18",
        input_cost_per_million=0.15, output_cost_per_million=0.60, json_schema=True,
    ) as router:
        result = await router.route("prompt")
    assert observed[0]["response_format"]["json_schema"]["strict"] is True
    assert result.cost_usd is None
    assert result.confidence_kind == "llm_self_report"


@pytest.fixture
async def aiohttp_server():
    """A loopback HTTP server exercises serialization and actual HTTP handling."""
    from aiohttp import web
    runners = []

    async def make(app):
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        runners.append(runner)
        port = site._server.sockets[0].getsockname()[1]

        class Server:
            def make_url(self, path):
                return f"http://127.0.0.1:{port}{path}"

        return Server()

    yield make
    await asyncio.gather(*(runner.cleanup() for runner in runners))
