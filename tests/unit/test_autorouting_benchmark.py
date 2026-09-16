"""Tests for the reproducible heuristic-versus-LLM routing benchmark."""

from __future__ import annotations

from collections import Counter

import pytest

from src.gateway.autorouting_benchmark import (
    ERROR_LABEL,
    BenchmarkCase,
    CaseResult,
    HeuristicRouter,
    HybridRouter,
    OpenAICompatibleLLMRouter,
    RouteDecision,
    TASK_TYPES,
    _parse_llm_classification,
    compare_summaries,
    evaluate_router,
    load_corpus,
    select_cases,
    summarize_results,
)


class _FakeLLMRouter:
    name = "llm-router"

    def __init__(self, task_type: str = "general") -> None:
        self.task_type = task_type
        self.calls = 0

    async def route(self, _prompt: str) -> RouteDecision:
        self.calls += 1
        return RouteDecision(
            strategy=self.name,
            task_type=self.task_type,
            confidence=0.9,
            latency_ms=10.0,
            llm_calls=1,
            input_tokens=100,
            output_tokens=10,
            cost_usd=0.0001,
        )


def test_packaged_corpus_is_balanced_and_scenario_labeled():
    cases = load_corpus()

    assert len(cases) == 72
    assert Counter(case.expected_task for case in cases) == {
        task_type: 12 for task_type in TASK_TYPES
    }
    assert all(case.tags for case in cases)
    assert any("collision" in case.tags for case in cases)
    assert any("multilingual" in case.tags for case in cases)


def test_case_selection_is_deterministic_and_filterable():
    cases = load_corpus()

    first = select_cases(cases, sample_size=12, seed=775)
    second = select_cases(cases, sample_size=12, seed=775)
    multilingual = select_cases(
        cases,
        include_tags=("multilingual",),
    )

    assert [case.case_id for case in first] == [
        case.case_id for case in second
    ]
    assert Counter(case.expected_task for case in first) == {
        task_type: 2 for task_type in TASK_TYPES
    }
    assert all("multilingual" in case.tags for case in multilingual)


@pytest.mark.asyncio
async def test_heuristic_baseline_runs_without_network_calls():
    cases = load_corpus()
    results = await evaluate_router(HeuristicRouter(), cases)
    summary = summarize_results(results)

    assert summary["strategy"] == "axon-heuristic"
    assert summary["requests"] == 72
    assert summary["errors"] == 0
    assert summary["llm_calls"] == 0
    assert summary["router_cost_usd"] == 0.0
    assert summary["accuracy"] >= 0.5
    assert summary["latency_ms"]["p95"] < 10.0


@pytest.mark.asyncio
async def test_hybrid_escalates_only_below_confidence_threshold():
    heuristic = HeuristicRouter()
    llm = _FakeLLMRouter(task_type="general")
    hybrid = HybridRouter(
        heuristic,
        llm,  # type: ignore[arg-type]
        confidence_threshold=0.3,
    )

    clear = await hybrid.route(
        "Implement a Python function to sort a list."
    )
    uncertain = await hybrid.route("Hello there")

    assert clear.task_type == "coding"
    assert clear.llm_calls == 0
    assert uncertain.task_type == "general"
    assert uncertain.llm_calls == 1
    assert uncertain.heuristic_confidence == 0.0
    assert llm.calls == 1


def test_llm_parser_accepts_plain_or_fenced_json():
    plain = {
        "choices": [
            {
                "message": {
                    "content": (
                        '{"task_type":"reasoning","confidence":0.82}'
                    )
                }
            }
        ]
    }
    fenced = {
        "choices": [
            {
                "message": {
                    "content": (
                        "```json\n"
                        '{"task_type":"math","confidence":0.91}'
                        "\n```"
                    )
                }
            }
        ]
    }

    assert _parse_llm_classification(plain) == ("reasoning", 0.82)
    assert _parse_llm_classification(fenced) == ("math", 0.91)


def test_llm_parser_rejects_labels_outside_shared_taxonomy():
    document = {
        "choices": [
            {
                "message": {
                    "content": (
                        '{"task_type":"medium","confidence":0.8}'
                    )
                }
            }
        ]
    }

    with pytest.raises(
        RuntimeError,
        match="unsupported task type",
    ):
        _parse_llm_classification(document)


def test_remote_plaintext_endpoint_is_rejected_before_key_use():
    with pytest.raises(ValueError, match="plaintext HTTP"):
        OpenAICompatibleLLMRouter(
            base_url="http://router.example.com/v1",
            model="small-router",
            api_key="not-sent",
        )


@pytest.mark.parametrize(
    ("timeout_seconds", "max_output_tokens", "message"),
    [
        (0.0, 256, "timeout"),
        (60.0, 0, "max output tokens"),
    ],
)
def test_llm_router_rejects_invalid_runtime_limits(
    timeout_seconds,
    max_output_tokens,
    message,
):
    with pytest.raises(ValueError, match=message):
        OpenAICompatibleLLMRouter(
            base_url="http://127.0.0.1:8001/v1",
            model="small-router",
            timeout_seconds=timeout_seconds,
            max_output_tokens=max_output_tokens,
        )


def test_summary_counts_failed_llm_attempts_and_known_cost():
    case = BenchmarkCase(
        case_id="case",
        prompt="prompt",
        expected_task="general",
        tags=("clear",),
    )
    result = CaseResult(
        case_id=case.case_id,
        prompt=case.prompt,
        expected_task=case.expected_task,
        tags=case.tags,
        decision=RouteDecision(
            strategy="llm-router",
            task_type=ERROR_LABEL,
            confidence=0.0,
            latency_ms=20.0,
            llm_calls=1,
            input_tokens=100,
            output_tokens=20,
            cost_usd=0.0002,
            error="malformed classification JSON",
        ),
    )

    summary = summarize_results([result])

    assert summary["errors"] == 1
    assert summary["llm_calls"] == 1
    assert summary["llm_call_rate"] == 1.0
    assert summary["router_cost_usd"] == pytest.approx(0.0002)
    assert summary["router_cost_per_1k_requests_usd"] == pytest.approx(
        0.2
    )


def test_comparison_reports_break_even_and_cost_per_correct_route():
    baseline = {
        "strategy": "axon-heuristic",
        "requests": 100,
        "correct": 70,
        "accuracy": 0.70,
        "macro_f1": 0.68,
        "latency_ms": {"p50": 0.01, "p95": 0.02},
        "router_cost_usd": 0.0,
    }
    candidate = {
        "strategy": "llm-router",
        "requests": 100,
        "correct": 80,
        "accuracy": 0.80,
        "macro_f1": 0.79,
        "latency_ms": {"p50": 300.0, "p95": 500.0},
        "router_cost_usd": 0.01,
    }

    comparison = compare_summaries(baseline, candidate)

    assert comparison["additional_correct_routes"] == 10
    assert comparison[
        "break_even_downstream_savings_per_request_usd"
    ] == pytest.approx(0.0001)
    assert comparison[
        "router_cost_per_additional_correct_route_usd"
    ] == pytest.approx(0.001)
