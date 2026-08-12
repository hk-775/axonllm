"""Unit tests for GatewayAgent ensemble routing integration and streaming.

Covers spec tasks 6.3 (agent integration) and 7.2 (streaming) of the
ensemble-routing spec.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.gateway.agent import GatewayAgent
from src.gateway.cache_manager import CacheManager
from src.gateway.cost_tracker import CostTracker
from src.gateway.guardrail_engine import GuardrailEngine
from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    EnsembleDecision,
    EnsemblePreset,
    Project,
    ProviderModelMapping,
    RateLimitResult,
    TokenPricing,
    TokenUsage,
)
from src.gateway.rate_limiter import SlidingWindowRateLimiter
from src.gateway.router import (
    EnsembleAccessError,
    EnsembleNoSurvivorsError,
    EnsembleQuorumError,
    EnsembleSynthesisError,
    Router,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(
    content: str = "Synthesized answer",
    model: str = "judge-model",
    provider: str = "anthropic",
    resp_id: str = "resp-1",
) -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id=resp_id,
        choices=[{"index": 0, "message": {"role": "assistant", "content": content}}],
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        model=model,
        provider=provider,
    )


def _make_preset(
    name: str = "budget",
    panel: list[str] | None = None,
    judge: str = "judge-model",
    quorum: int = 1,
    fallback_policy: str = "error",
) -> EnsemblePreset:
    return EnsemblePreset(
        name=name,
        panel=panel or ["model-a", "model-b"],
        judge=judge,
        quorum=quorum,
        fallback_policy=fallback_policy,
    )


def _make_decision(
    preset_name: str = "budget",
    panel_members: list[str] | None = None,
    judge_model: str = "judge-model",
    succeeded: list[str] | None = None,
    failed: list[dict] | None = None,
    quorum_met: bool = True,
    succeeded_count: int = 2,
    quorum_threshold: int = 1,
    total_cost: float = 0.05,
    cost_multiplier: float = 3.0,
    fallback_used: bool = False,
    judge_invoked: bool = True,
    error: str | None = None,
) -> EnsembleDecision:
    return EnsembleDecision(
        preset_name=preset_name,
        panel_members=panel_members if panel_members is not None else ["model-a", "model-b"],
        judge_model=judge_model,
        succeeded=succeeded if succeeded is not None else ["model-a", "model-b"],
        failed=failed if failed is not None else [],
        quorum_met=quorum_met,
        succeeded_count=succeeded_count,
        quorum_threshold=quorum_threshold,
        total_cost=total_cost,
        cost_multiplier=cost_multiplier,
        fallback_used=fallback_used,
        judge_invoked=judge_invoked,
        error=error,
    )


def _base_context() -> dict:
    return {
        "user_id": "user-1",
        "project_id": "proj-1",
        "roles": ["developer"],
        "scopes": ["chat"],
    }


def _make_ensemble_config(
    *,
    is_configured: bool = True,
    preset: EnsemblePreset | None = None,
    default: EnsemblePreset | None = None,
    get_preset_map: dict | None = None,
) -> MagicMock:
    """Build a mock EnsembleConfig with controllable preset resolution."""
    cfg = MagicMock()
    cfg.is_configured = is_configured
    if get_preset_map is not None:
        cfg.get_preset.side_effect = lambda name: get_preset_map.get(name)
    else:
        cfg.get_preset.return_value = preset
    cfg.default_preset.return_value = default if default is not None else preset
    return cfg


@pytest.fixture
def mock_rate_limiter():
    rl = MagicMock(spec=SlidingWindowRateLimiter)
    rl.check_rate_limit = AsyncMock(
        return_value=RateLimitResult(
            allowed=True, limit=60, remaining=59,
            reset_at=datetime.utcnow(), retry_after_seconds=None,
        )
    )
    return rl


@pytest.fixture
def cost_tracker():
    pricing = {
        "anthropic": {
            "claude-sonnet": TokenPricing(prompt_token_cost=0.003, completion_token_cost=0.015),
        }
    }
    return CostTracker(pricing_config=pricing)


def _make_router(ensemble_config: MagicMock | None) -> MagicMock:
    """Build a mock Router wired for ensemble routing tests."""
    router = MagicMock(spec=Router)
    router._smart_strategy = None
    router._ensemble_config = ensemble_config
    # Default: no fallback chain → per-call estimate resolves to 0.0 (budget
    # pre-check becomes a no-op) so non-budget tests are unaffected.
    router.get_fallback_chain.return_value = []
    router.ensemble_route = AsyncMock()
    router.execute_with_fallback = AsyncMock()
    return router


def _make_agent(
    router: MagicMock,
    cost_tracker: CostTracker,
    mock_rate_limiter,
    projects: dict | None = None,
) -> GatewayAgent:
    factory = MagicMock()
    factory.create = MagicMock(return_value=AsyncMock())
    return GatewayAgent(
        router=router,
        rate_limiter=mock_rate_limiter,
        guardrail_engine=GuardrailEngine(),
        cache_manager=CacheManager(),
        cost_tracker=cost_tracker,
        provider_fn_factory=factory,
        projects=projects,
    )


# ===========================================================================
# TASK 6.3 — _is_ensemble_request detection
# ===========================================================================


class TestIsEnsembleRequest:
    """Detection logic for ensemble routing (Req 7.1-7.7, 12.4)."""

    def _agent(self, mock_rate_limiter, cost_tracker, ensemble_config):
        router = _make_router(ensemble_config)
        return _make_agent(router, cost_tracker, mock_rate_limiter)

    def test_context_flag_true_uses_default_preset(self, mock_rate_limiter, cost_tracker):
        """context {"ensemble": True} → ensemble with default preset (Req 7.3)."""
        agent = self._agent(mock_rate_limiter, cost_tracker, _make_ensemble_config())
        req = ChatCompletionRequest(messages=[{"role": "user", "content": "hi"}], model="gpt-4")
        is_ens, preset_name, err = agent._is_ensemble_request(req, {"ensemble": True})
        assert is_ens is True
        assert preset_name is None
        assert err is None

    def test_model_ensemble_uses_default_preset(self, mock_rate_limiter, cost_tracker):
        """model == "ensemble" → ensemble with default preset (Req 7.1)."""
        agent = self._agent(mock_rate_limiter, cost_tracker, _make_ensemble_config())
        req = ChatCompletionRequest(messages=[{"role": "user", "content": "hi"}], model="ensemble")
        is_ens, preset_name, err = agent._is_ensemble_request(req, {})
        assert is_ens is True
        assert preset_name is None
        assert err is None

    def test_model_ensemble_named_preset(self, mock_rate_limiter, cost_tracker):
        """model == "ensemble:<name>" → named preset (Req 7.2)."""
        agent = self._agent(mock_rate_limiter, cost_tracker, _make_ensemble_config())
        req = ChatCompletionRequest(messages=[{"role": "user", "content": "hi"}], model="ensemble:budget")
        is_ens, preset_name, err = agent._is_ensemble_request(req, {})
        assert is_ens is True
        assert preset_name == "budget"
        assert err is None

    def test_model_value_precedence_over_flag(self, mock_rate_limiter, cost_tracker):
        """The model value takes precedence over the context flag (Req 7.7)."""
        agent = self._agent(mock_rate_limiter, cost_tracker, _make_ensemble_config())
        req = ChatCompletionRequest(
            messages=[{"role": "user", "content": "hi"}], model="ensemble:quality"
        )
        # Flag would imply the default preset, but the named model wins.
        is_ens, preset_name, err = agent._is_ensemble_request(req, {"ensemble": True})
        assert is_ens is True
        assert preset_name == "quality"
        assert err is None

    def test_empty_preset_name_returns_error(self, mock_rate_limiter, cost_tracker):
        """"ensemble:" with empty name → error (Req 7.6)."""
        agent = self._agent(mock_rate_limiter, cost_tracker, _make_ensemble_config())
        req = ChatCompletionRequest(messages=[{"role": "user", "content": "hi"}], model="ensemble:")
        is_ens, preset_name, err = agent._is_ensemble_request(req, {})
        assert is_ens is True
        assert preset_name is None
        assert err is not None
        assert "preset name" in err.lower()

    def test_no_ensemble_config_not_ensemble(self, mock_rate_limiter, cost_tracker):
        """ensemble_config absent (None) → never treated as ensemble (Req 12.4)."""
        agent = self._agent(mock_rate_limiter, cost_tracker, None)
        req = ChatCompletionRequest(messages=[{"role": "user", "content": "hi"}], model="ensemble")
        is_ens, preset_name, err = agent._is_ensemble_request(req, {"ensemble": True})
        assert is_ens is False
        assert preset_name is None
        assert err is None

    def test_normal_model_not_ensemble(self, mock_rate_limiter, cost_tracker):
        """A plain model with no flag is not an ensemble request."""
        agent = self._agent(mock_rate_limiter, cost_tracker, _make_ensemble_config())
        req = ChatCompletionRequest(messages=[{"role": "user", "content": "hi"}], model="claude-sonnet")
        is_ens, preset_name, err = agent._is_ensemble_request(req, {})
        assert is_ens is False


# ===========================================================================
# TASK 6.3 — handle_chat_completion ensemble path
# ===========================================================================


class TestEnsembleRoutingIntegration:
    """Ensemble routing in handle_chat_completion (Req 7.5-7.6, 9.6-9.7, 11.x, 12.x)."""

    @pytest.mark.asyncio
    async def test_named_preset_resolution_and_metadata(self, mock_rate_limiter, cost_tracker):
        """Named preset resolves and the ensemble metadata block is populated."""
        preset = _make_preset(name="budget")
        cfg = _make_ensemble_config(get_preset_map={"budget": preset})
        router = _make_router(cfg)
        decision = _make_decision(preset_name="budget")
        router.ensemble_route = AsyncMock(return_value=(_make_response(), decision))

        agent = _make_agent(router, cost_tracker, mock_rate_limiter)
        request_data = {
            "messages": [{"role": "user", "content": "Explain quorum"}],
            "model": "ensemble:budget",
        }

        result = await agent.handle_chat_completion(request_data, _base_context())

        # Named preset was resolved and passed to the router.
        router.ensemble_route.assert_awaited_once()
        passed_preset = router.ensemble_route.call_args[0][3]
        assert passed_preset.name == "budget"

        # Metadata block populated with all required keys.
        assert "ensemble" in result
        meta = result["ensemble"]
        assert meta["preset"] == "budget"
        assert meta["panel"] == ["model-a", "model-b"]
        assert meta["judge"] == "judge-model"
        assert meta["succeeded"] == ["model-a", "model-b"]
        assert meta["failed"] == []
        assert meta["quorum_met"] is True
        assert meta["succeeded_count"] == 2
        assert meta["quorum_threshold"] == 1
        assert meta["total_cost"] == 0.05
        assert meta["cost_multiplier"] == 3.0
        assert meta["fallback_used"] is False

    @pytest.mark.asyncio
    async def test_unavailable_member_rejects_before_any_panel_call(
        self,
        mock_rate_limiter,
        cost_tracker,
    ):
        preset = _make_preset()
        router = _make_router(
            _make_ensemble_config(preset=preset)
        )
        router.is_model_available.side_effect = (
            lambda model: model != "judge-model"
        )
        agent = _make_agent(router, cost_tracker, mock_rate_limiter)

        result = await agent.handle_chat_completion(
            {
                "messages": [{"role": "user", "content": "review"}],
                "model": "ensemble",
            },
            _base_context(),
        )

        assert result["status_code"] == 503
        assert result["error"]["code"] == "model_unavailable"
        router.ensemble_route.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_preset_returns_404(self, mock_rate_limiter, cost_tracker):
        """Unknown named preset → 404 ensemble_preset_not_found (Req 7.6)."""
        cfg = _make_ensemble_config(get_preset_map={"budget": _make_preset()})
        router = _make_router(cfg)
        agent = _make_agent(router, cost_tracker, mock_rate_limiter)

        request_data = {
            "messages": [{"role": "user", "content": "hi"}],
            "model": "ensemble:nope",
        }
        result = await agent.handle_chat_completion(request_data, _base_context())

        assert result["status_code"] == 404
        assert result["error"]["code"] == "ensemble_preset_not_found"
        router.ensemble_route.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_default_configured_returns_400(self, mock_rate_limiter, cost_tracker):
        """No default preset configured → 400 ensemble_no_default."""
        cfg = _make_ensemble_config(default=None, preset=None)
        router = _make_router(cfg)
        agent = _make_agent(router, cost_tracker, mock_rate_limiter)

        request_data = {
            "messages": [{"role": "user", "content": "hi"}],
            "model": "ensemble",
        }
        result = await agent.handle_chat_completion(request_data, _base_context())

        assert result["status_code"] == 400
        assert result["error"]["code"] == "ensemble_no_default"
        router.ensemble_route.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_preset_name_returns_400(self, mock_rate_limiter, cost_tracker):
        """"ensemble:" with empty name → 400 ensemble_preset_missing (Req 7.6)."""
        cfg = _make_ensemble_config(preset=_make_preset())
        router = _make_router(cfg)
        agent = _make_agent(router, cost_tracker, mock_rate_limiter)

        request_data = {
            "messages": [{"role": "user", "content": "hi"}],
            "model": "ensemble:",
        }
        result = await agent.handle_chat_completion(request_data, _base_context())

        assert result["status_code"] == 400
        assert result["error"]["code"] == "ensemble_preset_missing"
        router.ensemble_route.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_single_model_access_check_skipped_for_ensemble(self, mock_rate_limiter, cost_tracker):
        """The ensemble alias is skipped while every underlying model is allowed."""
        preset = _make_preset()
        cfg = _make_ensemble_config(default=preset, preset=preset)
        router = _make_router(cfg)
        router.ensemble_route = AsyncMock(return_value=(_make_response(), _make_decision()))

        # The alias is not grantable; authority applies to the actual calls.
        project = Project(
            project_id="proj-1",
            name="Test",
            allowed_models=["model-a", "model-b", "judge-model"],
        )
        agent = _make_agent(router, cost_tracker, mock_rate_limiter, projects={"proj-1": project})

        request_data = {
            "messages": [{"role": "user", "content": "hi"}],
            "model": "ensemble",
        }
        result = await agent.handle_chat_completion(request_data, _base_context())

        # No 403 — ensemble bypasses single-model access checks.
        assert result.get("status_code") != 403
        router.ensemble_route.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_budget_precheck_uses_estimated_n_plus_one_cost(self, mock_rate_limiter, cost_tracker):
        """Budget pre-check rejects on estimated (N+1) cost, no dispatch (Req 8.5, 11.8)."""
        preset = _make_preset(name="budget", panel=["model-a", "model-b"], judge="judge-model")
        cfg = _make_ensemble_config(default=preset, preset=preset)
        router = _make_router(cfg)
        # Resolve a real per-call cost via the judge's fallback chain.
        router.get_fallback_chain.return_value = [
            ProviderModelMapping(provider="anthropic", model_id="claude-sonnet")
        ]
        router.ensemble_route = AsyncMock(return_value=(_make_response(), _make_decision()))

        # Tiny project budget so the (N+1) estimate blows past it.
        cost_tracker.register_project("proj-1", budget_limit=0.001)
        project = Project(project_id="proj-1", name="Test")
        agent = _make_agent(router, cost_tracker, mock_rate_limiter, projects={"proj-1": project})

        request_data = {
            "messages": [{"role": "user", "content": "Write a function"}],
            "model": "ensemble",
            "max_tokens": 256,
        }
        result = await agent.handle_chat_completion(request_data, _base_context())

        assert result["status_code"] == 429
        assert result["error"]["code"] == "budget_exceeded"
        # No dispatch when the pre-check fails.
        router.ensemble_route.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_access_error_surfaces_403(self, mock_rate_limiter, cost_tracker):
        """Disallowed panel/judge model → 403 access-control error (Req 11.3)."""
        preset = _make_preset()
        cfg = _make_ensemble_config(default=preset, preset=preset)
        router = _make_router(cfg)
        router.ensemble_route = AsyncMock(side_effect=EnsembleAccessError("model-b"))

        agent = _make_agent(router, cost_tracker, mock_rate_limiter)
        request_data = {
            "messages": [{"role": "user", "content": "hi"}],
            "model": "ensemble",
        }
        result = await agent.handle_chat_completion(request_data, _base_context())

        assert result["status_code"] == 403
        assert result["error"]["code"] == "model_not_allowed"

    @pytest.mark.asyncio
    async def test_quorum_error_surfaces_decision_metadata(self, mock_rate_limiter, cost_tracker):
        """Quorum-not-met surfaces the EnsembleDecision with quorum_met false (Req 9.7)."""
        preset = _make_preset(quorum=2, fallback_policy="error")
        cfg = _make_ensemble_config(default=preset, preset=preset)
        router = _make_router(cfg)
        decision = _make_decision(
            quorum_met=False,
            succeeded=["model-a"],
            failed=[{"model": "model-b", "reason": "timeout"}],
            succeeded_count=1,
            quorum_threshold=2,
            judge_invoked=False,
            error="quorum not met: 1 < 2",
        )
        router.ensemble_route = AsyncMock(side_effect=EnsembleQuorumError(decision))

        agent = _make_agent(router, cost_tracker, mock_rate_limiter)
        request_data = {
            "messages": [{"role": "user", "content": "hi"}],
            "model": "ensemble",
        }
        result = await agent.handle_chat_completion(request_data, _base_context())

        assert result["status_code"] == 502
        assert result["error"]["code"] == "ensemble_quorum_not_met"
        assert "ensemble" in result
        assert result["ensemble"]["quorum_met"] is False
        assert result["ensemble"]["succeeded_count"] == 1
        assert result["ensemble"]["failed"] == [{"model": "model-b", "reason": "timeout"}]

    @pytest.mark.asyncio
    async def test_no_survivors_error_surfaces_decision(self, mock_rate_limiter, cost_tracker):
        """Zero survivors surfaces the decision for observability."""
        preset = _make_preset()
        cfg = _make_ensemble_config(default=preset, preset=preset)
        router = _make_router(cfg)
        decision = _make_decision(
            quorum_met=False,
            succeeded=[],
            failed=[
                {"model": "model-a", "reason": "error"},
                {"model": "model-b", "reason": "error"},
            ],
            succeeded_count=0,
            judge_invoked=False,
            error="no panel responses available",
        )
        router.ensemble_route = AsyncMock(side_effect=EnsembleNoSurvivorsError(decision))

        agent = _make_agent(router, cost_tracker, mock_rate_limiter)
        request_data = {"messages": [{"role": "user", "content": "hi"}], "model": "ensemble"}
        result = await agent.handle_chat_completion(request_data, _base_context())

        assert result["status_code"] == 502
        assert result["error"]["code"] == "ensemble_no_survivors"
        assert result["ensemble"]["succeeded_count"] == 0

    @pytest.mark.asyncio
    async def test_synthesis_error_surfaces_decision(self, mock_rate_limiter, cost_tracker):
        """Judge synthesis failure surfaces the decision with survivors preserved."""
        preset = _make_preset()
        cfg = _make_ensemble_config(default=preset, preset=preset)
        router = _make_router(cfg)
        decision = _make_decision(
            quorum_met=True,
            judge_invoked=False,
            error="judge synthesis failed",
        )
        router.ensemble_route = AsyncMock(side_effect=EnsembleSynthesisError(decision))

        agent = _make_agent(router, cost_tracker, mock_rate_limiter)
        request_data = {"messages": [{"role": "user", "content": "hi"}], "model": "ensemble"}
        result = await agent.handle_chat_completion(request_data, _base_context())

        assert result["status_code"] == 502
        assert result["error"]["code"] == "ensemble_synthesis_failed"
        assert result["ensemble"]["succeeded"] == ["model-a", "model-b"]

    @pytest.mark.asyncio
    async def test_non_ensemble_request_unchanged(self, mock_rate_limiter, cost_tracker):
        """A normal request still routes normally with no ensemble metadata (Req 12.1-12.3)."""
        cfg = _make_ensemble_config(preset=_make_preset())
        router = _make_router(cfg)
        normal_response = _make_response(content="hi there", model="claude-sonnet")
        router.execute_with_fallback = AsyncMock(return_value=normal_response)
        router.model_registry = MagicMock()

        agent = _make_agent(router, cost_tracker, mock_rate_limiter)
        request_data = {
            "messages": [{"role": "user", "content": "hi"}],
            "model": "claude-sonnet",
        }
        result = await agent.handle_chat_completion(request_data, _base_context())

        assert "ensemble" not in result
        assert "ensemble_unavailable" not in result
        assert result["model"] == "claude-sonnet"
        assert result["provider"] == "anthropic"
        router.ensemble_route.assert_not_awaited()
        router.execute_with_fallback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ensemble_unavailable_fallback(self, mock_rate_limiter, cost_tracker):
        """Ensemble requested but config not configured → fall back, note unavailable (Req 12.5)."""
        # Config object present but empty/unconfigured.
        cfg = _make_ensemble_config(is_configured=False, preset=None, default=None)
        router = _make_router(cfg)
        normal_response = _make_response(content="fallback answer", model="claude-sonnet")
        router.execute_with_fallback = AsyncMock(return_value=normal_response)
        router.model_registry = MagicMock()

        agent = _make_agent(router, cost_tracker, mock_rate_limiter)
        request_data = {
            "messages": [{"role": "user", "content": "hi"}],
            "model": "ensemble",
        }
        result = await agent.handle_chat_completion(request_data, _base_context())

        assert result.get("ensemble_unavailable") is True
        assert "ensemble" not in result
        router.ensemble_route.assert_not_awaited()
        router.execute_with_fallback.assert_awaited_once()


# ===========================================================================
# TASK 7.2 — ensemble streaming
# ===========================================================================


def _collect_content(chunks: list[dict]) -> str:
    """Concatenate streamed delta content from chunk dicts."""
    content = ""
    for ch in chunks:
        data = ch.get("data")
        if isinstance(data, dict) and "choices" in data:
            for choice in data["choices"]:
                content += choice.get("delta", {}).get("content", "")
    return content


def _data_errors(chunks: list[dict]) -> list[dict]:
    """Return error payloads found in the streamed chunks."""
    errors = []
    for ch in chunks:
        data = ch.get("data")
        if isinstance(data, dict) and "error" in data:
            errors.append(data["error"])
    return errors


class TestEnsembleStreaming:
    """Streaming ensemble behaviour (Req 10.1-10.4)."""

    @pytest.mark.asyncio
    async def test_quorum_met_streams_only_judge_output(self, mock_rate_limiter, cost_tracker):
        """Quorum met → only the judge's synthesized output is streamed (Req 10.2)."""
        preset = _make_preset()
        cfg = _make_ensemble_config(default=preset, preset=preset)
        router = _make_router(cfg)
        judge_response = _make_response(content="judge consensus output", model="judge-model")
        router.ensemble_route = AsyncMock(return_value=(judge_response, _make_decision()))

        agent = _make_agent(router, cost_tracker, mock_rate_limiter)
        request_data = {
            "messages": [{"role": "user", "content": "hi"}],
            "model": "ensemble",
            "stream": True,
        }
        gen = await agent.handle_chat_completion(request_data, _base_context())
        chunks = [c async for c in gen]

        # Only the judge content is streamed; no panel/survivor content.
        assert _collect_content(chunks) == "judge consensus output"
        assert _data_errors(chunks) == []
        # Stream terminates with DONE.
        assert any(c.get("data") == "[DONE]" for c in chunks)

    @pytest.mark.asyncio
    async def test_panel_phase_emits_no_chunks_before_resolution(self, mock_rate_limiter, cost_tracker):
        """No content chunk is emitted until ensemble_route resolves (Req 10.1)."""
        preset = _make_preset()
        cfg = _make_ensemble_config(default=preset, preset=preset)
        router = _make_router(cfg)

        emitted_before_resolution: list[bool] = []

        async def _route(*args, **kwargs):
            # At the moment the panel runs, nothing should have been yielded yet
            # beyond the (non-content) rate-limit header chunk.
            emitted_before_resolution.append(True)
            return (_make_response(content="final answer"), _make_decision())

        router.ensemble_route = AsyncMock(side_effect=_route)

        agent = _make_agent(router, cost_tracker, mock_rate_limiter)
        gen = agent._stream_ensemble_response(
            ChatCompletionRequest(messages=[{"role": "user", "content": "hi"}], model="ensemble", stream=True),
            "hi",
            preset,
            None,
            "proj-1",
            "user-1",
            0.0,
            None,
        )

        first = await gen.__anext__()
        # The first yielded item is the resolved judge content, never a panel
        # chunk produced before ensemble_route completed.
        assert emitted_before_resolution == [True]
        assert isinstance(first.get("data"), dict)
        assert "choices" in first["data"]

        rest = [c async for c in gen]
        assert _collect_content([first, *rest]) == "final answer"

    @pytest.mark.asyncio
    async def test_best_single_streams_ranked_survivor(self, mock_rate_limiter, cost_tracker):
        """Below quorum + best-single with ≥1 survivor → stream ranked survivor (Req 10.3)."""
        preset = _make_preset(quorum=2, fallback_policy="best-single")
        cfg = _make_ensemble_config(default=preset, preset=preset)
        router = _make_router(cfg)
        survivor_response = _make_response(content="ranked survivor content", model="model-a")
        decision = _make_decision(
            quorum_met=False,
            succeeded=["model-a"],
            failed=[{"model": "model-b", "reason": "timeout"}],
            succeeded_count=1,
            quorum_threshold=2,
            fallback_used=True,
            judge_invoked=False,
        )
        router.ensemble_route = AsyncMock(return_value=(survivor_response, decision))

        agent = _make_agent(router, cost_tracker, mock_rate_limiter)
        request_data = {
            "messages": [{"role": "user", "content": "hi"}],
            "model": "ensemble",
            "stream": True,
        }
        gen = await agent.handle_chat_completion(request_data, _base_context())
        chunks = [c async for c in gen]

        assert _collect_content(chunks) == "ranked survivor content"
        assert _data_errors(chunks) == []
        assert any(c.get("data") == "[DONE]" for c in chunks)

    @pytest.mark.asyncio
    async def test_error_policy_terminates_with_error_chunk(self, mock_rate_limiter, cost_tracker):
        """Below quorum + error policy → error chunk, no synthesized content (Req 10.4)."""
        preset = _make_preset(quorum=2, fallback_policy="error")
        cfg = _make_ensemble_config(default=preset, preset=preset)
        router = _make_router(cfg)
        decision = _make_decision(
            quorum_met=False,
            succeeded=["model-a"],
            failed=[{"model": "model-b", "reason": "timeout"}],
            succeeded_count=1,
            quorum_threshold=2,
            judge_invoked=False,
            error="quorum not met",
        )
        router.ensemble_route = AsyncMock(side_effect=EnsembleQuorumError(decision))

        agent = _make_agent(router, cost_tracker, mock_rate_limiter)
        request_data = {
            "messages": [{"role": "user", "content": "hi"}],
            "model": "ensemble",
            "stream": True,
        }
        gen = await agent.handle_chat_completion(request_data, _base_context())
        chunks = [c async for c in gen]

        # No synthesized content streamed.
        assert _collect_content(chunks) == ""
        # An error chunk is emitted before termination.
        errors = _data_errors(chunks)
        assert len(errors) == 1
        assert errors[0]["type"] == "ensemble_error"
        assert any(c.get("data") == "[DONE]" for c in chunks)

    @pytest.mark.asyncio
    async def test_zero_survivors_terminates_with_error_chunk(self, mock_rate_limiter, cost_tracker):
        """Zero survivors → error chunk, no synthesized content (Req 10.4)."""
        preset = _make_preset()
        cfg = _make_ensemble_config(default=preset, preset=preset)
        router = _make_router(cfg)
        decision = _make_decision(
            quorum_met=False,
            succeeded=[],
            failed=[
                {"model": "model-a", "reason": "error"},
                {"model": "model-b", "reason": "error"},
            ],
            succeeded_count=0,
            judge_invoked=False,
            error="no panel responses available",
        )
        router.ensemble_route = AsyncMock(side_effect=EnsembleNoSurvivorsError(decision))

        agent = _make_agent(router, cost_tracker, mock_rate_limiter)
        request_data = {
            "messages": [{"role": "user", "content": "hi"}],
            "model": "ensemble",
            "stream": True,
        }
        gen = await agent.handle_chat_completion(request_data, _base_context())
        chunks = [c async for c in gen]

        assert _collect_content(chunks) == ""
        errors = _data_errors(chunks)
        assert len(errors) == 1
        assert errors[0]["type"] == "ensemble_error"
        assert any(c.get("data") == "[DONE]" for c in chunks)
