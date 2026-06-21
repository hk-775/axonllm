"""Unit tests for Router.ensemble_route (scatter-gather-synthesize flow).

These tests exercise the orchestration logic of ``Router.ensemble_route`` by
mocking the ``execute_with_fallback`` boundary (the per-call provider path) so
that each panel member and the judge can be made to succeed, fail, or time out
deterministically. The cost tracker is mocked to verify per-call usage
recording and total-cost accounting.

Covers task 4.3 scenarios:
- Happy path: all panel members succeed, quorum met, judge synthesizes.
- Access control: disallowed panel/judge model raises before any dispatch.
- Cost ceiling: estimated (N+1)*per_call over ceiling raises with no dispatch.
- Quorum not met + best-single: returns ranked best survivor, judge skipped.
- Quorum not met + error: raises EnsembleQuorumError carrying the decision.
- Zero survivors: raises EnsembleNoSurvivorsError regardless of policy.
- Judge failure: raises EnsembleSynthesisError, survivors preserved.
- Partial panel failure tolerated: synthesis proceeds over survivors only.
"""

import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.gateway.ensemble import EnsembleStrategy
from src.gateway.health_tracker import ProviderHealthTracker
from src.gateway.model_registry import ModelRegistry
from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    EnsemblePreset,
    TokenUsage,
)
from src.gateway.router import (
    AllProvidersExhaustedError,
    EnsembleAccessError,
    EnsembleCostCeilingError,
    EnsembleNoSurvivorsError,
    EnsembleQuorumError,
    EnsembleSynthesisError,
    Router,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(model: str = "ensemble") -> ChatCompletionRequest:
    return ChatCompletionRequest(
        messages=[{"role": "user", "content": "What is the capital of France?"}],
        model=model,
    )


def _make_response(model: str, content: str = "answer", provider: str = "bedrock") -> ChatCompletionResponse:
    """Build a response whose ``model`` field doubles as the cost-map key."""
    return ChatCompletionResponse(
        id=f"resp-{model}",
        choices=[{"message": {"role": "assistant", "content": content}}],
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        model=model,
        provider=provider,
    )


def _make_preset(
    name: str = "test",
    panel: list[str] | None = None,
    judge: str = "claude-sonnet",
    quorum: int = 2,
    fallback_policy: str = "error",
    cost_ceiling: float | None = None,
    ranking_criteria: str = "length",
) -> EnsemblePreset:
    return EnsemblePreset(
        name=name,
        panel=panel if panel is not None else ["nova-lite", "mistral", "deepseek"],
        judge=judge,
        quorum=quorum,
        fallback_policy=fallback_policy,
        cost_ceiling=cost_ceiling,
        ranking_criteria=ranking_criteria,
    )


def _make_cost_tracker(costs: dict[str, float] | None = None):
    """Mock CostTracker: calculate_cost keyed by response.model, async record_usage."""
    costs = costs or {}
    tracker = MagicMock()
    tracker.calculate_cost = MagicMock(
        side_effect=lambda provider, model, *args, **kwargs: costs.get(model, 0.0)
    )
    tracker.record_usage = AsyncMock()
    return tracker


def _make_factory():
    """provider_fn_factory whose create() returns a dummy provider_fn.

    Since ``execute_with_fallback`` is mocked, the provider_fn itself is never
    invoked; the factory only needs a callable ``create`` method.
    """
    factory = MagicMock()
    factory.create = MagicMock(return_value=AsyncMock())
    return factory


def _make_router(cost_tracker=None) -> Router:
    """Router with a real (empty) registry/health tracker; callers patch
    ``execute_with_fallback`` to control per-model outcomes."""
    return Router(
        model_registry=ModelRegistry(),
        health_tracker=ProviderHealthTracker(),
        max_retries=0,
        base_delay=0.0,
        cost_tracker=cost_tracker,
    )


def _exec_side_effect(
    responses: dict[str, ChatCompletionResponse],
    failures: set[str] | None = None,
    timeouts: set[str] | None = None,
):
    """Build an ``execute_with_fallback`` side effect keyed by request.model.

    - models in ``timeouts`` sleep long enough to trip ``asyncio.wait_for``
    - models in ``failures`` raise ``AllProvidersExhaustedError``
    - otherwise the mapped response is returned
    """
    failures = failures or set()
    timeouts = timeouts or set()

    async def _side_effect(request, provider_fn, allowed_models=None, **kwargs):
        model = request.model
        if model in timeouts:
            await asyncio.sleep(10.0)
        if model in failures:
            raise AllProvidersExhaustedError(
                [{"provider": "bedrock", "status_code": 503, "message": "down"}]
            )
        return responses[model]

    return _side_effect


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestEnsembleHappyPath:
    @pytest.mark.asyncio
    async def test_all_succeed_quorum_met_judge_synthesizes(self):
        preset = _make_preset(quorum=2)
        costs = {
            "nova-lite": 0.01,
            "mistral": 0.02,
            "deepseek": 0.03,
            "claude-sonnet": 0.10,
        }
        responses = {
            "nova-lite": _make_response("nova-lite", "Paris is the capital."),
            "mistral": _make_response("mistral", "The capital is Paris."),
            "deepseek": _make_response("deepseek", "Paris."),
            "claude-sonnet": _make_response("claude-sonnet", "Final: Paris."),
        }
        tracker = _make_cost_tracker(costs)
        router = _make_router(cost_tracker=tracker)
        router.execute_with_fallback = AsyncMock(
            side_effect=_exec_side_effect(responses)
        )

        request = _make_request()
        response, decision = await router.ensemble_route(
            request,
            _make_factory(),
            prompt="What is the capital of France?",
            preset=preset,
            allowed_models=None,
            project_id="proj-1",
            user_id="user-1",
        )

        # Final response is the judge's synthesized answer
        assert response.model == "claude-sonnet"

        # Decision metadata
        assert decision.succeeded == ["nova-lite", "mistral", "deepseek"]
        assert decision.failed == []
        assert decision.succeeded_count == 3
        assert decision.quorum_met is True
        assert decision.judge_invoked is True
        assert decision.fallback_used is False
        assert decision.cost_multiplier == 4.0  # N + 1
        # total_cost = survivor panel costs + judge cost
        assert decision.total_cost == pytest.approx(0.01 + 0.02 + 0.03 + 0.10)

        # One usage entry per survivor + one for the judge = 4
        assert tracker.record_usage.await_count == 4

        # Judge invoked exactly once (4 panel + judge calls total = 4)
        assert router.execute_with_fallback.await_count == 4


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------


class TestEnsembleAccessControl:
    @pytest.mark.asyncio
    async def test_disallowed_judge_raises_before_dispatch(self):
        preset = _make_preset(quorum=2)
        router = _make_router(cost_tracker=_make_cost_tracker())
        router.execute_with_fallback = AsyncMock()  # should never be called

        # Panel allowed but judge missing
        allowed = {"nova-lite", "mistral", "deepseek"}

        with pytest.raises(EnsembleAccessError) as exc_info:
            await router.ensemble_route(
                _make_request(),
                _make_factory(),
                prompt="hi",
                preset=preset,
                allowed_models=allowed,
            )

        assert exc_info.value.model == "claude-sonnet"
        router.execute_with_fallback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_disallowed_panel_member_raises_before_dispatch(self):
        preset = _make_preset(quorum=2)
        router = _make_router(cost_tracker=_make_cost_tracker())
        router.execute_with_fallback = AsyncMock()

        allowed = {"nova-lite", "deepseek", "claude-sonnet"}  # missing "mistral"

        with pytest.raises(EnsembleAccessError) as exc_info:
            await router.ensemble_route(
                _make_request(),
                _make_factory(),
                prompt="hi",
                preset=preset,
                allowed_models=allowed,
            )

        assert exc_info.value.model == "mistral"
        router.execute_with_fallback.assert_not_awaited()


# ---------------------------------------------------------------------------
# Cost ceiling
# ---------------------------------------------------------------------------


class TestEnsembleCostCeiling:
    @pytest.mark.asyncio
    async def test_estimate_over_ceiling_raises_no_dispatch_no_usage(self):
        # N=3 → (N+1)=4; 4 * 0.20 = 0.80 > ceiling 0.50
        preset = _make_preset(quorum=2, cost_ceiling=0.50)
        tracker = _make_cost_tracker()
        router = _make_router(cost_tracker=tracker)
        router.execute_with_fallback = AsyncMock()

        with pytest.raises(EnsembleCostCeilingError) as exc_info:
            await router.ensemble_route(
                _make_request(),
                _make_factory(),
                prompt="hi",
                preset=preset,
                allowed_models=None,
                per_call_cost_estimate=0.20,
            )

        assert exc_info.value.estimated == pytest.approx(0.80)
        assert exc_info.value.ceiling == 0.50
        router.execute_with_fallback.assert_not_awaited()
        tracker.record_usage.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_estimate_at_or_under_ceiling_proceeds(self):
        # 4 * 0.10 = 0.40 <= ceiling 0.50 → dispatch proceeds
        preset = _make_preset(quorum=2, cost_ceiling=0.50)
        responses = {
            "nova-lite": _make_response("nova-lite", "a"),
            "mistral": _make_response("mistral", "b"),
            "deepseek": _make_response("deepseek", "c"),
            "claude-sonnet": _make_response("claude-sonnet", "final"),
        }
        router = _make_router(cost_tracker=_make_cost_tracker())
        router.execute_with_fallback = AsyncMock(
            side_effect=_exec_side_effect(responses)
        )

        response, decision = await router.ensemble_route(
            _make_request(),
            _make_factory(),
            prompt="hi",
            preset=preset,
            allowed_models=None,
            per_call_cost_estimate=0.10,
        )

        assert decision.judge_invoked is True
        assert response.model == "claude-sonnet"


# ---------------------------------------------------------------------------
# Quorum not met
# ---------------------------------------------------------------------------


class TestEnsembleQuorumNotMet:
    @pytest.mark.asyncio
    async def test_best_single_returns_ranked_survivor_no_judge(self):
        # quorum=3 but only 2 survive → below quorum, best-single fallback
        preset = _make_preset(quorum=3, fallback_policy="best-single")
        responses = {
            # "mistral" has the longest content → ranked best by "length"
            "nova-lite": _make_response("nova-lite", "short"),
            "mistral": _make_response("mistral", "this is a much longer answer"),
            "deepseek": _make_response("deepseek", "down"),  # will fail
        }
        router = _make_router(cost_tracker=_make_cost_tracker())
        router.execute_with_fallback = AsyncMock(
            side_effect=_exec_side_effect(responses, failures={"deepseek"})
        )

        response, decision = await router.ensemble_route(
            _make_request(),
            _make_factory(),
            prompt="hi",
            preset=preset,
            allowed_models=None,
        )

        # Best (longest) survivor returned, judge never invoked
        assert response.model == "mistral"
        assert decision.fallback_used is True
        assert decision.judge_invoked is False
        assert decision.quorum_met is False
        assert decision.succeeded_count == 2
        # Judge was not dispatched: only 3 panel calls happened
        assert router.execute_with_fallback.await_count == 3

    @pytest.mark.asyncio
    async def test_error_policy_raises_quorum_error_with_decision(self):
        # quorum=3, only 2 survive, error policy → EnsembleQuorumError
        preset = _make_preset(quorum=3, fallback_policy="error")
        responses = {
            "nova-lite": _make_response("nova-lite", "a"),
            "mistral": _make_response("mistral", "b"),
            "deepseek": _make_response("deepseek", "c"),
        }
        router = _make_router(cost_tracker=_make_cost_tracker())
        router.execute_with_fallback = AsyncMock(
            side_effect=_exec_side_effect(responses, failures={"deepseek"})
        )

        with pytest.raises(EnsembleQuorumError) as exc_info:
            await router.ensemble_route(
                _make_request(),
                _make_factory(),
                prompt="hi",
                preset=preset,
                allowed_models=None,
            )

        decision = exc_info.value.decision
        assert decision.quorum_met is False
        assert decision.succeeded_count == 2
        assert decision.quorum_threshold == 3
        assert decision.judge_invoked is False
        assert decision.error is not None
        # Judge not invoked: only the 3 panel calls happened
        assert router.execute_with_fallback.await_count == 3


# ---------------------------------------------------------------------------
# Zero survivors
# ---------------------------------------------------------------------------


class TestEnsembleNoSurvivors:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("policy", ["best-single", "error"])
    async def test_zero_survivors_raises_regardless_of_policy(self, policy):
        preset = _make_preset(quorum=1, fallback_policy=policy)
        responses: dict = {}
        router = _make_router(cost_tracker=_make_cost_tracker())
        router.execute_with_fallback = AsyncMock(
            side_effect=_exec_side_effect(
                responses, failures={"nova-lite", "mistral", "deepseek"}
            )
        )

        with pytest.raises(EnsembleNoSurvivorsError) as exc_info:
            await router.ensemble_route(
                _make_request(),
                _make_factory(),
                prompt="hi",
                preset=preset,
                allowed_models=None,
            )

        decision = exc_info.value.decision
        assert decision.succeeded == []
        assert len(decision.failed) == 3
        assert decision.judge_invoked is False
        # Only the 3 panel calls happened; judge never dispatched
        assert router.execute_with_fallback.await_count == 3


# ---------------------------------------------------------------------------
# Judge failure
# ---------------------------------------------------------------------------


class TestEnsembleJudgeFailure:
    @pytest.mark.asyncio
    async def test_judge_failure_raises_synthesis_error_survivors_preserved(self):
        preset = _make_preset(quorum=2)
        responses = {
            "nova-lite": _make_response("nova-lite", "a"),
            "mistral": _make_response("mistral", "b"),
            "deepseek": _make_response("deepseek", "c"),
            # judge entry omitted; judge is in failures set
        }
        router = _make_router(cost_tracker=_make_cost_tracker())
        router.execute_with_fallback = AsyncMock(
            side_effect=_exec_side_effect(responses, failures={"claude-sonnet"})
        )

        with pytest.raises(EnsembleSynthesisError) as exc_info:
            await router.ensemble_route(
                _make_request(),
                _make_factory(),
                prompt="hi",
                preset=preset,
                allowed_models=None,
            )

        decision = exc_info.value.decision
        # Survivors preserved in the decision despite judge failure
        assert decision.succeeded == ["nova-lite", "mistral", "deepseek"]
        assert decision.judge_invoked is False
        assert decision.error is not None
        assert "synthesis failed" in decision.error


# ---------------------------------------------------------------------------
# Partial panel failure tolerated
# ---------------------------------------------------------------------------


class TestEnsemblePartialFailureTolerated:
    @pytest.mark.asyncio
    async def test_some_fail_quorum_met_synthesis_over_survivors_only(self):
        # quorum=2, one of three members fails → 2 survivors meet quorum
        preset = _make_preset(quorum=2)
        responses = {
            "nova-lite": _make_response("nova-lite", "survivor one"),
            "deepseek": _make_response("deepseek", "survivor two"),
            "claude-sonnet": _make_response("claude-sonnet", "synthesized"),
            # "mistral" omitted; it fails
        }
        router = _make_router(cost_tracker=_make_cost_tracker())
        router.execute_with_fallback = AsyncMock(
            side_effect=_exec_side_effect(responses, failures={"mistral"})
        )

        response, decision = await router.ensemble_route(
            _make_request(),
            _make_factory(),
            prompt="What is the capital of France?",
            preset=preset,
            allowed_models=None,
        )

        # Synthesis proceeded
        assert response.model == "claude-sonnet"
        assert decision.judge_invoked is True
        assert decision.quorum_met is True
        assert decision.succeeded == ["nova-lite", "deepseek"]
        assert len(decision.failed) == 1
        assert decision.failed[0]["model"] == "mistral"
        assert decision.failed[0]["reason"] is not None

        # The judge request must contain ONLY survivor content, never the
        # failed member's identity/content (survivors-only synthesis).
        judge_call = router.execute_with_fallback.await_args_list[-1]
        judge_req = judge_call.args[0]
        assert judge_req.model == "claude-sonnet"
        judge_text = str(judge_req.messages)
        assert "survivor one" in judge_text
        assert "survivor two" in judge_text
        assert "mistral" not in judge_text


# ---------------------------------------------------------------------------
# Per-member timeout (tasks.md 4.3 extra)
# ---------------------------------------------------------------------------


class TestEnsembleMemberTimeout:
    @pytest.mark.asyncio
    async def test_member_exceeding_timeout_recorded_failed(self, monkeypatch):
        # Shrink the per-member timeout so a slow member trips wait_for quickly.
        monkeypatch.setattr(EnsembleStrategy, "PER_MEMBER_TIMEOUT_SECONDS", 0.05)

        preset = _make_preset(quorum=2)
        responses = {
            "nova-lite": _make_response("nova-lite", "fast a"),
            "mistral": _make_response("mistral", "fast b"),
            "deepseek": _make_response("deepseek", "slow"),  # times out
            "claude-sonnet": _make_response("claude-sonnet", "final"),
        }
        router = _make_router(cost_tracker=_make_cost_tracker())
        router.execute_with_fallback = AsyncMock(
            side_effect=_exec_side_effect(responses, timeouts={"deepseek"})
        )

        response, decision = await router.ensemble_route(
            _make_request(),
            _make_factory(),
            prompt="hi",
            preset=preset,
            allowed_models=None,
        )

        assert decision.judge_invoked is True
        assert "deepseek" not in decision.succeeded
        timed_out = [f for f in decision.failed if f["model"] == "deepseek"]
        assert len(timed_out) == 1
        assert "timeout" in timed_out[0]["reason"]
