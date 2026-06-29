"""Unit tests for access-aware routing feature."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.gateway.agent import GatewayAgent
from src.gateway.cache_manager import CacheManager
from src.gateway.cost_tracker import CostTracker
from src.gateway.guardrail_engine import GuardrailEngine
from src.gateway.health_tracker import ProviderHealthTracker
from src.gateway.model_registry import ModelRegistry
from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    Project,
    ProviderModelMapping,
    RateLimitResult,
    RoutingStrategy,
    TokenPricing,
    TokenUsage,
    ModelConfig,
)
from src.gateway.rate_limiter import SlidingWindowRateLimiter
from src.gateway.router import AllProvidersExhaustedError, Router


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_project(**overrides) -> Project:
    defaults = dict(project_id="proj-1", name="Test Project")
    defaults.update(overrides)
    return Project(**defaults)


def _make_agent(
    projects=None, user_configs=None, router=None,
) -> GatewayAgent:
    if router is None:
        router = MagicMock(spec=Router)
        router.execute_with_fallback = AsyncMock()
    rl = MagicMock(spec=SlidingWindowRateLimiter)
    rl.check_rate_limit = AsyncMock(
        return_value=RateLimitResult(
            allowed=True, limit=60, remaining=59,
            reset_at=datetime.utcnow(), retry_after_seconds=None,
        )
    )
    return GatewayAgent(
        router=router,
        rate_limiter=rl,
        guardrail_engine=GuardrailEngine(),
        cache_manager=CacheManager(),
        cost_tracker=CostTracker(pricing_config={}),
        projects=projects or {},
        user_configs=user_configs or {},
    )


def _build_registry() -> ModelRegistry:
    registry = ModelRegistry()
    registry.models["gpt-4"] = ModelConfig(
        name="gpt-4", description="GPT-4",
        providers=[
            ProviderModelMapping(provider="openai", model_id="gpt-4-turbo", fallback_order=1),
            ProviderModelMapping(provider="azure", model_id="gpt-4-azure", fallback_order=2),
        ],
    )
    registry.models["claude-3"] = ModelConfig(
        name="claude-3", description="Claude 3",
        providers=[
            ProviderModelMapping(provider="anthropic", model_id="claude-3-sonnet", fallback_order=1),
        ],
    )
    return registry


def _make_response(provider="openai") -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id="resp-1",
        choices=[{"message": {"role": "assistant", "content": "hi"}}],
        usage=TokenUsage(prompt_tokens=5, completion_tokens=3, total_tokens=8),
        model="gpt-4", provider=provider,
    )


# ---------------------------------------------------------------------------
# Task 1.2: _compute_effective_allowed_models
# ---------------------------------------------------------------------------


class TestComputeEffectiveAllowedModels:
    def test_both_lists_set_returns_intersection(self):
        project = _make_project(allowed_models=["gpt-4", "claude-3", "llama"])
        agent = _make_agent(
            projects={"proj-1": project},
            user_configs={"user-1": {"allowed_models": ["gpt-4", "gemini"]}},
        )
        result = agent._compute_effective_allowed_models(project, "user-1")
        assert result == {"gpt-4"}

    def test_only_project_set(self):
        project = _make_project(allowed_models=["gpt-4", "claude-3"])
        agent = _make_agent(projects={"proj-1": project})
        result = agent._compute_effective_allowed_models(project, "user-1")
        assert result == {"gpt-4", "claude-3"}

    def test_only_user_set(self):
        project = _make_project()  # no allowed_models
        agent = _make_agent(
            user_configs={"user-1": {"allowed_models": ["gpt-4"]}},
        )
        result = agent._compute_effective_allowed_models(project, "user-1")
        assert result == {"gpt-4"}

    def test_neither_set_returns_none(self):
        project = _make_project()
        agent = _make_agent()
        result = agent._compute_effective_allowed_models(project, "user-1")
        assert result is None

    def test_empty_intersection(self):
        project = _make_project(allowed_models=["gpt-4"])
        agent = _make_agent(
            projects={"proj-1": project},
            user_configs={"user-1": {"allowed_models": ["claude-3"]}},
        )
        result = agent._compute_effective_allowed_models(project, "user-1")
        assert result == set()

    def test_no_project_returns_user_list(self):
        agent = _make_agent(
            user_configs={"user-1": {"allowed_models": ["gpt-4"]}},
        )
        result = agent._compute_effective_allowed_models(None, "user-1")
        assert result == {"gpt-4"}

    def test_no_project_no_user_returns_none(self):
        agent = _make_agent()
        result = agent._compute_effective_allowed_models(None, "user-1")
        assert result is None


# ---------------------------------------------------------------------------
# Task 2.4: Router filtering with allowed_models
# ---------------------------------------------------------------------------


class TestRouterAllowedModelsFiltering:
    @pytest.mark.asyncio
    async def test_allowed_model_passes(self):
        registry = _build_registry()
        health_tracker = ProviderHealthTracker()
        router = Router(
            model_registry=registry, health_tracker=health_tracker,
            max_retries=0, base_delay=0.0,
        )

        async def provider_fn(mapping):
            return _make_response(mapping.provider)

        request = ChatCompletionRequest(
            messages=[{"role": "user", "content": "hi"}], model="gpt-4",
        )
        resp = await router.execute_with_fallback(
            request, provider_fn, allowed_models={"gpt-4", "claude-3"},
        )
        assert resp.provider in ("openai", "azure")

    @pytest.mark.asyncio
    async def test_disallowed_model_raises(self):
        registry = _build_registry()
        health_tracker = ProviderHealthTracker()
        router = Router(
            model_registry=registry, health_tracker=health_tracker,
            max_retries=0, base_delay=0.0,
        )

        async def provider_fn(mapping):
            return _make_response(mapping.provider)

        request = ChatCompletionRequest(
            messages=[{"role": "user", "content": "hi"}], model="gpt-4",
        )
        with pytest.raises(AllProvidersExhaustedError):
            await router.execute_with_fallback(
                request, provider_fn, allowed_models={"claude-3"},
            )

    @pytest.mark.asyncio
    async def test_none_filter_preserves_behavior(self):
        registry = _build_registry()
        health_tracker = ProviderHealthTracker()
        router = Router(
            model_registry=registry, health_tracker=health_tracker,
            max_retries=0, base_delay=0.0,
        )

        async def provider_fn(mapping):
            return _make_response(mapping.provider)

        request = ChatCompletionRequest(
            messages=[{"role": "user", "content": "hi"}], model="gpt-4",
        )
        resp = await router.execute_with_fallback(
            request, provider_fn, allowed_models=None,
        )
        assert resp.provider in ("openai", "azure")


# ---------------------------------------------------------------------------
# Task 3.3: GatewayAgent passes effective_allowed to router
# ---------------------------------------------------------------------------


class TestGatewayAgentPassesAllowedModels:
    @pytest.mark.asyncio
    async def test_passes_effective_allowed_to_router(self):
        mock_router = MagicMock(spec=Router)
        mock_router.execute_with_fallback = AsyncMock(return_value=_make_response())
        project = _make_project(allowed_models=["gpt-4"])
        agent = _make_agent(
            projects={"proj-1": project},
            user_configs={"user-1": {"allowed_models": ["gpt-4", "claude-3"]}},
            router=mock_router,
        )

        await agent.handle_chat_completion(
            {"messages": [{"role": "user", "content": "hi"}], "model": "gpt-4"},
            {"user_id": "user-1", "project_id": "proj-1"},
        )

        call_kwargs = mock_router.execute_with_fallback.call_args
        assert call_kwargs.kwargs.get("allowed_models") == {"gpt-4"}

    @pytest.mark.asyncio
    async def test_passes_none_when_no_restrictions(self):
        mock_router = MagicMock(spec=Router)
        mock_router.execute_with_fallback = AsyncMock(return_value=_make_response())
        agent = _make_agent(router=mock_router)

        await agent.handle_chat_completion(
            {"messages": [{"role": "user", "content": "hi"}], "model": "gpt-4"},
            {"user_id": "user-1", "project_id": "proj-1"},
        )

        call_kwargs = mock_router.execute_with_fallback.call_args
        assert call_kwargs.kwargs.get("allowed_models") is None


# ---------------------------------------------------------------------------
# Task 4.3: ClientAgent and ChatAPI
# ---------------------------------------------------------------------------


class TestClientAgentListModels:
    @pytest.mark.asyncio
    async def test_forwards_project_and_user_id(self):
        from src.gateway.chat.client_agent import ClientAgent

        mock_gateway = MagicMock()
        mock_gateway.handle_list_models = AsyncMock(return_value={"models": []})

        client = ClientAgent(mock_gateway, default_user_id="default-user", default_project_id="default-proj")
        await client.list_models(project_id="proj-1", user_id="user-1")

        mock_gateway.handle_list_models.assert_awaited_once_with(
            project_id="proj-1", user_id="user-1",
        )

    @pytest.mark.asyncio
    async def test_uses_defaults_when_not_provided(self):
        from src.gateway.chat.client_agent import ClientAgent

        mock_gateway = MagicMock()
        mock_gateway.handle_list_models = AsyncMock(return_value={"models": []})

        client = ClientAgent(mock_gateway, default_user_id="default-user", default_project_id="default-proj")
        await client.list_models()

        mock_gateway.handle_list_models.assert_awaited_once_with(
            project_id="default-proj", user_id="default-user",
        )


# ---------------------------------------------------------------------------
# handle_list_models filtering
# ---------------------------------------------------------------------------


class TestHandleListModelsFiltering:
    @pytest.mark.asyncio
    async def test_filters_by_project(self):
        registry = _build_registry()
        mock_router = MagicMock(spec=Router)
        mock_router.model_registry = registry
        project = _make_project(allowed_models=["gpt-4"])
        agent = _make_agent(projects={"proj-1": project}, router=mock_router)

        result = await agent.handle_list_models(project_id="proj-1")
        names = {m["name"] for m in result["models"]}
        assert names == {"gpt-4"}

    @pytest.mark.asyncio
    async def test_filters_by_user(self):
        registry = _build_registry()
        mock_router = MagicMock(spec=Router)
        mock_router.model_registry = registry
        agent = _make_agent(
            user_configs={"user-1": {"allowed_models": ["claude-3"]}},
            router=mock_router,
        )

        result = await agent.handle_list_models(user_id="user-1")
        names = {m["name"] for m in result["models"]}
        assert names == {"claude-3"}

    @pytest.mark.asyncio
    async def test_filters_by_both(self):
        registry = _build_registry()
        mock_router = MagicMock(spec=Router)
        mock_router.model_registry = registry
        project = _make_project(allowed_models=["gpt-4", "claude-3"])
        agent = _make_agent(
            projects={"proj-1": project},
            user_configs={"user-1": {"allowed_models": ["gpt-4"]}},
            router=mock_router,
        )

        result = await agent.handle_list_models(project_id="proj-1", user_id="user-1")
        names = {m["name"] for m in result["models"]}
        assert names == {"gpt-4"}

    @pytest.mark.asyncio
    async def test_no_filters_returns_all(self):
        registry = _build_registry()
        mock_router = MagicMock(spec=Router)
        mock_router.model_registry = registry
        agent = _make_agent(router=mock_router)

        result = await agent.handle_list_models()
        names = {m["name"] for m in result["models"]}
        assert names == {"gpt-4", "claude-3"}
