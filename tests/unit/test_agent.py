"""Unit tests for the Gateway Agent orchestration logic."""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.gateway.agent import GatewayAgent, _error_response, create_gateway_agent
from src.gateway.cache_manager import CacheManager
from src.gateway.cost_tracker import CostTracker
from src.gateway.guardrail_engine import GuardrailEngine
from src.gateway.models import (
    BudgetStatus,
    ChatCompletionRequest,
    ChatCompletionResponse,
    GuardrailResult,
    GuardrailRule,
    Project,
    ProviderModelMapping,
    RateLimitConfig,
    RateLimitResult,
    RequestContext,
    TokenPricing,
    TokenUsage,
)
from src.gateway.rate_limiter import SlidingWindowRateLimiter
from src.gateway.router import AllProvidersExhaustedError, Router
from src.gateway.session_manager import SessionManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_response(
    content: str = "Hello!",
    model: str = "gpt-4",
    provider: str = "openai",
) -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id="resp-1",
        choices=[{"index": 0, "message": {"role": "assistant", "content": content}}],
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        model=model,
        provider=provider,
    )


def _base_request_data() -> dict:
    return {
        "messages": [{"role": "user", "content": "Hi"}],
        "model": "gpt-4",
    }


def _base_context() -> dict:
    return {
        "user_id": "user-1",
        "project_id": "proj-1",
        "roles": ["developer"],
        "scopes": ["chat"],
    }


def _make_project(**overrides) -> Project:
    defaults = dict(
        project_id="proj-1",
        name="Test Project",
    )
    defaults.update(overrides)
    return Project(**defaults)


@pytest.fixture
def mock_router():
    router = MagicMock(spec=Router)
    response = _make_response()
    router.execute_with_fallback = AsyncMock(return_value=response)
    return router


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
def guardrail_engine():
    return GuardrailEngine()


@pytest.fixture
def cache_manager():
    return CacheManager()


@pytest.fixture
def cost_tracker():
    pricing = {
        "openai": {
            "gpt-4": TokenPricing(prompt_token_cost=0.01, completion_token_cost=0.03),
        }
    }
    return CostTracker(pricing_config=pricing)


@pytest.fixture
def agent(mock_router, mock_rate_limiter, guardrail_engine, cache_manager, cost_tracker):
    return GatewayAgent(
        router=mock_router,
        rate_limiter=mock_rate_limiter,
        guardrail_engine=guardrail_engine,
        cache_manager=cache_manager,
        cost_tracker=cost_tracker,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_non_streaming_request(agent, mock_router):
    """Successful non-streaming request returns a well-formed response dict."""
    result = await agent.handle_chat_completion(_base_request_data(), _base_context())

    assert isinstance(result, dict)
    assert result["id"] == "resp-1"
    assert result["model"] == "gpt-4"
    assert result["provider"] == "openai"
    assert result["usage"]["prompt_tokens"] == 10
    assert result["usage"]["completion_tokens"] == 5
    assert result["choices"][0]["message"]["content"] == "Hello!"
    mock_router.execute_with_fallback.assert_awaited_once()


@pytest.mark.asyncio
async def test_rate_limit_rejection(agent, mock_rate_limiter):
    """Rate limit exceeded returns a 429-style error dict."""
    mock_rate_limiter.check_rate_limit = AsyncMock(
        return_value=RateLimitResult(
            allowed=False, limit=60, remaining=0,
            reset_at=datetime.utcnow(), retry_after_seconds=30,
        )
    )

    result = await agent.handle_chat_completion(_base_request_data(), _base_context())

    assert result["status_code"] == 429
    assert result["error"]["type"] == "rate_limit_error"
    assert "30" in result["error"]["message"]


@pytest.mark.asyncio
async def test_request_guardrail_violation(mock_router, mock_rate_limiter, cache_manager, cost_tracker):
    """Request guardrail violation returns a 400-style error dict."""
    rules = [
        GuardrailRule(
            name="block_badword",
            rule_type="keyword_block",
            pattern="badword",
            action="block",
            applies_to="request",
        )
    ]
    project = _make_project(guardrail_rules=rules)
    agent = GatewayAgent(
        router=mock_router,
        rate_limiter=mock_rate_limiter,
        guardrail_engine=GuardrailEngine(),
        cache_manager=cache_manager,
        cost_tracker=cost_tracker,
        projects={"proj-1": project},
    )

    data = {"messages": [{"role": "user", "content": "say badword"}], "model": "gpt-4"}
    result = await agent.handle_chat_completion(data, _base_context())

    assert result["status_code"] == 400
    assert result["error"]["type"] == "content_policy_violation"
    assert "block_badword" in result["error"]["message"]


@pytest.mark.asyncio
async def test_cache_hit_returns_cached_response(mock_router, mock_rate_limiter, cost_tracker):
    """Cache hit returns the cached response without calling the router."""
    project = _make_project(cache_enabled=True, cache_ttl_seconds=300)
    cm = CacheManager()
    agent = GatewayAgent(
        router=mock_router,
        rate_limiter=mock_rate_limiter,
        guardrail_engine=GuardrailEngine(),
        cache_manager=cm,
        cost_tracker=cost_tracker,
        projects={"proj-1": project},
    )

    req_data = _base_request_data()
    ctx = _base_context()

    # Pre-populate cache
    request = ChatCompletionRequest(
        messages=req_data["messages"], model=req_data["model"]
    )
    cache_key = cm.compute_cache_key(request, "proj-1")
    cached_resp = _make_response(content="cached answer")
    await cm.put(cache_key, cached_resp, ttl_seconds=300)

    result = await agent.handle_chat_completion(req_data, ctx)

    assert result["is_cached"] is True
    assert result["choices"][0]["message"]["content"] == "cached answer"
    mock_router.execute_with_fallback.assert_not_awaited()


@pytest.mark.asyncio
async def test_budget_exceeded_rejects_request(mock_router, mock_rate_limiter, cache_manager):
    """When project budget is exceeded, the request is rejected with 429 before routing."""
    pricing = {
        "openai": {
            "gpt-4": TokenPricing(prompt_token_cost=0.01, completion_token_cost=0.03),
        }
    }
    ct = CostTracker(pricing_config=pricing, budgets={"proj-1": {"budget_limit": 0.0001, "alert_threshold": 0.00005}})
    project = _make_project(budget_limit=0.0001, alert_threshold=0.00005)
    agent = GatewayAgent(
        router=mock_router,
        rate_limiter=mock_rate_limiter,
        guardrail_engine=GuardrailEngine(),
        cache_manager=cache_manager,
        cost_tracker=ct,
        projects={"proj-1": project},
    )

    # Seed a usage record so the project is over budget
    from src.gateway.models import UsageRecord
    await ct.record_usage(UsageRecord(
        request_id="r-1", project_id="proj-1", user_id="user-1",
        provider="openai", model="gpt-4",
        prompt_tokens=100, completion_tokens=100, total_tokens=200,
        cost=1.0, timestamp=datetime.utcnow(),
    ))

    result = await agent.handle_chat_completion(_base_request_data(), _base_context())

    assert result["status_code"] == 429
    assert result["error"]["type"] == "budget_exceeded"
    assert "proj-1" in result["error"]["message"]
    mock_router.execute_with_fallback.assert_not_awaited()


@pytest.mark.asyncio
async def test_response_guardrail_replaces_content(mock_router, mock_rate_limiter, cache_manager, cost_tracker):
    """Response guardrail violation replaces the response content."""
    # The mock router returns a response containing "secret_data"
    mock_router.execute_with_fallback = AsyncMock(
        return_value=_make_response(content="Here is secret_data for you")
    )
    rules = [
        GuardrailRule(
            name="block_secrets",
            rule_type="keyword_block",
            pattern="secret_data",
            action="block",
            applies_to="response",
        )
    ]
    project = _make_project(guardrail_rules=rules)
    agent = GatewayAgent(
        router=mock_router,
        rate_limiter=mock_rate_limiter,
        guardrail_engine=GuardrailEngine(),
        cache_manager=cache_manager,
        cost_tracker=cost_tracker,
        projects={"proj-1": project},
    )

    result = await agent.handle_chat_completion(_base_request_data(), _base_context())

    # Content should be replaced with the guardrail message
    content = result["choices"][0]["message"]["content"]
    assert "secret_data" not in content
    assert "block_secrets" in content
    assert "Response modified by guardrail" in result.get("warnings", [])


@pytest.mark.asyncio
async def test_all_providers_exhausted(mock_router, mock_rate_limiter, guardrail_engine, cache_manager, cost_tracker):
    """When all providers fail, a 502 error is returned."""
    mock_router.execute_with_fallback = AsyncMock(
        side_effect=AllProvidersExhaustedError(
            [{"provider": "openai", "status_code": 500, "message": "Internal error"}]
        )
    )
    agent = GatewayAgent(
        router=mock_router,
        rate_limiter=mock_rate_limiter,
        guardrail_engine=guardrail_engine,
        cache_manager=cache_manager,
        cost_tracker=cost_tracker,
    )

    result = await agent.handle_chat_completion(_base_request_data(), _base_context())

    assert result["status_code"] == 502
    assert result["error"]["type"] == "provider_error"


@pytest.mark.asyncio
async def test_streaming_yields_sse_chunks(agent):
    """Streaming mode returns an async generator yielding SSE dicts with a [DONE] marker."""
    data = _base_request_data()
    data["stream"] = True

    result = await agent.handle_chat_completion(data, _base_context())

    # result should be an async generator
    chunks = []
    async for chunk in result:
        chunks.append(chunk)

    # Filter out rate limit header metadata chunks
    data_chunks = [c for c in chunks if "_rate_limit_headers" not in c]

    # Last chunk should be the DONE marker
    assert data_chunks[-1]["data"] == "[DONE]"
    # All preceding chunks should have data dicts with content
    for c in data_chunks[:-1]:
        assert "data" in c
        assert isinstance(c["data"], dict)


@pytest.mark.asyncio
async def test_session_storage_called_when_session_id_present(
    mock_router, mock_rate_limiter, guardrail_engine, cache_manager, cost_tracker
):
    """When session_id is in context, session_manager.store_exchange is called."""
    mock_sm = MagicMock(spec=SessionManager)
    mock_sm.store_exchange = AsyncMock()

    agent = GatewayAgent(
        router=mock_router,
        rate_limiter=mock_rate_limiter,
        guardrail_engine=guardrail_engine,
        cache_manager=cache_manager,
        cost_tracker=cost_tracker,
        session_manager=mock_sm,
    )

    ctx = _base_context()
    ctx["session_id"] = "sess-abc"

    await agent.handle_chat_completion(_base_request_data(), ctx)

    mock_sm.store_exchange.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_gateway_agent_factory():
    """create_gateway_agent wires up and returns a GatewayAgent."""
    router = MagicMock(spec=Router)
    rl = MagicMock(spec=SlidingWindowRateLimiter)
    ge = GuardrailEngine()
    cm = CacheManager()
    ct = CostTracker(pricing_config={})

    agent = create_gateway_agent(router, rl, ge, cm, ct)

    assert isinstance(agent, GatewayAgent)
    assert agent.router is router

# ---------------------------------------------------------------------------
# Task 14.2 — list_models and health_check
# ---------------------------------------------------------------------------

from src.gateway.models import VirtualModelConfig, RoutingStrategy
from src.gateway.health_tracker import ProviderHealthTracker
from src.gateway.model_registry import ModelRegistry


def _build_registry_with_models() -> ModelRegistry:
    """Build a ModelRegistry pre-loaded with two virtual models."""
    registry = ModelRegistry()
    registry.models = {
        "gpt-4": VirtualModelConfig(
            name="gpt-4",
            description="GPT-4 class model",
            providers=[
                ProviderModelMapping(provider="openai", model_id="gpt-4-turbo", weight=0.7),
                ProviderModelMapping(provider="azure_openai", model_id="gpt-4-turbo-2024", weight=0.3),
            ],
            routing_strategy=RoutingStrategy.WEIGHTED,
            capabilities=["chat", "streaming", "function_calling"],
        ),
        "claude-3": VirtualModelConfig(
            name="claude-3",
            description="Claude 3 class model",
            providers=[
                ProviderModelMapping(provider="anthropic", model_id="claude-3-sonnet"),
            ],
            routing_strategy=RoutingStrategy.COST_OPTIMIZED,
            capabilities=["chat", "streaming"],
        ),
    }
    return registry


@pytest.mark.asyncio
async def test_handle_list_models_returns_all_models(mock_rate_limiter, cache_manager, cost_tracker):
    """handle_list_models returns correct model info for all registered models."""
    registry = _build_registry_with_models()
    health_tracker = ProviderHealthTracker()

    router = MagicMock(spec=Router)
    router.model_registry = registry
    router.health_tracker = health_tracker

    agent = GatewayAgent(
        router=router,
        rate_limiter=mock_rate_limiter,
        guardrail_engine=GuardrailEngine(),
        cache_manager=cache_manager,
        cost_tracker=cost_tracker,
    )

    result = await agent.handle_list_models()

    assert "models" in result
    models = result["models"]
    assert len(models) == 2

    names = {m["name"] for m in models}
    assert names == {"gpt-4", "claude-3"}

    gpt4 = next(m for m in models if m["name"] == "gpt-4")
    assert gpt4["description"] == "GPT-4 class model"
    assert gpt4["providers"] == ["openai", "azure_openai"]
    assert gpt4["capabilities"] == ["chat", "streaming", "function_calling"]
    assert gpt4["routing_strategy"] == "weighted"

    claude = next(m for m in models if m["name"] == "claude-3")
    assert claude["providers"] == ["anthropic"]
    assert claude["routing_strategy"] == "cost-optimized"


@pytest.mark.asyncio
async def test_handle_list_models_empty_registry(mock_rate_limiter, cache_manager, cost_tracker):
    """handle_list_models returns empty list when no models are registered."""
    registry = ModelRegistry()
    health_tracker = ProviderHealthTracker()

    router = MagicMock(spec=Router)
    router.model_registry = registry
    router.health_tracker = health_tracker

    agent = GatewayAgent(
        router=router,
        rate_limiter=mock_rate_limiter,
        guardrail_engine=GuardrailEngine(),
        cache_manager=cache_manager,
        cost_tracker=cost_tracker,
    )

    result = await agent.handle_list_models()

    assert result == {"models": []}


@pytest.mark.asyncio
async def test_handle_health_check_all_healthy(mock_rate_limiter, cache_manager, cost_tracker):
    """health_check returns 'healthy' for all providers when none are in cooldown."""
    registry = _build_registry_with_models()
    health_tracker = ProviderHealthTracker()

    router = MagicMock(spec=Router)
    router.model_registry = registry
    router.health_tracker = health_tracker

    agent = GatewayAgent(
        router=router,
        rate_limiter=mock_rate_limiter,
        guardrail_engine=GuardrailEngine(),
        cache_manager=cache_manager,
        cost_tracker=cost_tracker,
    )

    result = await agent.handle_health_check()

    assert result["status"] == "ok"
    assert result["providers"]["openai"] == "healthy"
    assert result["providers"]["azure_openai"] == "healthy"
    assert result["providers"]["anthropic"] == "healthy"


@pytest.mark.asyncio
async def test_handle_health_check_with_unhealthy_provider(mock_rate_limiter, cache_manager, cost_tracker):
    """health_check reports 'unhealthy' for providers in cooldown."""
    registry = _build_registry_with_models()
    health_tracker = ProviderHealthTracker()
    health_tracker.mark_unhealthy("openai", cooldown_seconds=300)

    router = MagicMock(spec=Router)
    router.model_registry = registry
    router.health_tracker = health_tracker

    agent = GatewayAgent(
        router=router,
        rate_limiter=mock_rate_limiter,
        guardrail_engine=GuardrailEngine(),
        cache_manager=cache_manager,
        cost_tracker=cost_tracker,
    )

    result = await agent.handle_health_check()

    assert result["status"] == "ok"
    assert result["providers"]["openai"] == "unhealthy"
    assert result["providers"]["azure_openai"] == "healthy"
    assert result["providers"]["anthropic"] == "healthy"


@pytest.mark.asyncio
async def test_list_models_entrypoint_without_agent():
    """list_models entrypoint returns error when agent is not initialised."""
    from src.gateway import agent as agent_module

    original = agent_module._agent
    try:
        agent_module._agent = None
        result = await agent_module.list_models()
        assert result["status_code"] == 500
        assert result["error"]["type"] == "server_error"
    finally:
        agent_module._agent = original


@pytest.mark.asyncio
async def test_health_check_entrypoint_without_agent():
    """health_check entrypoint returns error when agent is not initialised."""
    from src.gateway import agent as agent_module

    original = agent_module._agent
    try:
        agent_module._agent = None
        result = await agent_module.health_check()
        assert result["status_code"] == 500
        assert result["error"]["type"] == "server_error"
    finally:
        agent_module._agent = original

